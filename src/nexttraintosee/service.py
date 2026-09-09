"""Logique partagée entre la ligne de commande et le serveur HTTP.

Deux besoins distincts convergent ici :

1. **Rattacher une observation au passage prédit le plus proche** — la même
   règle doit s'appliquer que l'observation vienne de `nexttraintosee observe`
   en ligne de commande ou de `POST /api/observe` depuis la PWA. On refuse le
   rattachement quand deux candidats sont à portée comparable : une
   observation mal attribuée fausse le recalage bien plus qu'une observation
   ignorée.
2. **`PassageService`** — encapsule tout ce que le serveur doit tenir à jour :
   le flux GTFS, le rafraîchissement périodique (temps réel + recalcul +
   journalisation, comme `cli._collect_passages`), le cache des passages
   prédits, et l'histogramme moyenné semaine / week-end. Le rafraîchissement
   est appelable directement (`refresh`) : le thread périodique n'est qu'une
   boucle autour, ce qui permet de tester toute la logique sans jamais
   démarrer de thread.
"""

from __future__ import annotations

import logging
import threading
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable, Sequence

from .config import AppConfig
from .gtfs import GtfsError, GtfsFeed
from .observation import Observation, ObservationKind
from .predict import Direction, Passage, next_passages, predict_passages, service_days_around
from .realtime import RealtimeError, empty_snapshot, load_snapshot
from .report import DEFAULT_FIRST_HOUR, DEFAULT_LAST_HOUR, hourly_histogram
from .store import Store

log = logging.getLogger(__name__)

#: Dupliqué de `cli.GTFS_DOWNLOAD_URL` : ce module ne doit pas importer `cli`
#: (qui importe déjà `service`), pour éviter une dépendance circulaire.
GTFS_DOWNLOAD_URL = "https://eu.ftp.opendatasoft.com/sncf/plandata/Export_OpenData_SNCF_GTFS_NewTripId.zip"

#: Tolérance par défaut pour rattacher une observation à un passage prédit,
#: identique à celle de la commande `observe`.
DEFAULT_OBSERVE_TOLERANCE_S = 180.0

#: Libellés de direction et indication d'où regarder, figés par le contrat
#: (§ 5 de `docs/app-v0.md`) : le client ne les infère pas, le serveur les donne.
_DIRECTION_LABELS = {
    Direction.OUTBOUND: "Depuis Matabiau",
    Direction.INBOUND: "Vers Matabiau",
}
_LOOK = {
    Direction.OUTBOUND: "tunnel",
    Direction.INBOUND: "sud",
}


def _open_feed(config: AppConfig) -> GtfsFeed:
    """Charge le flux GTFS théorique, avec un message clair s'il est absent."""
    if not config.data.gtfs_path.exists():
        raise GtfsError(
            f"archive GTFS absente ({config.data.gtfs_path}).\n"
            f"Téléchargez-la puis relancez :\n  curl -L -o {config.data.gtfs_path} {GTFS_DOWNLOAD_URL}"
        )
    return GtfsFeed.load(config.data.gtfs_path, anchor_name=config.site.anchor_station)


def _parse_iso(value: object) -> datetime | None:
    """Convertit une valeur JSON en date-heure ISO 8601, ou None si invalide."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.astimezone()


# -- rattachement d'une observation ------------------------------------------


@dataclass(frozen=True)
class PassageBinding:
    """Résultat du rattachement d'une observation à un passage prédit."""

    passage: Passage | None
    """Passage retenu ; `None` si aucun candidat, ou rattachement ambigu."""
    ambiguous: bool
    """Vrai si deux candidats étaient à portée comparable."""
    candidates: tuple[Passage, ...] = ()
    """Candidats triés par proximité temporelle croissante (pour diagnostic)."""


