"""Résolution des voies ferrées autour d'un point, via OpenStreetMap/Overpass.

Le GTFS SNCF ne publie pas de `shapes.txt` exploitable pour le rail : on n'a
donc pas la géométrie des lignes côté horaires. OSM la fournit, et avec une
qualité excellente sur le réseau ferré français (voies tracées une par une,
relations de ligne, électrification, vitesses).

Ce module sépare volontairement :

* les fonctions pures (`parse_overpass`, `stitch_ways`, `build_corridors`),
  testables hors ligne sur des fixtures ;
* le client réseau (`OverpassClient`), qui se contente de récupérer du JSON et
  de le mettre en cache sur disque.
"""

from __future__ import annotations

import hashlib
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .geo import (
    LatLon,
    cumulative_lengths_m,
    Projection,
    axis_distance_deg,
    bearing_distance_deg,
    haversine_m,
    initial_bearing_deg,
    interpolate_along,
    polyline_length_m,
    project_on_polyline,
)

log = logging.getLogger(__name__)

DEFAULT_ENDPOINT = "https://overpass-api.de/api/interpreter"

#: Types de voies qui portent du trafic susceptible d'être « vu » depuis un
#: point d'observation. On exclut les voies de service par défaut (`service=*`
#: : garages, tiroirs, faisceaux) car elles produisent surtout des manœuvres
#: non horairées — elles restent récupérables via `include_service`.
MAIN_RAILWAY_VALUES = ("rail", "light_rail", "narrow_gauge")


