"""Rejeu d'un enregistrement de niveaux, pour tester et calibrer hors ligne."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator


def read_levels(path: Path | str) -> Iterator[tuple[datetime, float]]:
    """Lit un CSV `timestamp,level` (timestamp ISO 8601)."""
    with Path(path).open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            yield datetime.fromisoformat(row["timestamp"]), float(row["level"])


def write_levels(path: Path | str, samples) -> None:
    """Écrit une série de mesures, pour rejeu ultérieur."""
    with Path(path).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["timestamp", "level"])
        for when, level in samples:
            writer.writerow([when.isoformat(), f"{level:.6g}"])


def synthetic_passage(
    start: datetime,
    duration_s: float,
    period_s: float,
    baseline: float,
    peak: float,
    total_s: float,
) -> list[tuple[datetime, float]]:
    """Série de test : un fond constant et une bosse triangulaire.

    Utile pour vérifier le détecteur sans matériel.
    """
    samples: list[tuple[datetime, float]] = []
    steps = int(total_s / period_s)
    for index in range(steps):
        when = start + timedelta(seconds=index * period_s)
        offset = (when - start).total_seconds() - (total_s - duration_s) / 2
        if 0 <= offset <= duration_s:
            # Rampe montante puis descendante, pic au milieu.
            shape = 1.0 - abs(2 * offset / duration_s - 1.0)
            samples.append((when, baseline + shape * (peak - baseline)))
        else:
            samples.append((when, baseline))
    return samples
