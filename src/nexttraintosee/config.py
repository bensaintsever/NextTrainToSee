"""Configuration du site d'observation, en TOML.

Un « site » réunit tout ce qui est propre à un point d'observation : ses
coordonnées, la gare d'appui sur laquelle on s'appuie, les branches
ferroviaires qui en partent, le profil de marche et les réglages du capteur.

Le format est TOML, lu par `tomllib` (bibliothèque standard depuis Python 3.11)
pour ne rien ajouter aux dépendances.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .motion import TractionProfile
from .osm import DEFAULT_LOOKAHEAD_M
from .predict import Branch, Site, TrainCategory
from .realtime import SNCF_TRIP_UPDATES_URL


class ConfigError(RuntimeError):
    """Fichier de configuration absent, mal formé ou incomplet."""


@dataclass(frozen=True)
class DataPaths:
    """Où trouver les données et où écrire les résultats."""

    gtfs_path: Path = Path("data/sncf-gtfs.zip")
    trip_updates_url: str = SNCF_TRIP_UPDATES_URL
    overpass_cache: Path = Path("cache/overpass")
    database: Path = Path("data/nexttraintosee.sqlite")


@dataclass(frozen=True)
class SensorSettings:
    """Réglages du détecteur de passage."""

    trigger_db: float = 9.0
    release_db: float = 4.0
    min_duration_s: float = 3.0
    max_duration_s: float = 120.0
    refractory_s: float = 15.0
    baseline_half_life_s: float = 90.0
    sample_rate_hz: int = 16_000
    block_seconds: float = 0.5
    device: str | int | None = None


@dataclass(frozen=True)
class AppConfig:
    """Configuration complète de l'outil."""

    site: Site
    data: DataPaths = field(default_factory=DataPaths)
    sensor: SensorSettings = field(default_factory=SensorSettings)
    search_radius_m: float = 400.0
    branch_lookahead_m: float = DEFAULT_LOOKAHEAD_M
    """Portée de visée au-delà du point, pour distinguer les branches."""


def _require(table: dict[str, Any], key: str, where: str) -> Any:
    if key not in table:
        raise ConfigError(f"clé « {key} » manquante dans [{where}]")
    return table[key]


