"""Fixtures partagées : un mini-GTFS calqué sur la géographie toulousaine.

Assez petit pour être lisible, assez complet pour exercer les cas qui comptent :
gare parente + quais, branches divergentes, arrêt technique, horaire après
minuit, exception de calendrier.
"""

import csv
import io
import zipfile
from pathlib import Path

import pytest

# Point d'observation de référence : 43°35'52.2"N 1°27'29.5"E.
OBSERVER = (43.597833, 1.458194)

STOPS = [
    # stop_id, name, lat, lon, location_type, parent_station
    ("SA:MTB", "Toulouse Matabiau", 43.6112, 1.4535, "1", ""),
    ("SP:MTB:1", "Toulouse Matabiau", 43.6112, 1.4535, "0", "SA:MTB"),
    ("SP:MTB:2", "Toulouse Matabiau", 43.6113, 1.4536, "0", "SA:MTB"),
    ("SA:VLF", "Villefranche-de-Lauragais", 43.4008, 1.7183, "1", ""),
    ("SP:VLF:1", "Villefranche-de-Lauragais", 43.4008, 1.7183, "0", "SA:VLF"),
    ("SA:STA", "Toulouse Saint-Agne", 43.57972, 1.45028, "1", ""),
    ("SP:STA:1", "Toulouse Saint-Agne", 43.57972, 1.45028, "0", "SA:STA"),
    ("SA:MTB_N", "Montauban-Ville-Bourbon", 44.0186, 1.3395, "1", ""),
    ("SP:MTB_N:1", "Montauban-Ville-Bourbon", 44.0186, 1.3395, "0", "SA:MTB_N"),
    ("SA:COL", "Colomiers", 43.6108, 1.3350, "1", ""),
    ("SP:COL:1", "Colomiers", 43.6108, 1.3350, "0", "SA:COL"),
]

ROUTES = [
    # route_id, short_name, long_name, route_type
    ("R:SE", "TER 1", "Toulouse - Narbonne", "2"),
    ("R:S", "TER 2", "Toulouse - Latour-de-Carol", "2"),
    ("R:N", "TER 3", "Toulouse - Montauban", "2"),
    ("R:W", "TER 4", "Toulouse - Colomiers", "2"),
    ("R:BUS", "CAR 5", "Substitution routière", "3"),
]

TRIPS = [
    # trip_id, route_id, service_id, headsign, direction_id
    ("T:SE:1", "R:SE", "S:WEEKDAY", "Narbonne", "0"),
    ("T:S:1", "R:S", "S:WEEKDAY", "Latour-de-Carol", "0"),
    ("T:N:1", "R:N", "S:WEEKDAY", "Toulouse Matabiau", "1"),
    ("T:W:1", "R:W", "S:WEEKDAY", "Colomiers", "0"),
    ("T:SE:NIGHT", "R:SE", "S:WEEKDAY", "Narbonne", "0"),
    ("T:SE:SUN", "R:SE", "S:SUNDAY", "Narbonne", "0"),
    ("T:BUS:1", "R:BUS", "S:WEEKDAY", "Narbonne", "0"),
    # Terminus Matabiau, en provenance du sud-est.
    ("T:SE:2", "R:SE", "S:WEEKDAY", "Toulouse Matabiau", "1"),
    # Traverse Matabiau sans arrêt commercial.
    ("T:SE:THRU", "R:SE", "S:WEEKDAY", "Narbonne", "0"),
    # Ne dessert pas la gare d'appui : doit être écartée dès le chargement.
    ("T:ELSEWHERE", "R:N", "S:WEEKDAY", "Colomiers", "0"),
]

STOP_TIMES = [
    # trip_id, stop_id, stop_sequence, arrival, departure, pickup, drop_off
    ("T:SE:1", "SP:MTB:1", 1, "08:00:00", "08:02:00", "0", "0"),
    ("T:SE:1", "SP:VLF:1", 2, "08:30:00", "08:31:00", "0", "0"),
    ("T:S:1", "SP:MTB:2", 1, "08:10:00", "08:12:00", "0", "0"),
    ("T:S:1", "SP:STA:1", 2, "08:18:00", "08:19:00", "0", "0"),
    ("T:N:1", "SP:MTB_N:1", 1, "07:30:00", "07:31:00", "0", "0"),
    ("T:N:1", "SP:MTB:1", 2, "08:15:00", "08:17:00", "0", "0"),
    ("T:W:1", "SP:MTB:1", 1, "09:00:00", "09:02:00", "0", "0"),
    ("T:W:1", "SP:COL:1", 2, "09:20:00", "09:21:00", "0", "0"),
    # Circulation après minuit, rattachée au service de la veille.
    ("T:SE:NIGHT", "SP:MTB:1", 1, "24:20:00", "24:22:00", "0", "0"),
    ("T:SE:NIGHT", "SP:VLF:1", 2, "24:50:00", "24:51:00", "0", "0"),
    ("T:SE:SUN", "SP:MTB:1", 1, "10:00:00", "10:02:00", "0", "0"),
    # Arrêt technique : ni montée ni descente.
    ("T:SE:SUN", "SP:STA:1", 2, "10:08:00", "10:09:00", "1", "1"),
    ("T:SE:SUN", "SP:VLF:1", 3, "10:30:00", "10:31:00", "0", "0"),
    ("T:BUS:1", "SP:MTB:1", 1, "11:00:00", "11:02:00", "0", "0"),
    ("T:BUS:1", "SP:VLF:1", 2, "12:00:00", "12:01:00", "0", "0"),
    ("T:SE:2", "SP:VLF:1", 1, "09:00:00", "09:01:00", "0", "0"),
    ("T:SE:2", "SP:MTB:1", 2, "09:30:00", "09:30:00", "0", "0"),
    ("T:SE:THRU", "SP:MTB_N:1", 1, "12:00:00", "12:01:00", "0", "0"),
    ("T:SE:THRU", "SP:MTB:1", 2, "12:45:00", "12:45:00", "1", "1"),
    ("T:SE:THRU", "SP:VLF:1", 3, "13:15:00", "13:16:00", "0", "0"),
    ("T:ELSEWHERE", "SP:MTB_N:1", 1, "14:00:00", "14:01:00", "0", "0"),
    ("T:ELSEWHERE", "SP:COL:1", 2, "14:40:00", "14:41:00", "0", "0"),
]

