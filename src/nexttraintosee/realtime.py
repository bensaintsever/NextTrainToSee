"""Temps réel GTFS-RT : retards et suppressions.

La SNCF publie ses `TripUpdates` en GTFS-RT via transport.data.gouv.fr, en
accès libre et sans clé. Le flux est rafraîchi toutes les deux minutes environ
et ne couvre que les circulations proches (horizon de l'ordre de l'heure).

Le décodage protobuf est isolé dans `parse_feed_message` : tout le reste du
module travaille sur des structures Python simples, testables sans dépendance.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Iterable

log = logging.getLogger(__name__)

#: Flux SNCF « TripUpdates », relayé par transport.data.gouv.fr (sans clé).
SNCF_TRIP_UPDATES_URL = "https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-trip-updates"
#: Flux « ServiceAlerts » (perturbations), même origine.
SNCF_SERVICE_ALERTS_URL = "https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-service-alerts"

SCHEDULED = "SCHEDULED"
SKIPPED = "SKIPPED"
CANCELED = "CANCELED"
ADDED = "ADDED"


class RealtimeError(RuntimeError):
    """Flux temps réel injoignable, illisible, ou dépendance manquante."""


@dataclass(frozen=True)
class StopUpdate:
    """Mise à jour temps réel pour un arrêt d'une circulation."""

    stop_id: str
    stop_sequence: int | None = None
    arrival_delay_s: int | None = None
    departure_delay_s: int | None = None
    schedule_relationship: str = SCHEDULED

    @property
    def is_skipped(self) -> bool:
        return self.schedule_relationship == SKIPPED


@dataclass(frozen=True)
class TripUpdate:
    """État temps réel d'une circulation."""

    trip_id: str
    stop_updates: tuple[StopUpdate, ...] = ()
    schedule_relationship: str = SCHEDULED
    timestamp: int | None = None

    @property
    def is_canceled(self) -> bool:
        return self.schedule_relationship == CANCELED


@dataclass
class RealtimeSnapshot:
    """Photographie du temps réel à un instant donné."""

    updates: dict[str, TripUpdate] = field(default_factory=dict)
    timestamp: int | None = None

    def delays_at(self, stop_ids: Iterable[str], prefer_departure: bool = True) -> dict[str, int]:
        """Retard, par circulation, au droit d'un ensemble d'arrêts.

        C'est le retard *à la gare d'appui* qui nous intéresse — pas le retard
        au terminus — puisque c'est de cet horaire-là qu'on déduit l'heure de
        passage devant le point d'observation.
        """
        wanted = set(stop_ids)
        delays: dict[str, int] = {}
        for trip_id, update in self.updates.items():
            delay = resolve_delay(update, wanted, prefer_departure=prefer_departure)
            if delay is not None:
                delays[trip_id] = delay
        return delays

    def canceled_trip_ids(self, stop_ids: Iterable[str] | None = None) -> set[str]:
        """Circulations supprimées, en totalité ou à l'arrêt considéré."""
        wanted = set(stop_ids or ())
        canceled = set()
        for trip_id, update in self.updates.items():
            if update.is_canceled:
                canceled.add(trip_id)
            elif wanted and any(su.is_skipped and su.stop_id in wanted for su in update.stop_updates):
                canceled.add(trip_id)
        return canceled


def resolve_delay(
    update: TripUpdate, stop_ids: set[str], prefer_departure: bool = True
) -> int | None:
    """Retard applicable à un arrêt donné d'une circulation.

    GTFS-RT n'oblige pas à publier une mise à jour pour chaque arrêt : la
    spécification prévoit qu'un retard se propage aux arrêts suivants jusqu'à la
    prochaine mise à jour. On applique cette règle, en préférant le retard au
    départ (celui qui gouverne la sortie de gare) au retard à l'arrivée.
    """
    propagated: int | None = None
    for stop_update in update.stop_updates:
        candidates = (
            (stop_update.departure_delay_s, stop_update.arrival_delay_s)
            if prefer_departure
            else (stop_update.arrival_delay_s, stop_update.departure_delay_s)
        )
        current = next((c for c in candidates if c is not None), None)

        if stop_update.stop_id in stop_ids:
            return current if current is not None else propagated
        if current is not None:
            propagated = current
    return None


def parse_feed_message(payload: bytes) -> RealtimeSnapshot:
    """Décode un `FeedMessage` GTFS-RT.

    Raises:
        RealtimeError: si les bindings protobuf ne sont pas installés
            (`pip install "nexttraintosee[realtime]"`) ou si le flux est illisible.
    """
    try:
        from google.transit import gtfs_realtime_pb2
    except ImportError as exc:  # pragma: no cover - dépend de l'environnement
        raise RealtimeError(
            "décodage GTFS-RT indisponible : installez les extras temps réel "
            '(pip install "nexttraintosee[realtime]")'
        ) from exc

    message = gtfs_realtime_pb2.FeedMessage()
    try:
        message.ParseFromString(payload)
    except Exception as exc:  # pragma: no cover - dépend du flux
        raise RealtimeError(f"flux GTFS-RT illisible : {exc}") from exc

    updates: dict[str, TripUpdate] = {}
    for entity in message.entity:
        if not entity.HasField("trip_update"):
            continue
        trip_update = entity.trip_update
        trip_id = trip_update.trip.trip_id
        if not trip_id:
            continue
        stop_updates = tuple(
            StopUpdate(
                stop_id=su.stop_id,
                stop_sequence=su.stop_sequence if su.HasField("stop_sequence") else None,
                arrival_delay_s=su.arrival.delay if su.HasField("arrival") else None,
                departure_delay_s=su.departure.delay if su.HasField("departure") else None,
                schedule_relationship=_relationship_name(
                    su.schedule_relationship, gtfs_realtime_pb2.TripUpdate.StopTimeUpdate
                ),
            )
            for su in trip_update.stop_time_update
        )
        updates[trip_id] = TripUpdate(
            trip_id=trip_id,
            stop_updates=stop_updates,
            schedule_relationship=_relationship_name(
                trip_update.trip.schedule_relationship, gtfs_realtime_pb2.TripDescriptor
            ),
            timestamp=trip_update.timestamp or None,
        )

    return RealtimeSnapshot(updates=updates, timestamp=message.header.timestamp or None)


def _relationship_name(value: int, enum_holder) -> str:
    """Nom lisible d'une valeur d'énumération protobuf, avec repli sûr."""
    try:
        return enum_holder.ScheduleRelationship.Name(value)
    except Exception:  # pragma: no cover - valeur hors spécification
        return SCHEDULED


def fetch(url: str = SNCF_TRIP_UPDATES_URL, timeout_s: float = 30.0) -> bytes:
    """Télécharge un flux GTFS-RT."""
    request = urllib.request.Request(url, headers={"User-Agent": "NextTrainToSee/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise RealtimeError(f"le flux temps réel a répondu {exc.code} : {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RealtimeError(f"flux temps réel injoignable : {exc.reason}") from exc


def load_snapshot(url: str = SNCF_TRIP_UPDATES_URL, timeout_s: float = 30.0) -> RealtimeSnapshot:
    """Récupère et décode le temps réel en une étape."""
    return parse_feed_message(fetch(url, timeout_s=timeout_s))


def empty_snapshot() -> RealtimeSnapshot:
    """Photographie vide : l'outil doit rester utilisable sans temps réel."""
    return RealtimeSnapshot()
