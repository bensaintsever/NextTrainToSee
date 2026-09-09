from datetime import date

import pytest

from nexttraintosee.geo import haversine_m
from nexttraintosee.gtfs import GtfsFeed
from nexttraintosee.osm import RailWay, build_corridors
from nexttraintosee.validate import (
    SegmentCheck,
    check_segments,
    collect_segment_times,
    format_duration,
    place_stop_on_corridors,
)

from conftest import MATABIAU, OBSERVER

WEEKDAY = date(2026, 9, 9)
SAINT_AGNE = (43.57972, 1.45028)
COLOMIERS = (43.6108, 1.3350)


@pytest.fixture
def feed(gtfs_zip) -> GtfsFeed:
    return GtfsFeed.load(gtfs_zip, anchor_name="Toulouse Matabiau")


@pytest.fixture
def corridors():
    """Un corridor passant exactement par la gare, le point, puis Saint-Agne."""
    way = RailWay(
        osm_id=1,
        geometry=(MATABIAU, OBSERVER, SAINT_AGNE),
        tags={"railway": "rail", "name": "Ligne de Toulouse à Bayonne"},
    )
    return build_corridors([way], OBSERVER, max_distance_m=400)


# -- placement d'un arrêt sur les corridors -----------------------------------


def test_a_stop_on_the_line_is_placed_with_its_track_distance(corridors):
    placed = place_stop_on_corridors(SAINT_AGNE, corridors, MATABIAU, OBSERVER)

    assert placed is not None
    corridor, length_m, covers = placed
    assert corridor.corridor_id == "c1"
    # La distance suit la voie : plus longue que le vol d'oiseau car elle passe
    # par le point d'observation, qui n'est pas aligné.
    assert length_m > haversine_m(MATABIAU, SAINT_AGNE)
    assert covers is True


def test_a_stop_far_from_every_corridor_is_not_placed(corridors):
    assert place_stop_on_corridors(COLOMIERS, corridors, MATABIAU, OBSERVER) is None


def test_a_stop_on_the_wrong_side_does_not_cover_the_observer(corridors):
    # Un arrêt situé au nord de la gare : le point, au sud, n'est pas entre les deux.
    north = (43.65, 1.4535)
    placed = place_stop_on_corridors(north, corridors, MATABIAU, OBSERVER)
    if placed is not None:
        assert placed[2] is False


# -- extraction des temps horaires --------------------------------------------


def test_segment_times_are_collected_for_each_neighbour(feed):
    times = collect_segment_times(feed, feed.station_stop_ids("Toulouse Matabiau"), WEEKDAY)

    assert "Toulouse Saint-Agne" in times
    # T:S:1 part de Matabiau à 08:12 et arrive à Saint-Agne à 08:18.
    assert 360.0 in times["Toulouse Saint-Agne"]


def test_inbound_segments_are_measured_from_the_neighbour(feed):
    times = collect_segment_times(feed, feed.station_stop_ids("Toulouse Matabiau"), WEEKDAY)
    # T:N:1 part de Montauban à 07:31 et arrive à Matabiau à 08:15 : 44 minutes.
    assert 44 * 60.0 in times["Montauban-Ville-Bourbon"]


def test_segments_without_usable_times_are_skipped(feed):
    times = collect_segment_times(feed, feed.station_stop_ids("Toulouse Matabiau"), WEEKDAY)
    assert all(all(t > 0 for t in values) for values in times.values())


# -- confrontation au modèle ---------------------------------------------------


def test_check_reports_the_gap_between_model_and_fastest_schedule(feed, site, corridors):
    checks = check_segments(feed, corridors, site, WEEKDAY, min_trips=1)
    by_name = {c.neighbour: c for c in checks}

    assert "Toulouse Saint-Agne" in by_name
    saint_agne = by_name["Toulouse Saint-Agne"]
    assert saint_agne.covers_observer is True
    # Trois marches sur ce segment : 4, 6 et 9 minutes.
    assert saint_agne.trip_count == 3
    assert saint_agne.fastest_s == 240.0
    assert saint_agne.median_s == 360.0
    assert saint_agne.spread_s == 300.0
    assert saint_agne.has_tight_run
    assert saint_agne.modelled_s > 0


def test_check_uses_the_profile_of_its_own_branch(feed, site, corridors):
    from dataclasses import replace

    slow = replace(site, branches=tuple(
        replace(b, line_speed_kmh=40.0) if b.branch_id == "s" else b for b in site.branches
    ))
    fast = replace(site, branches=tuple(
        replace(b, line_speed_kmh=160.0) if b.branch_id == "s" else b for b in site.branches
    ))

    def modelled(configured):
        checks = check_segments(feed, corridors, configured, WEEKDAY, min_trips=1)
        return next(c for c in checks if c.neighbour == "Toulouse Saint-Agne").modelled_s

    assert modelled(slow) > modelled(fast)


