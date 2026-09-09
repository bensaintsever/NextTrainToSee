from datetime import datetime, timedelta, timezone

import pytest

from nexttraintosee.observation import Observation, ObservationKind, from_detection
from nexttraintosee.sensor.base import Detection

NOON = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


def test_a_sighting_carries_its_time():
    observation = Observation(NOON)
    assert observation.kind is ObservationKind.SEEN
    assert observation.midpoint == NOON
    assert "12:00:00" in observation.describe()


def test_a_sighting_without_a_time_is_refused():
    with pytest.raises(ValueError, match="doit porter une heure"):
        Observation(None)


def test_a_missed_passage_needs_no_time():
    observation = Observation(None, ObservationKind.NOT_SEEN)
    assert "non passé" in observation.describe()
    with pytest.raises(ValueError, match="n'a pas d'instant"):
        observation.midpoint


def test_a_negative_precision_is_refused():
    with pytest.raises(ValueError, match="précision"):
        Observation(NOON, precision_s=-1.0)


def test_an_observation_naming_its_trip_is_binding():
    # Sur une ligne où il passe un train toutes les trois minutes, savoir lequel
    # était visé vaut mieux que de le deviner par proximité.
    assert not Observation(NOON).is_binding
    assert not Observation(NOON, trip_id="T:1").is_binding
    assert Observation(NOON, trip_id="T:1", anchor_time=NOON).is_binding


def test_the_source_appears_in_the_description():
    assert "utilisateur-42" in Observation(NOON, source="utilisateur-42").describe()


def test_a_sensor_detection_becomes_an_observation():
    detection = Detection(
        started_at=NOON - timedelta(seconds=6),
        peak_at=NOON,
        ended_at=NOON + timedelta(seconds=6),
        peak_db=-10.0,
        baseline_db=-40.0,
        sample_count=24,
    )
    observation = from_detection(detection)

    assert observation.midpoint == NOON
    assert observation.source == "capteur"
    # Un convoi long laisse plus de latitude sur l'instant exact du passage.
    assert observation.precision_s == pytest.approx(6.0)


def test_a_very_short_detection_keeps_a_floor_of_precision():
    brief = Detection(NOON, NOON, NOON + timedelta(seconds=1), -10.0, -40.0, 2)
    assert from_detection(brief).precision_s >= 1.0


def test_sensor_and_human_observations_are_interchangeable():
    # C'est tout l'intérêt : l'appariement ne doit pas les distinguer.
    detection = Detection(NOON, NOON, NOON + timedelta(seconds=8), -10.0, -40.0, 16)
    for observable in (from_detection(detection), Observation(NOON, source="app")):
        assert observable.midpoint == NOON
