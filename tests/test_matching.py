from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from nexttraintosee.matching import (
    Calibration,
    Match,
    ObservedRun,
    calibrate,
    fit_profile,
    match_detections,
    runs_from_matches,
)
from nexttraintosee.motion import Regime, TractionProfile, travel_time_s
from nexttraintosee.predict import Branch, Direction, Passage
from nexttraintosee.sensor.base import Detection

PARIS = ZoneInfo("Europe/Paris")
NOON = datetime(2026, 9, 9, 12, 0, tzinfo=PARIS)
SE = Branch("se", "Axe Narbonne", bearing_deg=137.5, track_distance_m=1650.0)


def detection(offset_s: float, peak_db: float = -12.0) -> Detection:
    when = NOON + timedelta(seconds=offset_s)
    return Detection(
        started_at=when - timedelta(seconds=5),
        peak_at=when,
        ended_at=when + timedelta(seconds=5),
        peak_db=peak_db,
        baseline_db=-40.0,
        sample_count=20,
    )


def passage(offset_s: float, regime: Regime = Regime.DEPARTING, trip_id: str = "T") -> Passage:
    when = NOON + timedelta(seconds=offset_s)
    return Passage(
        trip_id=trip_id,
        when=when,
        uncertainty_s=15.0,
        branch=SE,
        regime=regime,
        direction=Direction.OUTBOUND,
        speed_kmh=90.0,
        anchor_time=when - timedelta(seconds=86),
    )


# -- appariement -------------------------------------------------------------


def test_a_detection_close_to_a_prediction_is_matched():
    result = match_detections([detection(10)], [passage(0)])

    assert len(result.matches) == 1
    assert result.matches[0].residual_s == pytest.approx(10.0)
    assert not result.unmatched_detections and not result.unmatched_passages


def test_a_detection_beyond_tolerance_stays_unmatched():
    result = match_detections([detection(400)], [passage(0)], tolerance_s=180)

    assert not result.matches
    assert len(result.unmatched_detections) == 1
    assert len(result.unmatched_passages) == 1


def test_each_detection_and_prediction_is_used_at_most_once():
    result = match_detections([detection(0), detection(20)], [passage(5)])

    assert len(result.matches) == 1
    assert len(result.unmatched_detections) == 1


def test_matching_pairs_the_closest_candidates_first():
    # La détection à +100 s est plus proche du second train que du premier :
    # un appariement naïf dans l'ordre se tromperait.
    result = match_detections([detection(5), detection(100)], [passage(0), passage(105)])

    residuals = sorted(m.residual_s for m in result.matches)
    assert len(result.matches) == 2
    assert residuals == pytest.approx([-5.0, 5.0])


def test_unexplained_detections_are_the_freight_candidates():
    # Deux trains prédits, trois passages observés : le troisième n'est pas au GTFS.
    result = match_detections(
        [detection(0), detection(300), detection(600)],
        [passage(0), passage(600)],
        tolerance_s=120,
    )
    assert len(result.unmatched_detections) == 1
    assert result.unmatched_detections[0].midpoint == NOON + timedelta(seconds=300)


def test_predictions_without_detection_are_reported():
    result = match_detections([], [passage(0), passage(600)])
    assert len(result.unmatched_passages) == 2


def test_summary_reports_the_match_rate():
    result = match_detections([detection(0)], [passage(0), passage(600)])
    assert "1 appariements sur 2" in result.summary()


def test_matches_are_returned_in_chronological_order():
    result = match_detections(
        [detection(600), detection(0)], [passage(0), passage(600)], tolerance_s=60
    )
    assert [m.detection.midpoint for m in result.matches] == sorted(
        m.detection.midpoint for m in result.matches
    )


# -- calibration -------------------------------------------------------------


def test_calibration_recovers_a_systematic_bias():
    matches = [Match(detection(offset + 12), passage(offset)) for offset in (0, 300, 600, 900)]
    calibration = calibrate(matches)

    assert calibration.bias_s == pytest.approx(12.0)
    assert calibration.spread_s == pytest.approx(0.0)
    assert calibration.sample_size == 4


def test_calibration_is_robust_to_a_single_bad_match():
    # Trois résidus à +10 s, un aberrant à +170 s : la médiane tient.
    matches = [Match(detection(o + d), passage(o)) for o, d in ((0, 10), (300, 10), (600, 10), (900, 170))]
    assert calibrate(matches).bias_s == pytest.approx(10.0)


def test_calibration_separates_the_regimes():
    matches = [
        Match(detection(20), passage(0, Regime.DEPARTING, "a")),
        Match(detection(320), passage(300, Regime.DEPARTING, "b")),
        Match(detection(605), passage(600, Regime.ARRIVING, "c")),
        Match(detection(905), passage(900, Regime.ARRIVING, "d")),
    ]
    calibration = calibrate(matches)

    assert calibration.per_regime_bias_s[Regime.DEPARTING] == pytest.approx(20.0)
    assert calibration.per_regime_bias_s[Regime.ARRIVING] == pytest.approx(5.0)


def test_calibration_without_samples_is_refused():
    with pytest.raises(ValueError, match="aucun passage apparié"):
        calibrate([])


def test_adjusting_a_passage_applies_its_regime_bias():
    calibration = Calibration(
        bias_s=10.0,
        spread_s=4.0,
        sample_size=8,
        per_regime_bias_s={Regime.DEPARTING: 25.0},
    )
    adjusted = calibration.adjust(passage(0, Regime.DEPARTING))

    assert adjusted.when == NOON + timedelta(seconds=25)
    assert adjusted.uncertainty_s == 4.0


def test_adjusting_falls_back_to_the_global_bias():
    calibration = Calibration(bias_s=10.0, spread_s=4.0, sample_size=8, per_regime_bias_s={})
    assert calibration.adjust(passage(0)).when == NOON + timedelta(seconds=10)


