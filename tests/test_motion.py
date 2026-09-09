import math

import pytest

from nexttraintosee.motion import (
    Regime,
    TractionProfile,
    speed_at_point_ms,
    travel_time_s,
    travel_time_uncertainty_s,
)


def test_profile_rejects_impossible_parameters():
    with pytest.raises(ValueError):
        TractionProfile(accel_ms2=0.0)
    with pytest.raises(ValueError):
        TractionProfile(line_speed_kmh=-1.0)
    with pytest.raises(ValueError):
        TractionProfile(sinuosity=0.9)


def test_pure_acceleration_matches_closed_form():
    # 100 m à 0.5 m/s^2 : la vitesse limite n'est jamais atteinte,
    # donc t = sqrt(2 d / a).
    profile = TractionProfile(accel_ms2=0.5, line_speed_kmh=200.0)
    assert travel_time_s(100.0, Regime.DEPARTING, profile) == pytest.approx(
        math.sqrt(2 * 100.0 / 0.5)
    )


def test_long_run_adds_a_cruise_phase():
    profile = TractionProfile(accel_ms2=0.5, line_speed_kmh=90.0)
    v = 90 / 3.6
    ramp_distance = v**2 / (2 * 0.5)
    expected = v / 0.5 + (5000.0 - ramp_distance) / v
    assert travel_time_s(5000.0, Regime.DEPARTING, profile) == pytest.approx(expected)


def test_through_regime_is_constant_speed():
    profile = TractionProfile(line_speed_kmh=90.0)
    assert travel_time_s(900.0, Regime.THROUGH, profile) == pytest.approx(900.0 / (90 / 3.6))


def test_through_is_always_the_fastest_regime():
    profile = TractionProfile()
    times = {r: travel_time_s(1500.0, r, profile) for r in Regime}
    assert times[Regime.THROUGH] < times[Regime.ARRIVING] < times[Regime.DEPARTING]


def test_stronger_braking_shortens_the_arrival_run():
    gentle = TractionProfile(decel_ms2=0.4)
    firm = TractionProfile(decel_ms2=0.9)
    assert travel_time_s(1500.0, Regime.ARRIVING, firm) < travel_time_s(
        1500.0, Regime.ARRIVING, gentle
    )


def test_zero_distance_takes_no_time():
    for regime in Regime:
        assert travel_time_s(0.0, regime, TractionProfile()) == 0.0


def test_negative_distance_is_rejected():
    with pytest.raises(ValueError):
        travel_time_s(-1.0, Regime.DEPARTING, TractionProfile())


def test_speed_at_point_is_capped_by_line_speed():
    profile = TractionProfile(accel_ms2=0.5, line_speed_kmh=90.0)
    assert speed_at_point_ms(50.0, Regime.DEPARTING, profile) == pytest.approx(
        math.sqrt(2 * 0.5 * 50.0)
    )
    assert speed_at_point_ms(50_000.0, Regime.DEPARTING, profile) == pytest.approx(90 / 3.6)
    assert speed_at_point_ms(10.0, Regime.THROUGH, profile) == pytest.approx(90 / 3.6)


def test_uncertainty_grows_with_distance():
    profile = TractionProfile()
    near = travel_time_uncertainty_s(500.0, Regime.DEPARTING, profile)
    far = travel_time_uncertainty_s(3000.0, Regime.DEPARTING, profile)
    assert 0.0 < near < far


def test_uncertainty_is_a_few_tens_of_seconds_at_urban_range():
    # Ordre de grandeur attendu à ~1.5 km d'une gare : quelques dizaines de
    # secondes, pas quelques minutes.
    uncertainty = travel_time_uncertainty_s(1534.0, Regime.DEPARTING, TractionProfile())
    assert 5.0 < uncertainty < 40.0
