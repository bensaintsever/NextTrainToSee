"""Interface en ligne de commande."""

from __future__ import annotations

import argparse
import csv
import errno
import importlib
import logging
import socket
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from .config import AppConfig, ConfigError, load_config
from .coverage import analyse, summarise_delays
from .geo import bearing_distance_deg, initial_bearing_deg
from .gtfs import GtfsError, GtfsFeed
from .matching import calibrate, fit_line_speed_kmh, fit_profile, match_observations, runs_from_matches
from .osm import (
    OverpassClient,
    OverpassError,
    build_corridors,
    merged_length_m,
    nearest_stops,
    speed_profile,
    to_geojson,
)
from .predict import Passage, next_passages, predict_passages, service_days_around
from .report import csv_rows, french_date, hourly_histogram, render_histogram
from .realtime import RealtimeError, empty_snapshot, load_snapshot
from .sensor.base import PassageDetector
from .sensor.replay import read_levels
from .sensor.session import Status, listen_session
from .observation import Observation, ObservationKind, from_detection, times_the_passage
from .server import DEFAULT_WEBAPP_DIR, create_server
from .service import PassageService, resolve_binding
from .store import Store
from .validate import category_coverage, check_segments, format_duration

log = logging.getLogger("nexttraintosee")

GTFS_DOWNLOAD_URL = "https://eu.ftp.opendatasoft.com/sncf/plandata/Export_OpenData_SNCF_GTFS_NewTripId.zip"


def _load(args: argparse.Namespace) -> AppConfig:
    return load_config(args.config)


def dependency_status(module: str, extra: str) -> str:
    """État d'une dépendance optionnelle, en une ligne lisible.

    Un paquet peut être installé et pourtant inutilisable : `sounddevice` lève
    une `OSError` — et non une `ImportError` — quand la bibliothèque système
    PortAudio est absente. Les deux cas appellent des remèdes différents, on les
    distingue donc au lieu de tout traiter comme « absent ».
    """
    try:
        importlib.import_module(module)
    except ImportError:
        return f'absent → pip install "nexttraintosee[{extra}]"'
    except Exception as exc:
        return f"installé mais inutilisable ({exc})"
    return "disponible"


def _moment(value: str) -> datetime:
    """Convertit un argument ISO 8601 en date-heure, locale si non qualifiée."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"date-heure ISO 8601 attendue, reçu {value!r}") from exc
    return parsed if parsed.tzinfo else parsed.astimezone()


def _open_feed(config: AppConfig) -> GtfsFeed:
    if not config.data.gtfs_path.exists():
        raise GtfsError(
            f"archive GTFS absente ({config.data.gtfs_path}).\n"
            f"Téléchargez-la puis relancez :\n  curl -L -o {config.data.gtfs_path} {GTFS_DOWNLOAD_URL}"
        )
    return GtfsFeed.load(config.data.gtfs_path, anchor_name=config.site.anchor_station)


def _warn_if_outside_coverage(feed: GtfsFeed, day) -> None:
    """Signale une date hors du flux, que l'on confondrait avec une absence de trains."""
    if feed.calendar.covers(day):
        return
    window = feed.calendar.coverage()
    if window is None:
        print("Le flux GTFS ne décrit aucune journée de service.", file=sys.stderr)
        return
    print(
        f"⚠ {day.strftime('%d/%m/%Y')} est hors du flux, qui couvre du "
        f"{window[0].strftime('%d/%m/%Y')} au {window[1].strftime('%d/%m/%Y')}.\n"
        "  Aucun passage ne sera trouvé — ce n'est pas une absence de circulation.",
        file=sys.stderr,
    )


def _collect_passages(
    config: AppConfig, feed: GtfsFeed, now: datetime, use_realtime: bool
) -> list[Passage]:
    """Prédit les passages autour d'un instant, temps réel compris si demandé."""
    snapshot = empty_snapshot()
    if use_realtime:
        try:
            snapshot = load_snapshot(config.data.trip_updates_url)
            log.info("temps réel : %d circulations suivies", len(snapshot.updates))
        except RealtimeError as exc:
            log.warning("temps réel indisponible, repli sur l'horaire théorique : %s", exc)

    anchor_stop_ids = feed.station_stop_ids(config.site.anchor_station)
    delays = snapshot.delays_at(anchor_stop_ids)
    canceled = snapshot.canceled_trip_ids(anchor_stop_ids)

    passages: list[Passage] = []
    for day in service_days_around(now):
        passages.extend(
            predict_passages(feed, config.site, day, delays_s=delays, canceled_trip_ids=canceled)
        )
    passages.sort(key=lambda p: p.when)
    return passages


# -- commandes ---------------------------------------------------------------


def suggest_branches(config: AppConfig, corridors) -> list[tuple]:
    """Associe chaque branche configurée aux corridors qui lui correspondent.

    Une branche est définie par le cap sous lequel on quitte la gare d'appui ;
    un corridor sait vers où il part une fois passé le point d'observation. On
    apparie les deux.

    Plusieurs corridors peuvent porter la même branche — c'est le cas courant
    d'un faisceau où les voies d'une même ligne sont réparties de part et
    d'autre : on les renvoie tous, ordonnés par proximité du point, plutôt que
    d'en élire un arbitrairement.

    Returns:
        Un couple (branche, liste de (corridor, lien)) par branche configurée.
        La liste est vide quand aucun corridor ne part dans cette direction.
    """
    anchor = config.site.anchor_position
    links = {}
    if anchor is not None:
        links = {
            corridor.corridor_id: corridor.link_to(anchor, config.branch_lookahead_m)
            for corridor in corridors
        }

    suggestions = []
    for branch in config.site.branches:
        matches = []
        for corridor in corridors:
            link = links.get(corridor.corridor_id)
            if link is None or link.outbound_bearing_deg is None:
                continue
            if bearing_distance_deg(branch.bearing_deg, link.outbound_bearing_deg) <= branch.tolerance_deg:
                matches.append((corridor, link))
        # Le plus proche du point d'abord : c'est celui qu'on voit et qu'on entend.
        matches.sort(key=lambda item: item[0].distance_m)
        suggestions.append((branch, matches))
    return suggestions