def test_check_requires_the_anchor_position(feed, site, corridors):
    from dataclasses import replace

    with pytest.raises(ValueError, match="gare d'appui"):
        check_segments(feed, corridors, replace(site, anchor_position=None), WEEKDAY)


def test_rarely_served_segments_are_left_out(feed, site, corridors):
    # Le mini-GTFS n'a qu'une circulation par segment.
    assert check_segments(feed, corridors, site, WEEKDAY, min_trips=5) == []


# -- verdicts ------------------------------------------------------------------


def _check(fastest_s: float, modelled_s: float) -> SegmentCheck:
    return SegmentCheck(
        neighbour="X",
        length_m=4000.0,
        trip_count=20,
        fastest_s=fastest_s,
        median_s=fastest_s + 120.0,
        modelled_s=modelled_s,
        covers_observer=True,
    )


def test_a_model_matching_the_fastest_schedule_is_consistent():
    assert _check(180.0, 176.0).is_consistent
    assert _check(180.0, 200.0).is_consistent


def test_a_model_faster_than_every_schedule_is_flagged():
    check = _check(300.0, 200.0)
    assert check.verdict == "modèle trop rapide"
    assert check.gap_s == pytest.approx(-100.0)


def test_a_model_far_slower_than_the_schedule_is_flagged():
    assert _check(180.0, 400.0).verdict == "modèle trop lent"


def test_padding_is_measured_against_the_fastest_run():
    assert _check(180.0, 180.0).median_padding_s == pytest.approx(120.0)


def test_implied_speed_is_the_average_of_the_fastest_run():
    # 4000 m en 200 s, soit 72 km/h.
    assert _check(200.0, 200.0).implied_speed_kmh == pytest.approx(72.0)


# -- mise en forme -------------------------------------------------------------


def test_durations_round_on_the_total_not_on_the_seconds():
    # 179,6 s doit donner « 3 min 00 s », jamais « 2 min 60 s ».
    assert format_duration(179.6) == "3 min 00 s"
    assert format_duration(180.0) == "3 min 00 s"
    assert format_duration(0.0) == "0 min 00 s"


def test_negative_durations_keep_their_sign():
    assert format_duration(-92.0) == "-1 min 32 s"


# -- fiabilité du minimum horaire ---------------------------------------------


def test_a_segment_with_varied_timings_has_a_tight_run():
    check = SegmentCheck(
        neighbour="X", length_m=4000.0, trip_count=40, fastest_s=180.0,
        median_s=240.0, modelled_s=180.0, covers_observer=True, spread_s=300.0,
    )
    assert check.has_tight_run


def test_a_segment_where_every_train_takes_the_same_time_has_none():
    # Tous les horaires identiques : c'est une allocation standard reconduite,
    # pas une marche tendue. Le minimum ne dit rien de la limite physique.
    check = SegmentCheck(
        neighbour="X", length_m=4000.0, trip_count=40, fastest_s=300.0,
        median_s=300.0, modelled_s=300.0, covers_observer=True, spread_s=0.0,
    )
    assert not check.has_tight_run


def test_spread_is_measured_on_the_collected_times(feed, site, corridors):
    checks = check_segments(feed, corridors, site, WEEKDAY, min_trips=1)
    assert all(c.spread_s >= 0 for c in checks)


# -- ce que les horaires permettent de caler ----------------------------------


def _with_categories(site):
    from dataclasses import replace

    from nexttraintosee.predict import TrainCategory

    return replace(
        site,
        categories=(
            TrainCategory("ter", "TER", r"Latour|Narbonne|Auch"),
            TrainCategory("gl", "Grandes lignes", r"Colomiers"),
        ),
    )


def test_coverage_counts_segments_per_category(feed, site):
    from nexttraintosee.validate import category_coverage

    coverage = {c.category_id: c for c in category_coverage(
        feed, _with_categories(site), {"Toulouse Saint-Agne"}, WEEKDAY
    )}
    assert "ter" in coverage
    assert coverage["ter"].segment_count > 0


def test_a_category_that_never_stops_nearby_is_not_calibratable(feed, site):
    from nexttraintosee.validate import category_coverage

    coverage = {c.category_id: c for c in category_coverage(
        feed, _with_categories(site), {"Toulouse Saint-Agne"}, WEEKDAY
    )}
    # Les circulations de Colomiers ne desservent pas Saint-Agne.
    assert coverage["gl"].is_calibratable is False
    assert coverage["ter"].is_calibratable is True


def test_unclassified_trips_are_reported_separately(feed, site):
    from nexttraintosee.validate import category_coverage

    coverage = {c.category_id for c in category_coverage(feed, site, set(), WEEKDAY)}
    # Sans catégorie déclarée, tout tombe dans le fourre-tout.
    assert coverage == {"(non classé)"}


def test_coverage_is_ordered_by_weight(feed, site):
    from nexttraintosee.validate import category_coverage

    coverage = category_coverage(feed, _with_categories(site), {"Toulouse Saint-Agne"}, WEEKDAY)
    counts = [c.segment_count for c in coverage]
    assert counts == sorted(counts, reverse=True)
