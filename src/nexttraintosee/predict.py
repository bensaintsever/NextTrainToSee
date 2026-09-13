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
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Sequence

from .geo import LatLon, bearing_distance_deg, haversine_m, initial_bearing_deg
from .gtfs import GtfsFeed, Route, StopTime, Trip, service_datetime
from .motion import Regime, TractionProfile, speed_at_point_ms, travel_time_s, travel_time_uncertainty_s

log = logging.getLogger(__name__)

#: Marge résiduelle sur une distance relevée sur la géométrie OpenStreetMap.
MEASURED_DISTANCE_TOLERANCE_M = 20.0
#: Marge sur une distance seulement déduite du vol d'oiseau : la sinuosité réelle
#: d'une sortie de gare varie largement d'un site à l'autre.
ESTIMATED_DISTANCE_REL_TOLERANCE = 0.08


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
    tolerance_deg: float = 30.0
    """Demi-ouverture du secteur angulaire attribué à la branche.

    Trop large, deux branches divergentes se recouvrent ; trop étroite, une
    desserte dont l'arrêt voisin est légèrement décalé n'est plus rattachée.
    Trente degrés séparent proprement des axes distants d'une soixantaine de
    degrés, ce qui est le cas courant en sortie de gare.
    """
    track_distance_m: float | None = None
    """Distance curviligne gare d'appui -> point, mesurée sur la géométrie OSM."""
    line_speed_kmh: float | None = None
    """Vitesse limite locale, si elle diffère du profil par défaut."""
    corridor_id: str | None = None
    """Corridor OSM correspondant, à titre de traçabilité."""

    @property
    def is_distance_measured(self) -> bool:
        """Vrai si la distance vient de la géométrie, et non d'une estimation."""
        return self.track_distance_m is not None

    def matches(self, bearing_deg: float) -> bool:
        return bearing_distance_deg(self.bearing_deg, bearing_deg) <= self.tolerance_deg


@dataclass(frozen=True)
class TrainCategory:
    """Un type de matériel, reconnu à ce que le flux dit de la circulation.

    Un automoteur régional, une rame tractée d'Intercités et une rame à grande
    vitesse n'ont ni la même accélération ni la même vitesse pratique en sortie
    de gare. Les distinguer importe d'autant plus que les circulations qui ne
    s'arrêtent pas aux haltes voisines — précisément les Intercités et les TGV —
    échappent au calage sur les horaires : leur profil ne peut être qu'estimé,
    puis mesuré au capteur.
    """

    category_id: str
    label: str
    pattern: str
    """Expression régulière cherchée dans la désignation de la circulation."""
    accel_ms2: float | None = None
    decel_ms2: float | None = None
    line_speed_kmh: float | None = None

    def matches(self, descriptor: str) -> bool:
        return re.search(self.pattern, descriptor) is not None