def render_branch_toml(suggestions) -> str:
    """Bloc TOML prêt à coller, déduit de la géométrie mesurée."""
    lines = []
    for branch, matches in suggestions:
        lines.append("[[branches]]")
        lines.append(f'id = "{branch.branch_id}"')
        lines.append(f'label = "{branch.label}"')
        lines.append(f"bearing_deg = {branch.bearing_deg}")
        if not matches:
            if branch.passes_observer:
                # La géométrie ne voit rien partir par là, mais la configuration
                # l'affirme : c'est souvent délibéré — une desserte dont le cap
                # vers l'arrêt voisin ne reflète pas la direction de départ. On
                # signale la contradiction sans effacer la décision humaine.
                lines.append(
                    "passes_observer = true    # à confirmer : aucun corridor ne part "
                    "dans cette direction,"
                )
                lines.append(
                    "                          # mais votre configuration l'affirme "
                    "(cap trompeur ?)"
                )
            else:
                lines.append(
                    "passes_observer = false   # aucun corridor ne part dans cette direction"
                )
            lines.append("")
            continue

        lines.append("passes_observer = true")
        corridor, link = matches[0]
        others = ", ".join(c.corridor_id for c, _ in matches[1:])
        also = f" ; aussi {others}" if others else ""
        if link.is_plausible:
            lines.append(
                f"track_distance_m = {link.along_distance_m:.0f}"
                f"   # corridor {corridor.corridor_id}, à {corridor.distance_m:.0f} m du point{also}"
            )
        else:
            lines.append(
                f"# track_distance_m : non mesurable pour le corridor "
                f"{corridor.corridor_id} (géométrie incomplète)"
            )
        lines.append("")
    return "\n".join(lines).rstrip()