def test_calibration_describes_itself():
    calibration = Calibration(3.0, 7.0, 12, {Regime.DEPARTING: 3.0})
    text = calibration.describe()
    assert "12 passages" in text and "departing" in text


# -- ajustement du profil de marche ------------------------------------------


def test_runs_are_derived_from_matched_passages():
    matches = [Match(detection(12), passage(0))]
    runs = runs_from_matches(matches, {"se": 1650.0})

    assert len(runs) == 1
    assert runs[0].distance_m == 1650.0
    # 86 s d'écart entre l'horaire en gare et la prédiction, plus 12 s de retard.
    assert runs[0].observed_travel_s == pytest.approx(98.0)


def test_runs_skip_branches_without_a_known_distance():
    assert runs_from_matches([Match(detection(0), passage(0))], {}) == []


def test_fitting_recovers_the_profile_that_generated_the_data():
    truth = TractionProfile(accel_ms2=0.75, decel_ms2=0.45, line_speed_kmh=115.0)
    runs = [
        ObservedRun(distance, regime, travel_time_s(distance, regime, truth))
        for distance in (300.0, 800.0, 1650.0, 3000.0, 6000.0)
        for regime in (Regime.DEPARTING, Regime.ARRIVING)
    ]

    fitted, rms = fit_profile(runs)

    assert rms < 1.0
    assert fitted.accel_ms2 == pytest.approx(truth.accel_ms2, rel=0.15)
    assert fitted.decel_ms2 == pytest.approx(truth.decel_ms2, rel=0.15)
    assert fitted.line_speed_kmh == pytest.approx(truth.line_speed_kmh, rel=0.15)


def test_fitting_improves_on_the_starting_guess():
    truth = TractionProfile(accel_ms2=0.9, decel_ms2=0.35, line_speed_kmh=140.0)
    runs = [
        ObservedRun(d, r, travel_time_s(d, r, truth))
        for d in (500.0, 1500.0, 4000.0)
        for r in (Regime.DEPARTING, Regime.ARRIVING)
    ]
    default = TractionProfile()
    _, fitted_rms = fit_profile(runs, start=default)

    baseline_rms = (
        sum((travel_time_s(r.distance_m, r.regime, default) - r.observed_travel_s) ** 2 for r in runs)
        / len(runs)
    ) ** 0.5
    assert fitted_rms < baseline_rms


def test_fitting_without_observations_is_refused():
    with pytest.raises(ValueError, match="aucun temps de parcours"):
        fit_profile([])


# -- ajustement sur des temps d'arrêt à arrêt ---------------------------------


def test_segment_fitting_recovers_the_profile_that_generated_the_data():
    from nexttraintosee.matching import fit_segment_profile
    from nexttraintosee.motion import segment_time_s

    truth = TractionProfile(accel_ms2=0.6, decel_ms2=0.7, line_speed_kmh=95.0)
    segments = [(length, segment_time_s(length, truth)) for length in (2000, 4000, 8000, 15000)]

    fitted, rms = fit_segment_profile(segments)

    assert rms < 5.0
    assert fitted.line_speed_kmh == pytest.approx(truth.line_speed_kmh, rel=0.15)


def test_segment_fitting_prefers_being_slow_to_being_impossible():
    from nexttraintosee.matching import fit_segment_profile
    from nexttraintosee.motion import segment_time_s

    # Deux segments inconciliables : l'un veut un modèle rapide, l'autre lent.
    # Être plus rapide que l'horaire le plus rapide est physiquement impossible,
    # l'ajustement doit donc pencher du côté prudent.
    segments = [(4000.0, 180.0), (4000.0, 400.0)]
    fitted, _ = fit_segment_profile(segments)

    modelled = segment_time_s(4000.0, fitted)
    assert modelled > 290.0


def test_segment_fitting_without_data_is_refused():
    from nexttraintosee.matching import fit_segment_profile

    with pytest.raises(ValueError, match="aucun segment"):
        fit_segment_profile([])


# -- ajustement de la seule vitesse de ligne ----------------------------------


def test_line_speed_is_recovered_from_a_segment_time():
    from nexttraintosee.matching import fit_line_speed_kmh
    from nexttraintosee.motion import segment_time_s

    profile = TractionProfile(accel_ms2=0.5, decel_ms2=0.6, line_speed_kmh=90.0)
    target = segment_time_s(5000.0, replace(profile, line_speed_kmh=72.0))

    assert fit_line_speed_kmh(5000.0, target, profile) == pytest.approx(72.0, rel=0.02)


def test_a_slower_target_yields_a_lower_speed():
    from nexttraintosee.matching import fit_line_speed_kmh

    profile = TractionProfile()
    assert fit_line_speed_kmh(5000.0, 400.0, profile) < fit_line_speed_kmh(5000.0, 200.0, profile)


def test_an_unreachable_target_is_clamped_to_the_search_bounds():
    from nexttraintosee.matching import fit_line_speed_kmh

    profile = TractionProfile()
    # Une seconde pour 5 km : inatteignable, on borne au maximum.
    assert fit_line_speed_kmh(5000.0, 1.0, profile) == 200.0
    # Dix heures pour 5 km : on borne au minimum.
    assert fit_line_speed_kmh(5000.0, 36_000.0, profile) == 20.0


def test_line_speed_fitting_rejects_impossible_inputs():
    from nexttraintosee.matching import fit_line_speed_kmh

    with pytest.raises(ValueError, match="strictement positives"):
        fit_line_speed_kmh(0.0, 100.0, TractionProfile())
    with pytest.raises(ValueError, match="strictement positives"):
        fit_line_speed_kmh(1000.0, 0.0, TractionProfile())
