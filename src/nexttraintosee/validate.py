"""Vérifier le modèle de marche sans mettre les pieds sur le terrain.

Le point d'observation se trouve entre deux gares. Les horaires publient donc
déjà, pour chaque circulation, le temps mis à parcourir un segment qui *contient*
le point. C'est une vérité terrain gratuite, et elle suffit à dire si le modèle
de marche est plausible.

Deux précautions rendent cette vérification honnête :

* **On compare au plus rapide, jamais à la médiane.** Un horaire porte une marge
  de régularité de l'ordre de deux minutes ; seules les circulations les plus
  tendues approchent la limite physique.
* **On ne s'en sert pas pour interpoler.** Les horaires sont arrondis à la
  minute et inégalement margés : répartir une durée horaire le long du segment
  placerait les passages trop tard. Le modèle reste ancré sur l'heure en gare
  d'appui ; l'horaire ne sert qu'à le contrôler.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date

from .geo import LatLon, initial_bearing_deg, project_on_polyline
from .gtfs import GtfsFeed
from .motion import segment_time_s
from .osm import Corridor
from .predict import Site, trip_descriptor

#: Au-delà, on considère que l'arrêt n'est pas sur le corridor examiné.
MAX_STOP_OFFSET_M = 300.0

#: Écart en deçà duquel le modèle est jugé cohérent avec l'horaire le plus rapide.
AGREEMENT_TOLERANCE_S = 45.0


@dataclass(frozen=True)
class SegmentCheck:
    """Confrontation d'un segment horaire au modèle de marche."""

    neighbour: str
    length_m: float
    trip_count: int
    fastest_s: float
    median_s: float
    modelled_s: float
    covers_observer: bool
    spread_s: float = 0.0
    """Étendue des temps horaires sur le segment.

    Une étendue nulle signale une allocation standard reconduite d'un train à
    l'autre : le minimum n'y est alors pas une marche tendue, et ne dit rien de
    la limite physique.
    """
    branch_id: str | None = None
    """Branche à laquelle ce segment se rattache, si elle est identifiable."""
    line_speed_kmh: float | None = None
    """Vitesse de ligne effectivement employée pour le modèle."""

    @property
    def gap_s(self) -> float:
        """Modèle moins horaire le plus rapide. Négatif : le modèle est plus rapide."""
        return self.modelled_s - self.fastest_s

    @property
    def median_padding_s(self) -> float:
        """Marge de régularité médiane."""
        return self.median_s - self.fastest_s

    @property
    def implied_speed_kmh(self) -> float:
        """Vitesse moyenne du parcours le plus rapide."""
        return self.length_m / self.fastest_s * 3.6 if self.fastest_s else 0.0

    @property
    def verdict(self) -> str:
        if self.gap_s < -AGREEMENT_TOLERANCE_S:
            return "modèle trop rapide"
        if self.gap_s > AGREEMENT_TOLERANCE_S:
            return "modèle trop lent"
        return "cohérent"

    @property
    def is_consistent(self) -> bool:
        return self.verdict == "cohérent"

    @property
    def has_tight_run(self) -> bool:
        """Vrai si les horaires varient assez pour que le minimum ait un sens."""
        return self.spread_s >= 60.0


def place_stop_on_corridors(
    position: LatLon, corridors: list[Corridor], anchor: LatLon, observer: LatLon
) -> tuple[Corridor, float, bool] | None:
    """Rattache un arrêt au corridor qui le porte.

    Returns:
        Le corridor, la distance curviligne depuis la gare d'appui, et si le
        point d'observation tombe entre les deux. `None` si aucun corridor ne
        passe assez près de l'arrêt.
    """
    best: tuple[Corridor, float, bool] | None = None
    best_offset = MAX_STOP_OFFSET_M
    for corridor in corridors:
        projection = project_on_polyline(position, corridor.centerline)
        if projection.distance_m >= best_offset:
            continue
        anchor_along = project_on_polyline(anchor, corridor.centerline).along_m
        observer_along = project_on_polyline(observer, corridor.centerline).along_m
        low, high = sorted((anchor_along, projection.along_m))
        best = (
            corridor,
            abs(projection.along_m - anchor_along),
            low <= observer_along <= high,
        )
        best_offset = projection.distance_m
    return best


def collect_segment_times(
    feed: GtfsFeed, anchor_stop_ids: set[str], day: date
) -> dict[str, list[float]]:
    """Temps de parcours horaires entre la gare d'appui et chaque arrêt voisin."""
    times: dict[str, list[float]] = {}
    for trip in feed.trips_on(day):
        if not feed.is_rail(trip):
            continue
        index = trip.index_of_stop(sorted(anchor_stop_ids))
        if index is None:
            continue
        anchor_stop_time = trip.stop_times[index]
        for offset, outbound in ((-1, False), (1, True)):
            neighbour_index = index + offset
            if not 0 <= neighbour_index < len(trip.stop_times):
                continue
            neighbour = trip.stop_times[neighbour_index]
            stop = feed.stops.get(neighbour.stop_id)
            if stop is None or stop.position is None:
                continue
            if outbound:
                start, end = anchor_stop_time.departure_s, neighbour.arrival_s
            else:
                start, end = neighbour.departure_s, anchor_stop_time.arrival_s
            if start is None or end is None:
                continue
            duration = end - start
            if duration > 0:
                times.setdefault(stop.name, []).append(float(duration))
    return times


