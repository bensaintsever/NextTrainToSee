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


# -- parcours d'arrêt à arrêt --------------------------------------------------


def test_a_long_segment_reaches_the_line_speed():
    from nexttraintosee.motion import segment_profile

    shape = segment_profile(10_000.0, TractionProfile(line_speed_kmh=120.0))
    assert not shape.is_triangular
    assert shape.peak_speed_ms == pytest.approx(120 / 3.6)
    assert shape.cruise_distance_m > 0


def test_a_short_segment_never_reaches_the_line_speed():
    from nexttraintosee.motion import segment_profile

    shape = segment_profile(300.0, TractionProfile(line_speed_kmh=120.0))
    assert shape.is_triangular
    assert shape.peak_speed_ms < 120 / 3.6
    # Accélération et freinage se partagent toute la distance.
    assert shape.accel_distance_m < 300.0


def test_segment_time_grows_with_distance():
    from nexttraintosee.motion import segment_time_s

    profile = TractionProfile()
    times = [segment_time_s(d, profile) for d in (500, 1000, 4000, 10_000)]
    assert times == sorted(times)


def test_segment_time_shrinks_when_the_line_is_faster():
    from nexttraintosee.motion import segment_time_s

    slow = segment_time_s(5000.0, TractionProfile(line_speed_kmh=60.0))
    fast = segment_time_s(5000.0, TractionProfile(line_speed_kmh=140.0))
    assert fast < slow


def test_progress_runs_from_zero_to_the_full_duration():
    from nexttraintosee.motion import segment_progress_s, segment_time_s

    profile, length = TractionProfile(), 4000.0
    assert segment_progress_s(0.0, length, profile) == pytest.approx(0.0)
    assert segment_progress_s(length, length, profile) == pytest.approx(
        segment_time_s(length, profile)
    )


def test_progress_is_monotonic():
    from nexttraintosee.motion import segment_progress_s

    profile, length = TractionProfile(), 4000.0
    times = [segment_progress_s(x, length, profile) for x in range(0, 4001, 250)]
    assert times == sorted(times)


def test_progress_outside_the_segment_is_rejected():
    from nexttraintosee.motion import segment_progress_s

    with pytest.raises(ValueError, match="hors du segment"):
        segment_progress_s(5000.0, 4000.0, TractionProfile())


def test_the_first_half_takes_longer_than_the_second():
    # Le train accélère au départ et roule vite à l'arrivée : à mi-distance,
    # plus de la moitié du temps est écoulée. C'est précisément ce qu'une
    # interpolation linéaire manquerait.
    from nexttraintosee.motion import segment_fraction

    fraction = segment_fraction(2000.0, 4000.0, TractionProfile(line_speed_kmh=120.0))
    assert fraction > 0.5


def test_fraction_stays_within_bounds():
    from nexttraintosee.motion import segment_fraction

    profile = TractionProfile()
    assert segment_fraction(0.0, 3000.0, profile) == pytest.approx(0.0)
    assert segment_fraction(3000.0, 3000.0, profile) == pytest.approx(1.0)


def test_a_zero_length_segment_is_harmless():
    from nexttraintosee.motion import segment_fraction, segment_time_s

    assert segment_time_s(0.0, TractionProfile()) == 0.0
    assert segment_fraction(0.0, 0.0, TractionProfile()) == 0.0
