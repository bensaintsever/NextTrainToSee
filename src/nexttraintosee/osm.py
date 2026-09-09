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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .geo import (
    LatLon,
    Projection,
    axis_distance_deg,
    haversine_m,
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

    @property
    def track_count(self) -> int:
        return len(self.ways)

    @property
    def maxspeed_kmh(self) -> float | None:
        speeds = [w.maxspeed_kmh for w in self.ways if w.maxspeed_kmh]
        return max(speeds) if speeds else None

    def along_distance_to_m(self, position: LatLon) -> float:
        """Distance curviligne, le long des voies, entre `position` et le point.

        Beaucoup plus juste que la distance à vol d'oiseau dès que la ligne
        courbe — ce qui est la règle en sortie de gare.
        """
        other = project_on_polyline(position, self.centerline)
        return abs(other.along_m - self.projection.along_m)


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


def stitch_ways(ways: Sequence[RailWay]) -> list[list[LatLon]]:
    """Recolle des tronçons OSM en chaînes continues.

    OSM découpe une ligne en de nombreux `way` (changement de vitesse, de pont,
    d'électrification...). Pour mesurer une distance curviligne il faut d'abord
    reconstituer des polylignes continues en recollant les tronçons par leurs
    extrémités communes.
    """
    remaining = {w.osm_id: list(w.geometry) for w in ways}
    chains: list[list[LatLon]] = []

    while remaining:
        # On démarre sur le tronçon le plus long restant : cela donne des chaînes
        # stables et indépendantes de l'ordre de la réponse Overpass.
        seed_id = max(remaining, key=lambda i: polyline_length_m(remaining[i]))
        chain = remaining.pop(seed_id)
        extended = True
        while extended:
            extended = False
            head, tail = _endpoint_key(chain[0]), _endpoint_key(chain[-1])
            for other_id, geom in list(remaining.items()):
                g_head, g_tail = _endpoint_key(geom[0]), _endpoint_key(geom[-1])
                if g_head == tail:
                    chain.extend(geom[1:])
                elif g_tail == tail:
                    chain.extend(reversed(geom[:-1]))
                elif g_tail == head:
                    chain[:0] = geom[:-1]
                elif g_head == head:
                    chain[:0] = list(reversed(geom[1:]))
                else:
                    continue
                del remaining[other_id]
                extended = True
                break
        chains.append(chain)

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
    candidates: list[tuple[RailWay, Projection]] = []
    for way in ways:
        if not include_service and way.is_service:
            continue
        if way.tags.get("railway") not in MAIN_RAILWAY_VALUES:
            continue
        projection = project_on_polyline(point, way.geometry)
        if projection.distance_m <= max_distance_m:
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
        chains = stitch_ways(group_ways)
        centerline = max(chains, key=polyline_length_m)
        projection = project_on_polyline(point, centerline)
        nearest = min(p.distance_m for _, p in group)
        label = next(
            (w.name for w in group_ways if w.name),
            next((w.ref for w in group_ways if w.ref), f"corridor {index + 1}"),
        )
        corridors.append(
            Corridor(
                corridor_id=f"c{index + 1}",
                label=label,
                ways=tuple(group_ways),
                distance_m=nearest,
                axis_deg=projection.bearing_deg % 180.0,
                centerline=tuple(centerline),
                projection=projection,
            )
        )

    corridors.sort(key=lambda c: c.distance_m)
    return corridors


def build_query(point: LatLon, radius_m: float, include_service: bool = True) -> str:
    """Construit la requête Overpass QL pour un point d'observation."""
    lat, lon = point
    railway_filter = "|".join(MAIN_RAILWAY_VALUES)
    service_clause = "" if include_service else '["service"!~"."]'
    return f"""
[out:json][timeout:90];
(
  way(around:{radius_m:.0f},{lat:.6f},{lon:.6f})["railway"~"^({railway_filter})$"]{service_clause};
  node(around:{radius_m * 4:.0f},{lat:.6f},{lon:.6f})["railway"~"^(station|halt)$"];
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

    def _cache_path(self, query: str) -> Path:
        digest = hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]
        return self.cache_dir / f"{digest}.json"

    def query(self, query: str, refresh: bool = False) -> dict[str, Any]:
        """Exécute une requête Overpass QL, en passant par le cache disque."""
        path = self._cache_path(query)
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
        self, point: LatLon, radius_m: float = 400.0, refresh: bool = False
    ) -> tuple[list[RailWay], list[RailStop]]:
        """Récupère voies et gares autour d'un point."""
        return parse_overpass(self.query(build_query(point, radius_m), refresh=refresh))


def nearest_stops(stops: Iterable[RailStop], point: LatLon, limit: int = 5) -> list[tuple[RailStop, float]]:
    """Gares les plus proches du point, avec leur distance à vol d'oiseau."""
    ranked = [(stop, haversine_m(point, stop.position)) for stop in stops]
    ranked.sort(key=lambda item: item[1])
    return ranked[:limit]
