from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from nexttraintosee.cli import main
from nexttraintosee.gtfs import GtfsFeed
from nexttraintosee.predict import predict_passages
from nexttraintosee.sensor.replay import synthetic_passage, write_levels


PARIS = ZoneInfo("Europe/Paris")
MORNING = datetime(2026, 9, 9, 8, 0, tzinfo=PARIS)

CONFIG = """
[site]
name = "Site de test"
lat = 43.597833
lon = 1.458194
anchor_station = "Toulouse Matabiau"
anchor_lat = 43.6112
anchor_lon = 1.4535

[profile]
accel_ms2 = 0.5
decel_ms2 = 0.6
line_speed_kmh = 90.0

[[branches]]
id = "se"
label = "Axe Narbonne"
bearing_deg = 137.5
track_distance_m = 1650.0

[[branches]]
id = "sud"
label = "Axe Latour-de-Carol"
bearing_deg = 184.2
track_distance_m = 1500.0

[[branches]]
id = "nord"
label = "Axe Bordeaux"
bearing_deg = 348.6
passes_observer = false

[[branches]]
id = "ouest"
label = "Axe Colomiers"
bearing_deg = 269.8
passes_observer = false

[data]
gtfs_path = "{gtfs}"
database = "{database}"
"""


@pytest.fixture
def config_path(tmp_path: Path, gtfs_zip: Path) -> Path:
    path = tmp_path / "site.toml"
    path.write_text(
        CONFIG.format(gtfs=gtfs_zip, database=tmp_path / "journal.sqlite"), encoding="utf-8"
    )
    return path


def run(config_path: Path, *argv: str) -> int:
    return main(["-c", str(config_path), *argv])


def test_doctor_reports_the_site_and_the_gtfs(config_path, capsys):
    assert run(config_path, "doctor") == 0
    out = capsys.readouterr().out
    assert "Site de test" in out
    assert "Toulouse Matabiau" in out
    assert "se, sud" in out


def test_doctor_flags_a_missing_gtfs(tmp_path, capsys):
    path = tmp_path / "site.toml"
    path.write_text(
        CONFIG.format(gtfs=tmp_path / "absent.zip", database=tmp_path / "j.sqlite"),
        encoding="utf-8",
    )
    assert run(path, "doctor") == 0
    assert "absent" in capsys.readouterr().out


def test_next_lists_the_upcoming_passages(config_path, capsys):
    assert run(config_path, "next", "--no-realtime", "--at", MORNING.isoformat()) == 0
    out = capsys.readouterr().out
    assert "Narbonne" in out
    assert "dans" in out and "min" in out


def test_next_respects_the_horizon(config_path, capsys):
    assert run(config_path, "next", "--no-realtime", "--at", MORNING.isoformat(), "--horizon", "1") == 0
    assert "Aucun passage prédit" in capsys.readouterr().out


def test_next_respects_the_limit(config_path, capsys):
    assert run(
        config_path, "next", "--no-realtime", "--at", MORNING.isoformat(), "--horizon", "600", "--limit", "1"
    ) == 0
    lines = [l for l in capsys.readouterr().out.splitlines() if l.strip().startswith("dans")]
    assert len(lines) == 1


def test_next_can_journal_its_predictions(config_path, tmp_path, capsys):
    assert run(config_path, "next", "--no-realtime", "--at", MORNING.isoformat(), "--record") == 0
    assert "prédictions enregistrées" in capsys.readouterr().out
    assert (tmp_path / "journal.sqlite").exists()


def test_next_reports_a_missing_gtfs_clearly(tmp_path, capsys):
    path = tmp_path / "site.toml"
    path.write_text(
        CONFIG.format(gtfs=tmp_path / "absent.zip", database=tmp_path / "j.sqlite"),
        encoding="utf-8",
    )
    assert run(path, "next", "--no-realtime") == 2
    assert "GTFS absente" in capsys.readouterr().err