def trip_descriptor(trip: Trip, route: Route | None) -> str:
    """Désignation d'une circulation, sur laquelle les catégories s'apparient.

    Rassemble ce que le flux offre de discriminant : numéro de circulation, code
    et intitulé de la ligne.
    """
    parts = [trip.headsign]
    if route is not None:
        parts += [route.short_name, route.long_name]
    return " ".join(part for part in parts if part)


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
    lead_margin_s: float = 0.0
    """Marge d'anticipation ajoutée à l'incertitude pour l'heure annoncée.

    Le temps qu'il faut pour se poster à la fenêtre. Manquer un train parce que
    l'annonce arrive trop tard est un échec ; attendre quelques secondes de trop
    ne l'est pas.
    """
    categories: tuple[TrainCategory, ...] = ()
    """Profils par type de matériel, essayés dans l'ordre déclaré."""

    def category_for(self, descriptor: str) -> TrainCategory | None:
        """Première catégorie dont le motif reconnaît cette circulation."""
        return next((c for c in self.categories if c.matches(descriptor)), None)

    def profile_for(
        self, branch: Branch | None = None, category: TrainCategory | None = None
    ) -> TractionProfile:
        """Profil de marche applicable, du plus général au plus précis.

        Trois couches se superposent : le profil du site, puis la vitesse propre
        à la branche — la marche diffère d'un axe à l'autre — puis le matériel,
        qui l'emporte. C'est bien cet ordre qu'il faut : une vitesse de branche
        est calée sur les circulations qui desservent les haltes de l'axe, donc
        sur des omnibus ; elle n'a pas à s'imposer à un train qui les traverse.
        """
        profile = self.profile
        if branch is not None and branch.line_speed_kmh is not None:
            profile = replace(profile, line_speed_kmh=branch.line_speed_kmh)
        if category is not None:
            profile = replace(
                profile,
                accel_ms2=category.accel_ms2 or profile.accel_ms2,
                decel_ms2=category.decel_ms2 or profile.decel_ms2,
                line_speed_kmh=category.line_speed_kmh or profile.line_speed_kmh,
            )
        return profile

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
    category_id: str | None = None
    """Catégorie de matériel retenue, si elle a pu être reconnue."""
    lead_margin_s: float = 0.0
    """Marge d'anticipation appliquée à l'heure annoncée."""

    @property
    def announce_at(self) -> datetime:
        """Heure à annoncer : le plus tôt où le train peut se présenter.

        L'erreur d'annonce n'est pas symétrique. Annoncer trop tôt coûte
        quelques secondes d'attente ; annoncer trop tard fait manquer le
        passage, et c'est irrattrapable. On annonce donc la borne basse de la
        fenêtre, diminuée de la marge d'anticipation.
        """
        return self.when - timedelta(seconds=self.uncertainty_s + self.lead_margin_s)

    @property
    def is_realtime(self) -> bool:
        return self.delay_s is not None

    @property
    def window(self) -> tuple[datetime, datetime]:
        """Fenêtre de passage plausible."""
        margin = timedelta(seconds=self.uncertainty_s)
        return (self.when - margin, self.when + margin)

    @property
    def margin_marker(self) -> str:
        """« ± » sur une distance mesurée, « ±~ » sur une distance estimée.

        L'incertitude reste juste dans les deux cas, mais sa nature diffère :
        le tilde dit que `tracks` peut encore resserrer la fenêtre.
        """
        return "±" if self.branch.is_distance_measured else "±~"

    def describe(self) -> str:
        arrow = "→" if self.direction is Direction.OUTBOUND else "←"
        stamp = self.when.strftime("%H:%M:%S")
        flag = "TR" if self.is_realtime else "th"
        delay = ""
        if self.delay_s:
            delay = f" ({self.delay_s // 60:+d} min)"
        category = f" [{self.category_id}]" if self.category_id else ""
        return (
            f"{stamp} {self.margin_marker}{self.uncertainty_s:.0f}s [{flag}] {arrow} "
            f"{self.branch.label} · {self.route_label} {self.headsign}{category}{delay} "
            f"· {self.speed_kmh:.0f} km/h"
        )

    def describe_watch(self) -> str:
        """Formulation orientée guet : à partir de quand se tenir prêt."""
        arrow = "→" if self.direction is Direction.OUTBOUND else "←"
        category = f" [{self.category_id}]" if self.category_id else ""
        return (
            f"guetter dès {self.announce_at.strftime('%H:%M:%S')} · "
            f"passage vers {self.when.strftime('%H:%M:%S')} "
            f"({self.margin_marker}{self.uncertainty_s:.0f} s) "
            f"{arrow} {self.branch.label} · {self.route_label} {self.headsign}{category}"
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


def _distance_to_observer_m(site: Site, branch: Branch, anchor: LatLon) -> tuple[float, float]:
    """Distance gare d'appui -> point, et la marge à lui associer.

    Une distance mesurée sur OSM et une distance déduite du vol d'oiseau ne
    valent pas la même chose : renvoyer la marge avec la valeur évite de
    présenter la seconde avec la précision de la première.
    """
    if branch.track_distance_m is not None:
        return branch.track_distance_m, MEASURED_DISTANCE_TOLERANCE_M
    # Repli sans géométrie OSM : distance à vol d'oiseau corrigée de la sinuosité.
    estimated = haversine_m(anchor, site.position) * site.profile.sinuosity
    return estimated, estimated * ESTIMATED_DISTANCE_REL_TOLERANCE


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
        category = site.category_for(trip_descriptor(trip, route))
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

            profile = site.profile_for(branch, category)
            distance, distance_tolerance = _distance_to_observer_m(site, branch, anchor)
            travel = travel_time_s(distance, regime, profile)
            uncertainty = travel_time_uncertainty_s(
                distance, regime, profile, distance_uncertainty_m=distance_tolerance
            )
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
                    category_id=category.category_id if category else None,
                    lead_margin_s=site.lead_margin_s,
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
