"""Export statique des passages prédits, pour une PWA publiée sans serveur.

La PWA n'a plus de serveur à interroger : elle est publiée sur GitHub Pages,
et un fichier JSON régénéré chaque nuit lui tient lieu d'API. Ce qui change en
direct — le retard temps réel — est lu par le téléphone lui-même sur le flux
GTFS-RT de la SNCF ; tout le reste est figé ici, à l'avance.

C'est possible parce que la prédiction se décompose exactement en deux
termes (voir `predict_passages`) :

    passage = horaire en gare d'appui + retard temps réel ± temps de parcours

Le temps de parcours ne dépend que de la branche, du régime et du matériel,
jamais du retard. On exporte donc pour chaque passage l'horaire en gare et le
décalage signé jusqu'au point d'observation ; le client n'a plus qu'à y
ajouter le retard. Le modèle de marche reste écrit une seule fois, en Python.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from .gtfs import GtfsFeed
from .predict import Passage, Site, predict_passages
from .service import _DIRECTION_LABELS, _LOOK, histogram_payload

#: Version du format, à incrémenter à tout changement incompatible : le client
#: refuse un fichier qu'il ne sait pas lire plutôt que d'afficher n'importe quoi.
EXPORT_FORMAT = 1

#: Jours exportés à partir du jour de génération. L'export est régénéré chaque
#: nuit : une semaine de marge laisse l'application utilisable même si la
#: publication échoue plusieurs nuits de suite.
DEFAULT_EXPORT_DAYS = 8


def _passage_entry(passage: Passage) -> dict:
    """Un passage, sans retard : tout ce que le client ne peut pas recalculer."""
    return {
        "trip_id": passage.trip_id,
        # Secondes Unix : le client compare des instants, jamais des heures
        # locales, ce qui le dispense de toute gestion de fuseau.
        "anchor": int(passage.anchor_time.timestamp()),
        "offset_s": round((passage.when - passage.anchor_time).total_seconds(), 1),
        "uncertainty_s": round(passage.uncertainty_s, 1),
        "lead_margin_s": passage.lead_margin_s,
        "direction": passage.direction.value,
        "direction_label": _DIRECTION_LABELS[passage.direction],
        "look": _LOOK[passage.direction],
        "branch_id": passage.branch.branch_id,
        "branch_label": passage.branch.label,
        "category_id": passage.category_id,
        "headsign": passage.headsign,
        "route_label": passage.route_label,
        "speed_kmh": round(passage.speed_kmh, 1),
    }


def build_timetable(
    feed: GtfsFeed,
    site: Site,
    start_day: date,
    *,
    days: int = DEFAULT_EXPORT_DAYS,
    generated_at: datetime,
) -> dict:
    """Passages théoriques de `start_day` sur `days` jours, et histogramme.

    La veille est incluse : un train de 00h20 appartient à la journée de
    service précédente (voir `service_days_around`).
    """
    passages: list[Passage] = []
    for offset in range(-1, days):
        passages.extend(predict_passages(feed, site, start_day + timedelta(days=offset)))
    passages.sort(key=lambda p: p.when)

    window = feed.calendar.coverage()
    return {
        "format": EXPORT_FORMAT,
        "generated_at": generated_at.isoformat(),
        "site": site.name,
        "first_day": start_day.isoformat(),
        "last_day": (start_day + timedelta(days=days - 1)).isoformat(),
        "feed_end": window[1].isoformat() if window else None,
        # Le retard d'une circulation se lit au droit de la gare d'appui, sur
        # l'un quelconque de ses quais.
        "anchor_stop_ids": sorted(feed.station_stop_ids(site.anchor_station)),
        "histogram": histogram_payload(feed, site, start_day),
        "passages": [_passage_entry(p) for p in passages],
    }