def test_an_invalid_instant_is_rejected(config_path):
    with pytest.raises(SystemExit):
        run(config_path, "next", "--at", "pas une date")


def test_a_missing_configuration_is_reported(tmp_path, capsys):
    assert main(["-c", str(tmp_path / "absent.toml"), "doctor"]) == 2
    assert "introuvable" in capsys.readouterr().err


def test_replay_detects_a_synthetic_passage(config_path, tmp_path, capsys):
    levels = tmp_path / "levels.csv"
    write_levels(
        levels,
        synthetic_passage(MORNING, duration_s=12.0, period_s=0.5, baseline=0.01, peak=0.2, total_s=180.0),
    )
    assert run(config_path, "replay", str(levels)) == 0
    assert "1 passage(s) détecté(s)" in capsys.readouterr().out


def test_calibrate_needs_observations_first(config_path, capsys):
    assert run(config_path, "calibrate") == 1
    assert "Aucun passage rapporté" in capsys.readouterr().out


def test_calibrate_matches_observations_against_predictions(config_path, gtfs_zip, tmp_path, capsys):
    # 1. On journalise les prédictions de la matinée.
    assert run(config_path, "next", "--no-realtime", "--at", MORNING.isoformat(), "--horizon", "600", "--record") == 0

    # 2. On fabrique un enregistrement où le train passe 20 s après l'heure prédite.
    feed = GtfsFeed.load(gtfs_zip, anchor_name="Toulouse Matabiau")
    from nexttraintosee.config import load_config

    site = load_config(config_path).site
    predicted = sorted(predict_passages(feed, site, MORNING.date()), key=lambda p: p.when)
    assert predicted, "le mini-GTFS doit produire au moins un passage"
    observed_at = predicted[0].when + timedelta(seconds=20)

    levels = tmp_path / "levels.csv"
    write_levels(
        levels,
        synthetic_passage(
            observed_at - timedelta(seconds=90),
            duration_s=12.0,
            period_s=0.5,
            baseline=0.01,
            peak=0.2,
            total_s=180.0,
        ),
    )
    assert run(config_path, "replay", str(levels), "--record") == 0

    # 3. La calibration doit retrouver ce décalage de ~20 s.
    until = (observed_at + timedelta(hours=1)).isoformat()
    assert run(config_path, "calibrate", "--until", until, "--days", "1") == 0
    out = capsys.readouterr().out
    assert "1 appariements" in out
    assert "Recalage" in out
    assert "+20s" in out or "+19s" in out or "+21s" in out


def test_calibrate_reports_unexplained_detections(config_path, tmp_path, capsys):
    assert run(config_path, "next", "--no-realtime", "--at", MORNING.isoformat(), "--horizon", "600", "--record") == 0

    # Un passage à 3 h du matin ne correspond à aucun train du mini-GTFS.
    ghost = datetime(2026, 9, 9, 3, 0, tzinfo=PARIS)
    levels = tmp_path / "ghost.csv"
    write_levels(
        levels,
        synthetic_passage(ghost, duration_s=25.0, period_s=0.5, baseline=0.01, peak=0.2, total_s=200.0),
    )
    assert run(config_path, "replay", str(levels), "--record") == 0

    until = datetime(2026, 9, 9, 23, 0, tzinfo=PARIS).isoformat()
    assert run(config_path, "calibrate", "--until", until, "--days", "1") == 0
    out = capsys.readouterr().out
    assert "hors horaires publics" in out
    assert "fret" in out


# -- diagnostic des dépendances optionnelles ---------------------------------


def test_dependency_status_reports_an_available_module():
    from nexttraintosee.cli import dependency_status

    assert dependency_status("json", "realtime") == "disponible"


def test_dependency_status_points_at_the_right_extra():
    from nexttraintosee.cli import dependency_status

    status = dependency_status("module.qui.nexiste.pas", "sensor")
    assert 'pip install "nexttraintosee[sensor]"' in status