def cmd_tracks(args: argparse.Namespace) -> int:
    """Résout la géométrie ferroviaire autour du point via OpenStreetMap."""
    config = _load(args)
    client = OverpassClient(cache_dir=config.data.overpass_cache)
    try:
        ways, stops = client.around(
            config.site.position,
            radius_m=config.search_radius_m,
            refresh=args.refresh,
            anchor=config.site.anchor_position,
            lookahead_m=config.branch_lookahead_m,
        )
    except OverpassError as exc:
        print(f"Overpass : {exc}", file=sys.stderr)
        return 2

    corridors = build_corridors(
        ways,
        config.site.position,
        max_distance_m=config.search_radius_m,
        include_service=args.include_service,
    )
    if not corridors:
        print(f"Aucune voie ferrée à moins de {config.search_radius_m:.0f} m du point.")
        return 1

    anchor = config.site.anchor_position
    print(f"Point : {config.site.position[0]:.6f}, {config.site.position[1]:.6f}")
    print(f"{len(corridors)} corridor(s) dans un rayon de {config.search_radius_m:.0f} m :\n")

    for corridor in corridors:
        speed = f"{corridor.maxspeed_kmh:.0f} km/h" if corridor.maxspeed_kmh else "vitesse inconnue"
        print(f"  [{corridor.corridor_id}] {corridor.label}")
        print(
            f"      {corridor.distance_m:6.0f} m du point · axe {corridor.axis_deg:.0f}° "
            f"· {speed} · {corridor.segment_count} tronçon(s) OSM "
            f"sur {corridor.lateral_spread_m:.0f} m de large"
        )
        if anchor is None:
            print("      (renseignez anchor_lat / anchor_lon pour mesurer la distance à la gare)")
        else:
            link = corridor.link_to(anchor, config.branch_lookahead_m)
            if link.is_plausible:
                heading = (
                    f", repart au cap {link.outbound_bearing_deg:.0f}°"
                    if link.outbound_bearing_deg is not None
                    else ""
                )
                print(
                    f"      {link.along_distance_m:.0f} m par la voie jusqu'à "
                    f"{config.site.anchor_station} ({link.straight_distance_m:.0f} m à vol "
                    f"d'oiseau){heading}"
                )
            else:
                print(
                    f"      ⚠ distance à {config.site.anchor_station} non mesurable : la "
                    f"géométrie s'arrête à {link.anchor_offset_m:.0f} m de la gare."
                )
                print("        Augmentez search_radius_m, puis relancez avec --refresh.")
        print()

    if stops:
        print("Gares ferroviaires les plus proches :")
        for stop, distance in nearest_stops(stops, config.site.position, limit=4):
            print(f"  {distance:6.0f} m  {stop.name or '(sans nom)'} [{stop.kind}]")
        print()

    if anchor is not None and args.profile:
        for corridor in corridors:
            segments = speed_profile(corridor, ways, anchor)
            if not segments:
                continue
            print(
                f"Profil de voie, {config.site.anchor_station} → point, corridor "
                f"{corridor.corridor_id} « {corridor.label} » :"
            )
            # Le relevé suit une seule voie du faisceau. Les voies d'un même
            # corridor n'ont pas nécessairement la même limite, et un train peut
            # emprunter l'autre : lire ce profil comme celui du corridor entier
            # conduirait à des conclusions fausses.
            if corridor.segment_count > 1:
                print(
                    f"  ({corridor.segment_count} tronçons dans ce corridor ; le relevé "
                    "suit la voie que longe la polyligne,\n   les voies voisines peuvent "
                    "porter d'autres limites)"
                )
            print()
            for segment in segments:
                print(f"  {segment.describe()}")
            restricted = [s for s in segments if s.maxspeed_kmh and s.maxspeed_kmh < 80]
            if restricted:
                # Longueur fusionnée : deux tronçons qui se recouvrent ne
                # couvrent pas deux fois la même voie.
                covered = merged_length_m(restricted)
                print(
                    f"\n  Sous 80 km/h sur {covered:.0f} m des {segments[-1].end_m:.0f} m "
                    "du parcours : une restriction locale,\n  que le modèle ne sait pas "
                    "représenter — il n'applique qu'une vitesse par branche."
                )
            print()

    if args.geojson:
        import json

        collection = to_geojson(
            corridors, ways, config.site.position, anchor, config.site.anchor_station
        )
        args.geojson.parent.mkdir(parents=True, exist_ok=True)
        args.geojson.write_text(
            json.dumps(collection, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(
            f"Géométrie écrite dans {args.geojson} "
            f"({len(collection['features'])} objets).\n"
            "  Déposez le fichier sur https://geojson.io pour la voir sur un fond de carte.\n"
        )

    if anchor is not None:
        suggestions = suggest_branches(config, corridors)
        print("Correspondance branches ↔ corridors :\n")
        for branch, matches in suggestions:
            header = f"  {branch.branch_id:8s} (cap {branch.bearing_deg:5.1f}°) → "
            if not matches:
                print(header + "aucun corridor")
                continue
            print(header + f"{len(matches)} corridor(s)")
            for corridor, link in matches:
                print(
                    f"      {corridor.corridor_id} à {corridor.distance_m:4.0f} m, "
                    f"repart au cap {link.outbound_bearing_deg:3.0f}° · {corridor.label}"
                )
        print("\nÀ recopier dans votre configuration, en remplacement de vos sections de branches :\n")
        print(render_branch_toml(suggestions))
    return 0


def cmd_next(args: argparse.Namespace) -> int:
    """Affiche les prochains passages prédits devant le point."""
    config = _load(args)
    try:
        feed = _open_feed(config)
    except GtfsError as exc:
        print(exc, file=sys.stderr)
        return 2

    now = (
        args.at.astimezone(feed.timezone)
        if args.at is not None
        else datetime.now().astimezone(feed.timezone)
    )
    passages = _collect_passages(config, feed, now, use_realtime=not args.no_realtime)
    upcoming = next_passages(
        passages, now, horizon=timedelta(minutes=args.horizon), limit=args.limit
    )

    print(f"{config.site.name}")
    print(f"{now.strftime('%d/%m/%Y %H:%M:%S')} — prochaines {args.horizon} minutes\n")
    if not upcoming:
        print("Aucun passage prédit dans cette fenêtre.")
        return 0

    for passage in upcoming:
        if args.watch:
            countdown = (passage.announce_at - now).total_seconds() / 60
            print(f"  dans {countdown:5.1f} min  {passage.describe_watch()}")
            if args.ids:
                print(f"{'':20s}{passage.trip_id}")
        else:
            countdown = (passage.when - now).total_seconds() / 60
            print(f"  dans {countdown:5.1f} min  {passage.describe()}")
            if args.ids:
                print(f"{'':20s}{passage.trip_id}")

    if args.record:
        with Store(config.data.database) as store:
            saved = store.record_passages(config.site.name, passages, predicted_at=now)
        print(f"\n{saved} prédictions enregistrées dans {config.data.database}")
    return 0


def _build_detector(config: AppConfig) -> PassageDetector:
    return PassageDetector(
        trigger_db=config.sensor.trigger_db,
        release_db=config.sensor.release_db,
        min_duration_s=config.sensor.min_duration_s,
        max_duration_s=config.sensor.max_duration_s,
        refractory_s=config.sensor.refractory_s,
        baseline_half_life_s=config.sensor.baseline_half_life_s,
    )


def cmd_listen(args: argparse.Namespace) -> int:
    """Écoute le capteur et journalise les passages réellement observés."""
    config = _load(args)
    from .sensor.audio import AudioUnavailable, listen

    try:
        samples = listen(
            sample_rate_hz=config.sensor.sample_rate_hz,
            block_seconds=config.sensor.block_seconds,
            device=config.sensor.device,
        )
    except AudioUnavailable as exc:
        print(exc, file=sys.stderr)
        return 2

    horizon = f"{args.duration} min" if args.duration else "sans limite (Ctrl-C pour arrêter)"
    print(f"Écoute en cours sur « {config.site.name} » — {horizon}.")
    print("Seul le niveau sonore est calculé ; aucun son n'est enregistré.\n")

    with Store(config.data.database) as store:

        def on_detection(detection) -> None:
            store.record_detection(config.site.name, detection)
            print(f"  passage observé : {detection.describe()}", flush=True)

        def on_status(status: Status) -> None:
            print(
                f"  {status.at.strftime('%H:%M:%S')} · fond {status.baseline_db:6.1f} dB "
                f"· pic {status.peak_db:6.1f} dB (+{status.headroom_db:.1f}) "
                f"· {status.detections} détection(s)",
                flush=True,
            )

        try:
            stats = listen_session(
                _build_detector(config),
                samples,
                duration_s=args.duration * 60 if args.duration else None,
                on_detection=on_detection,
                on_status=on_status,
                status_every_s=args.status_every,
            )
        except KeyboardInterrupt:
            detections, predictions = store.counts(config.site.name)
            print(f"\nArrêt. {detections} détections et {predictions} prédictions en base.")
            return 0

    print(f"\n{stats.describe()}")
    if not stats.detections:
        print(
            "Aucun passage détecté. Si des trains sont pourtant passés, comparez le pic\n"
            f"au fond dans les points d'étape : le seuil actuel est de "
            f"{config.sensor.trigger_db:.0f} dB au-dessus du fond."
        )
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    """Rejoue un enregistrement de niveaux à travers le détecteur."""
    config = _load(args)
    stats = listen_session(_build_detector(config), read_levels(args.levels))

    print(f"{len(stats.detections)} passage(s) détecté(s) dans {args.levels} :")
    for detection in stats.detections:
        print(f"  {detection.describe()}")

    if args.record and stats.detections:
        with Store(config.data.database) as store:
            for detection in stats.detections:
                store.record_detection(config.site.name, detection)
        print(f"\nEnregistrées dans {config.data.database}")
    return 0


def _resolve_time(value: str) -> datetime:
    """Interprète « now », une heure du jour, ou une date-heure complète."""
    if value.lower() in ("now", "maintenant"):
        return datetime.now().astimezone()
    for pattern in ("%H:%M:%S", "%H:%M"):
        try:
            parsed = datetime.strptime(value, pattern).time()
        except ValueError:
            continue
        return datetime.combine(datetime.now().date(), parsed).astimezone()
    return _moment(value)


def cmd_observe(args: argparse.Namespace) -> int:
    """Consigne un passage réellement vu — ou l'absence d'un passage annoncé."""
    config = _load(args)
    moment = _resolve_time(args.time)

    with Store(config.data.database) as store:
        # Même règle que celle qu'expose le serveur HTTP sur `POST /api/observe`
        # (module partagé `service`) : désignation si elle est fournie, sinon
        # rattachement au passage prédit le plus proche.
        binding = resolve_binding(
            store, config.site, moment, args.tolerance, trip_id=args.trip_id
        )
        if args.trip_id and binding.passage is None:
            print(
                f"⚠ aucune circulation « {args.trip_id} » prédite à moins de 30 min : "
                "vérifiez l'identifiant, ou lancez d'abord `next --record`."
            )
        if binding.ambiguous:
            first, second = binding.candidates[0], binding.candidates[1]
            print(
                f"⚠ deux passages sont à portée ({first.headsign} à "
                f"{first.when:%H:%M:%S}, {second.headsign} à "
                f"{second.when:%H:%M:%S}) : observation laissée non liée."
            )
        bound = binding.passage

        observation = Observation(
            observed_at=None if args.not_seen else moment,
            kind=ObservationKind.NOT_SEEN if args.not_seen else ObservationKind.SEEN,
            source=args.source,
            trip_id=bound.trip_id if bound else None,
            direction=bound.direction.value if bound else None,
            anchor_time=bound.anchor_time if bound else (None if not args.not_seen else moment),
            precision_s=args.precision,
            note=args.note,
        )
        observation_id = store.record_observation(config.site.name, observation)

    # L'identifiant est imprimé pour que l'observation reste rétractable : une
    # observation mal rattachée fausse le recalage plus sûrement qu'une
    # observation manquante.
    print(f"Enregistré (n° {observation_id}) : {observation.describe()}")
    if bound is not None:
        gap = (moment - bound.when).total_seconds()
        comment = "désigné" if binding.method == "designated" else "le plus proche dans le temps"
        print(
            f"  rattaché à {bound.route_label} {bound.headsign} "
            f"prédit à {bound.when:%H:%M:%S} — écart {gap:+.0f} s ({comment})"
        )
        if binding.method == "nearest":
            print(f"  si ce n'est pas le bon : nexttraintosee forget {observation_id}")
    elif not args.not_seen:
        print("  aucun passage prédit à portée : observation conservée telle quelle.")
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    """Confronte les passages observés aux passages prédits et recale le modèle."""
    config = _load(args)
    end = args.until.astimezone() if args.until is not None else datetime.now().astimezone()
    start = end - timedelta(days=args.days)

    with Store(config.data.database) as store:
        detections = store.detections_between(config.site.name, start, end)
        reported = store.observations_between(config.site.name, start, end)
        passages = store.passages_between(config.site.name, start, end, config.site.branches)

    # Capteur et rapports humains sont la même donnée : un instant de passage.
    # Sauf ceux dont l'heure n'en est pas une — voir NON_TIMING_SOURCES.
    seen_all = [o for o in reported if o.kind is ObservationKind.SEEN]
    seen = [o for o in seen_all if times_the_passage(o)]
    echoes = len(seen_all) - len(seen)
    observations = [from_detection(d) for d in detections] + seen
    missed = [o for o in reported if o.kind is ObservationKind.NOT_SEEN]

    if not observations:
        print(
            "Aucun passage rapporté. Alimentez la base par `listen`, `replay --record`,\n"
            "ou `observe` pour une observation à la main."
        )
        return 1
    if not passages:
        print("Aucune prédiction enregistrée : lancez d'abord `next --record`.")
        return 1

    result = match_observations(observations, passages, tolerance_s=args.tolerance)
    print(
        f"Sur {args.days} jour(s) : {len(detections)} détections de capteur, "
        f"{len(seen)} rapports, {len(missed)} passages signalés absents"
    )
    if echoes:
        print(
            f"  {echoes} réponse(s) à une carte de confirmation écartée(s) : elles\n"
            "  reprennent l'heure prédite comme heure observée, donc un écart nul\n"
            "  quelle que soit la réalité. Préférez le bouton au moment du passage."
        )
    print(f"{result.summary()}\n")

    if result.matches:
        # Un passage prédit sans retard temps réel est un horaire théorique :
        # l'écart mesuré y mélange l'erreur du modèle et le retard réel du
        # train, que rien ne permet ensuite de séparer. Recaler là-dessus
        # revient à corriger le modèle du retard des trains.
        theoretical = [m for m in result.matches if m.passage.delay_s is None]
        if theoretical:
            share = 100 * len(theoretical) / len(result.matches)
            print(
                f"⚠ {len(theoretical)} appariement(s) sur {len(result.matches)} "
                f"({share:.0f} %) portent sur un horaire théorique, sans temps réel.\n"
                "  L'écart y confond erreur du modèle et retard du train : le recalage\n"
                "  qui suit en hérite. Pour des prédictions horodatées temps réel,\n"
                "  faites tourner `./scripts/collect.sh` en fond.\n"
            )

        calibration = calibrate(result.matches)
        print(f"Recalage : {calibration.describe()}")

        distances = {
            branch.branch_id: branch.track_distance_m
            for branch in config.site.branches
            if branch.track_distance_m is not None
        }
        runs = runs_from_matches(result.matches, distances)
        if len(runs) >= args.min_runs:
            profile, rms = fit_profile(runs, start=config.site.profile)
            print(
                f"\nProfil de marche ajusté sur {len(runs)} passages "
                f"(erreur résiduelle {rms:.1f} s) :\n"
                f"  accel_ms2 = {profile.accel_ms2:.2f}\n"
                f"  decel_ms2 = {profile.decel_ms2:.2f}\n"
                f"  line_speed_kmh = {profile.line_speed_kmh:.0f}\n"
                "Reportez ces valeurs dans la section [profile] de votre configuration."
            )
        else:
            print(
                f"\n{len(runs)} passage(s) exploitable(s) : il en faut au moins "
                f"{args.min_runs} pour ajuster le profil de marche."
            )

    if result.unmatched_detections:
        print(f"\n{len(result.unmatched_detections)} passage(s) observé(s) hors horaires publics :")
        for detection in result.unmatched_detections:
            print(f"  {detection.midpoint.strftime('%d/%m %H:%M:%S')} · {detection.describe()}")
        print(
            "\nCe sont les candidats fret / haut-le-pied / travaux : ils ne figurent\n"
            "dans aucun flux ouvert, seul le capteur les voit."
        )
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    """Confronte le modèle de marche aux horaires, sans capteur ni présence."""
    config = _load(args)
    if config.site.anchor_position is None:
        print("Renseignez anchor_lat / anchor_lon pour vérifier le modèle.", file=sys.stderr)
        return 2
    try:
        feed = _open_feed(config)
    except GtfsError as exc:
        print(exc, file=sys.stderr)
        return 2

    client = OverpassClient(cache_dir=config.data.overpass_cache)
    try:
        ways, _ = client.around(
            config.site.position,
            radius_m=config.search_radius_m,
            anchor=config.site.anchor_position,
            lookahead_m=config.branch_lookahead_m,
        )
    except OverpassError as exc:
        print(f"Overpass : {exc}\nLancez d'abord `tracks` pour constituer le cache.", file=sys.stderr)
        return 2

    corridors = build_corridors(
        ways, config.site.position, max_distance_m=config.search_radius_m
    )
    day = args.day or datetime.now().date()
    _warn_if_outside_coverage(feed, day)
    checks = check_segments(feed, corridors, config.site, day, min_trips=args.min_trips)
    if not checks:
        print("Aucun segment exploitable : trop peu de circulations, ou géométrie absente.")
        return 1

    print(f"Modèle de marche confronté aux horaires du {day.strftime('%d/%m/%Y')}\n")
    print(
        f"Accélération {config.site.profile.accel_ms2:.2f} m/s², "
        f"freinage {config.site.profile.decel_ms2:.2f} m/s² ; "
        "vitesse de ligne propre à chaque branche.\n"
    )

    spanning = [c for c in checks if c.covers_observer]
    for check in checks:
        marker = "▸" if check.covers_observer else " "
        print(f"{marker} {config.site.anchor_station} ↔ {check.neighbour}")
        branch = f" · branche {check.branch_id}" if check.branch_id else ""
        speed = f" · modèle à {check.line_speed_kmh:.0f} km/h" if check.line_speed_kmh else ""
        print(
            f"      {check.length_m:.0f} m par la voie · {check.trip_count} circulations"
            f"{branch}{speed}"
            f"{'' if check.covers_observer else ' · ne passe pas devant le point'}"
        )
        print(
            f"      horaire le plus rapide  {format_duration(check.fastest_s):>12s}"
            f"   ({check.implied_speed_kmh:.0f} km/h de moyenne)"
        )
        print(
            f"      modèle                  {format_duration(check.modelled_s):>12s}"
            f"   écart {check.gap_s:+.0f} s — {check.verdict}"
        )
        print(
            f"      marge horaire médiane   {format_duration(check.median_padding_s):>12s}"
            f"   étendue {format_duration(check.spread_s)}"
        )
        if not check.has_tight_run:
            print(
                "      ⚠ tous les horaires sont identiques : le minimum est une allocation"
            )
            print("        standard, pas une marche tendue — à ne pas prendre pour une limite.")
        print()

    if spanning:
        consistent = sum(1 for c in spanning if c.is_consistent)
        print(
            f"{consistent} segment(s) cohérent(s) sur {len(spanning)} qui encadrent le point.\n"
        )
    spanning_names = {c.neighbour for c in spanning}
    coverage = category_coverage(feed, config.site, spanning_names, day)
    if coverage:
        print("Ce que les horaires permettent de caler, par type de matériel :\n")
        for entry in coverage:
            if entry.is_calibratable:
                verdict = f"{entry.calibratable_count} segments mesurables"
            else:
                verdict = "aucun segment mesurable — profil estimé, à confirmer au capteur"
            print(f"  {entry.label:28s} {entry.segment_count:4d} segments · {verdict}")
        print()

    if args.fit:
        _print_speed_fit(config, feed, checks)

    print(
        "Lecture : l'horaire le plus rapide approche la limite physique, la médiane\n"
        "porte la marge de régularité. Un modèle plus rapide que tout horaire est\n"
        "trop optimiste ; nettement plus lent, il retardera les passages prédits.\n"
        "Cette vérification ne remplace pas un capteur : elle valide la marche, pas\n"
        "l'heure de passage, et ne voit aucune circulation absente des horaires."
    )
    return 0


def _print_speed_fit(config: AppConfig, feed, checks) -> None:
    """Propose une vitesse de ligne par branche, ajustée sur l'horaire le plus rapide."""
    anchor = config.site.anchor_position
    assert anchor is not None

    suggestions: dict[str, tuple[float, object]] = {}
    for check in checks:
        if not check.covers_observer:
            continue
        stops = [s for s in feed.find_stops_by_name(check.neighbour) if s.position]
        if not stops:
            continue
        branch = config.site.branch_for(initial_bearing_deg(anchor, stops[0].position))
        if branch is None:
            continue
        if not check.has_tight_run:
            continue
        speed = fit_line_speed_kmh(check.length_m, check.fastest_s, config.site.profile)
        # Le segment le plus long contraint le mieux la vitesse de palier.
        if branch.branch_id not in suggestions or check.length_m > suggestions[branch.branch_id][1].length_m:
            suggestions[branch.branch_id] = (speed, check)

    if not suggestions:
        print(
            "Aucune vitesse proposée : les segments disponibles n'ont pas d'horaire\n"
            "assez tendu pour approcher la limite physique.\n"
        )
        return

    print("Vitesse de ligne ajustée sur l'horaire le plus rapide, branche par branche :\n")
    # Mise en garde née d'une erreur constatée : sur le site de référence, cet
    # ajustement proposait 113 km/h pour une branche dont les 1 564 premiers
    # mètres se parcourent en réalité à 60 km/h équivalents. La valeur était
    # juste pour le segment entier, où un avant-gare lent est compensé par une
    # fin de parcours rapide — et fausse de 29 s sur la portion prédite.
    print(
        "  \u26a0 Ces vitesses sont calées sur le segment gare <-> gare voisine,\n"
        "    souvent bien plus long que les quelques kilomètres qui séparent la\n"
        "    gare du point d'observation. Quand la voie change de vitesse en\n"
        "    route — un avant-gare lent, une restriction — la moyenne obtenue est\n"
        "    juste pour le segment et fausse pour la portion prédite.\n"
        "    `tracks --profile` dit si le profil est uniforme ; s'il ne l'est pas,\n"
        "    préférez la vitesse qui reproduit le temps de parcours relevé.\n"
    )
    for branch_id, (speed, check) in sorted(suggestions.items()):
        print("[[branches]]")
        print(f'id = "{branch_id}"')
        print(
            f"line_speed_kmh = {speed:.0f}"
            f"   # {check.length_m:.0f} m en {format_duration(check.fastest_s)} vers {check.neighbour}"
        )
        print()


def cmd_histogram(args: argparse.Namespace) -> int:
    """Répartition horaire des passages devant le point."""
    config = _load(args)
    try:
        feed = _open_feed(config)
    except GtfsError as exc:
        print(exc, file=sys.stderr)
        return 2

    day = args.day or datetime.now().date()
    _warn_if_outside_coverage(feed, day)
    passages = predict_passages(feed, config.site, day)
    try:
        buckets = hourly_histogram(passages, args.first_hour, args.last_hour)
    except ValueError as exc:
        print(f"Plage horaire : {exc}", file=sys.stderr)
        return 2

    total = sum(b.total for b in buckets)
    print(f"{config.site.name}")
    print(f"Passages prédits le {french_date(day)}, "
          f"de {args.first_hour:02d} h à {args.last_hour:02d} h\n")
    print("  heure  nb   intervalle")
    print(render_histogram(buckets))
    print(f"\n  total {total:3d} passages sur la plage "
          f"({len(passages)} sur les 24 heures)")

    branches = Counter()
    categories = Counter()
    for bucket in buckets:
        branches.update(bucket.by_branch)
        categories.update(bucket.by_category)
    print(f"  par branche   : {dict(branches)}")
    print(f"  par catégorie : {dict(categories)}")

    if args.week:
        print("\nSur la semaine :\n")
        totals = []
        for offset in range(7):
            other = day + timedelta(days=offset)
            if not feed.calendar.covers(other):
                totals.append((other, None))
                continue
            totals.append((other, predict_passages(feed, config.site, other)))
        peak = max((len(p) for _, p in totals if p is not None), default=1) or 1
        for other, found in totals:
            if found is None:
                print(f"  {french_date(other):22s}   —  hors du flux")
                continue
            ter = sum(1 for x in found if x.category_id == "ter")
            print(
                f"  {french_date(other):22s} {len(found):3d}  "
                f"dont {ter:3d} TER  {'█' * round(32 * len(found) / peak)}"
            )

    if args.csv:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        with args.csv.open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle, lineterminator="\n").writerows(csv_rows(buckets))
        print(f"\nTableau écrit dans {args.csv}")
    return 0


def cmd_coverage(args: argparse.Namespace) -> int:
    """Mesure ce que valent les prédictions à l'avance, d'après l'historique."""
    config = _load(args)
    end = datetime.now().astimezone() + timedelta(days=args.days)
    start = end - timedelta(days=args.days * 2)

    with Store(config.data.database) as store:
        history = store.prediction_samples(config.site.name, start, end)

    if not history:
        print(
            "Aucun historique. Enregistrez des prédictions à intervalles réguliers :\n"
            "  while true; do nexttraintosee next --record --horizon 120; sleep 180; done"
        )
        return 1

    repeated = sum(1 for estimates in history.values() if len(estimates) > 1)
    print(f"{len(history)} passages suivis, dont {repeated} estimés plusieurs fois\n")

    print(f"{'échéance':<18s} {'estim.':>7s} {'temps réel':>11s} "
          f"{'dérive méd.':>12s} {'annoncé trop tard':>19s} {'pire':>8s}")
    for entry in analyse(history):
        if not entry.sample_count:
            print(f"{entry.bucket.label:<18s} {'—':>7s}")
            continue
        median = entry.drift_median_s
        late = f"{entry.too_late_count()} sur {entry.sample_count}"
        print(
            f"{entry.bucket.label:<18s} {entry.sample_count:>7d} "
            f"{entry.realtime_share:>10.0%} "
            f"{(f'{median:.0f} s' if median is not None else '—'):>12s} "
            f"{late:>13s} {entry.too_late_share():>5.1%} "
            f"{entry.too_late_worst_s():>6.0f} s"
        )

    summary = summarise_delays(history)
    print(
        f"\nRetards : {summary.with_realtime}/{summary.passage_count} passages suivis en "
        f"temps réel ({summary.realtime_share:.0%})"
    )
    if summary.with_realtime:
        print(
            f"          {summary.on_time_share:.0%} à l'heure (moins d'une minute), "
            f"médiane {summary.median_delay_s:+.0f} s, "
            f"pire {summary.worst_delay_s:+.0f} s"
        )

    print(
        "\nLecture : « annoncé trop tard » est la seule erreur qui coûte vraiment —\n"
        "l'estimation précoce plaçait le passage après son heure finale, donc\n"
        "quelqu'un se serait posté après le passage du train. Une annonce en avance\n"
        "ne coûte que de l'attente. Tout ceci mesure la stabilité, pas la justesse :\n"
        "une prédiction stable et fausse passerait inaperçue, seul un capteur la\n"
        "démasquerait."
    )
    return 0


def cmd_forget(args: argparse.Namespace) -> int:
    """Efface une observation — typiquement une qui s'est liée au mauvais train."""
    config = _load(args)
    with Store(config.data.database) as store:
        removed = store.delete_observation(config.site.name, args.observation_id)

    if removed:
        print(f"Observation n° {args.observation_id} effacée.")
        return 0
    print(
        f"Aucune observation n° {args.observation_id} pour ce site.",
        file=sys.stderr,
    )
    return 1


def cmd_doctor(args: argparse.Namespace) -> int:
    """Vérifie ce qui est disponible dans l'environnement."""
    config = _load(args)
    print(f"Configuration : {args.config}")
    print(f"  site                 {config.site.name}")
    print(f"  gare d'appui         {config.site.anchor_station}")
    print(
        f"  branches visibles    "
        f"{', '.join(b.branch_id for b in config.site.branches if b.passes_observer)}"
    )

    gtfs = config.data.gtfs_path
    size = f"{gtfs.stat().st_size / 1e6:.0f} Mo" if gtfs.exists() else "absent"
    print(f"\n  GTFS statique        {gtfs} ({size})")
    if not gtfs.exists():
        print(f"      → curl -L -o {gtfs} {GTFS_DOWNLOAD_URL}")
    else:
        try:
            window = _open_feed(config).calendar.coverage()
        except GtfsError:
            window = None
        if window is not None:
            remaining = (window[1] - datetime.now().date()).days
            print(
                f"      couvre du {window[0].strftime('%d/%m/%Y')} au "
                f"{window[1].strftime('%d/%m/%Y')} — {remaining} jours restants"
            )
            if remaining < 30:
                print("      → pensez à retélécharger le flux")

    for module, extra, purpose in (
        ("google.transit.gtfs_realtime_pb2", "realtime", "temps réel GTFS-RT"),
        ("sounddevice", "sensor", "capture audio"),
        ("numpy", "sensor", "traitement du signal"),
    ):
        print(f"  {purpose:20s} {dependency_status(module, extra)}")

    database = config.data.database
    if database.exists():
        with Store(database) as store:
            detections, predictions = store.counts(config.site.name)
        print(f"\n  journal              {detections} détections, {predictions} prédictions")
    else:
        print(f"\n  journal              {database} (pas encore créé)")
    return 0


def _lan_ip() -> str | None:
    """Adresse IP locale du poste, telle que la verrait un téléphone du réseau.

    Ouvrir un socket UDP vers une adresse extérieure ne transmet rien : cela ne
    sert qu'à demander au système quelle interface il choisirait.
    """
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("192.0.2.1", 80))
            return probe.getsockname()[0]
        finally:
            probe.close()
    except OSError:
        return None


