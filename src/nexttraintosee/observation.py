"""Observations de passage : ce que quelqu'un — ou quelque chose — a vraiment vu.

Un capteur qui détecte un souffle et un utilisateur qui appuie sur un bouton
produisent la même donnée : un instant, et la certitude qu'un train est passé.
Les traiter comme une seule notion évite de dédoubler tout l'appariement et le
recalage, et permet de mélanger les deux sources dans une même analyse.

La distinction utile n'est pas l'origine mais la **nature** de l'observation :

* un passage vu, avec son heure ;
* un passage annoncé qui n'a **pas** eu lieu, information au moins aussi
  précieuse — elle révèle une suppression absente du temps réel, ou une
  circulation rattachée à la mauvaise branche.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


#: Sources dont l'heure rapportée n'est pas une mesure.
#:
#: La carte de confirmation différée de l'application demande « le train de
#: 17:45 est-il passé ? ». Une réponse affirmative enregistre l'heure *prédite*
#: comme heure observée : le résidu vaut zéro par construction, quelle que soit
#: la réalité. C'est une observation de présence, pas de temps — utile pour
#: mesurer la couverture, trompeuse pour recaler le modèle, qu'elle tire
#: silencieusement vers un biais nul.
NON_TIMING_SOURCES = frozenset({"app:confirmation"})


def times_the_passage(observation: "Observation") -> bool:
    """Vrai si l'heure rapportée par cette observation mesure quelque chose."""
    return observation.source not in NON_TIMING_SOURCES


class ObservationKind(str, Enum):
    """Ce que l'observateur rapporte."""

    SEEN = "seen"
    """Un train est passé, à l'heure indiquée."""
    NOT_SEEN = "not_seen"
    """Le passage annoncé n'a pas eu lieu dans la fenêtre surveillée."""


@dataclass(frozen=True)
class Observation:
    """Un passage rapporté, quelle qu'en soit la source.

    L'heure est celle du passage lui-même, pas celle du signalement : une
    application qui demande « le train est-il passé ? » deux minutes après doit
    envoyer l'heure du passage, jamais celle de la réponse.
    """

    observed_at: datetime | None
    """Instant du passage. Absent pour un passage annoncé qui n'a pas eu lieu."""
    kind: ObservationKind = ObservationKind.SEEN
    source: str = "manuel"
    """Origine : « capteur », « manuel », un identifiant d'utilisateur…"""
    trip_id: str | None = None
    """Circulation à laquelle l'observation se rattache, si elle est connue."""
    direction: str | None = None
    anchor_time: datetime | None = None
    precision_s: float = 5.0
    """Précision revendiquée. Un appui au passage vaut quelques secondes ;
    une heure saisie de mémoire, bien davantage."""
    note: str = ""

    def __post_init__(self) -> None:
        if self.kind is ObservationKind.SEEN and self.observed_at is None:
            raise ValueError("une observation de passage doit porter une heure")
        if self.precision_s < 0:
            raise ValueError("la précision ne peut pas être négative")

    @property
    def midpoint(self) -> datetime:
        """Instant représentatif, pour l'appariement.

        Même nom que sur une détection de capteur, afin que l'appariement
        traite les deux sans les distinguer.
        """
        if self.observed_at is None:
            raise ValueError("un passage non vu n'a pas d'instant")
        return self.observed_at

    @property
    def is_binding(self) -> bool:
        """Vrai si l'observation désigne explicitement une circulation.

        Une observation liée se passe d'appariement : on sait déjà quel train
        était visé, ce qui écarte le risque de l'attribuer au voisin — sur une
        ligne où il passe un train toutes les trois minutes, ce risque est réel.
        """
        return self.trip_id is not None and self.anchor_time is not None

    def describe(self) -> str:
        if self.kind is ObservationKind.NOT_SEEN:
            return f"non passé · {self.source}"
        assert self.observed_at is not None
        return (
            f"{self.observed_at.strftime('%H:%M:%S')} ±{self.precision_s:.0f}s · {self.source}"
        )


def from_detection(detection, source: str = "capteur") -> Observation:
    """Convertit une détection de capteur en observation.

    La précision retenue est la moitié de la durée du passage : le pic se situe
    quelque part dans le convoi, et un train long laisse davantage de latitude
    qu'un autocar de deux caisses.
    """
    return Observation(
        observed_at=detection.midpoint,
        kind=ObservationKind.SEEN,
        source=source,
        precision_s=max(1.0, detection.duration_s / 2),
    )
