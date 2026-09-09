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


def travel_time_uncertainty_s(
    distance_m: float,
    regime: Regime,
    profile: TractionProfile,
    accel_rel_tol: float = 0.30,
    speed_rel_tol: float = 0.20,
) -> float:
    """Demi-largeur de l'incertitude sur le temps de parcours.

    On fait varier accélération et vitesse limite dans leurs tolérances et on
    prend la demi-amplitude des temps extrêmes. C'est grossier mais honnête :
    l'erreur dominante à 1-2 km d'une gare vient de la marche du train, pas de
    la géométrie.
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
    return abs(
        travel_time_s(distance_m, regime, slow) - travel_time_s(distance_m, regime, fast)
    ) / 2
