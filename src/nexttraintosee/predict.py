"""Cœur de la prédiction : dater le passage d'un train en pleine voie.

Le GTFS ne donne des horaires qu'aux gares. Pour dater un passage devant un
point quelconque, on s'appuie sur la gare la plus proche (« gare d'appui ») et
on raisonne en trois temps :

1. **Quelles circulations peuvent passer ?** Toutes celles qui desservent la
   gare d'appui, et dont l'arrêt voisin (précédent ou suivant) se trouve dans
   la direction du point d'observation. Le cap gare -> arrêt voisin suffit à
   déterminer la branche empruntée en sortie de gare.
2. **Dans quel régime ?** Un train qui part de la gare accélère ; un train qui
   y arrive freine ; un train qui la traverse sans arrêt roule à vitesse de
   ligne. Les trois donnent des temps de parcours nettement différents.
3. **Quand exactement ?** Horaire en gare +/- le temps de parcours du modèle de
   marche, décalé du retard temps réel s'il est connu.

Limite assumée et documentée : une circulation qui ne figure pas au GTFS
(fret, haut-le-pied, train de travaux) est invisible ici. C'est précisément ce
que le capteur local vient compléter.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Sequence

from .geo import LatLon, bearing_distance_deg, haversine_m, initial_bearing_deg
from .gtfs import GtfsFeed, StopTime, Trip, service_datetime
from .motion import Regime, TractionProfile, speed_at_point_ms, travel_time_s, travel_time_uncertainty_s

log = logging.getLogger(__name__)


class Direction(str, Enum):
    """Sens de circulation vu depuis la gare d'appui."""

    OUTBOUND = "outbound"
    """Le train s'éloigne de la gare d'appui."""
    INBOUND = "inbound"
    """Le train se dirige vers la gare d'appui."""


@dataclass(frozen=True)
class Branch:
    """Une direction ferroviaire au départ de la gare d'appui.

    Une branche est identifiée par le cap sous lequel on la quitte : depuis
    Toulouse-Matabiau, l'axe de Narbonne part au sud-est, celui de Latour-de-Carol
    au sud, celui de Bordeaux au nord-ouest. Rattacher une circulation à sa
    branche revient donc à comparer le cap « gare d'appui -> arrêt voisin » aux
    caps déclarés ici.
    """

    branch_id: str
    label: str
    bearing_deg: float
    """Cap moyen, depuis la gare d'appui, des gares situées sur cette branche."""
    passes_observer: bool = True
    """Faux pour une branche qui s'éloigne sans passer devant le point."""
    tolerance_deg: float = 40.0
    """Demi-ouverture du secteur angulaire attribué à la branche."""
    track_distance_m: float | None = None
    """Distance curviligne gare d'appui -> point, mesurée sur la géométrie OSM."""
    line_speed_kmh: float | None = None
    """Vitesse limite locale, si elle diffère du profil par défaut."""
    corridor_id: str | None = None
    """Corridor OSM correspondant, à titre de traçabilité."""

    def matches(self, bearing_deg: float) -> bool:
        return bearing_distance_deg(self.bearing_deg, bearing_deg) <= self.tolerance_deg


@dataclass(frozen=True)
class Site:
    """Un point d'observation et tout ce qu'il faut pour y prédire les passages."""

    name: str
    position: LatLon
    anchor_station: str
    """Nom de la gare d'appui, tel qu'il apparaît dans le GTFS."""
    branches: tuple[Branch, ...]
    anchor_position: LatLon | None = None
    """Position de la gare d'appui ; déduite du GTFS si absente."""
    profile: TractionProfile = field(default_factory=TractionProfile)

    def branch_for(self, bearing_deg: float) -> Branch | None:
        """Branche dont le secteur angulaire contient ce cap, la plus proche d'abord."""
        candidates = [b for b in self.branches if b.matches(bearing_deg)]
        if not candidates:
            return None
        return min(candidates, key=lambda b: bearing_distance_deg(b.bearing_deg, bearing_deg))