CALENDAR = [
    # service_id, lun..dim, start, end
    ("S:WEEKDAY", 1, 1, 1, 1, 1, 0, 0, "20260101", "20261231"),
    ("S:SUNDAY", 0, 0, 0, 0, 0, 0, 1, "20260101", "20261231"),
]

CALENDAR_DATES = [
    # Jour férié : pas de service en semaine ce jour-là.
    ("S:WEEKDAY", "20260501", "2"),
    # Renfort exceptionnel un dimanche de semaine.
    ("S:SUNDAY", "20260501", "1"),
]


def _csv(header, rows):
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue()


def build_gtfs_zip(path: Path, *, with_calendar: bool = True) -> Path:
    """Écrit une archive GTFS minimale mais valide à `path`."""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "stops.txt",
            _csv(
                ["stop_id", "stop_name", "stop_lat", "stop_lon", "location_type", "parent_station"],
                STOPS,
            ),
        )
        archive.writestr(
            "routes.txt",
            _csv(["route_id", "route_short_name", "route_long_name", "route_type"], ROUTES),
        )
        archive.writestr(
            "trips.txt",
            _csv(["trip_id", "route_id", "service_id", "trip_headsign", "direction_id"], TRIPS),
        )
        archive.writestr(
            "stop_times.txt",
            _csv(
                [
                    "trip_id",
                    "stop_id",
                    "stop_sequence",
                    "arrival_time",
                    "departure_time",
                    "pickup_type",
                    "drop_off_type",
                ],
                STOP_TIMES,
            ),
        )
        if with_calendar:
            archive.writestr(
                "calendar.txt",
                _csv(
                    [
                        "service_id",
                        "monday",
                        "tuesday",
                        "wednesday",
                        "thursday",
                        "friday",
                        "saturday",
                        "sunday",
                        "start_date",
                        "end_date",
                    ],
                    CALENDAR,
                ),
            )
            archive.writestr(
                "calendar_dates.txt",
                _csv(["service_id", "date", "exception_type"], CALENDAR_DATES),
            )
    return path


@pytest.fixture
def gtfs_zip(tmp_path: Path) -> Path:
    return build_gtfs_zip(tmp_path / "mini-gtfs.zip")


@pytest.fixture
def gtfs_zip_builder(tmp_path: Path):
    """Fabrique d'archives GTFS, pour les cas dégradés."""

    def _build(name: str = "gtfs.zip", **kwargs) -> Path:
        return build_gtfs_zip(tmp_path / name, **kwargs)

    return _build


# --- site d'observation de référence, calé sur le mini-GTFS ------------------

from nexttraintosee.motion import TractionProfile  # noqa: E402
from nexttraintosee.predict import Branch, Site  # noqa: E402

MATABIAU = (43.6112, 1.4535)

#: Caps mesurés depuis Matabiau vers chaque branche du mini-GTFS.
BRANCHES = (
    Branch("se", "Axe Narbonne / Sète", bearing_deg=137.5, track_distance_m=1650.0),
    Branch("s", "Axe Latour-de-Carol", bearing_deg=184.2, track_distance_m=1500.0),
    Branch("n", "Axe Bordeaux", bearing_deg=348.6, passes_observer=False),
    Branch("w", "Axe Auch / Bayonne", bearing_deg=269.8, passes_observer=False),
)


@pytest.fixture
def site() -> Site:
    return Site(
        name="Toulouse — point d'observation",
        position=OBSERVER,
        anchor_station="Toulouse Matabiau",
        anchor_position=MATABIAU,
        branches=BRANCHES,
        profile=TractionProfile(accel_ms2=0.5, decel_ms2=0.6, line_speed_kmh=90.0),
    )