@dataclass(frozen=True)
class RailWay:
    """Un tronçon de voie OSM avec sa géométrie."""

    osm_id: int
    geometry: tuple[LatLon, ...]
    tags: dict[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str | None:
        return self.tags.get("name")

    @property
    def ref(self) -> str | None:
        return self.tags.get("ref")

    @property
    def is_service(self) -> bool:
        return "service" in self.tags

    @property
    def usage(self) -> str | None:
        return self.tags.get("usage")

    @property
    def maxspeed_kmh(self) -> float | None:
        raw = self.tags.get("maxspeed")
        if not raw:
            return None
        try:
            return float(raw.split()[0])
        except ValueError:
            return None

    def label(self) -> str:
        return self.name or self.ref or f"way/{self.osm_id}"


@dataclass(frozen=True)
class RailStop:
    """Une gare, halte ou position d'arrêt OSM."""

    osm_id: int
    position: LatLon
    tags: dict[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str | None:
        return self.tags.get("name")

    @property
    def kind(self) -> str | None:
        return self.tags.get("railway")

    @property
    def uic_ref(self) -> str | None:
        return self.tags.get("uic_ref") or self.tags.get("ref:SNCF")


#: Tolérance accordée à une voie dont le pied de projection tombe sur une
#: extrémité : OSM peut découper un tronçon à quelques mètres du point.
ENDPOINT_TOLERANCE_M = 25.0

#: En deçà de cette distance de visée, la géométrie récupérée est trop courte
#: pour dire vers où part le corridor.
MIN_LOOKAHEAD_M = 250.0

#: Distance de visée par défaut au-delà du point d'observation. Elle doit
#: dépasser l'éloignement des bifurcations : tant que les lignes sont encore
#: parallèles, rien ne les distingue.
DEFAULT_LOOKAHEAD_M = 5000.0


@dataclass(frozen=True)
class SpeedSegment:
    """Un tronçon du parcours gare -> point, avec ce que la carte en dit."""

    start_m: float
    """Abscisse de début, comptée depuis la gare d'appui."""
    end_m: float
    maxspeed_kmh: float | None
    tunnel: bool
    bridge: bool
    label: str

    @property
    def length_m(self) -> float:
        return self.end_m - self.start_m

    def describe(self) -> str:
        speed = f"{self.maxspeed_kmh:.0f} km/h" if self.maxspeed_kmh else "vitesse non cartographiée"
        traits = "".join(t for t, on in ((" · tunnel", self.tunnel), (" · pont", self.bridge)) if on)
        return (
            f"{self.start_m:6.0f} → {self.end_m:6.0f} m  "
            f"({self.length_m:4.0f} m)  {speed}{traits}"
        )


@dataclass(frozen=True)
class AnchorLink:
    """Relation géométrique entre un corridor et la gare d'appui."""

    along_distance_m: float
    """Distance à parcourir sur la voie entre la gare et le point."""
    anchor_offset_m: float
    """Distance de la gare à la polyligne du corridor.

    Si elle est grande, la géométrie récupérée n'atteint pas la gare et
    `along_distance_m` n'a aucun sens.
    """
    outbound_bearing_deg: float | None
    """Cap, depuis la gare, de la direction que prend le corridor après le point."""
    straight_distance_m: float
    """Distance gare -> point à vol d'oiseau, pour contrôle de vraisemblance."""

    @property
    def is_plausible(self) -> bool:
        """Une distance sur la voie ne peut pas être plus courte qu'à vol d'oiseau.

        Si elle l'est, c'est que la polyligne s'arrête avant la gare et que la
        projection a été bornée à son extrémité : le résultat est inexploitable.
        """
        # Une gare peut être bâtie à l'écart de la voie : la distance à vol
        # d'oiseau depuis le bâtiment inclut alors ce décalage, qu'il faut
        # déduire avant de comparer.
        reachable = max(0.0, self.straight_distance_m - self.anchor_offset_m)
        return self.anchor_offset_m <= MAX_ANCHOR_OFFSET_M and self.along_distance_m >= reachable * 0.95


#: Au-delà, on considère que le corridor n'atteint pas la gare d'appui. Le seuil
#: est large : un bâtiment voyageurs est parfois bâti à l'écart des voies, et la
#: position configurée n'est souvent qu'un centroïde approximatif.
MAX_ANCHOR_OFFSET_M = 400.0


@dataclass(frozen=True)
class Corridor:
    """Un faisceau de voies parallèles vu depuis le point d'observation.

    Un « corridor » regroupe les voies qui, au droit du point, sont à la fois
    proches les unes des autres et parallèles : concrètement, les deux voies
    d'une ligne à double voie forment un seul corridor. C'est l'unité à laquelle
    on rattache les circulations, puisqu'un train d'un sens ou de l'autre passe
    au même endroit à quelques mètres près.
    """

    corridor_id: str
    label: str
    ways: tuple[RailWay, ...]
    distance_m: float
    """Distance du point d'observation à la voie la plus proche du corridor."""
    axis_deg: float
    """Orientation de l'axe des voies au droit du point, dans [0, 180[."""
    centerline: tuple[LatLon, ...]
    """Polyligne représentative (la plus longue chaîne du corridor)."""
    projection: Projection
    """Projection du point d'observation sur `centerline`."""
    observer: LatLon = (0.0, 0.0)
    """Point d'observation ayant servi à construire le corridor."""
    path_way_ids: frozenset[int] = frozenset()
    """Tronçons OSM effectivement parcourus par `centerline`.

    Indispensable pour relever un profil : à l'approche d'une gare, des dizaines
    de voies passent à quelques mètres les unes des autres, et un filtre par
    distance latérale les confond toutes.
    """

    @property
    def segment_count(self) -> int:
        """Nombre de tronçons OSM regroupés.

        Ce n'est **pas** le nombre de voies physiques : OSM découpe une même
        voie en plusieurs `way` à chaque changement de pont, de vitesse ou
        d'électrification. Voir `lateral_spread_m` pour la largeur du faisceau.
        """
        return len(self.ways)

    @property
    def lateral_spread_m(self) -> float:
        """Écart latéral entre le tronçon le plus proche et le plus éloigné.

        Un faisceau à deux voies fait quelques mètres de large ; une valeur de
        plusieurs dizaines de mètres indique un vrai faisceau de plusieurs voies.
        """
        distances = [project_on_polyline(self.observer, w.geometry).distance_m for w in self.ways]
        return max(distances) - min(distances) if distances else 0.0

    @property
    def maxspeed_kmh(self) -> float | None:
        speeds = [w.maxspeed_kmh for w in self.ways if w.maxspeed_kmh]
        return max(speeds) if speeds else None

    def link_to(self, anchor: LatLon, lookahead_m: float = 4000.0) -> "AnchorLink":
        """Relie le corridor à une gare d'appui.

        Args:
            anchor: position de la gare d'appui.
            lookahead_m: distance de visée au-delà du point, pour déterminer
                vers où part le corridor.
        """
        anchor_projection = project_on_polyline(anchor, self.centerline)
        along = abs(anchor_projection.along_m - self.projection.along_m)

        # Sens dans lequel on s'éloigne de la gare en passant par le point.
        forward = 1.0 if self.projection.along_m >= anchor_projection.along_m else -1.0
        total = polyline_length_m(self.centerline)
        target = min(max(self.projection.along_m + forward * lookahead_m, 0.0), total)
        available = abs(target - self.projection.along_m)
        outbound = (
            initial_bearing_deg(anchor, interpolate_along(self.centerline, target))
            if available >= MIN_LOOKAHEAD_M
            else None
        )
        return AnchorLink(
            along_distance_m=along,
            anchor_offset_m=anchor_projection.distance_m,
            outbound_bearing_deg=outbound,
            straight_distance_m=haversine_m(anchor, self.observer),
        )


# --------------------------------------------------------------------------
# Fonctions pures
# --------------------------------------------------------------------------


def parse_overpass(payload: dict[str, Any]) -> tuple[list[RailWay], list[RailStop]]:
    """Convertit une réponse Overpass (`out geom`) en objets du domaine."""
    ways: list[RailWay] = []
    stops: list[RailStop] = []
    for element in payload.get("elements", []):
        tags = {str(k): str(v) for k, v in (element.get("tags") or {}).items()}
        if element.get("type") == "way":
            geometry = tuple(
                (float(p["lat"]), float(p["lon"])) for p in element.get("geometry") or []
            )
            if len(geometry) >= 2:
                ways.append(RailWay(osm_id=int(element["id"]), geometry=geometry, tags=tags))
        elif element.get("type") == "node" and "lat" in element:
            stops.append(
                RailStop(
                    osm_id=int(element["id"]),
                    position=(float(element["lat"]), float(element["lon"])),
                    tags=tags,
                )
            )
    return ways, stops


def _endpoint_key(point: LatLon, precision: int = 7) -> tuple[float, float]:
    return (round(point[0], precision), round(point[1], precision))


def _oriented_candidates(chain: list[LatLon], geometry: list[LatLon]):
    """Façons de raccorder `geometry` à une extrémité de `chain`.

    Produit des triplets (au_bout, géométrie orientée, angle de virage). L'angle
    mesure combien la voie tourne au raccord : c'est lui qui départage les
    embranchements.
    """
    head, tail = _endpoint_key(chain[0]), _endpoint_key(chain[-1])
    g_head, g_tail = _endpoint_key(geometry[0]), _endpoint_key(geometry[-1])

    if g_head == tail:
        oriented = geometry
    elif g_tail == tail:
        oriented = list(reversed(geometry))
    else:
        oriented = None
    if oriented is not None:
        incoming = initial_bearing_deg(chain[-2], chain[-1])
        outgoing = initial_bearing_deg(oriented[0], oriented[1])
        yield True, oriented, bearing_distance_deg(incoming, outgoing)

    if g_tail == head:
        oriented = geometry
    elif g_head == head:
        oriented = list(reversed(geometry))
    else:
        oriented = None
    if oriented is not None:
        # On prolonge par l'amont : on compare le cap d'arrivée du candidat au
        # cap de départ de la chaîne.
        arriving = initial_bearing_deg(oriented[-2], oriented[-1])
        chain_start = initial_bearing_deg(chain[0], chain[1])
        yield False, oriented, bearing_distance_deg(arriving, chain_start)


#: Virage maximal toléré pour considérer que deux tronçons prolongent la même
#: ligne. Au-delà, on est passé sur une branche voisine.
MAX_CONTINUATION_TURN_DEG = 55.0


def extend_chain(
    chain: list[LatLon],
    pool: dict[int, list[LatLon]],
    max_turn_deg: float = MAX_CONTINUATION_TURN_DEG,
) -> list[LatLon]:
    """Prolonge une chaîne par les tronçons du vivier qui la continuent.

    À chaque pas on retient le raccord le plus droit, et on refuse au-delà de
    `max_turn_deg` : c'est ce qui empêche la chaîne de basculer sur une ligne
    voisine en traversant une gare, où toutes les lignes partagent des nœuds.

    Le vivier est consommé au passage.
    """
    while True:
        best: tuple[float, int, bool, list[LatLon]] | None = None
        for other_id, geometry in pool.items():
            for at_tail, oriented, turn in _oriented_candidates(chain, geometry):
                if turn <= max_turn_deg and (best is None or turn < best[0]):
                    best = (turn, other_id, at_tail, oriented)
        if best is None:
            return chain
        _, other_id, at_tail, oriented = best
        del pool[other_id]
        if at_tail:
            chain.extend(oriented[1:])
        else:
            chain[:0] = oriented[:-1]


def stitch_ways(
    ways: Sequence[RailWay], max_turn_deg: float = MAX_CONTINUATION_TURN_DEG
) -> list[list[LatLon]]:
    """Recolle des tronçons OSM en chaînes continues.

    OSM découpe une ligne en de nombreux `way` (changement de vitesse, de pont,
    d'électrification...). Pour mesurer une distance curviligne il faut d'abord
    reconstituer des polylignes continues en recollant les tronçons par leurs
    extrémités communes.

    À un embranchement, plusieurs tronçons partagent le même nœud : on retient
    alors **le plus droit**. C'est l'heuristique usuelle pour suivre une ligne à
    travers un aiguillage, et elle évite qu'une chaîne ne parte sur la branche
    voisine au milieu du parcours.
    """
    remaining = {w.osm_id: list(w.geometry) for w in ways}
    chains: list[list[LatLon]] = []

    while remaining:
        # On démarre sur le tronçon le plus long restant : cela donne des chaînes
        # stables et indépendantes de l'ordre de la réponse Overpass.
        seed_id = max(remaining, key=lambda i: polyline_length_m(remaining[i]))
        chains.append(extend_chain(remaining.pop(seed_id), remaining, max_turn_deg))

    chains.sort(key=polyline_length_m, reverse=True)
    return chains


def build_corridors(
    ways: Sequence[RailWay],
    point: LatLon,
    max_distance_m: float = 400.0,
    corridor_width_m: float = 60.0,
    parallel_tolerance_deg: float = 25.0,
    include_service: bool = False,
) -> list[Corridor]:
    """Regroupe les voies proches du point en corridors parallèles.

    Args:
        ways: tronçons candidats (typiquement la réponse Overpass complète).
        point: point d'observation.
        max_distance_m: on ignore les voies plus éloignées que cela.
        corridor_width_m: écart latéral maximal entre deux voies d'un même faisceau.
        parallel_tolerance_deg: écart d'axe maximal entre deux voies d'un même faisceau.
        include_service: inclure les voies de service (garages, faisceaux fret).

    Returns:
        Les corridors triés du plus proche au plus éloigné.
    """
    usable = [
        way
        for way in ways
        if way.tags.get("railway") in MAIN_RAILWAY_VALUES
        and (include_service or not way.is_service)
    ]

    candidates: list[tuple[RailWay, Projection]] = []
    for way in usable:
        projection = project_on_polyline(point, way.geometry)
        if projection.distance_m > max_distance_m:
            continue
        # Une voie qui s'arrête avant le point s'y projette sur son extrémité :
        # la distance mesurée est alors longitudinale, pas latérale. La retenir
        # ferait naître un corridor fantôme, « à 383 m », là où il n'y a qu'un
        # tronçon de la même voie qui ne va pas jusqu'au bout. Elle rejoindra
        # tout de même le corridor par recollement.
        if projection.clamped and projection.distance_m > ENDPOINT_TOLERANCE_M:
            continue
        candidates.append((way, projection))

    candidates.sort(key=lambda item: item[1].distance_m)

    groups: list[list[tuple[RailWay, Projection]]] = []
    for way, projection in candidates:
        for group in groups:
            ref_way, ref_projection = group[0]
            close = haversine_m(projection.point, ref_projection.point) <= corridor_width_m
            parallel = (
                axis_distance_deg(projection.bearing_deg, ref_projection.bearing_deg)
                <= parallel_tolerance_deg
            )
            if close and parallel:
                group.append((way, projection))
                break
        else:
            groups.append([(way, projection)])

    corridors: list[Corridor] = []
    for index, group in enumerate(groups):
        group_ways = [w for w, _ in group]
        nearest = min(p.distance_m for _, p in group)
        # La polyligne représentative est amorcée sur les voies du corridor
        # lui-même, puis prolongée par les tronçons voisins qui la continuent
        # sans virage brusque. L'amorçage est essentiel : une chaîne construite
        # globalement traverserait la gare et basculerait sur la ligne voisine,
        # puisque toutes y partagent des nœuds.
        group_ids = {w.osm_id for w in group_ways}
        # On suit la consommation du vivier pour savoir quels tronçons forment
        # la polyligne, plutôt que de le deviner après coup.
        inner = {w.osm_id: list(w.geometry) for w in group_ways}
        seed_id = max(inner, key=lambda i: polyline_length_m(inner[i]))
        centerline = inner.pop(seed_id)
        path_ids = {seed_id}

        remaining = set(inner)
        centerline = extend_chain(centerline, inner)
        path_ids |= remaining - set(inner)

        outer = {w.osm_id: list(w.geometry) for w in usable if w.osm_id not in group_ids}
        remaining = set(outer)
        centerline = extend_chain(centerline, outer)
        path_ids |= remaining - set(outer)

        projection = project_on_polyline(point, centerline)
        # Dans un tronc commun, un même faisceau porte des voies de plusieurs
        # lignes : les nommer toutes est plus juste que de retenir la première
        # rencontrée, qui donnerait au corridor l'identité d'une seule.
        names = Counter(w.name for w in group_ways if w.name)
        if names:
            label = " + ".join(name for name, _ in names.most_common(2))
        else:
            label = next((w.ref for w in group_ways if w.ref), f"corridor {index + 1}")
        corridors.append(
            Corridor(
                corridor_id=f"c{index + 1}",
                label=label,
                ways=tuple(group_ways),
                distance_m=nearest,
                axis_deg=projection.bearing_deg % 180.0,
                centerline=tuple(centerline),
                projection=projection,
                observer=point,
                path_way_ids=frozenset(path_ids),
            )
        )

    corridors.sort(key=lambda c: c.distance_m)
    return corridors


def merged_length_m(segments: Sequence[SpeedSegment]) -> float:
    """Longueur couverte par des tronçons, sans compter deux fois ce qui se recouvre."""
    spans = sorted((s.start_m, s.end_m) for s in segments)
    total, current_end = 0.0, float("-inf")
    for start, end in spans:
        start = max(start, current_end)
        if end > start:
            total += end - start
            current_end = end
    return total


def speed_profile(
    corridor: "Corridor",
    ways: Sequence[RailWay],
    anchor: LatLon,
) -> list[SpeedSegment]:
    """Relevé des vitesses et ouvrages le long du parcours gare -> point.

    Le modèle de marche ne connaît qu'une vitesse par branche, alors que la voie
    en change plusieurs fois : une restriction de tunnel, une courbe, les
    appareils de voie d'un avant-gare. Ce relevé montre où elles tombent, ce
    qu'un panneau vu depuis une passerelle ne dit pas — un panneau donne une
    limite, pas l'étendue sur laquelle elle porte.

    Les abscisses sont comptées **depuis la gare d'appui**, quel que soit le
    sens dans lequel la polyligne a été reconstituée.

    Args:
        corridor: corridor dont la polyligne sert de référence.
        ways: tous les tronçons récupérés ; seuls ceux que la polyligne
            emprunte réellement sont retenus.
        anchor: position de la gare d'appui.

    Returns:
        Les tronçons rencontrés entre la gare et le point, dans cet ordre.
    """
    centerline = list(corridor.centerline)
    anchor_along = project_on_polyline(anchor, centerline).along_m
    observer_along = corridor.projection.along_m
    # Sens de parcours : la gare est l'origine, le point l'extrémité.
    forward = 1.0 if observer_along >= anchor_along else -1.0
    total = abs(observer_along - anchor_along)

    def from_anchor(along: float) -> float:
        return (along - anchor_along) * forward

    segments: list[SpeedSegment] = []
    for way in ways:
        if way.osm_id not in corridor.path_way_ids:
            continue
        ends = sorted(
            from_anchor(project_on_polyline(point, centerline).along_m)
            for point in (way.geometry[0], way.geometry[-1])
        )
        start, end = max(ends[0], 0.0), min(ends[1], total)
        if end - start < 1.0:
            continue
        segments.append(
            SpeedSegment(
                start_m=start,
                end_m=end,
                maxspeed_kmh=way.maxspeed_kmh,
                tunnel=way.tags.get("tunnel") not in (None, "no"),
                bridge=way.tags.get("bridge") not in (None, "no"),
                label=way.label(),
            )
        )

    segments.sort(key=lambda s: s.start_m)
    return segments


def slice_polyline(
    polyline: Sequence[LatLon], start_m: float, end_m: float
) -> list[LatLon]:
    """Extrait la portion d'une polyligne entre deux abscisses curvilignes."""
    if end_m <= start_m:
        return []
    cumulative = cumulative_lengths_m(polyline)
    points = [interpolate_along(polyline, start_m)]
    points.extend(
        vertex
        for vertex, along in zip(polyline, cumulative)
        if start_m < along < end_m
    )
    points.append(interpolate_along(polyline, end_m))
    return points


def _speed_colour(maxspeed_kmh: float | None) -> str:
    """Couleur simplestyle d'un tronçon, selon sa vitesse."""
    if maxspeed_kmh is None:
        return "#8E8E93"
    if maxspeed_kmh < 40:
        return "#C0392B"
    if maxspeed_kmh < 80:
        return "#E67E22"
    if maxspeed_kmh < 110:
        return "#2980B9"
    return "#27AE60"


def to_geojson(
    corridors: Sequence["Corridor"],
    ways: Sequence[RailWay],
    observer: LatLon,
    anchor: LatLon | None = None,
    anchor_name: str = "gare d'appui",
) -> dict[str, Any]:
    """Assemble la géométrie résolue en une collection GeoJSON.

    Destinée à être déposée sur un fond de carte — geojson.io, QGIS, My Maps —
    pour voir ce que l'outil a réellement trouvé, plutôt que de s'en remettre à
    la description qu'il en fait. Les propriétés suivent la convention
    « simplestyle », comprise par la plupart des visualiseurs.
    """
    features: list[dict[str, Any]] = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [observer[1], observer[0]]},
            "properties": {
                "title": "Point d'observation",
                "marker-color": "#B5312B",
                "marker-symbol": "star",
            },
        }
    ]
    if anchor is not None:
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [anchor[1], anchor[0]]},
                "properties": {
                    "title": anchor_name,
                    "marker-color": "#1D6B77",
                    "marker-symbol": "rail",
                },
            }
        )

    for corridor in corridors:
        centerline = list(corridor.centerline)
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[p[1], p[0]] for p in centerline],
                },
                "properties": {
                    "title": f"{corridor.corridor_id} — {corridor.label}",
                    "description": (
                        f"{corridor.distance_m:.0f} m du point · "
                        f"axe {corridor.axis_deg:.0f}° · "
                        f"{corridor.segment_count} tronçon(s)"
                    ),
                    "stroke": "#4A4A4A",
                    "stroke-width": 1,
                    "stroke-opacity": 0.45,
                },
            }
        )

        if anchor is None:
            continue
        anchor_along = project_on_polyline(anchor, centerline).along_m
        forward = 1.0 if corridor.projection.along_m >= anchor_along else -1.0
        for segment in speed_profile(corridor, ways, anchor):
            bounds = sorted(
                (anchor_along + forward * segment.start_m, anchor_along + forward * segment.end_m)
            )
            portion = slice_polyline(centerline, bounds[0], bounds[1])
            if len(portion) < 2:
                continue
            features.append(
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[p[1], p[0]] for p in portion],
                    },
                    "properties": {
                        "title": segment.describe(),
                        # Le nom du tronçon dit s'il s'agit d'une ligne
                        # principale ou d'une voie de service : près d'une gare,
                        # c'est ce qui permet de juger si le profil suit
                        # l'itinéraire que prennent réellement les trains.
                        "voie": segment.label,
                        "corridor": corridor.corridor_id,
                        "maxspeed_kmh": segment.maxspeed_kmh,
                        "tunnel": segment.tunnel,
                        "bridge": segment.bridge,
                        "start_m": round(segment.start_m),
                        "end_m": round(segment.end_m),
                        "stroke": _speed_colour(segment.maxspeed_kmh),
                        "stroke-width": 6 if segment.tunnel else 4,
                        "stroke-opacity": 0.9,
                    },
                }
            )

    return {"type": "FeatureCollection", "features": features}


