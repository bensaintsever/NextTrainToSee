from datetime import date
from pathlib import Path

import pytest

from nexttraintosee.gtfs import (
    Calendar,
    GtfsError,
    GtfsFeed,
    normalize_name,
    parse_gtfs_time,
    service_datetime,
)

WEEKDAY = date(2026, 9, 9)   # un mercredi
SUNDAY = date(2026, 9, 13)
HOLIDAY = date(2026, 5, 1)   # exception de calendrier


def load(path: Path) -> GtfsFeed:
    return GtfsFeed.load(path, anchor_name="Toulouse Matabiau")


def test_parse_gtfs_time_handles_times_past_midnight():
    assert parse_gtfs_time("00:00:00") == 0
    assert parse_gtfs_time("08:02:30") == 8 * 3600 + 2 * 60 + 30
    assert parse_gtfs_time("24:20:00") == 87_600
    assert parse_gtfs_time("") is None
    assert parse_gtfs_time("   ") is None


def test_parse_gtfs_time_rejects_malformed_values():
    with pytest.raises(ValueError):
        parse_gtfs_time("08:02")


def test_service_datetime_places_the_time_in_local_zone():
    moment = service_datetime(WEEKDAY, 8 * 3600 + 2 * 60)
    assert (moment.hour, moment.minute) == (8, 2)
    assert moment.utcoffset().total_seconds() == 2 * 3600  # heure d'été


def test_service_datetime_rolls_over_past_midnight():
    moment = service_datetime(WEEKDAY, 87_600)  # 24:20:00
    assert moment.date() == date(2026, 9, 10)
    assert (moment.hour, moment.minute) == (0, 20)


def test_service_datetime_survives_the_dst_switch():
    # Le 25 octobre 2026 la France recule d'une heure à 3 h du matin.
    # La convention « midi moins douze heures » garde 08:00 à 08:00 local.
    moment = service_datetime(date(2026, 10, 25), 8 * 3600)
    assert (moment.hour, moment.minute) == (8, 0)
    assert moment.utcoffset().total_seconds() == 3600


def test_normalize_name_ignores_accents_case_and_punctuation():
    assert normalize_name("Toulouse-Matabiau") == normalize_name("TOULOUSE MATABIAU")
    assert normalize_name("Villefranche-de-Lauragais") == "villefranche de lauragais"


def test_load_keeps_only_trips_calling_at_the_anchor(gtfs_zip):
    feed = load(gtfs_zip)
    # La circulation Montauban -> Colomiers ne passe pas par la gare d'appui :
    # elle est écartée dès le chargement, ce qui est tout l'intérêt du filtrage.
    assert "T:ELSEWHERE" not in feed.trips
    assert set(feed.trips) == {
        "T:SE:1",
        "T:S:1",
        "T:N:1",
        "T:W:1",
        "T:SE:NIGHT",
        "T:SE:SUN",
        "T:BUS:1",
        "T:SE:2",
        "T:SE:THRU",
        "T:S:FAST",
        "T:S:SLOW",
    }


def test_load_reads_the_full_calling_pattern_of_each_trip(gtfs_zip):
    feed = load(gtfs_zip)
    trip = feed.trips["T:N:1"]
    assert [st.stop_id for st in trip.stop_times] == ["SP:MTB_N:1", "SP:MTB:1"]
    assert trip.stop_times[0].departure_s == 7 * 3600 + 31 * 60


def test_stop_times_are_ordered_by_sequence(gtfs_zip):
    feed = load(gtfs_zip)
    for trip in feed.trips.values():
        sequences = [st.stop_sequence for st in trip.stop_times]
        assert sequences == sorted(sequences)


def test_station_resolution_includes_parent_and_platforms(gtfs_zip):
    feed = load(gtfs_zip)
    assert feed.station_stop_ids("Toulouse Matabiau") == {"SA:MTB", "SP:MTB:1", "SP:MTB:2"}


def test_station_resolution_is_accent_insensitive(gtfs_zip):
    feed = load(gtfs_zip)
    assert feed.station_stop_ids("toulouse matabiau") == feed.station_stop_ids("Toulouse-Matabiau")


def test_station_resolution_does_not_leak_into_other_stations(gtfs_zip):
    feed = load(gtfs_zip)
    # « Toulouse Saint-Agne » ne doit pas absorber « Toulouse Matabiau ».
    assert feed.station_stop_ids("Toulouse Saint-Agne") == {"SA:STA", "SP:STA:1"}


def test_unknown_anchor_is_reported_clearly(gtfs_zip):
    with pytest.raises(GtfsError, match="gare d'appui"):
        GtfsFeed.load(gtfs_zip, anchor_name="Gare de Nulle Part")


def test_missing_archive_is_reported_clearly(tmp_path):
    with pytest.raises(GtfsError, match="introuvable"):
        GtfsFeed.load(tmp_path / "absent.zip", anchor_name="Toulouse Matabiau")


def test_feed_without_any_calendar_is_rejected(gtfs_zip_builder):
    path = gtfs_zip_builder("no-calendar.zip", with_calendar=False)
    with pytest.raises(GtfsError, match="calendar"):
        GtfsFeed.load(path, anchor_name="Toulouse Matabiau")


def test_weekday_and_sunday_services_are_distinct(gtfs_zip):
    feed = load(gtfs_zip)
    assert {t.trip_id for t in feed.trips_on(WEEKDAY)} >= {"T:SE:1", "T:S:1"}
    assert "T:SE:SUN" not in {t.trip_id for t in feed.trips_on(WEEKDAY)}
    assert {t.trip_id for t in feed.trips_on(SUNDAY)} == {"T:SE:SUN"}


def test_calendar_exceptions_override_the_weekly_pattern(gtfs_zip):
    feed = load(gtfs_zip)
    # Le 1er mai 2026 est un vendredi : le service semaine est supprimé,
    # le service dimanche est ajouté.
    running = {t.trip_id for t in feed.trips_on(HOLIDAY)}
    assert running == {"T:SE:SUN"}


def test_calendar_ignores_dates_outside_the_validity_window():
    calendar = Calendar(weekly={"S": (frozenset(range(7)), date(2026, 1, 1), date(2026, 1, 31))})
    assert calendar.active_services(date(2026, 1, 15)) == {"S"}
    assert calendar.active_services(date(2026, 2, 1)) == set()


def test_bus_substitutions_are_not_rail(gtfs_zip):
    feed = load(gtfs_zip)
    assert feed.is_rail(feed.trips["T:SE:1"]) is True
    assert feed.is_rail(feed.trips["T:BUS:1"]) is False


def test_technical_stops_are_flagged(gtfs_zip):
    feed = load(gtfs_zip)
    technical = feed.trips["T:SE:SUN"].stop_times[1]
    assert technical.stop_id == "SP:STA:1"
    assert technical.is_revenue_stop is False
    assert feed.trips["T:SE:SUN"].stop_times[0].is_revenue_stop is True


def test_index_of_stop_finds_the_anchor_position(gtfs_zip):
    feed = load(gtfs_zip)
    anchor = feed.station_stop_ids("Toulouse Matabiau")
    assert feed.trips["T:SE:1"].index_of_stop(sorted(anchor)) == 0
    assert feed.trips["T:N:1"].index_of_stop(sorted(anchor)) == 1
    assert feed.trips["T:N:1"].index_of_stop(["SP:NOWHERE"]) is None
