from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from nexttraintosee.motion import Regime
from nexttraintosee.predict import Branch, Direction, Passage
from nexttraintosee.sensor.base import Detection
from nexttraintosee.store import Store

PARIS = ZoneInfo("Europe/Paris")
NOON = datetime(2026, 9, 9, 12, 0, tzinfo=PARIS)
SE = Branch("se", "Axe Narbonne", bearing_deg=137.5, track_distance_m=1650.0)
SITE = "site de test"


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "nested" / "journal.sqlite") as opened:
        yield opened


def detection(offset_s: float) -> Detection:
    when = NOON + timedelta(seconds=offset_s)
    return Detection(when - timedelta(seconds=5), when, when + timedelta(seconds=5), -12.0, -40.0, 20)


def passage(offset_s: float, trip_id: str = "T:1", delay_s: int | None = None) -> Passage:
    when = NOON + timedelta(seconds=offset_s)
    return Passage(
        trip_id=trip_id,
        when=when,
        uncertainty_s=15.0,
        branch=SE,
        regime=Regime.DEPARTING,
        direction=Direction.OUTBOUND,
        speed_kmh=88.0,
        anchor_time=when - timedelta(seconds=86),
        route_label="TER Toulouse - Narbonne",
        headsign="Narbonne",
        delay_s=delay_s,
    )


def test_the_database_file_and_its_parents_are_created(tmp_path):
    path = tmp_path / "a" / "b" / "journal.sqlite"
    with Store(path):
        pass
    assert path.exists()


def test_a_detection_survives_a_round_trip(store):
    store.record_detection(SITE, detection(0))
    restored = store.detections_between(SITE, NOON - timedelta(hours=1), NOON + timedelta(hours=1))

    assert len(restored) == 1
    assert restored[0].peak_at == NOON
    assert restored[0].peak_db == -12.0
    assert restored[0].sample_count == 20


def test_recording_the_same_detection_twice_is_harmless(store):
    store.record_detection(SITE, detection(0))
    store.record_detection(SITE, detection(0))
    assert store.counts(SITE) == (1, 0)


def test_detections_outside_the_window_are_not_returned(store):
    store.record_detection(SITE, detection(0))
    store.record_detection(SITE, detection(7200))
    window = store.detections_between(SITE, NOON - timedelta(minutes=1), NOON + timedelta(minutes=1))
    assert len(window) == 1


def test_detections_are_returned_in_chronological_order(store):
    for offset in (600, 0, 300):
        store.record_detection(SITE, detection(offset))
    restored = store.detections_between(SITE, NOON - timedelta(hours=1), NOON + timedelta(hours=1))
    assert [d.peak_at for d in restored] == sorted(d.peak_at for d in restored)


def test_a_passage_survives_a_round_trip(store):
    store.record_passages(SITE, [passage(0, delay_s=120)], predicted_at=NOON)
    restored = store.passages_between(SITE, NOON - timedelta(hours=1), NOON + timedelta(hours=1), [SE])

    assert len(restored) == 1
    assert restored[0].trip_id == "T:1"
    assert restored[0].branch is SE
    assert restored[0].regime is Regime.DEPARTING
    assert restored[0].direction is Direction.OUTBOUND
    assert restored[0].delay_s == 120
    assert restored[0].headsign == "Narbonne"


def test_repredicting_a_trip_replaces_the_previous_estimate(store):
    store.record_passages(SITE, [passage(0)], predicted_at=NOON)
    later = passage(0)
    updated = Passage(**{**later.__dict__, "when": NOON + timedelta(seconds=240), "delay_s": 240})
    store.record_passages(SITE, [updated], predicted_at=NOON + timedelta(minutes=5))

    restored = store.passages_between(SITE, NOON - timedelta(hours=1), NOON + timedelta(hours=1), [SE])
    assert len(restored) == 1
    assert restored[0].delay_s == 240


def test_predictions_for_a_branch_no_longer_configured_are_skipped(store):
    store.record_passages(SITE, [passage(0)], predicted_at=NOON)
    other = Branch("autre", "Autre", bearing_deg=0.0)
    assert store.passages_between(SITE, NOON - timedelta(hours=1), NOON + timedelta(hours=1), [other]) == []


def test_sites_do_not_see_each_other(store):
    store.record_detection(SITE, detection(0))
    store.record_passages(SITE, [passage(0)], predicted_at=NOON)
    assert store.counts("un autre site") == (0, 0)


def test_counts_report_both_tables(store):
    store.record_detection(SITE, detection(0))
    store.record_detection(SITE, detection(600))
    store.record_passages(SITE, [passage(0), passage(600, trip_id="T:2")], predicted_at=NOON)
    assert store.counts(SITE) == (2, 2)


def test_the_store_reopens_an_existing_database(tmp_path):
    path = tmp_path / "journal.sqlite"
    with Store(path) as first:
        first.record_detection(SITE, detection(0))
    with Store(path) as second:
        assert second.counts(SITE) == (1, 0)