def build_query(
    point: LatLon,
    radius_m: float,
    include_service: bool = True,
    anchor: LatLon | None = None,
    anchor_corridor_m: float = 400.0,
    lookahead_m: float = DEFAULT_LOOKAHEAD_M,
) -> str:
    """Construit la requête Overpass QL pour un point d'observation.

    Args:
        point: point d'observation.
        radius_m: rayon de recherche des voies autour du point.
        include_service: récupérer aussi les voies de service.
        anchor: gare d'appui. Quand elle est fournie, la requête récupère aussi
            les voies longeant le segment point <-> gare : sans elles, les
            polylignes s'arrêtent au bord du rayon de recherche et toute
            distance mesurée jusqu'à la gare est fausse.
        anchor_corridor_m: demi-largeur du couloir récupéré le long de ce segment.
        lookahead_m: portée au-delà du point. Sans elle, la géométrie s'arrête
            avant les bifurcations et tous les corridors semblent partir dans la
            même direction — ce qui les rend indiscernables. Seules les lignes
            (`usage=main|branch`) sont ramenées à cette distance, pour ne pas
            aspirer tous les faisceaux de la ville.
    """
    lat, lon = point
    railway_filter = "|".join(MAIN_RAILWAY_VALUES)
    service_clause = "" if include_service else '["service"!~"."]'
    rail_clause = f'["railway"~"^({railway_filter})$"]{service_clause}'

    lookahead_clause = ""
    if lookahead_m > 0:
        lookahead_clause = (
            f"  way(around:{lookahead_m + 500:.0f},{lat:.6f},{lon:.6f})"
            f'["railway"~"^({railway_filter})$"]["usage"~"^(main|branch)$"];\n'
        )

    corridor_clause = ""
    station_radius = radius_m * 4
    if anchor is not None:
        # `around` accepte une polyligne : on balaie tout le couloir menant à la gare.
        corridor_clause = (
            f"  way(around:{anchor_corridor_m:.0f},"
            f"{lat:.6f},{lon:.6f},{anchor[0]:.6f},{anchor[1]:.6f}){rail_clause};\n"
        )
        station_radius = max(station_radius, haversine_m(point, anchor) + 1000.0)

    return f"""
[out:json][timeout:120];
(
  way(around:{radius_m:.0f},{lat:.6f},{lon:.6f}){rail_clause};
{corridor_clause}{lookahead_clause}  node(around:{station_radius:.0f},{lat:.6f},{lon:.6f})["railway"~"^(station|halt)$"]["station"!~"^(subway|light_rail|monorail)$"];
);
out tags geom;
""".strip()


