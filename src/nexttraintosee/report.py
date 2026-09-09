"""Mise en forme des passages : répartition horaire.

Sert à dimensionner un usage plutôt qu'à prédire : savoir qu'il passe dix-huit
trains entre 7 h et 8 h et deux entre 22 h et 23 h dit tout de suite si guetter
en vaut la peine, et à quelle heure.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Sequence

#: Noms des jours, écrits en dur plutôt que tirés de la locale du système :
#: une sortie ne devrait pas changer de langue selon la machine.
WEEKDAYS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")


def french_date(day: date) -> str:
    """Date en toutes lettres, indépendamment de la locale."""
    return f"{WEEKDAYS[day.weekday()]} {day.strftime('%d/%m/%Y')}"

#: Plage par défaut : la nuit ferroviaire ne porte aucune circulation voyageurs,
#: l'afficher n'apporterait que des lignes vides.
DEFAULT_FIRST_HOUR = 5
DEFAULT_LAST_HOUR = 23


@dataclass
class HourBucket:
    """Ce qui passe pendant une heure donnée."""

    hour: int
    by_branch: Counter = field(default_factory=Counter)
    by_category: Counter = field(default_factory=Counter)

    @property
    def total(self) -> int:
        return sum(self.by_branch.values())

    @property
    def mean_interval_min(self) -> float | None:
        """Intervalle moyen entre deux passages dans l'heure."""
        return 60.0 / self.total if self.total else None


def hourly_histogram(
    passages: Sequence,
    first_hour: int = DEFAULT_FIRST_HOUR,
    last_hour: int = DEFAULT_LAST_HOUR,
) -> list[HourBucket]:
    """Répartit les passages par heure, sur une plage bornée.

    Args:
        passages: passages prédits.
        first_hour: première heure incluse.
        last_hour: première heure exclue.

    Raises:
        ValueError: si la plage est vide ou hors des vingt-quatre heures.
    """
    if not 0 <= first_hour < last_hour <= 24:
        raise ValueError("la plage horaire doit vérifier 0 <= début < fin <= 24")

    buckets = {hour: HourBucket(hour) for hour in range(first_hour, last_hour)}
    for passage in passages:
        bucket = buckets.get(passage.when.hour)
        if bucket is None:
            continue
        bucket.by_branch[passage.branch.branch_id] += 1
        bucket.by_category[passage.category_id or "non classé"] += 1
    return [buckets[hour] for hour in sorted(buckets)]


def render_histogram(buckets: Sequence[HourBucket], width: int = 36) -> str:
    """Trace l'histogramme en caractères, à largeur bornée."""
    if not buckets:
        return "Aucune heure dans la plage demandée."
    peak = max(b.total for b in buckets) or 1
    lines = []
    for bucket in buckets:
        bar = "█" * round(width * bucket.total / peak)
        interval = bucket.mean_interval_min
        gap = f"{interval:5.1f} min" if interval else "    —    "
        lines.append(f"  {bucket.hour:02d} h  {bucket.total:3d}  {gap}  {bar}")
    return "\n".join(lines)


def csv_rows(buckets: Sequence[HourBucket]) -> list[list[str]]:
    """Tableau exportable : une ligne par heure, une colonne par branche.

    Les colonnes sont déduites de l'ensemble des branches rencontrées, pour que
    le fichier reste lisible d'un jour à l'autre.
    """
    branches = sorted({name for b in buckets for name in b.by_branch})
    categories = sorted({name for b in buckets for name in b.by_category})
    header = ["heure", "total", "intervalle_moyen_min"]
    header += [f"branche_{name}" for name in branches]
    header += [f"categorie_{name}" for name in categories]

    rows = [header]
    for bucket in buckets:
        interval = bucket.mean_interval_min
        row = [
            f"{bucket.hour:02d}",
            str(bucket.total),
            f"{interval:.2f}" if interval else "",
        ]
        row += [str(bucket.by_branch.get(name, 0)) for name in branches]
        row += [str(bucket.by_category.get(name, 0)) for name in categories]
        rows.append(row)
    return rows