def test_dependency_status_distinguishes_installed_but_broken(monkeypatch):
    # sounddevice s'installe sans PortAudio et lève alors une OSError, pas une
    # ImportError : le diagnostic doit dire « installé mais inutilisable »,
    # pas « absent », faute de quoi le remède proposé est le mauvais.
    import importlib

    from nexttraintosee import cli

    def explode(name):
        raise OSError("PortAudio library not found")

    monkeypatch.setattr(importlib, "import_module", explode)
    status = cli.dependency_status("sounddevice", "sensor")

    assert "installé mais inutilisable" in status
    assert "PortAudio" in status
    assert "pip install" not in status


def test_doctor_survives_a_broken_optional_dependency(config_path, monkeypatch, capsys):
    import importlib

    def explode(name):
        raise OSError("PortAudio library not found")

    monkeypatch.setattr(importlib, "import_module", explode)
    assert run(config_path, "doctor") == 0
    assert "installé mais inutilisable" in capsys.readouterr().out


# -- résolution des voies (tracks) -------------------------------------------


def _overpass_payload():
    """Réponse Overpass synthétique : deux lignes divergeant au sud de la gare.

    Reproduit la configuration toulousaine — l'axe de Sète part au sud-est,
    celui de Bayonne plein sud — pour vérifier que chaque branche est rattachée
    au bon corridor.
    """
    from nexttraintosee.geo import from_local_xy, haversine_m, initial_bearing_deg, to_local_xy

    observer = (43.597833, 1.458194)
    anchor = (43.6112, 1.4535)

    def far(bearing_deg: float, distance_m: float):
        import math

        rad = math.radians(bearing_deg)
        return from_local_xy(anchor, (distance_m * math.sin(rad), distance_m * math.cos(rad)))

    def offset(east_m: float):
        x, y = to_local_xy(observer, observer)
        return from_local_xy(observer, (x + east_m, y))

    def way(osm_id, name, points):
        return {
            "type": "way",
            "id": osm_id,
            "tags": {"railway": "rail", "name": name, "maxspeed": "120", "usage": "main"},
            "geometry": [{"lat": p[0], "lon": p[1]} for p in points],
        }

    sete = [anchor, offset(20.0), far(137.5, 8000.0)]
    bayonne = [anchor, offset(-380.0), far(184.2, 8000.0)]

    assert haversine_m(observer, offset(20.0)) < 40
    assert 130 < initial_bearing_deg(anchor, sete[-1]) < 145

    return {
        "elements": [
            way(1, "Ligne de Bordeaux-Saint-Jean à Sète-Ville", sete),
            way(2, "Ligne de Toulouse à Bayonne", bayonne),
            {
                "type": "node",
                "id": 10,
                "lat": anchor[0],
                "lon": anchor[1],
                "tags": {"railway": "station", "name": "Toulouse Matabiau"},
            },
        ]
    }


@pytest.fixture
def tracks_config(tmp_path: Path, gtfs_zip: Path) -> Path:
    """Configuration dont le cache Overpass est pré-rempli, pour tester hors ligne."""
    import json

    from nexttraintosee.osm import OverpassClient, build_query

    path = tmp_path / "site.toml"
    path.write_text(
        CONFIG.format(gtfs=gtfs_zip, database=tmp_path / "journal.sqlite")
        + f'\noverpass_cache = "{tmp_path / "overpass"}"\n',
        encoding="utf-8",
    )

    from nexttraintosee.config import load_config

    config = load_config(path)
    client = OverpassClient(cache_dir=config.data.overpass_cache)
    query = build_query(
        config.site.position, config.search_radius_m, anchor=config.site.anchor_position
    )
    cache_file = client.cache_path(query)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(_overpass_payload()), encoding="utf-8")
    return path


