from datetime import datetime, timedelta, timezone

import pytest

from nexttraintosee.sensor.base import PassageDetector
from nexttraintosee.sensor.replay import synthetic_passage
from nexttraintosee.sensor.session import Status, listen_session

START = datetime(2026, 9, 9, 8, 0, tzinfo=timezone.utc)


def steady(level, seconds, period=0.5, start=START):
    return [(start + timedelta(seconds=i * period), level) for i in range(int(seconds / period))]


def test_a_session_reports_what_it_consumed():
    stats = listen_session(PassageDetector(), steady(0.01, 60.0))

    assert stats.samples == 120
    assert stats.started_at == START
    assert stats.duration_s == pytest.approx(59.5)
    assert stats.detections == []


def test_an_empty_stream_yields_an_empty_session():
    stats = listen_session(PassageDetector(), [])
    assert stats.samples == 0 and stats.duration_s == 0.0
    assert "0 passage(s)" in stats.describe()


def test_detections_are_collected_and_announced():
    seen = []
    samples = synthetic_passage(START, 12.0, 0.5, 0.01, 0.2, 180.0)
    stats = listen_session(PassageDetector(), samples, on_detection=seen.append)

    assert len(stats.detections) == 1
    assert seen == stats.detections


def test_the_duration_is_measured_on_the_samples_not_the_clock():
    # Deux heures de mesures simulées, session bornée à cinq minutes.
    stats = listen_session(PassageDetector(), steady(0.01, 7200.0), duration_s=300.0)

    assert stats.duration_s == pytest.approx(300.0, abs=1.0)
    assert stats.samples < 7200 / 0.5


def test_a_session_shorter_than_the_limit_stops_at_the_stream_end():
    stats = listen_session(PassageDetector(), steady(0.01, 60.0), duration_s=3600.0)
    assert stats.samples == 120


def test_a_passage_still_open_at_the_end_is_closed():
    # Le flux s'arrête en plein passage : il ne doit pas être perdu.
    samples = steady(0.01, 120.0) + [
        (START + timedelta(seconds=120 + i * 0.5), 0.25) for i in range(40)
    ]
    stats = listen_session(PassageDetector(), samples)

    assert len(stats.detections) == 1
    assert stats.detections[0].duration_s == pytest.approx(19.5, abs=1.0)


def test_status_points_are_emitted_at_the_requested_cadence():
    statuses: list[Status] = []
    listen_session(
        PassageDetector(), steady(0.01, 600.0), on_status=statuses.append, status_every_s=60.0
    )
    assert len(statuses) == pytest.approx(9, abs=1)
    assert all(isinstance(s.baseline_db, float) for s in statuses)


def test_status_headroom_shows_how_far_a_passage_stands_out():
    statuses: list[Status] = []
    samples = synthetic_passage(START, 12.0, 0.5, 0.01, 0.2, 180.0)
    listen_session(PassageDetector(), samples, on_status=statuses.append, status_every_s=60.0)

    # Le passage culmine à 0.2 contre un fond à 0.01, soit ~26 dB de plus.
    assert max(s.headroom_db for s in statuses) > 20.0


def test_status_headroom_stays_small_when_nothing_happens():
    statuses: list[Status] = []
    listen_session(
        PassageDetector(), steady(0.01, 300.0), on_status=statuses.append, status_every_s=60.0
    )
    assert all(s.headroom_db < 1.0 for s in statuses)


def test_the_detector_state_carries_across_calls():
    # Une session peut être reprise sur le même détecteur sans réapprendre le fond.
    detector = PassageDetector()
    listen_session(detector, steady(0.01, 300.0))
    baseline = detector.baseline_db

    listen_session(detector, steady(0.01, 60.0, start=START + timedelta(seconds=300)))
    assert detector.baseline_db == pytest.approx(baseline, abs=0.5)
