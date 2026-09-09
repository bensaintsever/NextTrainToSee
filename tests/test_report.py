from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from nexttraintosee.motion import Regime
from nexttraintosee.predict import Branch, Direction, Passage
from nexttraintosee.report import (
    csv_rows,
    french_date,
    hourly_histogram,
    render_histogram,
)

PARIS = ZoneInfo("Europe/Paris")
SE = Branch("se", "Narbonne", bearing_deg=137.5)
SUD = Branch("sud", "Saint-Agne", bearing_deg=184.2)


def passage(hour: int, minute: int = 0, branch: Branch = SE, category: str | None = "ter"):
    when = datetime(2026, 9, 9, hour, minute, tzinfo=PARIS)
    return Passage(
        trip_id=f"t{hour}{minute}",
        when=when,
        uncertainty_s=13.0,
        branch=branch,
        regime=Regime.DEPARTING,
        direction=Direction.OUTBOUND,
        speed_kmh=100.0,
        anchor_time=when - timedelta(seconds=80),
        category_id=category,
    )


def test_every_hour_of_the_range_appears_even_when_empty():
    buckets = hourly_histogram([passage(7)], first_hour=5, last_hour=23)
    assert [b.hour for b in buckets] == list(range(5, 23))
    assert next(b for b in buckets if b.hour == 6).total == 0


def test_passages_are_counted_in_their_hour():
    buckets = {b.hour: b for b in hourly_histogram([passage(7), passage(7, 30), passage(8)])}
    assert buckets[7].total == 2
    assert buckets[8].total == 1


def test_passages_outside_the_range_are_dropped():
    # La nuit ferroviaire est volontairement exclue.
    buckets = hourly_histogram([passage(3), passage(7)], first_hour=5, last_hour=23)
    assert sum(b.total for b in buckets) == 1


def test_counts_are_broken_down_by_branch_and_category():
    buckets = {b.hour: b for b in hourly_histogram(
        [passage(7, 0, SE, "ter"), passage(7, 10, SUD, "ter"), passage(7, 20, SUD, "grandes-lignes")]
    )}
    assert buckets[7].by_branch == {"se": 1, "sud": 2}
    assert buckets[7].by_category == {"ter": 2, "grandes-lignes": 1}


def test_uncategorised_passages_are_labelled():
    buckets = {b.hour: b for b in hourly_histogram([passage(7, 0, SE, None)])}
    assert buckets[7].by_category == {"non classé": 1}


def test_the_mean_interval_follows_the_count():
    buckets = {b.hour: b for b in hourly_histogram([passage(7, m) for m in (0, 15, 30, 45)])}
    assert buckets[7].mean_interval_min == pytest.approx(15.0)
    assert buckets[6].mean_interval_min is None


def test_an_inverted_or_empty_range_is_refused():
    with pytest.raises(ValueError, match="0 <= début < fin <= 24"):
        hourly_histogram([], first_hour=10, last_hour=10)
    with pytest.raises(ValueError, match="0 <= début < fin <= 24"):
        hourly_histogram([], first_hour=5, last_hour=25)


def test_the_chart_scales_to_the_busiest_hour():
    buckets = hourly_histogram(
        [passage(7, m) for m in range(0, 50, 10)] + [passage(8)], first_hour=7, last_hour=9
    )
    lines = render_histogram(buckets, width=10).splitlines()
    assert lines[0].count("█") == 10  # l'heure de pointe remplit la largeur
    assert lines[1].count("█") == 2


def test_the_chart_survives_an_empty_range():
    assert "Aucune heure" in render_histogram([])


def test_the_table_has_one_row_per_hour_and_a_header():
    buckets = hourly_histogram([passage(7), passage(8, 0, SUD)], first_hour=7, last_hour=9)
    rows = csv_rows(buckets)

    assert rows[0][:3] == ["heure", "total", "intervalle_moyen_min"]
    assert "branche_se" in rows[0] and "branche_sud" in rows[0]
    assert len(rows) == 3
    assert rows[1][0] == "07"


def test_the_table_leaves_the_interval_blank_for_empty_hours():
    rows = csv_rows(hourly_histogram([passage(7)], first_hour=7, last_hour=9))
    assert rows[2][1] == "0" and rows[2][2] == ""


def test_dates_are_written_in_french_whatever_the_system_locale():
    assert french_date(date(2026, 9, 9)) == "mercredi 09/09/2026"
    assert french_date(date(2026, 9, 13)) == "dimanche 13/09/2026"
