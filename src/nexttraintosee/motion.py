"""Modèle de marche : combien de temps entre la gare et le point d'observation ?

Le GTFS ne donne des horaires qu'aux gares. Pour dater un passage en pleine
voie, il faut modéliser la marche du train entre la gare d'appui (« ancre ») et
le point d'observation. On utilise un profil trapézoïdal classique
(accélération constante -> palier à la vitesse limite -> freinage constant),
qui suffit largement sur les 1 à 3 km qui séparent typiquement un point
d'observation urbain de sa gare.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from enum import Enum


class Regime(str, Enum):
    """Comment le train aborde le segment gare <-> point d'observation."""

    DEPARTING = "departing"
    """Le train quitte la gare d'appui et accélère vers le point."""
    ARRIVING = "arriving"
    """Le train vient du point et freine pour s'arrêter en gare d'appui."""
    THROUGH = "through"
    """Le train ne s'arrête pas à la gare d'appui : vitesse de ligne constante."""


@dataclass(frozen=True)
class TractionProfile:
    """Paramètres de marche d'un matériel sur un tronçon donné.

    Les valeurs par défaut correspondent à un automoteur régional en zone
    urbaine ; elles sont volontairement conservatrices et destinées à être
    recalées sur des passages réellement observés (cf. `calibration`).
    """

    accel_ms2: float = 0.5
    decel_ms2: float = 0.6
    line_speed_kmh: float = 90.0
    #: Rapport longueur réelle de la voie / distance à vol d'oiseau, quand on ne
    #: dispose pas de la géométrie OSM pour mesurer la distance curviligne.
    sinuosity: float = 1.05

    def __post_init__(self) -> None:
        if self.accel_ms2 <= 0 or self.decel_ms2 <= 0:
            raise ValueError("les accélérations doivent être strictement positives")
        if self.line_speed_kmh <= 0:
            raise ValueError("line_speed_kmh doit être strictement positive")
        if self.sinuosity < 1.0:
            raise ValueError("la sinuosité ne peut pas être inférieure à 1")

    @property
    def line_speed_ms(self) -> float:
        return self.line_speed_kmh / 3.6


def _ramp_time_s(distance_m: float, accel_ms2: float, cruise_ms: float) -> float:
    """Durée pour parcourir `distance_m` en partant (ou finissant) à l'arrêt.

    Phase de rampe à `accel_ms2` jusqu'à `cruise_ms`, puis palier. Le cas
    « arrivée » est le symétrique temporel du cas « départ », d'où la fonction
    unique.
    """
    if distance_m < 0:
        raise ValueError("la distance doit être positive")
    if distance_m == 0:
        return 0.0
    ramp_distance = cruise_ms**2 / (2 * accel_ms2)
    if distance_m <= ramp_distance:
        return math.sqrt(2 * distance_m / accel_ms2)
    return cruise_ms / accel_ms2 + (distance_m - ramp_distance) / cruise_ms


def travel_time_s(distance_m: float, regime: Regime, profile: TractionProfile) -> float:
    """Temps de parcours entre la gare d'appui et le point d'observation."""
    if regime is Regime.DEPARTING:
        return _ramp_time_s(distance_m, profile.accel_ms2, profile.line_speed_ms)
    if regime is Regime.ARRIVING:
        return _ramp_time_s(distance_m, profile.decel_ms2, profile.line_speed_ms)
    if regime is Regime.THROUGH:
        return distance_m / profile.line_speed_ms
    raise ValueError(f"régime inconnu : {regime!r}")


def speed_at_point_ms(distance_m: float, regime: Regime, profile: TractionProfile) -> float:
    """Vitesse instantanée du train au droit du point d'observation.

    Sert à estimer la signature d'un passage pour le capteur (durée du
    « souffle », amplitude) et à borner l'incertitude temporelle.
    """
    if regime is Regime.THROUGH:
        return profile.line_speed_ms
    accel = profile.accel_ms2 if regime is Regime.DEPARTING else profile.decel_ms2
    reached = math.sqrt(2 * accel * distance_m)
    return min(reached, profile.line_speed_ms)


@dataclass(frozen=True)
class SegmentProfile:
    """Découpage d'un parcours d'arrêt à arrêt en trois phases.

    Un train qui relie deux gares accélère, roule au palier si la distance le
    permet, puis freine. Quand elle ne le permet pas, le palier disparaît et le
    profil devient triangulaire, avec une vitesse de crête inférieure à la
    vitesse de ligne.
    """

    length_m: float
    peak_speed_ms: float
    accel_distance_m: float
    cruise_distance_m: float
    accel_time_s: float
    cruise_time_s: float
    decel_time_s: float

    @property
    def total_time_s(self) -> float:
        return self.accel_time_s + self.cruise_time_s + self.decel_time_s

    @property
    def is_triangular(self) -> bool:
        """Vrai quand le segment est trop court pour atteindre la vitesse de ligne."""
        return self.cruise_distance_m <= 0.0