def _mesh_ip() -> str | None:
    """Adresse du poste sur un réseau maillé privé, s'il y en a un.

    Tailscale et consorts attribuent des adresses dans 100.64.0.0/10. Elles
    valent bien plus que l'adresse du réseau local pour ce projet : elles
    restent joignables depuis un téléphone en 4G, donc depuis le point
    d'observation lui-même, là où le Wi-Fi de la maison ne porte pas.
    """
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return None
    for info in infos:
        address = info[4][0]
        first, second = (int(part) for part in address.split(".")[:2])
        if first == 100 and 64 <= second <= 127:
            return address
    return None


def _mdns_hostname() -> str:
    """Nom d'hôte mDNS du poste, tel qu'un iPhone le résoudrait en « .local ».

    Sur macOS, `socket.gethostname()` renvoie déjà un nom suffixé de
    « .local » : ajouter le suffixe sans condition produirait
    « machine.local.local », que personne ne peut joindre.
    """
    hostname = socket.gethostname()
    return hostname if hostname.endswith(".local") else f"{hostname}.local"


def cmd_serve(args: argparse.Namespace) -> int:
    """Sert la PWA et l'API HTTP décrites par le contrat du projet (§ 4)."""
    config = _load(args)
    try:
        service = PassageService(config, refresh_every_s=args.refresh_every)
    except GtfsError as exc:
        print(exc, file=sys.stderr)
        return 2

    # Premier chargement synchrone : le cache n'est jamais vide au démarrage,
    # même si la boucle périodique n'a pas encore tourné.
    service.refresh()
    service.start()

    webapp_dir = args.webapp_dir if args.webapp_dir is not None else DEFAULT_WEBAPP_DIR
    try:
        server = create_server(service, host=args.host, port=args.port, webapp_dir=webapp_dir)
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
        # Occuper un port déjà pris est la façon la plus banale d'échouer ici,
        # et la plus probable est qu'on ait simplement relancé un serveur déjà
        # en marche. Une trace Python n'aide personne à s'en rendre compte.
        service.stop()
        print(
            f"Le port {args.port} est déjà occupé.\n"
            "\n"
            "  Si `serve` tourne déjà, l'application est accessible : pas besoin\n"
            "  de le relancer.\n"
            "\n"
            f"  Pour savoir qui l'occupe :  lsof -i :{args.port}\n"
            f"  Pour en utiliser un autre :  nexttraintosee serve --port {args.port + 1}",
            file=sys.stderr,
        )
        return 2
    port = server.server_address[1]
    hostname = _mdns_hostname()
    lan_ip = _lan_ip()
    mesh_ip = _mesh_ip()

    print(f"{config.site.name}")
    print(f"Serveur démarré sur http://{args.host}:{port}")
    if mesh_ip:
        # La seule adresse qui vaille depuis la passerelle : le Wi-Fi de la
        # maison n'y porte pas, un réseau maillé privé si.
        print(f"Depuis le téléphone, partout (réseau maillé) : http://{mesh_ip}:{port}")
    if lan_ip:
        # Android ne résout pas les noms mDNS en « .local » de façon fiable :
        # l'adresse IP est la seule qui fonctionne à coup sûr sur un Pixel.
        print(f"Depuis le téléphone (même Wi-Fi) : http://{lan_ip}:{port}")
        print(f"                     (ou, si iOS : http://{hostname}:{port})")
    else:
        print(f"Depuis le téléphone (même Wi-Fi) : http://{hostname}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêt du serveur.")
    finally:
        service.stop()
        server.shutdown()
        server.server_close()
    return 0