@dataclass(frozen=True)
class Passage:
    """Un passage prédit devant le point d'observation."""

    trip_id: str
    when: datetime
    uncertainty_s: float
    branch: Branch
    regime: Regime
    direction: Direction
    speed_kmh: float
    anchor_time: datetime
    """Horaire théorique en gare d'appui qui sert de référence."""
    route_label: str = ""
    headsign: str = ""
    delay_s: int | None = None
    """Retard temps réel appliqué, en secondes. None si aucune donnée."""

    @property
    def is_realtime(self) -> bool:
        return self.delay_s is not None

    @property
    def window(self) -> tuple[datetime, datetime]:
        """Fenêtre de passage plausible."""
        margin = timedelta(seconds=self.uncertainty_s)
        return (self.when - margin, self.when + margin)

    def describe(self) -> str:
        arrow = "→" if self.direction is Direction.OUTBOUND else "←"
        stamp = self.when.strftime("%H:%M:%S")
        flag = "TR" if self.is_realtime else "th"
        delay = ""
        if self.delay_s:
            delay = f" ({self.delay_s // 60:+d} min)"
        return (
            f"{stamp} ±{self.uncertainty_s:.0f}s [{flag}] {arrow} {self.branch.label} "
            f"· {self.route_label} {self.headsign}{delay} · {self.speed_kmh:.0f} km/h"
        )


def _anchor_position(feed: GtfsFeed, site: Site) -> LatLon:
    """Position de la gare d'appui : celle du site, sinon celle du GTFS."""
    if site.anchor_position is not None:
        return site.anchor_position
    for stop in feed.find_stops_by_name(site.anchor_station):
        if stop.position:
            return stop.position
    raise ValueError(
        f"position de la gare d'appui « {site.anchor_station} » introuvable : "
        "renseignez anchor_position dans la configuration du site"
    )


def _distance_to_observer_m(site: Site, branch: Branch, anchor: LatLon) -> float:
    """Distance à parcourir entre la gare d'appui et le point d'observation."""
    if branch.track_distance_m is not None:
        return branch.track_distance_m
    # Repli sans géométrie OSM : distance à vol d'oiseau corrigée de la sinuosité.
    return haversine_m(anchor, site.position) * site.profile.sinuosity


def _profile_for(site: Site, branch: Branch) -> TractionProfile:
    if branch.line_speed_kmh is None:
        return site.profile
    return replace(site.profile, line_speed_kmh=branch.line_speed_kmh)


def _neighbour_segments(
    trip: Trip, anchor_index: int
) -> list[tuple[StopTime, StopTime, Direction]]:
    """Segments adjacents à la gare d'appui dans la desserte d'une circulation.

    Renvoie au plus deux couples (arrêt d'appui, arrêt voisin, sens) : le
    segment amont (le train arrive) et le segment aval (le train repart).
    """
    segments: list[tuple[StopTime, StopTime, Direction]] = []
    anchor_stop_time = trip.stop_times[anchor_index]
    if anchor_index > 0:
        segments.append((anchor_stop_time, trip.stop_times[anchor_index - 1], Direction.INBOUND))
    if anchor_index < len(trip.stop_times) - 1:
        segments.append((anchor_stop_time, trip.stop_times[anchor_index + 1], Direction.OUTBOUND))
    return segments


def _regime_for(anchor_stop_time: StopTime, direction: Direction) -> Regime:
    """Régime de marche au droit de la gare d'appui.

    Un arrêt purement technique (ni montée ni descente) correspond en pratique
    à un passage sans arrêt commercial : on suppose alors une marche continue.
    """
    if not anchor_stop_time.is_revenue_stop:
        return Regime.THROUGH
    return Regime.DEPARTING if direction is Direction.OUTBOUND else Regime.ARRIVING


