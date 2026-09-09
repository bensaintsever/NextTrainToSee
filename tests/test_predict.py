from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from nexttraintosee.gtfs import GtfsFeed
from nexttraintosee.motion import Regime, TractionProfile, travel_time_s
from nexttraintosee.predict import (
    Branch,
    Direction,
    Site,
    next_passages,
    predict_passages,
    service_days_around,
)

from conftest import BRANCHES, MATABIAU, OBSERVER

PARIS = ZoneInfo("Europe/Paris")
WEEKDAY = date(2026, 9, 9)


@pytest.fixture
def feed(gtfs_zip) -> GtfsFeed:
    return GtfsFeed.load(gtfs_zip, anchor_name="Toulouse Matabiau")


def by_trip(passages):
    result = {}
    for passage in passages:
        result.setdefault(passage.trip_id, []).append(passage)
    return result


# -- sélection des circulations ---------------------------------------------


def test_only_trips_heading_past_the_observer_are_kept(feed, site):
    trips = set(by_trip(predict_passages(feed, site, WEEKDAY)))
    # Vers Narbonne (sud-est) et Latour-de-Carol (sud) : ça passe.
    assert {"T:SE:1", "T:S:1", "T:SE:2", "T:SE:THRU"} <= trips
    # Vers Colomiers (ouest) : la branche ne passe pas devant le point.
    assert "T:W:1" not in trips
    # Le train venant de Montauban (nord) et terminus Matabiau non plus.
    assert "T:N:1" not in trips


def test_bus_substitutions_are_excluded_by_default(feed, site):
    assert "T:BUS:1" not in by_trip(predict_passages(feed, site, WEEKDAY))


def test_bus_substitutions_can_be_forced_in(feed, site):
    passages = predict_passages(feed, site, WEEKDAY, include_non_rail=True)
    assert "T:BUS:1" in by_trip(passages)


def test_sunday_services_do_not_appear_on_a_weekday(feed, site):
    assert "T:SE:SUN" not in by_trip(predict_passages(feed, site, WEEKDAY))


# -- datation ----------------------------------------------------------------


def test_departing_train_passes_after_its_scheduled_departure(feed, site):
    passage = by_trip(predict_passages(feed, site, WEEKDAY))["T:SE:1"][0]
    expected_travel = travel_time_s(1650.0, Regime.DEPARTING, site.profile)

    assert passage.direction is Direction.OUTBOUND
    assert passage.regime is Regime.DEPARTING
    assert passage.anchor_time == datetime(2026, 9, 9, 8, 2, tzinfo=PARIS)
    assert (passage.when - passage.anchor_time).total_seconds() == pytest.approx(expected_travel)


def test_arriving_train_passes_before_its_scheduled_arrival(feed, site):
    passage = by_trip(predict_passages(feed, site, WEEKDAY))["T:SE:2"][0]
    expected_travel = travel_time_s(1650.0, Regime.ARRIVING, site.profile)

    assert passage.direction is Direction.INBOUND
    assert passage.regime is Regime.ARRIVING
    assert passage.anchor_time == datetime(2026, 9, 9, 9, 30, tzinfo=PARIS)
    assert (passage.anchor_time - passage.when).total_seconds() == pytest.approx(expected_travel)


def test_a_train_calling_at_the_anchor_is_seen_once_per_adjacent_branch(feed, site):
    # T:SE:THRU vient du nord (branche invisible) et repart au sud-est : un seul passage.
    passages = by_trip(predict_passages(feed, site, WEEKDAY))["T:SE:THRU"]
    assert len(passages) == 1
    assert passages[0].direction is Direction.OUTBOUND


def test_a_technical_stop_is_modelled_as_a_run_through(feed, site):
    passage = by_trip(predict_passages(feed, site, WEEKDAY))["T:SE:THRU"][0]
    assert passage.regime is Regime.THROUGH
    # Sans arrêt, le train couvre la distance plus vite qu'au démarrage.
    assert (passage.when - passage.anchor_time).total_seconds() < travel_time_s(
        1650.0, Regime.DEPARTING, site.profile
    )


def test_times_past_midnight_land_on_the_following_day(feed, site):
    passage = by_trip(predict_passages(feed, site, WEEKDAY))["T:SE:NIGHT"][0]
    assert passage.anchor_time.date() == date(2026, 9, 10)
    assert (passage.anchor_time.hour, passage.anchor_time.minute) == (0, 22)


def test_passages_are_returned_in_chronological_order(feed, site):
    passages = predict_passages(feed, site, WEEKDAY)
    assert [p.when for p in passages] == sorted(p.when for p in passages)