# -- point d'entrée ----------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nexttraintosee",
        description="Prédire le passage des trains devant un point d'observation.",
    )
    parser.add_argument(
        "-c",
        "--config",
        default="config/toulouse-guilhemery.toml",
        type=Path,
        help="fichier de configuration TOML du site",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="journalisation détaillée")
    subparsers = parser.add_subparsers(dest="command", required=True)

    tracks = subparsers.add_parser("tracks", help="résoudre les voies via OpenStreetMap")
    tracks.add_argument("--refresh", action="store_true", help="ignorer le cache Overpass")
    tracks.add_argument(
        "--include-service", action="store_true", help="inclure les voies de service"
    )
    tracks.add_argument(
        "--geojson", type=Path, default=None,
        help="écrire la géométrie résolue dans un fichier GeoJSON, à ouvrir sur une carte",
    )
    tracks.add_argument(
        "--profile", action="store_true",
        help="relever vitesses, tunnels et ponts le long du parcours jusqu'à la gare",
    )
    tracks.set_defaults(func=cmd_tracks)

    upcoming = subparsers.add_parser("next", help="prochains passages prédits")
    upcoming.add_argument("--horizon", type=int, default=120, help="fenêtre en minutes")
    upcoming.add_argument("--limit", type=int, default=20, help="nombre maximal de passages")
    upcoming.add_argument(
        "--no-realtime", action="store_true", help="horaires théoriques seulement"
    )
    upcoming.add_argument("--record", action="store_true", help="journaliser les prédictions")
    upcoming.add_argument(
        "--ids", action="store_true",
        help="afficher l'identifiant de chaque circulation, à passer à `observe --trip-id`",
    )
    upcoming.add_argument(
        "--watch",
        action="store_true",
        help="annoncer le plus tôt où le train peut passer, plutôt que l'heure centrale",
    )
    upcoming.add_argument(
        "--at",
        type=_moment,
        default=None,
        help="raisonner à cet instant (ISO 8601) plutôt qu'à maintenant",
    )
    upcoming.set_defaults(func=cmd_next)

    listening = subparsers.add_parser("listen", help="écouter le capteur audio")
    listening.add_argument(
        "--duration", type=float, default=None, help="durée de la session, en minutes"
    )
    listening.add_argument(
        "--status-every", type=float, default=60.0, help="intervalle des points d'étape, en secondes"
    )
    listening.set_defaults(func=cmd_listen)

    replaying = subparsers.add_parser("replay", help="rejouer un enregistrement de niveaux")
    replaying.add_argument("levels", type=Path, help="CSV timestamp,level")
    replaying.add_argument("--record", action="store_true", help="journaliser les détections")
    replaying.set_defaults(func=cmd_replay)

    calibrating = subparsers.add_parser("calibrate", help="recaler le modèle sur les observations")
    calibrating.add_argument("--days", type=int, default=7, help="profondeur d'historique")
    calibrating.add_argument(
        "--tolerance", type=float, default=180.0, help="écart maximal d'appariement, en secondes"
    )
    calibrating.add_argument(
        "--min-runs", type=int, default=8, help="observations minimales pour ajuster le profil"
    )
    calibrating.add_argument(
        "--until",
        type=_moment,
        default=None,
        help="fin de la fenêtre d'analyse (ISO 8601) ; par défaut maintenant",
    )
    calibrating.set_defaults(func=cmd_calibrate)

    validating = subparsers.add_parser(
        "validate", help="confronter le modèle de marche aux horaires"
    )
    validating.add_argument(
        "--day", type=lambda v: datetime.strptime(v, "%Y-%m-%d").date(), default=None,
        help="journée de service à examiner (AAAA-MM-JJ)",
    )
    validating.add_argument(
        "--min-trips", type=int, default=5, help="circulations minimales par segment"
    )
    validating.add_argument(
        "--fit", action="store_true", help="proposer une vitesse de ligne par branche"
    )
    validating.set_defaults(func=cmd_validate)

    histogram = subparsers.add_parser(
        "histogram", help="répartition horaire des passages"
    )
    histogram.add_argument(
        "--day", type=lambda v: datetime.strptime(v, "%Y-%m-%d").date(), default=None,
        help="journée de service (AAAA-MM-JJ)",
    )
    histogram.add_argument("--first-hour", type=int, default=5, help="première heure incluse")
    histogram.add_argument("--last-hour", type=int, default=23, help="première heure exclue")
    histogram.add_argument(
        "--week", action="store_true", help="comparer les sept jours à partir de --day"
    )
    histogram.add_argument("--csv", type=Path, default=None, help="écrire aussi un tableau CSV")
    histogram.set_defaults(func=cmd_histogram)

    covering = subparsers.add_parser(
        "coverage", help="mesurer couverture temps réel et stabilité des prédictions"
    )
    covering.add_argument("--days", type=int, default=2, help="profondeur d'historique")
    covering.set_defaults(func=cmd_coverage)

    observing = subparsers.add_parser(
        "observe", help="consigner un passage réellement vu"
    )
    observing.add_argument("time", help="« now », HH:MM[:SS], ou une date-heure ISO 8601")
    observing.add_argument(
        "--not-seen", action="store_true",
        help="le passage annoncé à cette heure n'a pas eu lieu",
    )
    observing.add_argument("--source", default="manuel", help="origine de l'observation")
    observing.add_argument(
        "--precision", type=float, default=5.0, help="précision revendiquée, en secondes"
    )
    observing.add_argument("--note", default="", help="remarque libre")
    observing.add_argument(
        "--trip-id", default=None,
        help="désigner la circulation vue, au lieu de la deviner par l'heure "
             "(identifiant affiché par `next`)",
    )
    observing.add_argument(
        "--tolerance", type=float, default=180.0,
        help="écart maximal pour rattacher l'observation à un passage prédit "
             "quand aucune circulation n'est désignée",
    )
    observing.set_defaults(func=cmd_observe)

    forgetting = subparsers.add_parser(
        "forget", help="effacer une observation mal rattachée"
    )
    forgetting.add_argument(
        "observation_id", type=int, help="numéro affiché par `observe`"
    )
    forgetting.set_defaults(func=cmd_forget)

    doctor = subparsers.add_parser("doctor", help="vérifier l'environnement")
    doctor.set_defaults(func=cmd_doctor)

    serving = subparsers.add_parser(
        "serve", help="servir la PWA et l'API HTTP (téléphone, même Wi-Fi)"
    )
    serving.add_argument("--port", type=int, default=8770, help="port d'écoute")
    serving.add_argument("--host", default="0.0.0.0", help="adresse d'écoute")
    serving.add_argument(
        "--refresh-every", type=float, default=90.0,
        help="intervalle de rafraîchissement du temps réel, en secondes",
    )
    serving.add_argument(
        "--webapp-dir", type=Path, default=None,
        help="dossier statique à servir (par défaut : webapp/ du dépôt)",
    )
    serving.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"Configuration : {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
