from datetime import datetime, timedelta, timezone

import pytest

from nexttraintosee.sensor.base import PassageDetector, to_db
from nexttraintosee.sensor.replay import read_levels, synthetic_passage, write_levels

START = datetime(2026, 9, 9, 8, 0, tzinfo=timezone.utc)


def run(detector, samples):
    return list(detector.feed(samples))


def steady(level, seconds, period=0.5, start=START):
    return [(start + timedelta(seconds=i * period), level) for i in range(int(seconds / period))]


def test_to_db_is_monotonic_and_safe_at_zero():
    assert to_db(1.0) == pytest.approx(0.0)
    assert to_db(10.0) == pytest.approx(20.0)
    assert to_db(0.0) < -200


def test_detector_rejects_inverted_hysteresis():
    with pytest.raises(ValueError, match="release_db"):
        PassageDetector(trigger_db=4.0, release_db=9.0)


def test_detector_rejects_inconsistent_durations():
    with pytest.raises(ValueError, match="durées"):
        PassageDetector(min_duration_s=30.0, max_duration_s=10.0)


def test_a_clean_passage_is_detected_once():
    samples = synthetic_passage(
        START, duration_s=12.0, period_s=0.5, baseline=0.01, peak=0.2, total_s=180.0
    )
    detections = run(PassageDetector(baseline_half_life_s=60.0), samples)

    assert len(detections) == 1
    assert detections[0].duration_s == pytest.approx(12.0, abs=1.5)
    assert detections[0].prominence_db > 9.0


def test_quiet_background_produces_nothing():
    assert run(PassageDetector(), steady(0.01, 300.0)) == []


def test_a_brief_spike_is_not_a_train():
    # Un claquement d'une seconde : au-dessus du seuil, mais trop court.
    samples = synthetic_passage(
        START, duration_s=1.0, period_s=0.5, baseline=0.01, peak=0.3, total_s=120.0
    )
    assert run(PassageDetector(min_duration_s=3.0), samples) == []


def test_sustained_noise_is_rejected_as_too_long():
    # Travaux ou averse : le niveau reste haut bien plus longtemps qu'un train.
    samples = steady(0.01, 60.0) + [
        (START + timedelta(seconds=60 + i * 0.5), 0.3) for i in range(600)
    ]
    assert run(PassageDetector(max_duration_s=120.0), samples) == []


def test_a_dip_inside_a_passage_does_not_split_it():
    # Hystérésis : le niveau redescend un peu au milieu du convoi sans
    # repasser sous le seuil de relâchement.
    period = 0.5
    samples = steady(0.01, 60.0)
    profile = [0.2, 0.25, 0.05, 0.25, 0.2]  # creux central bien au-dessus du fond
    for index, level in enumerate(profile):
        for step in range(8):
            offset = 60 + index * 4 + step * period
            samples.append((START + timedelta(seconds=offset), level))
    samples += [(START + timedelta(seconds=90 + i * period), 0.01) for i in range(120)]

    detections = run(PassageDetector(trigger_db=9.0, release_db=4.0), samples)
    assert len(detections) == 1


def test_two_passages_separated_by_silence_are_counted_twice():
    first = synthetic_passage(START, 12.0, 0.5, 0.01, 0.2, 120.0)
    second = synthetic_passage(
        START + timedelta(seconds=120), 12.0, 0.5, 0.01, 0.2, 120.0
    )
    detections = run(PassageDetector(refractory_s=15.0), first + second)
    assert len(detections) == 2


def test_the_refractory_period_suppresses_an_immediate_repeat():
    first = synthetic_passage(START, 12.0, 0.5, 0.01, 0.2, 60.0)
    second = synthetic_passage(START + timedelta(seconds=60), 12.0, 0.5, 0.01, 0.2, 60.0)
    detections = run(PassageDetector(refractory_s=600.0), first + second)
    assert len(detections) == 1


def test_the_peak_marks_the_closest_approach():
    samples = synthetic_passage(START, 20.0, 0.5, 0.01, 0.2, 200.0)
    detection = run(PassageDetector(), samples)[0]
    assert detection.started_at <= detection.midpoint <= detection.ended_at
    assert detection.midpoint == detection.peak_at


def test_the_baseline_follows_a_rising_background():
    # Le fond monte progressivement (heure de pointe routière) : pas de faux positif.
    samples = [
        (START + timedelta(seconds=i * 0.5), 0.01 * (1 + i / 400)) for i in range(1200)
    ]
    detector = PassageDetector(baseline_half_life_s=60.0)
    assert run(detector, samples) == []
    assert detector.baseline_db > to_db(0.01)


def test_flush_closes_an_event_left_open():
    detector = PassageDetector(min_duration_s=3.0)
    for when, level in steady(0.01, 60.0):
        detector.push(when, level)
    for index in range(20):
        detector.push(START + timedelta(seconds=60 + index * 0.5), 0.3)

    assert detector.in_event
    detection = detector.flush(START + timedelta(seconds=70))
    assert detection is not None and detection.duration_s == pytest.approx(10.0, abs=0.5)
    assert not detector.in_event


def test_flush_without_an_open_event_returns_nothing():
    assert PassageDetector().flush() is None


def test_levels_survive_a_write_read_round_trip(tmp_path):
    samples = synthetic_passage(START, 10.0, 1.0, 0.01, 0.2, 30.0)
    path = tmp_path / "levels.csv"
    write_levels(path, samples)
    restored = list(read_levels(path))

    assert len(restored) == len(samples)
    assert restored[0][0] == samples[0][0]
    assert restored[-1][1] == pytest.approx(samples[-1][1], rel=1e-5)