def check_segments(
    feed: GtfsFeed,
    corridors: list[Corridor],
    site: Site,
    day: date,
    min_trips: int = 5,
) -> list[SegmentCheck]:
    """Confronte le modèle de marche aux horaires, segment par segment.

    Chaque segment est évalué avec le profil de **sa** branche : deux axes au
    départ d'une même gare n'ont pas la même marche, et les confondre ferait
    conclure à tort que le modèle est faux.

    Seuls les segments desservis par au moins `min_trips` circulations sont
    retenus : sur un ou deux horaires, le plus rapide n'a aucune valeur
    statistique.
    """
    anchor = site.anchor_position
    if anchor is None:
        raise ValueError("la position de la gare d'appui est nécessaire")
    observer = site.position
    anchor_stop_ids = feed.station_stop_ids(site.anchor_station)
    times_by_stop = collect_segment_times(feed, anchor_stop_ids, day)

    checks: list[SegmentCheck] = []
    for name, times in times_by_stop.items():
        if len(times) < min_trips:
            continue
        stops = [s for s in feed.find_stops_by_name(name) if s.position]
        if not stops:
            continue
        placed = place_stop_on_corridors(stops[0].position, corridors, anchor, observer)
        if placed is None:
            continue
        _, length_m, covers = placed
        if length_m <= 0:
            continue
        branch = site.branch_for(initial_bearing_deg(anchor, stops[0].position))
        profile = site.profile_for(branch)
        checks.append(
            SegmentCheck(
                neighbour=name,
                length_m=length_m,
                trip_count=len(times),
                fastest_s=min(times),
                median_s=statistics.median(times),
                spread_s=max(times) - min(times),
                modelled_s=segment_time_s(length_m, profile),
                covers_observer=covers,
                branch_id=branch.branch_id if branch else None,
                line_speed_kmh=profile.line_speed_kmh,
            )
        )

    checks.sort(key=lambda c: (not c.covers_observer, c.length_m))
    return checks


def format_duration(seconds: float) -> str:
    """Durée en « m min s s », lisible dans un tableau.

    L'arrondi porte sur le total, jamais sur les secondes seules : arrondir
    après la division produit des « 2 min 60 s ».
    """
    sign = "-" if seconds < 0 else ""
    total = int(round(abs(seconds)))
    return f"{sign}{total // 60} min {total % 60:02d} s"


@dataclass(frozen=True)
class CategoryCoverage:
    """Ce que les horaires permettent — ou non — de caler, pour un type de matériel."""

    category_id: str
    label: str
    segment_count: int
    """Segments de cette catégorie au départ ou à l'arrivée de la gare d'appui."""
    calibratable_count: int
    """Ceux dont l'arrêt voisin est encadré par un segment mesurable."""

    @property
    def is_calibratable(self) -> bool:
        return self.calibratable_count > 0


def category_coverage(
    feed: GtfsFeed, site: Site, checked_neighbours: set[str], day: date
) -> list[CategoryCoverage]:
    """Dit quelles catégories les horaires permettent de caler.

    Une catégorie dont aucune circulation ne s'arrête à une gare voisine
    mesurable ne peut pas être calée sur les horaires : son profil reste une
    estimation jusqu'à ce qu'un capteur la mesure. C'est le cas des trains de
    grandes lignes, qui traversent les haltes de banlieue sans s'y arrêter.
    """
    anchor_stop_ids = sorted(feed.station_stop_ids(site.anchor_station))
    totals: dict[str, int] = {}
    calibratable: dict[str, int] = {}

    for trip in feed.trips_on(day):
        if not feed.is_rail(trip):
            continue
        index = trip.index_of_stop(anchor_stop_ids)
        if index is None:
            continue
        route = feed.routes.get(trip.route_id)
        category = site.category_for(trip_descriptor(trip, route))
        key = category.category_id if category else "(non classé)"
        for offset in (-1, 1):
            neighbour_index = index + offset
            if not 0 <= neighbour_index < len(trip.stop_times):
                continue
            stop = feed.stops.get(trip.stop_times[neighbour_index].stop_id)
            if stop is None:
                continue
            totals[key] = totals.get(key, 0) + 1
            if stop.name in checked_neighbours:
                calibratable[key] = calibratable.get(key, 0) + 1

    labels = {c.category_id: c.label for c in site.categories}
    coverage = [
        CategoryCoverage(
            category_id=key,
            label=labels.get(key, key),
            segment_count=count,
            calibratable_count=calibratable.get(key, 0),
        )
        for key, count in totals.items()
    ]
    coverage.sort(key=lambda c: -c.segment_count)
    return coverage
