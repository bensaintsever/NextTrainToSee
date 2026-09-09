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
    assert "Aucune détection" in capsys.readouterr().out


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
