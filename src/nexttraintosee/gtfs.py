"""Lecture du GTFS statique (horaires théoriques).

Le GTFS SNCF est volumineux : `stop_times.txt` pèse plusieurs centaines de Mo
et ne tient pas confortablement en mémoire. On ne charge donc jamais tout :
on fait deux passes en streaming sur l'archive zip.

1. première passe : quels `trip_id` desservent la gare d'appui ?
2. seconde passe : toutes les lignes horaires de ces circulations-là,
   pour connaître leurs arrêts voisins (c'est le voisin qui dit quelle
   branche le train emprunte en sortie de gare).

Le reste (routes, stops, calendriers) est petit et se charge intégralement.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Iterator, Sequence
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

DEFAULT_TIMEZONE = ZoneInfo("Europe/Paris")

#: `route_type` GTFS correspondant à du ferroviaire lourd.
RAIL_ROUTE_TYPES = frozenset({"2", "100", "101", "102", "103", "105", "106", "107", "109"})


class GtfsError(RuntimeError):
    """Archive GTFS absente, incomplète ou illisible."""


def parse_gtfs_time(value: str) -> int | None:
    """Convertit un horaire GTFS `HH:MM:SS` en secondes depuis minuit.

    Les heures au-delà de 24 sont légales en GTFS (un train de 00h20 rattaché au
    service de la veille s'écrit `24:20:00`) et sont conservées telles quelles.
    Renvoie None pour un champ vide (arrêt sans horaire publié).
    """
    value = (value or "").strip()
    if not value:
        return None
    parts = value.split(":")
    if len(parts) != 3:
        raise ValueError(f"horaire GTFS invalide : {value!r}")
    hours, minutes, seconds = (int(p) for p in parts)
    return hours * 3600 + minutes * 60 + seconds


def service_datetime(service_day: date, seconds: int, tz: ZoneInfo = DEFAULT_TIMEZONE) -> datetime:
    """Date-heure absolue d'un horaire GTFS.

    Suit la convention de la spécification : on part de midi moins douze heures
    plutôt que de minuit, ce qui donne le bon résultat même les jours de
    changement d'heure.
    """
    noon = datetime.combine(service_day, datetime.min.time(), tzinfo=tz).replace(hour=12)
    return noon - timedelta(hours=12) + timedelta(seconds=seconds)


def normalize_name(value: str) -> str:
    """Forme normalisée d'un nom de gare, pour comparaison souple.

    « Toulouse-Matabiau », « TOULOUSE MATABIAU » et « Toulouse Matabiau » doivent
    se ressembler.
    """
    stripped = unicodedata.normalize("NFKD", value)
    stripped = "".join(c for c in stripped if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", stripped.lower()).strip()


@dataclass(frozen=True)
class Stop:
    stop_id: str
    name: str
    lat: float | None
    lon: float | None
    parent_station: str = ""
    location_type: str = "0"

    @property
    def is_station(self) -> bool:
        return self.location_type == "1"

    @property
    def position(self) -> tuple[float, float] | None:
        return None if self.lat is None or self.lon is None else (self.lat, self.lon)


@dataclass(frozen=True)
class Route:
    route_id: str
    short_name: str = ""
    long_name: str = ""
    route_type: str = ""

    def label(self) -> str:
        return self.long_name or self.short_name or self.route_id


@dataclass(frozen=True)
class StopTime:
    trip_id: str
    stop_id: str
    stop_sequence: int
    arrival_s: int | None
    departure_s: int | None
    pickup_type: str = "0"
    drop_off_type: str = "0"

    @property
    def is_pickup_allowed(self) -> bool:
        return self.pickup_type != "1"

    @property
    def is_drop_off_allowed(self) -> bool:
        return self.drop_off_type != "1"

    @property
    def is_revenue_stop(self) -> bool:
        """Faux pour un arrêt technique où personne ne monte ni ne descend."""
        return self.is_pickup_allowed or self.is_drop_off_allowed


@dataclass
class Trip:
    trip_id: str
    route_id: str
    service_id: str
    headsign: str = ""
    direction_id: str = ""
    stop_times: list[StopTime] = field(default_factory=list)

    def sort_stop_times(self) -> None:
        self.stop_times.sort(key=lambda st: st.stop_sequence)

    def index_of_stop(self, stop_ids: Sequence[str]) -> int | None:
        """Position du premier arrêt de la circulation figurant dans `stop_ids`."""
        wanted = set(stop_ids)
        for index, stop_time in enumerate(self.stop_times):
            if stop_time.stop_id in wanted:
                return index
        return None


@dataclass
class Calendar:
    """Calendriers de service GTFS (`calendar.txt` + `calendar_dates.txt`)."""

    weekly: dict[str, tuple[frozenset[int], date, date]] = field(default_factory=dict)
    exceptions: dict[tuple[str, date], str] = field(default_factory=dict)

    def active_services(self, day: date) -> set[str]:
        """`service_id` circulant à la date donnée."""
        active: set[str] = set()
        for service_id, (weekdays, start, end) in self.weekly.items():
            if start <= day <= end and day.weekday() in weekdays:
                active.add(service_id)
        for (service_id, exception_day), kind in self.exceptions.items():
            if exception_day != day:
                continue
            if kind == "1":
                active.add(service_id)
            elif kind == "2":
                active.discard(service_id)
        return active


_WEEKDAY_COLUMNS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _read_csv(archive: zipfile.ZipFile, name: str, required: bool = True) -> Iterator[dict[str, str]]:
    """Itère sur un fichier CSV de l'archive sans le charger entièrement."""
    try:
        handle = archive.open(name)
    except KeyError:
        if required:
            raise GtfsError(f"fichier {name} absent de l'archive GTFS") from None
        return
    with handle:
        # utf-8-sig : les exports SNCF sont fréquemment préfixés d'un BOM.
        text = io.TextIOWrapper(handle, encoding="utf-8-sig", newline="")
        yield from csv.DictReader(text)


def _parse_date(value: str) -> date:
    return datetime.strptime(value.strip(), "%Y%m%d").date()


class GtfsFeed:
    """Un jeu d'horaires théoriques, chargé autour d'une gare d'appui."""

    def __init__(
        self,
        stops: dict[str, Stop],
        routes: dict[str, Route],
        trips: dict[str, Trip],
        calendar: Calendar,
        timezone: ZoneInfo = DEFAULT_TIMEZONE,
    ) -> None:
        self.stops = stops
        self.routes = routes
        self.trips = trips
        self.calendar = calendar
        self.timezone = timezone
        self._by_normalized_name: dict[str, list[Stop]] = {}
        for stop in stops.values():
            self._by_normalized_name.setdefault(normalize_name(stop.name), []).append(stop)

    # -- chargement --------------------------------------------------------

    @classmethod
    def load(
        cls,
        path: Path | str,
        anchor_stop_ids: Iterable[str] | None = None,
        anchor_name: str | None = None,
        timezone: ZoneInfo = DEFAULT_TIMEZONE,
    ) -> "GtfsFeed":
        """Charge une archive GTFS en ne gardant que les circulations utiles.

        Args:
            path: chemin de l'archive `.zip`.
            anchor_stop_ids: identifiants d'arrêt de la gare d'appui. Si omis,
                ils sont déduits de `anchor_name`.
            anchor_name: nom de la gare d'appui, résolu de façon souple.
            timezone: fuseau des horaires du flux.

        Raises:
            GtfsError: archive illisible, ou gare d'appui introuvable.
        """
        path = Path(path)
        if not path.exists():
            raise GtfsError(f"archive GTFS introuvable : {path}")

        with zipfile.ZipFile(path) as archive:
            stops = cls._load_stops(archive)
            routes = cls._load_routes(archive)
            calendar = cls._load_calendar(archive)

            resolved = set(anchor_stop_ids or ())
            if anchor_name and not resolved:
                resolved = _resolve_station_stop_ids(stops, anchor_name)
            if not resolved:
                raise GtfsError(
                    "impossible de déterminer la gare d'appui : fournissez "
                    "anchor_stop_ids ou un anchor_name présent dans le flux"
                )

            trips = cls._load_trips(archive, resolved)

        feed = cls(stops, routes, trips, calendar, timezone)
        log.info(
            "GTFS chargé : %d arrêts, %d lignes, %d circulations desservant la gare d'appui",
            len(stops),
            len(routes),
            len(trips),
        )
        return feed

    @staticmethod
    def _load_stops(archive: zipfile.ZipFile) -> dict[str, Stop]:
        stops: dict[str, Stop] = {}
        for row in _read_csv(archive, "stops.txt"):
            stop_id = row["stop_id"]
            stops[stop_id] = Stop(
                stop_id=stop_id,
                name=(row.get("stop_name") or "").strip(),
                lat=float(row["stop_lat"]) if row.get("stop_lat") else None,
                lon=float(row["stop_lon"]) if row.get("stop_lon") else None,
                parent_station=(row.get("parent_station") or "").strip(),
                location_type=(row.get("location_type") or "0").strip() or "0",
            )
        return stops

    @staticmethod
    def _load_routes(archive: zipfile.ZipFile) -> dict[str, Route]:
        routes: dict[str, Route] = {}
        for row in _read_csv(archive, "routes.txt"):
            route_id = row["route_id"]
            routes[route_id] = Route(
                route_id=route_id,
                short_name=(row.get("route_short_name") or "").strip(),
                long_name=(row.get("route_long_name") or "").strip(),
                route_type=(row.get("route_type") or "").strip(),
            )
        return routes

    @staticmethod
    def _load_calendar(archive: zipfile.ZipFile) -> Calendar:
        calendar = Calendar()
        for row in _read_csv(archive, "calendar.txt", required=False):
            weekdays = frozenset(
                index for index, column in enumerate(_WEEKDAY_COLUMNS) if row.get(column) == "1"
            )
            calendar.weekly[row["service_id"]] = (
                weekdays,
                _parse_date(row["start_date"]),
                _parse_date(row["end_date"]),
            )
        for row in _read_csv(archive, "calendar_dates.txt", required=False):
            calendar.exceptions[(row["service_id"], _parse_date(row["date"]))] = (
                row.get("exception_type") or "1"
            ).strip()
        if not calendar.weekly and not calendar.exceptions:
            raise GtfsError("ni calendar.txt ni calendar_dates.txt : flux inexploitable")
        return calendar

    @staticmethod
    def _load_trips(archive: zipfile.ZipFile, anchor_stop_ids: set[str]) -> dict[str, Trip]:
        # Passe 1 : repérer les circulations qui desservent la gare d'appui.
        wanted: set[str] = set()
        for row in _read_csv(archive, "stop_times.txt"):
            if row["stop_id"] in anchor_stop_ids:
                wanted.add(row["trip_id"])
        if not wanted:
            log.warning("aucune circulation ne dessert la gare d'appui dans ce flux")

        trips: dict[str, Trip] = {}
        for row in _read_csv(archive, "trips.txt"):
            trip_id = row["trip_id"]
            if trip_id in wanted:
                trips[trip_id] = Trip(
                    trip_id=trip_id,
                    route_id=row["route_id"],
                    service_id=row["service_id"],
                    headsign=(row.get("trip_headsign") or "").strip(),
                    direction_id=(row.get("direction_id") or "").strip(),
                )

        # Passe 2 : récupérer la desserte complète de ces circulations.
        for row in _read_csv(archive, "stop_times.txt"):
            trip = trips.get(row["trip_id"])
            if trip is None:
                continue
            trip.stop_times.append(
                StopTime(
                    trip_id=row["trip_id"],
                    stop_id=row["stop_id"],
                    stop_sequence=int(row["stop_sequence"]),
                    arrival_s=parse_gtfs_time(row.get("arrival_time", "")),
                    departure_s=parse_gtfs_time(row.get("departure_time", "")),
                    pickup_type=(row.get("pickup_type") or "0").strip() or "0",
                    drop_off_type=(row.get("drop_off_type") or "0").strip() or "0",
                )
            )
        for trip in trips.values():
            trip.sort_stop_times()
        return trips

    # -- interrogation -----------------------------------------------------

    def find_stops_by_name(self, name: str) -> list[Stop]:
        """Arrêts dont le nom correspond, en normalisant accents et ponctuation."""
        return list(self._by_normalized_name.get(normalize_name(name), []))

    def station_stop_ids(self, name: str) -> set[str]:
        """Tous les identifiants d'arrêt rattachés à une gare (parent + quais)."""
        return _resolve_station_stop_ids(self.stops, name)

    def trips_on(self, day: date) -> list[Trip]:
        """Circulations actives à une date donnée."""
        active = self.calendar.active_services(day)
        return [t for t in self.trips.values() if t.service_id in active]

    def is_rail(self, trip: Trip) -> bool:
        """Vrai si la circulation est ferroviaire (et non un car de substitution)."""
        route = self.routes.get(trip.route_id)
        return route is not None and route.route_type in RAIL_ROUTE_TYPES


def _resolve_station_stop_ids(stops: dict[str, Stop], name: str) -> set[str]:
    """Identifiants d'arrêt d'une gare désignée par son nom.

    Un GTFS représente une gare par un arrêt parent (`location_type=1`) et ses
    quais (`parent_station`). Les `stop_times` référencent les quais : il faut
    donc les deux.
    """
    matches = [s for s in stops.values() if normalize_name(s.name) == normalize_name(name)]
    if not matches:
        # Repli : correspondance par préfixe, utile pour « Toulouse Matabiau »
        # face à « Toulouse Matabiau (Toulouse) ».
        target = normalize_name(name)
        matches = [s for s in stops.values() if normalize_name(s.name).startswith(target)]
    resolved = {s.stop_id for s in matches}
    parents = {s.stop_id for s in matches if s.is_station} | {
        s.parent_station for s in matches if s.parent_station
    }
    resolved |= {s.stop_id for s in stops.values() if s.parent_station in parents}
    resolved |= parents
    return {stop_id for stop_id in resolved if stop_id}