def bind_passage(
    candidates: Sequence[Passage], moment: datetime, tolerance_s: float
) -> PassageBinding:
    """Rattache un instant observé au passage prédit le plus proche.

    Avec un train toutes les quelques minutes, deux candidats proches rendent
    l'attribution douteuse : mieux vaut ne rien lier que lier au mauvais
    train. On refuse donc le rattachement dès que l'écart entre le premier et
    le second candidat est inférieur à la moitié de la tolérance.
    """
    if not candidates:
        return PassageBinding(None, False, ())

    ordered = tuple(sorted(candidates, key=lambda p: abs((p.when - moment).total_seconds())))
    nearest = ordered[0]
    if len(ordered) > 1:
        first_gap = abs((nearest.when - moment).total_seconds())
        second_gap = abs((ordered[1].when - moment).total_seconds())
        if second_gap - first_gap < tolerance_s / 2:
            return PassageBinding(None, True, ordered)
    return PassageBinding(nearest, False, ordered)


def _passage_payload(passage: Passage) -> dict:
    """Un passage prédit, mis en forme selon `GET /api/next` du contrat."""
    return {
        "trip_id": passage.trip_id,
        "when": passage.when.isoformat(),
        "announce_at": passage.announce_at.isoformat(),
        "uncertainty_s": passage.uncertainty_s,
        "direction": passage.direction.value,
        "direction_label": _DIRECTION_LABELS[passage.direction],
        "look": _LOOK[passage.direction],
        "branch_id": passage.branch.branch_id,
        "branch_label": passage.branch.label,
        "category_id": passage.category_id,
        "headsign": passage.headsign,
        "route_label": passage.route_label,
        "delay_s": passage.delay_s,
        "realtime": passage.delay_s is not None,
        "speed_kmh": passage.speed_kmh,
    }


# -- service partagé ----------------------------------------------------------