def test_tracks_lists_the_corridors(tracks_config, capsys):
    assert run(tracks_config, "tracks") == 0
    out = capsys.readouterr().out

    assert "Sète-Ville" in out and "Bayonne" in out
    assert "tronçon(s) OSM" in out
    assert "120 km/h" in out


def test_tracks_measures_the_distance_along_the_track(tracks_config, capsys):
    assert run(tracks_config, "tracks") == 0
    out = capsys.readouterr().out

    assert "par la voie jusqu'à Toulouse Matabiau" in out
    # Jamais plus court qu'à vol d'oiseau : c'était le symptôme du bug.
    assert "non mesurable" not in out


def _mapping_blocks(output: str) -> dict[str, list[str]]:
    """Découpe la section « Correspondance » en un bloc de lignes par branche."""
    section = output[output.index("Correspondance branches") : output.index("À recopier")]
    blocks: dict[str, list[str]] = {}
    current = None
    for line in section.splitlines():
        if "(cap" in line:
            current = line.strip().split()[0]
            blocks[current] = []
        elif current and line.strip().startswith("c"):
            blocks[current].append(line)
    return blocks


def test_tracks_maps_each_branch_to_its_corridor(tracks_config, capsys):
    assert run(tracks_config, "tracks") == 0
    blocks = _mapping_blocks(capsys.readouterr().out)

    assert "Sète-Ville" in "".join(blocks["se"])
    assert "Bayonne" in "".join(blocks["sud"])
    # Les axes nord et ouest n'existent pas dans ce jeu de données.
    assert blocks["nord"] == []


def test_tracks_lists_every_corridor_carrying_a_branch(tracks_config, capsys):
    # Un axe peut être porté par plusieurs faisceaux : il faut tous les voir,
    # pas seulement le premier.
    assert run(tracks_config, "tracks") == 0
    blocks = _mapping_blocks(capsys.readouterr().out)
    assert len(blocks["se"]) >= 1
    assert all("repart au cap" in line for line in blocks["se"])


def test_tracks_emits_a_ready_to_paste_toml_block(tracks_config, capsys):
    import tomllib

    assert run(tracks_config, "tracks") == 0
    out = capsys.readouterr().out

    block = out[out.index("[[branches]]") :]
    parsed = tomllib.loads(block)
    branches = {b["id"]: b for b in parsed["branches"]}

    assert set(branches) == {"se", "sud", "nord", "ouest"}
    assert branches["se"]["passes_observer"] is True
    assert branches["se"]["track_distance_m"] > 1500
    # Aucun corridor ne part vers le nord dans ce jeu de données.
    assert branches["nord"]["passes_observer"] is False
    assert "track_distance_m" not in branches["nord"]


def test_tracks_reports_when_no_track_is_near(tmp_path, gtfs_zip, capsys):
    import json

    from nexttraintosee.config import load_config
    from nexttraintosee.osm import OverpassClient, build_query

    path = tmp_path / "empty.toml"
    path.write_text(
        CONFIG.format(gtfs=gtfs_zip, database=tmp_path / "j.sqlite")
        + f'\noverpass_cache = "{tmp_path / "overpass"}"\n',
        encoding="utf-8",
    )
    config = load_config(path)
    client = OverpassClient(cache_dir=config.data.overpass_cache)
    query = build_query(
        config.site.position, config.search_radius_m, anchor=config.site.anchor_position
    )
    cache_file = client.cache_path(query)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps({"elements": []}), encoding="utf-8")

    assert run(path, "tracks") == 1
    assert "Aucune voie ferrée" in capsys.readouterr().out