def segment_profile(distance_m: float, profile: TractionProfile) -> SegmentProfile:
    """Décompose un parcours d'arrêt à arrêt.

    Raises:
        ValueError: si la distance est négative.
    """
    if distance_m < 0:
        raise ValueError("la distance doit être positive")

    accel, decel = profile.accel_ms2, profile.decel_ms2
    cruise = profile.line_speed_ms
    accel_distance = cruise**2 / (2 * accel)
    decel_distance = cruise**2 / (2 * decel)

    if accel_distance + decel_distance <= distance_m:
        cruise_distance = distance_m - accel_distance - decel_distance
        return SegmentProfile(
            length_m=distance_m,
            peak_speed_ms=cruise,
            accel_distance_m=accel_distance,
            cruise_distance_m=cruise_distance,
            accel_time_s=cruise / accel,
            cruise_time_s=cruise_distance / cruise,
            decel_time_s=cruise / decel,
        )

    # Trop court pour le palier : profil triangulaire. La vitesse de crête est
    # celle où les distances d'accélération et de freinage épuisent le segment.
    peak = math.sqrt(2 * distance_m * accel * decel / (accel + decel)) if distance_m > 0 else 0.0
    return SegmentProfile(
        length_m=distance_m,
        peak_speed_ms=peak,
        accel_distance_m=peak**2 / (2 * accel) if peak else 0.0,
        cruise_distance_m=0.0,
        accel_time_s=peak / accel if peak else 0.0,
        cruise_time_s=0.0,
        decel_time_s=peak / decel if peak else 0.0,
    )


def segment_time_s(distance_m: float, profile: TractionProfile) -> float:
    """Durée d'un parcours d'arrêt à arrêt."""
    return segment_profile(distance_m, profile).total_time_s


def segment_progress_s(
    covered_m: float, distance_m: float, profile: TractionProfile
) -> float:
    """Temps écoulé après avoir parcouru `covered_m` d'un segment de `distance_m`.

    Raises:
        ValueError: si `covered_m` sort du segment.
    """
    if not 0.0 <= covered_m <= distance_m:
        raise ValueError(
            f"{covered_m:.0f} m est hors du segment de {distance_m:.0f} m"
        )
    shape = segment_profile(distance_m, profile)
    if shape.total_time_s == 0.0:
        return 0.0

    if covered_m <= shape.accel_distance_m:
        return math.sqrt(2 * covered_m / profile.accel_ms2)
    if covered_m <= shape.accel_distance_m + shape.cruise_distance_m:
        return shape.accel_time_s + (covered_m - shape.accel_distance_m) / shape.peak_speed_ms
    # Phase de freinage : on la parcourt à rebours depuis l'arrêt final.
    remaining = distance_m - covered_m
    return shape.total_time_s - math.sqrt(2 * remaining / profile.decel_ms2)


def segment_fraction(covered_m: float, distance_m: float, profile: TractionProfile) -> float:
    """Part du temps de parcours écoulée à `covered_m` du départ, dans [0, 1].

    C'est la grandeur utile pour dater un passage en pleine voie quand on
    connaît les horaires aux deux extrémités du segment : le modèle de marche ne
    sert plus qu'à répartir une durée réelle, jamais à la prédire. Une erreur
    sur l'accélération ne déplace donc plus le passage que de quelques secondes,
    au lieu de décaler tout le calcul.
    """
    total = segment_time_s(distance_m, profile)
    if total <= 0:
        return 0.0
    return segment_progress_s(covered_m, distance_m, profile) / total


def travel_time_uncertainty_s(
    distance_m: float,
    regime: Regime,
    profile: TractionProfile,
    accel_rel_tol: float = 0.30,
    speed_rel_tol: float = 0.20,
    distance_uncertainty_m: float = 0.0,
) -> float:
    """Demi-largeur de l'incertitude sur le temps de parcours.

    On combine les sources d'erreur en prenant les cas extrêmes : le train le
    plus vif sur la distance la plus courte, le plus mou sur la plus longue.
    C'est grossier mais honnête, et surtout cela ne présente pas une distance
    seulement estimée avec la précision d'une distance mesurée.

    Args:
        distance_uncertainty_m: marge sur la distance gare <-> point. Quelques
            mètres quand la géométrie vient d'OpenStreetMap, plusieurs dizaines
            quand elle est déduite du vol d'oiseau.
    """
    fast = replace(
        profile,
        accel_ms2=profile.accel_ms2 * (1 + accel_rel_tol),
        decel_ms2=profile.decel_ms2 * (1 + accel_rel_tol),
        line_speed_kmh=profile.line_speed_kmh * (1 + speed_rel_tol),
    )
    slow = replace(
        profile,
        accel_ms2=profile.accel_ms2 * (1 - accel_rel_tol),
        decel_ms2=profile.decel_ms2 * (1 - accel_rel_tol),
        line_speed_kmh=profile.line_speed_kmh * (1 - speed_rel_tol),
    )
    shortest = max(0.0, distance_m - distance_uncertainty_m)
    longest = distance_m + distance_uncertainty_m
    return abs(
        travel_time_s(longest, regime, slow) - travel_time_s(shortest, regime, fast)
    ) / 2