class PassageService:
    """Ce que le serveur HTTP (et, à terme, la ligne de commande) tient à jour.

    Trois responsabilités : charger le flux GTFS une fois, rafraîchir
    périodiquement les passages prédits (temps réel compris, avec repli
    théorique en cas de panne), et exposer les réponses du contrat d'API sans
    jamais faire tomber le serveur.
    """

    def __init__(
        self,
        config: AppConfig,
        *,
        refresh_every_s: float = 90.0,
        use_realtime: bool = True,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Charge le flux GTFS et prépare le service, sans le rafraîchir.

        Args:
            config: configuration du site observé.
            refresh_every_s: intervalle de la boucle de fond, en secondes.
            use_realtime: interroger le flux GTFS-RT à chaque rafraîchissement.
                Désactivé dans les tests, pour rester sans réseau.
            clock: horloge injectable, par défaut `datetime.now()` locale.

        Raises:
            GtfsError: archive GTFS absente ou gare d'appui introuvable.
        """
        self.config = config
        self.feed = _open_feed(config)
        self.refresh_every_s = refresh_every_s
        self.use_realtime = use_realtime
        self._clock = clock or (lambda: datetime.now().astimezone())

        self._lock = threading.Lock()
        self._passages: list[Passage] = []
        self._realtime_ok = False
        self._last_refresh: datetime | None = None
        self._histogram: dict | None = None

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # -- rafraîchissement ----------------------------------------------------

    def refresh(self, now: datetime | None = None) -> None:
        """Recalcule les passages prédits et les journalise.

        Appelable directement — pour les tests, ou une actualisation forcée —
        ou depuis la boucle périodique, dont c'est l'unique tâche. Une panne
        du temps réel ne doit jamais interrompre le service : on bascule sur
        l'horaire théorique et on l'indique via `realtime`.
        """
        now = now or self._clock()
        snapshot = empty_snapshot()
        realtime_ok = False
        if self.use_realtime:
            try:
                snapshot = load_snapshot(self.config.data.trip_updates_url)
                realtime_ok = True
            except RealtimeError as exc:
                log.warning("temps réel indisponible, repli sur l'horaire théorique : %s", exc)

        anchor_stop_ids = self.feed.station_stop_ids(self.config.site.anchor_station)
        delays = snapshot.delays_at(anchor_stop_ids)
        canceled = snapshot.canceled_trip_ids(anchor_stop_ids)

        passages: list[Passage] = []
        for day in service_days_around(now):
            passages.extend(
                predict_passages(
                    self.feed, self.config.site, day, delays_s=delays, canceled_trip_ids=canceled
                )
            )
        passages.sort(key=lambda p: p.when)

        with Store(self.config.data.database) as store:
            store.record_passages(self.config.site.name, passages, predicted_at=now)

        with self._lock:
            self._passages = passages
            self._realtime_ok = realtime_ok
            self._last_refresh = now

    def start(self) -> None:
        """Démarre la boucle de rafraîchissement périodique, si elle ne l'est pas déjà."""
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="passage-refresh", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Arrête proprement la boucle de fond et attend sa terminaison.

        `Event.wait` est interrompu dès `set()` : l'arrêt n'attend jamais un
        intervalle de rafraîchissement complet, ce qui importe pour les tests.
        """
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def _run(self) -> None:
        while not self._stop_event.wait(self.refresh_every_s):
            try:
                self.refresh()
            except Exception:  # pragma: no cover - garde-fou de la boucle de fond
                log.exception("échec du rafraîchissement périodique")

    # -- réponses du contrat d'API --------------------------------------------

    def next_response(self, *, limit: int = 2, now: datetime | None = None) -> dict:
        """Réponse de `GET /api/next` : prochains passages sur 12 h."""
        now = now or self._clock()
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 2
        limit = max(1, min(limit, 10))

        with self._lock:
            passages = list(self._passages)
            realtime_ok = self._realtime_ok

        upcoming = next_passages(passages, now, horizon=timedelta(hours=12), limit=limit)
        return {
            "generated_at": now.isoformat(),
            "site": self.config.site.name,
            "realtime": realtime_ok,
            "passages": [_passage_payload(p) for p in upcoming],
        }

    def histogram_response(self, *, now: datetime | None = None) -> dict:
        """Réponse de `GET /api/histogram`, calculée une fois puis mise en cache.

        Les horaires théoriques ne bougent pas en cours de journée : recalculer
        à chaque requête n'apporterait rien et coûterait cher.
        """
        now = now or self._clock()
        with self._lock:
            if self._histogram is None:
                self._histogram = self._compute_histogram(now.date())
            return self._histogram

    def health_response(self, *, now: datetime | None = None) -> dict:
        """Réponse de `GET /api/health` : l'état du service, pas celui d'un train."""
        now = now or self._clock()
        with self._lock:
            last_refresh = self._last_refresh
            passages_cached = len(self._passages)
        window = self.feed.calendar.coverage()
        return {
            "ok": True,
            "feed_start": window[0].isoformat() if window else None,
            "feed_end": window[1].isoformat() if window else None,
            "realtime_age_s": (now - last_refresh).total_seconds() if last_refresh else None,
            "last_refresh": last_refresh.isoformat() if last_refresh else None,
            "passages_cached": passages_cached,
        }

    def observe(self, payload: object, *, now: datetime | None = None) -> dict:
        """Réponse de `POST /api/observe`, même logique que `observe` en CLI.

        Raises:
            ValueError: corps mal formé — le serveur en fait un 400.
        """
        if not isinstance(payload, dict):
            raise ValueError("corps JSON invalide : un objet est attendu")

        seen = payload.get("seen")
        if not isinstance(seen, bool):
            raise ValueError("champ « seen » booléen requis")
        source = str(payload.get("source", "app"))
        precision_s = float(payload.get("precision_s", 5.0))

        if seen:
            moment = _parse_iso(payload.get("observed_at"))
            if moment is None:
                raise ValueError("« observed_at » (ISO 8601) requis quand seen=true")
        else:
            moment = _parse_iso(payload.get("anchor"))
            if moment is None:
                raise ValueError("« anchor » (ISO 8601) requis quand seen=false")

        window = timedelta(seconds=DEFAULT_OBSERVE_TOLERANCE_S)
        with Store(self.config.data.database) as store:
            candidates = store.passages_between(
                self.config.site.name, moment - window, moment + window, self.config.site.branches
            )
            binding = bind_passage(candidates, moment, DEFAULT_OBSERVE_TOLERANCE_S)

            observation = Observation(
                observed_at=moment if seen else None,
                kind=ObservationKind.SEEN if seen else ObservationKind.NOT_SEEN,
                source=source,
                trip_id=binding.passage.trip_id if binding.passage else None,
                direction=binding.passage.direction.value if binding.passage else None,
                anchor_time=(
                    binding.passage.anchor_time
                    if binding.passage
                    else (moment if not seen else None)
                ),
                precision_s=precision_s,
            )
            observation_id = store.record_observation(self.config.site.name, observation)

        bound_to = None
        gap_s = None
        if binding.passage is not None:
            gap_s = (moment - binding.passage.when).total_seconds()
            bound_to = {
                "trip_id": binding.passage.trip_id,
                "when": binding.passage.when.isoformat(),
                "headsign": binding.passage.headsign,
            }
        return {
            "recorded": True,
            "id": observation_id,
            "bound_to": bound_to,
            "ambiguous": binding.ambiguous,
            "gap_s": gap_s,
        }

    def delete_observation(self, observation_id: int) -> bool:
        """Annule une observation rapportée par erreur (§ 6 : le geste doit être réversible).

        Returns:
            Vrai si l'observation existait et a été supprimée.
        """
        with Store(self.config.data.database) as store:
            return store.delete_observation(self.config.site.name, observation_id)

    # -- histogramme moyenné semaine / week-end -------------------------------

    def _compute_histogram(self, start_day: date) -> dict:
        weekday_days = self._covered_days(start_day, weekend=False, count=5)
        weekend_days = self._covered_days(start_day, weekend=True, count=2)

        weekday_hours = self._averaged_hours(weekday_days)
        weekend_hours = self._averaged_hours(weekend_days)
        peak = max(
            [h["total"] for h in weekday_hours] + [h["total"] for h in weekend_hours],
            default=0.0,
        )

        return {
            "first_hour": DEFAULT_FIRST_HOUR,
            "last_hour": DEFAULT_LAST_HOUR,
            "peak": peak,
            "weekday": {"label": "Semaine", "days": len(weekday_days), "hours": weekday_hours},
            "weekend": {"label": "Week-end", "days": len(weekend_days), "hours": weekend_hours},
        }

    def _covered_days(self, start_day: date, *, weekend: bool, count: int) -> list[date]:
        """Les `count` prochains jours (ouvrés ou de week-end) couverts par le flux."""
        days: list[date] = []
        day = start_day
        # Un an suffit largement à trouver 5 jours ouvrés et 2 jours de
        # week-end dans un flux valide ; ce garde-fou n'est là que pour ne
        # jamais boucler indéfiniment sur un flux vide ou expiré.
        for _ in range(366):
            is_weekend_day = day.weekday() >= 5
            if is_weekend_day == weekend and self.feed.calendar.covers(day):
                days.append(day)
                if len(days) == count:
                    break
            day += timedelta(days=1)
        return days

    def _averaged_hours(self, days: Sequence[date]) -> list[dict]:
        hours = range(DEFAULT_FIRST_HOUR, DEFAULT_LAST_HOUR)
        if not days:
            return [{"hour": hour, "total": 0.0} for hour in hours]

        totals: Counter = Counter()
        for day in days:
            passages = predict_passages(self.feed, self.config.site, day)
            for bucket in hourly_histogram(passages, DEFAULT_FIRST_HOUR, DEFAULT_LAST_HOUR):
                totals[bucket.hour] += bucket.total

        return [{"hour": hour, "total": totals[hour] / len(days)} for hour in hours]