def test_toml_keeps_a_manual_passes_observer_but_flags_it():
    # Une desserte peut passer devant le point sans qu'aucun corridor ne parte
    # dans la direction de son arrêt voisin — c'est le cas des trains de
    # Colomiers, annoncés à l'ouest mais partant au sud. L'outil ne doit pas
    # effacer cette décision humaine, seulement la signaler.
    from nexttraintosee.cli import render_branch_toml
    from nexttraintosee.predict import Branch

    visible = Branch("ouest", "Axe Colomiers", bearing_deg=269.8, passes_observer=True)
    hidden = Branch("nord", "Axe Bordeaux", bearing_deg=348.6, passes_observer=False)

    block = render_branch_toml([(visible, []), (hidden, [])])

    assert "passes_observer = true" in block and "à confirmer" in block
    assert "passes_observer = false" in block


def test_toml_stays_parseable_when_branches_are_flagged():
    import tomllib

    from nexttraintosee.cli import render_branch_toml
    from nexttraintosee.predict import Branch

    block = render_branch_toml(
        [(Branch("ouest", "Axe Colomiers", bearing_deg=269.8, passes_observer=True), [])]
    )
    parsed = tomllib.loads(block)
    assert parsed["branches"][0]["passes_observer"] is True


# -- confrontation du modèle aux horaires -------------------------------------


def test_validate_compares_the_model_to_the_fastest_schedule(tracks_config, capsys):
    assert run(tracks_config, "validate", "--min-trips", "1", "--day", "2026-09-09") == 0
    out = capsys.readouterr().out

    assert "horaire le plus rapide" in out
    assert "marge horaire médiane" in out
    assert "Saint-Agne" in out


def test_validate_marks_segments_that_span_the_observer(tracks_config, capsys):
    assert run(tracks_config, "validate", "--min-trips", "1", "--day", "2026-09-09") == 0
    out = capsys.readouterr().out
    assert "qui encadrent le point" in out


def test_validate_can_suggest_a_line_speed_per_branch(tracks_config, capsys):
    import tomllib

    assert run(tracks_config, "validate", "--min-trips", "1", "--day", "2026-09-09", "--fit") == 0
    out = capsys.readouterr().out

    block = out[out.index("[[branches]]") : out.index("Lecture :")]
    parsed = tomllib.loads(block)
    assert parsed["branches"]
    assert all("line_speed_kmh" in b for b in parsed["branches"])


def test_validate_refuses_to_fit_on_segments_without_a_tight_run(tracks_config, capsys):
    # L'axe sud-est du mini-GTFS n'a qu'une circulation : aucune étendue, donc
    # son minimum n'est qu'une allocation standard et ne peut rien caler.
    assert run(tracks_config, "validate", "--min-trips", "1", "--day", "2026-09-09", "--fit") == 0
    out = capsys.readouterr().out

    block = out[out.index("branche par branche") :]
    assert 'id = "se"' not in block
    assert 'id = "sud"' in block


def test_validate_needs_enough_trips_to_be_meaningful(tracks_config, capsys):
    # Le mini-GTFS n'a qu'une circulation par segment.
    assert run(tracks_config, "validate", "--min-trips", "50", "--day", "2026-09-09") == 1
    assert "Aucun segment exploitable" in capsys.readouterr().out


def test_validate_reports_a_missing_gtfs(tmp_path, capsys):
    path = tmp_path / "site.toml"
    path.write_text(
        CONFIG.format(gtfs=tmp_path / "absent.zip", database=tmp_path / "j.sqlite"),
        encoding="utf-8",
    )
    assert run(path, "validate") == 2
    assert "GTFS absente" in capsys.readouterr().err


def test_validate_says_which_categories_it_cannot_calibrate(tracks_config, tmp_path, capsys):
    path = tmp_path / "with-categories.toml"
    path.write_text(
        tracks_config.read_text(encoding="utf-8")
        + '\n[[categories]]\nid = "ter"\nlabel = "TER"\npattern = "Narbonne|Latour|Auch"\n'
        + '\n[[categories]]\nid = "gl"\nlabel = "Grandes lignes"\npattern = "Colomiers"\n',
        encoding="utf-8",
    )
    assert run(path, "validate", "--min-trips", "1", "--day", "2026-09-09") == 0
    out = capsys.readouterr().out

    assert "par type de matériel" in out
    assert "aucun segment mesurable" in out