def predict_passages(
    feed: GtfsFeed,
    site: Site,
    day: date,
    delays_s: dict[str, int] | None = None,
    canceled_trip_ids: Sequence[str] | None = None,
    include_non_rail: bool = False,
) -> list[Passage]:
    """Tous les passages prédits devant le point, pour une journée de service.

    Args:
        feed: horaires théoriques chargés autour de la gare d'appui.
        site: point d'observation et description de ses branches.
        day: journée de service GTFS (attention : un train de 00h20 appartient
            à la journée de la veille).
        delays_s: retard temps réel par `trip_id`, en secondes.
        canceled_trip_ids: circulations supprimées, à ne pas prédire.
        include_non_rail: conserver les cars de substitution (jamais sur rail,
            donc exclus par défaut).

    Returns:
        Les passages triés par heure croissante.
    """
    anchor = _anchor_position(feed, site)
    anchor_stop_ids = sorted(feed.station_stop_ids(site.anchor_station))
    if not anchor_stop_ids:
        raise ValueError(f"gare d'appui « {site.anchor_station} » absente du flux GTFS")

    delays_s = delays_s or {}
    canceled = set(canceled_trip_ids or ())
    passages: list[Passage] = []

    for trip in feed.trips_on(day):
        if trip.trip_id in canceled:
            continue
        if not include_non_rail and not feed.is_rail(trip):
            continue
        anchor_index = trip.index_of_stop(anchor_stop_ids)
        if anchor_index is None:
            continue

        route = feed.routes.get(trip.route_id)
        for anchor_stop_time, neighbour, direction in _neighbour_segments(trip, anchor_index):
            neighbour_stop = feed.stops.get(neighbour.stop_id)
            if neighbour_stop is None or neighbour_stop.position is None:
                log.debug("arrêt voisin %s sans position, segment ignoré", neighbour.stop_id)
                continue

            bearing = initial_bearing_deg(anchor, neighbour_stop.position)
            branch = site.branch_for(bearing)
            if branch is None:
                log.debug(
                    "cap %.0f° (vers %s) hors de toute branche déclarée", bearing, neighbour_stop.name
                )
                continue
            if not branch.passes_observer:
                continue

            regime = _regime_for(anchor_stop_time, direction)
            reference_s = (
                anchor_stop_time.departure_s
                if direction is Direction.OUTBOUND
                else anchor_stop_time.arrival_s
            )
            if reference_s is None:
                continue

            profile = _profile_for(site, branch)
            distance = _distance_to_observer_m(site, branch, anchor)
            travel = travel_time_s(distance, regime, profile)
            uncertainty = travel_time_uncertainty_s(distance, regime, profile)
            speed = speed_at_point_ms(distance, regime, profile) * 3.6

            anchor_time = service_datetime(day, reference_s, feed.timezone)
            delay = delays_s.get(trip.trip_id)
            # Le retard décale l'horaire en gare ; le temps de parcours s'ajoute
            # après la gare au départ, et se retranche avant elle à l'arrivée.
            when = anchor_time + timedelta(seconds=delay or 0)
            if direction is Direction.OUTBOUND:
                when += timedelta(seconds=travel)
            else:
                when -= timedelta(seconds=travel)

            passages.append(
                Passage(
                    trip_id=trip.trip_id,
                    when=when,
                    uncertainty_s=uncertainty,
                    branch=branch,
                    regime=regime,
                    direction=direction,
                    speed_kmh=speed,
                    anchor_time=anchor_time,
                    route_label=route.label() if route else "",
                    headsign=trip.headsign,
                    delay_s=delay,
                )
            )

    passages.sort(key=lambda p: p.when)
    return passages


def next_passages(
    passages: Sequence[Passage],
    now: datetime,
    horizon: timedelta = timedelta(hours=2),
    limit: int | None = None,
) -> list[Passage]:
    """Filtre les passages à venir dans une fenêtre glissante."""
    upcoming = [p for p in passages if now <= p.when <= now + horizon]
    return upcoming[:limit] if limit else upcoming


def service_days_around(moment: datetime) -> list[date]:
    """Journées de service à interroger pour couvrir un instant donné.

    Un train à 00h20 appartient à la journée de service de la veille : pour ne
    rien manquer autour de minuit, il faut charger les deux.
    """
    return [moment.date() - timedelta(days=1), moment.date()]
