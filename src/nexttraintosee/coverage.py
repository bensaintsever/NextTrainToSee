"""Mesurer ce que vaut une prédiction à l'avance.

Une application qui annonce « prochain train dans 7 minutes » pose deux
questions qu'aucune validation hors ligne ne tranche :

* **Le temps réel est-il là quand on en a besoin ?** Le flux GTFS-RT ne couvre
  que les circulations proches ; à une heure d'échéance, une prédiction n'est
  souvent que théorique.
* **L'heure annoncée tient-elle ?** Si l'estimation bouge de deux minutes entre
  T−30 et T−5, l'annonce précoce ne vaut rien.

Ce module répond aux deux à partir de l'historique des estimations, en les
classant par **échéance** : le temps restant, au moment du calcul, avant le
passage *tel qu'il est alors annoncé*. C'est bien cette grandeur-là qu'il faut
retenir, et non l'écart à l'heure finale : elle correspond au « dans X minutes »
que lit l'utilisateur, seule information dont il dispose sur le moment.

Une précaution de lecture : la dernière estimation sert de référence, ce qui
mesure la **stabilité** de la prédiction, pas sa justesse. Une prédiction stable
et fausse resterait indétectable ici ; seul un capteur la démasquerait.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime

#: Historique d'un passage : (instant du calcul, heure estimée, retard appliqué).
Estimate = tuple[datetime, datetime, "int | None"]
History = dict[tuple[str, str, str], list[Estimate]]


@dataclass(frozen=True)
class LeadBucket:
    """Une tranche d'échéance avant le passage."""

    label: str
    low_s: float
    high_s: float

    def contains(self, lead_s: float) -> bool:
        return self.low_s <= lead_s < self.high_s


DEFAULT_BUCKETS = (
    LeadBucket("moins de 10 min", 0.0, 600.0),
    LeadBucket("10 à 30 min", 600.0, 1800.0),
    LeadBucket("30 à 60 min", 1800.0, 3600.0),
    LeadBucket("plus d'une heure", 3600.0, float("inf")),
)


@dataclass
class BucketStats:
    """Ce que valent les prédictions à une échéance donnée."""

    bucket: LeadBucket
    sample_count: int = 0
    realtime_count: int = 0
    drifts_s: list[float] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.drifts_s is None:
            self.drifts_s = []

    @property
    def realtime_share(self) -> float:
        """Part des estimations appuyées sur le temps réel."""
        return self.realtime_count / self.sample_count if self.sample_count else 0.0

    @property
    def drift_median_s(self) -> float | None:
        """Écart absolu médian à l'estimation finale."""
        return statistics.median(abs(d) for d in self.drifts_s) if self.drifts_s else None

    @property
    def drift_worst_s(self) -> float | None:
        """Écart absolu au-delà duquel se trouve un dixième des estimations."""
        if not self.drifts_s:
            return None
        ordered = sorted(abs(d) for d in self.drifts_s)
        return ordered[min(len(ordered) - 1, int(0.9 * len(ordered)))]

    def too_late_share(self, tolerance_s: float = 30.0) -> float:
        """Part des estimations qui annonçaient le passage trop tard.

        C'est la seule erreur vraiment coûteuse pour qui veut voir passer le
        train : une annonce en avance se solde par quelques secondes d'attente,
        une annonce en retard par un passage manqué.
        """
        if not self.drifts_s:
            return 0.0
        return sum(1 for d in self.drifts_s if d > tolerance_s) / len(self.drifts_s)

    def too_late_worst_s(self) -> float:
        """Pire retard d'annonce constaté, en secondes."""
        return max((d for d in self.drifts_s), default=0.0)


def analyse(history: History, buckets: tuple[LeadBucket, ...] = DEFAULT_BUCKETS) -> list[BucketStats]:
    """Classe les estimations par échéance et mesure couverture et dérive.

    Args:
        history: estimations successives, groupées par passage.
        buckets: tranches d'échéance à examiner.

    Returns:
        Une statistique par tranche, y compris vide, pour que l'absence de
        données se voie.
    """
    stats = [BucketStats(bucket) for bucket in buckets]

    for estimates in history.values():
        if not estimates:
            continue
        ordered = sorted(estimates, key=lambda e: e[0])
        # La dernière estimation, la plus proche de l'événement, sert de repère.
        final_passage = ordered[-1][1]

        for computed_at, passes_at, delay_s in ordered:
            lead_s = (passes_at - computed_at).total_seconds()
            if lead_s < 0:
                continue
            for entry in stats:
                if entry.bucket.contains(lead_s):
                    entry.sample_count += 1
                    if delay_s is not None:
                        entry.realtime_count += 1
                    # Écart signé : positif quand l'estimation annonçait le
                    # passage *plus tard* qu'il n'a finalement lieu — le cas
                    # dangereux, celui qui fait arriver après le train.
                    entry.drifts_s.append((passes_at - final_passage).total_seconds())
                    break
    return stats


@dataclass
class DelaySummary:
    """Distribution des retards effectivement rencontrés."""

    passage_count: int
    with_realtime: int
    on_time: int
    """Passages dont le retard final est inférieur à une minute."""
    median_delay_s: float | None
    worst_delay_s: float | None

    @property
    def realtime_share(self) -> float:
        return self.with_realtime / self.passage_count if self.passage_count else 0.0

    @property
    def on_time_share(self) -> float:
        return self.on_time / self.with_realtime if self.with_realtime else 0.0


def summarise_delays(history: History, on_time_threshold_s: float = 60.0) -> DelaySummary:
    """Résume les retards constatés sur la dernière estimation de chaque passage."""
    final_delays: list[int] = []
    for estimates in history.values():
        if not estimates:
            continue
        delay = sorted(estimates, key=lambda e: e[0])[-1][2]
        if delay is not None:
            final_delays.append(delay)

    return DelaySummary(
        passage_count=len(history),
        with_realtime=len(final_delays),
        on_time=sum(1 for d in final_delays if abs(d) < on_time_threshold_s),
        median_delay_s=statistics.median(final_delays) if final_delays else None,
        worst_delay_s=max(final_delays, key=abs) if final_delays else None,
    )