def test_validate_reports_the_spread_of_each_segment(tracks_config, capsys):
    assert run(tracks_config, "validate", "--min-trips", "1", "--day", "2026-09-09") == 0
    assert "étendue" in capsys.readouterr().out


# -- répartition horaire -------------------------------------------------------


def test_histogram_shows_one_line_per_hour(config_path, capsys):
    assert run(config_path, "histogram", "--day", "2026-09-09") == 0
    out = capsys.readouterr().out

    assert "mercredi 09/09/2026" in out
    for hour in ("05 h", "12 h", "22 h"):
        assert hour in out
    # La nuit ferroviaire est exclue par défaut.
    assert "02 h" not in out


def _histogram_hours(output: str) -> list[str]:
    """Heures effectivement tracées, en ignorant l'en-tête qui cite la plage."""
    return [
        line.split()[0]
        for line in output.splitlines()
        if line.startswith("  ") and line.strip()[:2].isdigit() and " h " in line
    ]


def test_histogram_range_is_adjustable(config_path, capsys):
    assert run(config_path, "histogram", "--day", "2026-09-09", "--first-hour", "8",
               "--last-hour", "10") == 0
    assert _histogram_hours(capsys.readouterr().out) == ["08", "09"]


def test_histogram_rejects_an_impossible_range(config_path, capsys):
    assert run(config_path, "histogram", "--first-hour", "12", "--last-hour", "12") == 2
    assert "Plage horaire" in capsys.readouterr().err


def test_histogram_can_write_a_table(config_path, tmp_path, capsys):
    import csv as csv_module

    target = tmp_path / "sortie" / "histogramme.csv"
    assert run(config_path, "histogram", "--day", "2026-09-09", "--csv", str(target)) == 0

    assert target.exists()
    rows = list(csv_module.reader(target.open(encoding="utf-8")))
    assert rows[0][0] == "heure"
    assert len(rows) == 19  # en-tête plus dix-huit heures


def test_next_can_announce_when_to_be_ready(config_path, capsys):
    assert run(config_path, "next", "--no-realtime", "--at", MORNING.isoformat(), "--watch") == 0
    out = capsys.readouterr().out
    assert "guetter dès" in out
    assert "passage vers" in out


def test_a_day_outside_the_feed_is_flagged(config_path, capsys):
    assert run(config_path, "histogram", "--day", "2027-06-01") == 0
    captured = capsys.readouterr()
    assert "hors du flux" in captured.err
    assert "pas une absence de circulation" in captured.err


def test_histogram_can_compare_a_whole_week(config_path, capsys):
    assert run(config_path, "histogram", "--day", "2026-09-09", "--week") == 0
    out = capsys.readouterr().out

    assert "Sur la semaine" in out
    for day in ("mercredi 09/09/2026", "samedi 12/09/2026", "dimanche 13/09/2026"):
        assert day in out


def test_days_outside_the_feed_are_marked_in_the_weekly_view(config_path, capsys):
    # Le mini-GTFS s'arrête au 31/12/2026 : la semaine à cheval le montre.
    assert run(config_path, "histogram", "--day", "2026-12-29", "--week") == 0
    assert "hors du flux" in capsys.readouterr().out


# -- observations rapportées ---------------------------------------------------


def test_an_observation_is_bound_to_the_predicted_passage(config_path, gtfs_zip, capsys):
    from nexttraintosee.config import load_config
    from nexttraintosee.gtfs import GtfsFeed
    from nexttraintosee.predict import predict_passages

    assert run(config_path, "next", "--no-realtime", "--at", MORNING.isoformat(),
               "--horizon", "600", "--record") == 0
    capsys.readouterr()

    feed = GtfsFeed.load(gtfs_zip, anchor_name="Toulouse Matabiau")
    site = load_config(config_path).site
    predicted = sorted(predict_passages(feed, site, MORNING.date()), key=lambda p: p.when)
    seen_at = predicted[0].when + timedelta(seconds=18)

    assert run(config_path, "observe", seen_at.isoformat()) == 0
    out = capsys.readouterr().out
    assert "Enregistré" in out
    assert "rattaché à" in out
    assert "+18 s" in out


