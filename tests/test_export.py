"""Tests de l'export statique lu par la PWA publiée sans serveur.

L'invariant qui compte : à partir de l'export, le client calcule
`anchor + retard + offset_s`. Ce calcul doit redonner exactement l'heure que
`predict_passages` aurait prédite avec ce même retard — sinon l'application
publiée et la ligne de commande annonceraient deux heures différentes.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from nexttraintosee.cli import main
from nexttraintosee.export import EXPORT_FORMAT, build_timetable
from nexttraintosee.gtfs import GtfsFeed
from nexttraintosee.predict import predict_passages

PARIS = ZoneInfo("Europe/Paris")
DAY = date(2026, 9, 9)  # mercredi : jour ouvré du mini-GTFS
GENERATED_AT = datetime(2026, 9, 9, 3, 0, tzinfo=PARIS)


@pytest.fixture
def feed(gtfs_zip) -> GtfsFeed:
    return GtfsFeed.load(gtfs_zip, anchor_name="Toulouse Matabiau")


@pytest.fixture
def timetable(feed, site) -> dict:
    return build_timetable(feed, site, DAY, days=2, generated_at=GENERATED_AT)


def _key(trip_id: str, direction: str, anchor: int) -> tuple[str, str, int]:
    return (trip_id, direction, anchor)


def test_anchor_plus_delay_plus_offset_reproduces_the_prediction(timetable, feed, site):
    entries = {_key(e["trip_id"], e["direction"], e["anchor"]): e for e in timetable["passages"]}
    predicted = predict_passages(feed, site, DAY)
    assert predicted, "le mini-GTFS doit produire des passages ce jour-là"

    delay = 137
    delayed = predict_passages(feed, site, DAY, delays_s={p.trip_id: delay for p in predicted})
    for passage in delayed:
        entry = entries[_key(passage.trip_id, passage.direction.value, int(passage.anchor_time.timestamp()))]
        rebuilt = entry["anchor"] + delay + entry["offset_s"]
        assert rebuilt == pytest.approx(passage.when.timestamp(), abs=0.1)

        announce = rebuilt - entry["uncertainty_s"] - entry["lead_margin_s"]
        assert announce == pytest.approx(passage.announce_at.timestamp(), abs=0.2)


def test_the_previous_service_day_is_included_for_trains_after_midnight(timetable, feed, site):
    eve = predict_passages(feed, site, DAY - timedelta(days=1))
    exported = {(e["trip_id"], e["anchor"]) for e in timetable["passages"]}
    assert {(p.trip_id, int(p.anchor_time.timestamp())) for p in eve} <= exported


def test_the_export_carries_what_the_client_cannot_infer(timetable):
    assert timetable["format"] == EXPORT_FORMAT
    assert timetable["first_day"] == "2026-09-09"
    assert timetable["last_day"] == "2026-09-10"
    # Le retard se lit sur les quais, pas seulement sur la gare parente.
    assert {"SP:MTB:1", "SP:MTB:2"} <= set(timetable["anchor_stop_ids"])
    assert timetable["histogram"]["weekday"]["label"] == "Semaine"

    entry = timetable["passages"][0]
    assert entry["direction_label"] in {"Depuis Matabiau", "Vers Matabiau"}
    assert entry["look"] in {"tunnel", "sud"}
    whens = [e["anchor"] + e["offset_s"] for e in timetable["passages"]]
    assert whens == sorted(whens)


def test_the_export_command_writes_compact_json(tmp_path, gtfs_zip, monkeypatch):
    config = tmp_path / "site.toml"
    config.write_text(
        f"""
[site]
name = "Test"
lat = 43.597833
lon = 1.458194
anchor_station = "Toulouse Matabiau"

[[branches]]
id = "sud"
label = "Sud"
bearing_deg = 184.2

[data]
gtfs_path = "{gtfs_zip}"
database = "{tmp_path / 'journal.sqlite'}"
""",
        encoding="utf-8",
    )
    output = tmp_path / "out" / "timetable.json"

    assert main(["-c", str(config), "export", "-o", str(output), "--days", "3"]) == 0

    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["format"] == EXPORT_FORMAT
    assert data["site"] == "Test"