# --------------------------------------------------------------------------
# Client réseau
# --------------------------------------------------------------------------


class OverpassError(RuntimeError):
    """Overpass a renvoyé une erreur ou est injoignable."""


class OverpassClient:
    """Client Overpass minimal avec cache disque.

    La géométrie ferroviaire ne bouge quasiment jamais : on met la réponse en
    cache indéfiniment et on ne rappelle Overpass que sur demande explicite
    (`refresh=True`). Cela rend l'outil utilisable hors ligne une fois la
    première résolution faite.
    """

    def __init__(
        self,
        cache_dir: Path | str = "cache/overpass",
        endpoint: str = DEFAULT_ENDPOINT,
        timeout_s: float = 120.0,
        user_agent: str = "NextTrainToSee/0.1 (+https://github.com/bensaintsever/NextTrainToSee)",
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.endpoint = endpoint
        self.timeout_s = timeout_s
        self.user_agent = user_agent

    def cache_path(self, query: str) -> Path:
        """Emplacement du cache pour une requête donnée."""
        digest = hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]
        return self.cache_dir / f"{digest}.json"

    def query(self, query: str, refresh: bool = False) -> dict[str, Any]:
        """Exécute une requête Overpass QL, en passant par le cache disque."""
        path = self.cache_path(query)
        if path.exists() and not refresh:
            log.debug("Overpass : réponse servie depuis le cache %s", path)
            return json.loads(path.read_text(encoding="utf-8"))

        data = urllib.parse.urlencode({"data": query}).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint, data=data, headers={"User-Agent": self.user_agent}
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:  # pragma: no cover - dépend du réseau
            raise OverpassError(f"Overpass a répondu {exc.code} : {exc.reason}") from exc
        except urllib.error.URLError as exc:  # pragma: no cover - dépend du réseau
            raise OverpassError(f"Overpass injoignable : {exc.reason}") from exc

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return payload

    def around(
        self,
        point: LatLon,
        radius_m: float = 400.0,
        refresh: bool = False,
        anchor: LatLon | None = None,
        lookahead_m: float = DEFAULT_LOOKAHEAD_M,
    ) -> tuple[list[RailWay], list[RailStop]]:
        """Récupère voies et gares autour d'un point, et jusqu'à la gare d'appui."""
        query = build_query(point, radius_m, anchor=anchor, lookahead_m=lookahead_m)
        return parse_overpass(self.query(query, refresh=refresh))


def nearest_stops(stops: Iterable[RailStop], point: LatLon, limit: int = 5) -> list[tuple[RailStop, float]]:
    """Gares les plus proches du point, avec leur distance à vol d'oiseau."""
    ranked = [(stop, haversine_m(point, stop.position)) for stop in stops]
    ranked.sort(key=lambda item: item[1])
    return ranked[:limit]
