
from nexttraintosee.realtime import (
    CANCELED,
    SKIPPED,
    RealtimeSnapshot,
    StopUpdate,
    TripUpdate,
    empty_snapshot,
    resolve_delay,
)

ANCHOR = {"SP:MTB:1", "SP:MTB:2"}


def trip(*stop_updates, **kwargs):
    return TripUpdate(trip_id=kwargs.pop("trip_id", "T:1"), stop_updates=tuple(stop_updates), **kwargs)


def test_delay_published_at_the_anchor_is_used_directly():
    update = trip(
        StopUpdate("SP:VLF:1", departure_delay_s=60),
        StopUpdate("SP:MTB:1", departure_delay_s=180),
    )
    assert resolve_delay(update, ANCHOR) == 180


def test_departure_delay_wins_over_arrival_delay():
    update = trip(StopUpdate("SP:MTB:1", arrival_delay_s=120, departure_delay_s=240))
    assert resolve_delay(update, ANCHOR) == 240
    assert resolve_delay(update, ANCHOR, prefer_departure=False) == 120


def test_arrival_delay_is_used_when_no_departure_is_published():
    update = trip(StopUpdate("SP:MTB:1", arrival_delay_s=90))
    assert resolve_delay(update, ANCHOR) == 90


def test_delay_propagates_from_the_previous_published_stop():
    # GTFS-RT n'oblige pas à republier le retard à chaque arrêt : il se propage.
    update = trip(
        StopUpdate("SP:MTB_N:1", departure_delay_s=300),
        StopUpdate("SP:MTB:1"),
        StopUpdate("SP:VLF:1", departure_delay_s=420),
    )
    assert resolve_delay(update, ANCHOR) == 300


def test_a_later_stop_does_not_leak_its_delay_backwards():
    update = trip(StopUpdate("SP:MTB:1"), StopUpdate("SP:VLF:1", departure_delay_s=420))
    assert resolve_delay(update, ANCHOR) is None


def test_trips_not_calling_at_the_anchor_yield_no_delay():
    update = trip(StopUpdate("SP:COL:1", departure_delay_s=60))
    assert resolve_delay(update, ANCHOR) is None


def test_advance_is_reported_as_a_negative_delay():
    update = trip(StopUpdate("SP:MTB:1", departure_delay_s=-45))
    assert resolve_delay(update, ANCHOR) == -45


def test_snapshot_collects_delays_by_trip():
    snapshot = RealtimeSnapshot(
        updates={
            "A": trip(StopUpdate("SP:MTB:1", departure_delay_s=60), trip_id="A"),
            "B": trip(StopUpdate("SP:COL:1", departure_delay_s=90), trip_id="B"),
        }
    )
    assert snapshot.delays_at(ANCHOR) == {"A": 60}


def test_snapshot_lists_fully_canceled_trips():
    snapshot = RealtimeSnapshot(
        updates={"A": trip(trip_id="A", schedule_relationship=CANCELED), "B": trip(trip_id="B")}
    )
    assert snapshot.canceled_trip_ids() == {"A"}


def test_a_stop_skipped_at_the_anchor_counts_as_canceled_there():
    snapshot = RealtimeSnapshot(
        updates={
            "A": trip(StopUpdate("SP:MTB:1", schedule_relationship=SKIPPED), trip_id="A"),
            "B": trip(StopUpdate("SP:COL:1", schedule_relationship=SKIPPED), trip_id="B"),
        }
    )
    assert snapshot.canceled_trip_ids(ANCHOR) == {"A"}


def test_empty_snapshot_is_harmless():
    snapshot = empty_snapshot()
    assert snapshot.delays_at(ANCHOR) == {}
    assert snapshot.canceled_trip_ids() == set()
