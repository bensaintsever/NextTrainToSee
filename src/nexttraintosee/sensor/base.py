"""Détection de passage à partir d'un signal scalaire.

Le principe est indépendant du capteur : qu'on mesure un niveau sonore, une
amplitude de vibration ou une variation de champ magnétique, un train qui passe
produit la même forme — une bosse nettement au-dessus du bruit de fond, qui dure
quelques secondes à quelques dizaines de secondes.

Le détecteur ci-dessous suit un fond adaptatif et déclenche sur dépassement,
avec hystérésis (pour ne pas hacher un passage en plusieurs événements) et
période réfractaire (pour ne pas compter deux fois le même train).

Il ne dépend d'aucune bibliothèque : on peut le nourrir avec un flux audio réel
comme avec une série rejouée depuis un fichier, ce qui le rend testable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Iterator

#: Niveau plancher, pour éviter log(0) sur un signal nul.
EPSILON = 1e-12


def to_db(level: float) -> float:
    """Convertit une amplitude linéaire en décibels relatifs."""
    return 20.0 * math.log10(max(abs(level), EPSILON))


@dataclass(frozen=True)
class Detection:
    """Un passage détecté par le capteur."""

    started_at: datetime
    peak_at: datetime
    ended_at: datetime
    peak_db: float
    baseline_db: float
    sample_count: int

    @property
    def duration_s(self) -> float:
        return (self.ended_at - self.started_at).total_seconds()

    @property
    def prominence_db(self) -> float:
        """De combien le passage dépasse le bruit de fond."""
        return self.peak_db - self.baseline_db

    @property
    def midpoint(self) -> datetime:
        """Instant représentatif du passage.

        On retient le pic plutôt que le milieu : c'est le moment où le train est
        au plus près du capteur, donc celui qui se compare à une heure de
        passage prédite.
        """
        return self.peak_at

    def describe(self) -> str:
        return (
            f"{self.peak_at.strftime('%H:%M:%S')} · {self.duration_s:.0f}s "
            f"· +{self.prominence_db:.1f} dB au-dessus du fond"
        )


class PassageDetector:
    """Détecteur à seuil adaptatif et hystérésis.

    Args:
        trigger_db: dépassement du fond nécessaire pour ouvrir un événement.
        release_db: dépassement en dessous duquel l'événement se referme.
            Doit être inférieur à `trigger_db` (c'est l'hystérésis).
        min_duration_s: en dessous, on considère que c'est un bruit parasite
            (portière, klaxon) et non un train.
        max_duration_s: au-dessus, ce n'est plus un passage (travaux, pluie) ;
            l'événement est refermé et rejeté.
        refractory_s: délai minimal entre deux détections.
        baseline_half_life_s: inertie du fond sonore. Le fond n'est mis à jour
            qu'en dehors des événements, sinon un train long le tirerait vers le
            haut et se couperait lui-même.
    """

    def __init__(
        self,
        trigger_db: float = 9.0,
        release_db: float = 4.0,
        min_duration_s: float = 3.0,
        max_duration_s: float = 120.0,
        refractory_s: float = 15.0,
        baseline_half_life_s: float = 90.0,
    ) -> None:
        if release_db >= trigger_db:
            raise ValueError("release_db doit être strictement inférieur à trigger_db")
        if min_duration_s <= 0 or max_duration_s <= min_duration_s:
            raise ValueError("durées incohérentes : 0 < min_duration_s < max_duration_s")
        self.trigger_db = trigger_db
        self.release_db = release_db
        self.min_duration_s = min_duration_s
        self.max_duration_s = max_duration_s
        self.refractory_s = refractory_s
        self.baseline_half_life_s = baseline_half_life_s

        self.baseline_db: float | None = None
        self._last_sample_at: datetime | None = None
        self._last_detection_at: datetime | None = None
        self._event: dict | None = None

    @property
    def in_event(self) -> bool:
        return self._event is not None

    def _update_baseline(self, when: datetime, level_db: float) -> None:
        """Moyenne mobile exponentielle, pondérée par l'intervalle réel."""
        if self.baseline_db is None or self._last_sample_at is None:
            self.baseline_db = level_db
            return
        dt = max(0.0, (when - self._last_sample_at).total_seconds())
        # alpha dérivé de la demi-vie : le poids du passé décroît de moitié
        # tous les baseline_half_life_s, indépendamment de la cadence des mesures.
        alpha = 1.0 - 0.5 ** (dt / self.baseline_half_life_s) if dt > 0 else 0.0
        self.baseline_db += alpha * (level_db - self.baseline_db)

    def push(self, when: datetime, level: float) -> Detection | None:
        """Ingère une mesure ; renvoie une détection quand un passage se termine."""
        level_db = to_db(level)
        detection: Detection | None = None

        if self._event is None:
            self._update_baseline(when, level_db)
            assert self.baseline_db is not None
            ready = (
                self._last_detection_at is None
                or (when - self._last_detection_at).total_seconds() >= self.refractory_s
            )
            if ready and level_db >= self.baseline_db + self.trigger_db:
                self._event = {
                    "started_at": when,
                    "peak_at": when,
                    "peak_db": level_db,
                    "baseline_db": self.baseline_db,
                    "samples": 1,
                }
        else:
            event = self._event
            event["samples"] += 1
            if level_db > event["peak_db"]:
                event["peak_db"] = level_db
                event["peak_at"] = when

            duration = (when - event["started_at"]).total_seconds()
            timed_out = duration >= self.max_duration_s
            below = level_db < event["baseline_db"] + self.release_db
            if below or timed_out:
                self._event = None
                if timed_out:
                    # Un niveau qui reste haut aussi longtemps n'est pas un train
                    # mais un bruit installé (travaux, averse, chantier) : on
                    # l'adopte comme nouveau fond pour cesser de se déclencher.
                    self.baseline_db = level_db
                    self._last_detection_at = when
                elif duration >= self.min_duration_s:
                    detection = Detection(
                        started_at=event["started_at"],
                        peak_at=event["peak_at"],
                        ended_at=when,
                        peak_db=event["peak_db"],
                        baseline_db=event["baseline_db"],
                        sample_count=event["samples"],
                    )
                    self._last_detection_at = when

        self._last_sample_at = when
        return detection

    def feed(self, samples: Iterable[tuple[datetime, float]]) -> Iterator[Detection]:
        """Consomme une série de mesures et émet les détections rencontrées."""
        for when, level in samples:
            detection = self.push(when, level)
            if detection is not None:
                yield detection

    def flush(self, when: datetime | None = None) -> Detection | None:
        """Referme un événement resté ouvert en fin de flux."""
        if self._event is None:
            return None
        event = self._event
        end = when or event["peak_at"]
        self._event = None
        duration = (end - event["started_at"]).total_seconds()
        if not self.min_duration_s <= duration <= self.max_duration_s:
            return None
        self._last_detection_at = end
        return Detection(
            started_at=event["started_at"],
            peak_at=event["peak_at"],
            ended_at=end,
            peak_db=event["peak_db"],
            baseline_db=event["baseline_db"],
            sample_count=event["samples"],
        )