def test_speed_at_the_point_is_reported(feed, site):
    passage = by_trip(predict_passages(feed, site, WEEKDAY))["T:SE:1"][0]
    assert 0 < passage.speed_kmh <= site.profile.line_speed_kmh


def test_uncertainty_is_reported_as_a_window(feed, site):
    passage = by_trip(predict_passages(feed, site, WEEKDAY))["T:SE:1"][0]
    low, high = passage.window
    assert low < passage.when < high
    assert (high - low).total_seconds() == pytest.approx(2 * passage.uncertainty_s)


# -- temps réel --------------------------------------------------------------


def test_delay_shifts_a_departure_later(feed, site):
    baseline = by_trip(predict_passages(feed, site, WEEKDAY))["T:SE:1"][0]
    delayed = by_trip(predict_passages(feed, site, WEEKDAY, delays_s={"T:SE:1": 300}))["T:SE:1"][0]

    assert delayed.when - baseline.when == timedelta(seconds=300)
    assert delayed.delay_s == 300
    assert delayed.is_realtime and not baseline.is_realtime


def test_delay_shifts_an_arrival_later_too(feed, site):
    baseline = by_trip(predict_passages(feed, site, WEEKDAY))["T:SE:2"][0]
    delayed = by_trip(predict_passages(feed, site, WEEKDAY, delays_s={"T:SE:2": 180}))["T:SE:2"][0]
    assert delayed.when - baseline.when == timedelta(seconds=180)


def test_a_delay_on_another_trip_changes_nothing(feed, site):
    baseline = by_trip(predict_passages(feed, site, WEEKDAY))["T:SE:1"][0]
    other = by_trip(predict_passages(feed, site, WEEKDAY, delays_s={"T:S:1": 600}))["T:SE:1"][0]
    assert other.when == baseline.when


# -- branches ----------------------------------------------------------------


def test_branch_matching_uses_an_angular_sector():
    branch = Branch("se", "sud-est", bearing_deg=137.5, tolerance_deg=40.0)
    assert branch.matches(137.5) and branch.matches(170.0) and branch.matches(100.0)
    assert not branch.matches(190.0)


def test_overlapping_sectors_resolve_to_the_closest_branch(site):
    # 160° tombe dans les secteurs sud-est (137.5°) et sud (184.2°).
    assert site.branch_for(160.0).branch_id == "se"
    assert site.branch_for(175.0).branch_id == "s"


def test_bearings_outside_every_sector_yield_no_branch(site):
    assert site.branch_for(60.0) is None


def test_track_distance_falls_back_to_sinuous_crow_flight(feed):
    # Sans track_distance_m, on corrige la distance à vol d'oiseau (~1.53 km).
    site = Site(
        name="repli",
        position=OBSERVER,
        anchor_station="Toulouse Matabiau",
        anchor_position=MATABIAU,
        branches=(Branch("se", "sud-est", bearing_deg=137.5),),
        profile=TractionProfile(sinuosity=1.05),
    )
    passage = by_trip(predict_passages(feed, site, WEEKDAY))["T:SE:1"][0]
    travel = (passage.when - passage.anchor_time).total_seconds()
    assert travel == pytest.approx(
        travel_time_s(1534.0 * 1.05, Regime.DEPARTING, site.profile), rel=0.02
    )


def test_missing_anchor_position_is_reported(feed):
    site = Site(
        name="sans ancre",
        position=OBSERVER,
        anchor_station="Gare Inconnue",
        branches=BRANCHES,
    )
    with pytest.raises(ValueError, match="gare d'appui"):
        predict_passages(feed, site, WEEKDAY)


# -- fenêtrage ---------------------------------------------------------------


def test_next_passages_keeps_only_the_upcoming_window(feed, site):
    passages = predict_passages(feed, site, WEEKDAY)
    now = datetime(2026, 9, 9, 8, 0, tzinfo=PARIS)
    upcoming = next_passages(passages, now, horizon=timedelta(minutes=30))

    assert upcoming
    assert all(now <= p.when <= now + timedelta(minutes=30) for p in upcoming)


def test_next_passages_honours_the_limit(feed, site):
    passages = predict_passages(feed, site, WEEKDAY)
    now = datetime(2026, 9, 9, 0, 0, tzinfo=PARIS)
    assert len(next_passages(passages, now, horizon=timedelta(days=1), limit=2)) == 2


def test_service_days_around_covers_the_previous_night():
    days = service_days_around(datetime(2026, 9, 10, 0, 10, tzinfo=PARIS))
    assert days == [date(2026, 9, 9), date(2026, 9, 10)]


def test_describe_is_human_readable(feed, site):
    passage = by_trip(predict_passages(feed, site, WEEKDAY))["T:SE:1"][0]
    text = passage.describe()
    assert "Narbonne" in text and "±" in text and "km/h" in text
