"""Session d'écoute : consommer un flux de mesures pendant une durée bornée.

Séparé du détecteur pour rester testable : la durée et la cadence des points
d'étape se mesurent sur l'horodatage des échantillons, pas sur l'horloge
murale. Une session se rejoue donc à l'identique depuis un enregistrement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Iterable

from .base import Detection, PassageDetector, to_db


@dataclass
class Status:
    """Point d'étape périodique, pour voir ce que le capteur entend."""

    at: datetime
    baseline_db: float
    peak_db: float
    """Niveau le plus fort depuis le point d'étape précédent."""
    detections: int

    @property
    def headroom_db(self) -> float:
        """De combien le pic dépasse le bruit de fond.

        C'est le chiffre qui dit si un passage se distingue : s'il ne monte
        jamais au-dessus du seuil de déclenchement, le capteur est mal placé ou
        le seuil est trop haut.
        """
        return self.peak_db - self.baseline_db


@dataclass
class SessionStats:
    """Bilan d'une session d'écoute."""

    samples: int = 0
    detections: list[Detection] = field(default_factory=list)
    started_at: datetime | None = None
    ended_at: datetime | None = None

    @property
    def duration_s(self) -> float:
        if self.started_at is None or self.ended_at is None:
            return 0.0
        return (self.ended_at - self.started_at).total_seconds()

    def describe(self) -> str:
        minutes = self.duration_s / 60
        return (
            f"{len(self.detections)} passage(s) détecté(s) en {minutes:.0f} min "
            f"({self.samples} mesures)"
        )


def listen_session(
    detector: PassageDetector,
    samples: Iterable[tuple[datetime, float]],
    duration_s: float | None = None,
    on_detection: Callable[[Detection], None] | None = None,
    on_status: Callable[[Status], None] | None = None,
    status_every_s: float = 60.0,
) -> SessionStats:
    """Fait passer un flux de mesures dans le détecteur.

    Args:
        detector: détecteur de passage, dont l'état est conservé d'un appel à
            l'autre.
        samples: couples (instant, niveau).
        duration_s: durée maximale, mesurée sur les horodatages des
            échantillons. `None` pour aller jusqu'au bout du flux.
        on_detection: appelé à chaque passage détecté.
        on_status: appelé périodiquement avec un point d'étape.
        status_every_s: intervalle entre deux points d'étape.

    Returns:
        Le bilan de la session, l'éventuel passage en cours étant refermé.
    """
    stats = SessionStats()
    last_status: datetime | None = None
    peak_db = float("-inf")

    for when, level in samples:
        if stats.started_at is None:
            stats.started_at = when
            last_status = when
        stats.samples += 1
        stats.ended_at = when

        detection = detector.push(when, level)
        if detection is not None:
            stats.detections.append(detection)
            if on_detection is not None:
                on_detection(detection)

        peak_db = max(peak_db, to_db(level))

        if (
            on_status is not None
            and last_status is not None
            and (when - last_status).total_seconds() >= status_every_s
        ):
            on_status(
                Status(
                    at=when,
                    baseline_db=detector.baseline_db if detector.baseline_db is not None else 0.0,
                    peak_db=peak_db,
                    detections=len(stats.detections),
                )
            )
            last_status = when
            peak_db = float("-inf")

        if (
            duration_s is not None
            and stats.started_at is not None
            and (when - stats.started_at).total_seconds() >= duration_s
        ):
            break

    leftover = detector.flush(stats.ended_at)
    if leftover is not None:
        stats.detections.append(leftover)
        if on_detection is not None:
            on_detection(leftover)

    return stats