def test_two_nearby_passages_leave_the_observation_unbound(config_path, gtfs_zip, capsys):
    from nexttraintosee.config import load_config
    from nexttraintosee.gtfs import GtfsFeed
    from nexttraintosee.predict import predict_passages

    assert run(config_path, "next", "--no-realtime", "--at", MORNING.isoformat(),
               "--horizon", "600", "--record") == 0
    capsys.readouterr()

    feed = GtfsFeed.load(gtfs_zip, anchor_name="Toulouse Matabiau")
    site = load_config(config_path).site
    predicted = sorted(predict_passages(feed, site, MORNING.date()), key=lambda p: p.when)
    # À mi-chemin entre deux passages : l'attribution serait un coup de dé.
    middle = predicted[0].when + (predicted[1].when - predicted[0].when) / 2

    assert run(config_path, "observe", middle.isoformat(), "--tolerance", "900") == 0
    out = capsys.readouterr().out
    assert "deux passages sont à portée" in out
    assert "non liée" in out


def test_an_observation_without_any_prediction_is_still_kept(config_path, capsys):
    assert run(config_path, "observe", "2026-09-09T03:00:00") == 0
    out = capsys.readouterr().out
    assert "Enregistré" in out
    assert "aucun passage prédit à portée" in out


def test_a_missed_passage_can_be_reported(config_path, capsys):
    assert run(config_path, "observe", "2026-09-09T08:05:00", "--not-seen") == 0
    assert "non passé" in capsys.readouterr().out


def test_observations_feed_the_calibration(config_path, gtfs_zip, capsys):
    from nexttraintosee.config import load_config
    from nexttraintosee.gtfs import GtfsFeed
    from nexttraintosee.predict import predict_passages

    assert run(config_path, "next", "--no-realtime", "--at", MORNING.isoformat(),
               "--horizon", "600", "--record") == 0

    feed = GtfsFeed.load(gtfs_zip, anchor_name="Toulouse Matabiau")
    site = load_config(config_path).site
    predicted = sorted(predict_passages(feed, site, MORNING.date()), key=lambda p: p.when)
    for passage in predicted[:3]:
        assert run(config_path, "observe", (passage.when + timedelta(seconds=25)).isoformat()) == 0
    capsys.readouterr()

    until = (predicted[-1].when + timedelta(hours=1)).isoformat()
    assert run(config_path, "calibrate", "--until", until, "--days", "1") == 0
    out = capsys.readouterr().out
    assert "3 rapports" in out
    assert "+25s" in out or "+24s" in out or "+26s" in out


# -- nom d'hôte mDNS annoncé par `serve` -------------------------------------


def test_mdns_hostname_adds_the_suffix_when_absent(monkeypatch):
    from nexttraintosee.cli import _mdns_hostname
    import socket

    monkeypatch.setattr(socket, "gethostname", lambda: "MacBookAir")
    assert _mdns_hostname() == "MacBookAir.local"


def test_mdns_hostname_does_not_double_the_suffix(monkeypatch):
    # Sur macOS, gethostname() renvoie déjà un nom en « .local » : ajouter le
    # suffixe sans condition produirait « machine.local.local », injoignable
    # depuis un iPhone. C'est le bug réellement observé sur la machine de Benjamin.
    from nexttraintosee.cli import _mdns_hostname
    import socket

    monkeypatch.setattr(socket, "gethostname", lambda: "MacBook-Air-de-Benjamin.local")
    assert _mdns_hostname() == "MacBook-Air-de-Benjamin.local"