def parse_config(raw: dict[str, Any], base_dir: Path | None = None) -> AppConfig:
    """Construit la configuration à partir d'un TOML déjà décodé."""
    base_dir = base_dir or Path.cwd()

    site_table = raw.get("site")
    if not site_table:
        raise ConfigError("section [site] manquante")

    profile_table = raw.get("profile", {})
    profile = TractionProfile(
        accel_ms2=float(profile_table.get("accel_ms2", 0.5)),
        decel_ms2=float(profile_table.get("decel_ms2", 0.6)),
        line_speed_kmh=float(profile_table.get("line_speed_kmh", 90.0)),
        sinuosity=float(profile_table.get("sinuosity", 1.05)),
    )

    branch_tables = raw.get("branches", [])
    if not branch_tables:
        raise ConfigError("aucune branche déclarée : ajoutez au moins un [[branches]]")
    branches = tuple(
        Branch(
            branch_id=str(_require(table, "id", "branches")),
            label=str(table.get("label", table["id"])),
            bearing_deg=float(_require(table, "bearing_deg", "branches")),
            passes_observer=bool(table.get("passes_observer", True)),
            tolerance_deg=float(table.get("tolerance_deg", 30.0)),
            track_distance_m=(
                float(table["track_distance_m"]) if "track_distance_m" in table else None
            ),
            line_speed_kmh=(
                float(table["line_speed_kmh"]) if "line_speed_kmh" in table else None
            ),
            approach_speed_kmh=(
                float(table["approach_speed_kmh"]) if "approach_speed_kmh" in table else None
            ),
            corridor_id=table.get("corridor_id"),
        )
        for table in branch_tables
    )
    if not any(branch.passes_observer for branch in branches):
        raise ConfigError(
            "aucune branche ne passe devant le point : au moins une doit avoir "
            "passes_observer = true"
        )

    categories = tuple(
        TrainCategory(
            category_id=str(_require(table, "id", "categories")),
            label=str(table.get("label", table["id"])),
            pattern=str(_require(table, "pattern", "categories")),
            accel_ms2=float(table["accel_ms2"]) if "accel_ms2" in table else None,
            decel_ms2=float(table["decel_ms2"]) if "decel_ms2" in table else None,
            line_speed_kmh=(
                float(table["line_speed_kmh"]) if "line_speed_kmh" in table else None
            ),
        )
        for table in raw.get("categories", [])
    )
    for category in categories:
        try:
            re.compile(category.pattern)
        except re.error as exc:
            raise ConfigError(
                f"motif invalide pour la catégorie « {category.category_id} » : {exc}"
            ) from exc

    anchor_position = None
    if "anchor_lat" in site_table and "anchor_lon" in site_table:
        anchor_position = (float(site_table["anchor_lat"]), float(site_table["anchor_lon"]))

    site = Site(
        name=str(site_table.get("name", "site sans nom")),
        position=(float(_require(site_table, "lat", "site")), float(_require(site_table, "lon", "site"))),
        anchor_station=str(_require(site_table, "anchor_station", "site")),
        anchor_position=anchor_position,
        branches=branches,
        profile=profile,
        categories=categories,
        lead_margin_s=float(site_table.get("lead_margin_s", 0.0)),
    )

    data_table = raw.get("data", {})
    defaults = DataPaths()
    data = DataPaths(
        gtfs_path=_resolve(base_dir, data_table.get("gtfs_path", defaults.gtfs_path)),
        trip_updates_url=str(data_table.get("trip_updates_url", defaults.trip_updates_url)),
        overpass_cache=_resolve(base_dir, data_table.get("overpass_cache", defaults.overpass_cache)),
        database=_resolve(base_dir, data_table.get("database", defaults.database)),
    )

    sensor_table = raw.get("sensor", {})
    sensor_defaults = SensorSettings()
    sensor = SensorSettings(
        trigger_db=float(sensor_table.get("trigger_db", sensor_defaults.trigger_db)),
        release_db=float(sensor_table.get("release_db", sensor_defaults.release_db)),
        min_duration_s=float(sensor_table.get("min_duration_s", sensor_defaults.min_duration_s)),
        max_duration_s=float(sensor_table.get("max_duration_s", sensor_defaults.max_duration_s)),
        refractory_s=float(sensor_table.get("refractory_s", sensor_defaults.refractory_s)),
        baseline_half_life_s=float(
            sensor_table.get("baseline_half_life_s", sensor_defaults.baseline_half_life_s)
        ),
        sample_rate_hz=int(sensor_table.get("sample_rate_hz", sensor_defaults.sample_rate_hz)),
        block_seconds=float(sensor_table.get("block_seconds", sensor_defaults.block_seconds)),
        device=sensor_table.get("device"),
    )

    return AppConfig(
        site=site,
        data=data,
        sensor=sensor,
        search_radius_m=float(site_table.get("search_radius_m", 400.0)),
        branch_lookahead_m=float(site_table.get("branch_lookahead_m", DEFAULT_LOOKAHEAD_M)),
    )


def _resolve(base_dir: Path, value: Path | str) -> Path:
    """Résout un chemin relatif par rapport au fichier de configuration."""
    path = Path(value)
    if path.is_absolute():
        return path
    # normpath plutôt que resolve : on garde un chemin relatif lisible dans les
    # messages, sans les « .. » intermédiaires.
    return Path(os.path.normpath(base_dir / path))


def load_config(path: Path | str) -> AppConfig:
    """Lit un fichier de configuration TOML.

    Raises:
        ConfigError: fichier absent ou invalide.
    """
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"configuration introuvable : {path}")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"TOML invalide dans {path} : {exc}") from exc
    return parse_config(raw, base_dir=path.parent)
