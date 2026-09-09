import tomllib
from pathlib import Path

import pytest

from nexttraintosee.config import ConfigError, load_config, parse_config

MINIMAL = """
[site]
name = "Test"
lat = 43.597833
lon = 1.458194
anchor_station = "Toulouse Matabiau"

[[branches]]
id = "se"
bearing_deg = 137.5
"""


def parse(text: str, base_dir: Path | None = None):
    return parse_config(tomllib.loads(text), base_dir=base_dir)


def test_minimal_configuration_is_accepted():
    config = parse(MINIMAL)
    assert config.site.position == (43.597833, 1.458194)
    assert config.site.branches[0].branch_id == "se"
    assert config.site.branches[0].label == "se"
    assert config.site.branches[0].passes_observer is True


def test_defaults_are_applied_when_sections_are_absent():
    config = parse(MINIMAL)
    assert config.site.profile.accel_ms2 == 0.5
    assert config.sensor.trigger_db == 9.0
    assert config.search_radius_m == 400.0


def test_profile_and_sensor_sections_override_defaults():
    config = parse(
        MINIMAL
        + """
[profile]
accel_ms2 = 0.8
line_speed_kmh = 120

[sensor]
trigger_db = 12
refractory_s = 30
"""
    )
    assert config.site.profile.accel_ms2 == 0.8
    assert config.site.profile.line_speed_kmh == 120.0
    assert config.site.profile.decel_ms2 == 0.6  # non redéfini
    assert config.sensor.trigger_db == 12.0
    assert config.sensor.refractory_s == 30.0


def test_anchor_position_is_optional_but_read_when_present():
    assert parse(MINIMAL).site.anchor_position is None
    config = parse(
        MINIMAL.replace(
            'anchor_station = "Toulouse Matabiau"',
            'anchor_station = "Toulouse Matabiau"\nanchor_lat = 43.6112\nanchor_lon = 1.4535',
        )
    )
    assert config.site.anchor_position == (43.6112, 1.4535)


def test_branch_options_are_read():
    config = parse(
        MINIMAL
        + """
[[branches]]
id = "nord"
label = "Axe Bordeaux"
bearing_deg = 348.6
passes_observer = false
tolerance_deg = 25
track_distance_m = 1650
line_speed_kmh = 60
"""
    )
    nord = config.site.branches[1]
    assert nord.label == "Axe Bordeaux"
    assert nord.passes_observer is False
    assert nord.tolerance_deg == 25.0
    assert nord.track_distance_m == 1650.0
    assert nord.line_speed_kmh == 60.0


def test_missing_site_section_is_reported():
    with pytest.raises(ConfigError, match=r"\[site\]"):
        parse('[[branches]]\nid = "se"\nbearing_deg = 1')


def test_missing_mandatory_site_key_is_reported():
    with pytest.raises(ConfigError, match="anchor_station"):
        parse(MINIMAL.replace('anchor_station = "Toulouse Matabiau"', ""))


def test_configuration_without_branches_is_rejected():
    with pytest.raises(ConfigError, match="branche"):
        parse('[site]\nlat = 1\nlon = 2\nanchor_station = "X"')


def test_configuration_where_no_branch_is_visible_is_rejected():
    with pytest.raises(ConfigError, match="passes_observer"):
        parse(MINIMAL + "passes_observer = false\n")


def test_branch_without_bearing_is_reported():
    with pytest.raises(ConfigError, match="bearing_deg"):
        parse('[site]\nlat = 1\nlon = 2\nanchor_station = "X"\n\n[[branches]]\nid = "se"')


def test_relative_paths_resolve_against_the_config_file(tmp_path):
    config = parse(MINIMAL + '\n[data]\ngtfs_path = "gtfs.zip"\n', base_dir=tmp_path / "conf")
    assert config.data.gtfs_path == tmp_path / "conf" / "gtfs.zip"


def test_parent_references_are_normalised(tmp_path):
    config = parse(MINIMAL + '\n[data]\ngtfs_path = "../data/gtfs.zip"\n', base_dir=tmp_path / "conf")
    assert config.data.gtfs_path == tmp_path / "data" / "gtfs.zip"
    assert ".." not in str(config.data.gtfs_path)


def test_absolute_paths_are_left_alone(tmp_path):
    absolute = tmp_path / "elsewhere.zip"
    config = parse(MINIMAL + f'\n[data]\ngtfs_path = "{absolute}"\n', base_dir=tmp_path / "conf")
    assert config.data.gtfs_path == absolute


def test_missing_file_is_reported(tmp_path):
    with pytest.raises(ConfigError, match="introuvable"):
        load_config(tmp_path / "absent.toml")


def test_malformed_toml_is_reported(tmp_path):
    path = tmp_path / "broken.toml"
    path.write_text("[site\nlat = 1", encoding="utf-8")
    with pytest.raises(ConfigError, match="TOML invalide"):
        load_config(path)


def test_the_shipped_toulouse_configuration_loads():
    config = load_config(Path(__file__).resolve().parents[1] / "config" / "toulouse-guilhemery.toml")
    assert config.site.anchor_station == "Toulouse Matabiau"
    visible = [b.branch_id for b in config.site.branches if b.passes_observer]
    # Le point est dans le tronc commun au sud de Matabiau : tout ce qui part
    # vers le sud passe devant, y compris les dessertes de Colomiers dont le cap
    # est pourtant plein ouest. Seul l'axe nord est exclu.
    assert visible == ["se", "sud", "ouest"]
    assert config.site.profile.line_speed_kmh == 120.0
