"""Interface en ligne de commande."""

from __future__ import annotations

import argparse
import importlib
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

from .config import AppConfig, ConfigError, load_config
from .geo import bearing_distance_deg
from .gtfs import GtfsError, GtfsFeed
from .matching import calibrate, fit_profile, match_detections, runs_from_matches
from .osm import OverpassClient, OverpassError, build_corridors, nearest_stops
from .predict import Passage, next_passages, predict_passages, service_days_around
from .realtime import RealtimeError, empty_snapshot, load_snapshot
from .sensor.base import PassageDetector
from .sensor.replay import read_levels
from .store import Store

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
    """Associe chaque branche configurée au corridor qui lui correspond.

    Une branche est définie par le cap sous lequel on quitte la gare d'appui ;
    un corridor sait vers où il part une fois passé le point d'observation. On
    apparie les deux, et une branche sans corridor est une branche qui ne passe
    pas devant le point.

    Returns:
        Un couple (branche, corridor ou None, lien vers la gare ou None) par
        branche configurée.
    """
    anchor = config.site.anchor_position
    links = {}
    if anchor is not None:
        links = {corridor.corridor_id: corridor.link_to(anchor) for corridor in corridors}

    suggestions = []
    for branch in config.site.branches:
        best = None
        for corridor in corridors:
            link = links.get(corridor.corridor_id)
            if link is None or link.outbound_bearing_deg is None:
                continue
            gap = bearing_distance_deg(branch.bearing_deg, link.outbound_bearing_deg)
            if gap <= branch.tolerance_deg and (best is None or gap < best[0]):
                best = (gap, corridor, link)
        suggestions.append((branch, best[1] if best else None, best[2] if best else None))
    return suggestions


def render_branch_toml(suggestions) -> str:
    """Bloc TOML prêt à coller, déduit de la géométrie mesurée."""
    lines = []
    for branch, corridor, link in suggestions:
        lines.append("[[branches]]")
        lines.append(f'id = "{branch.branch_id}"')
        lines.append(f'label = "{branch.label}"')
        lines.append(f"bearing_deg = {branch.bearing_deg}")
        if corridor is None:
            lines.append("passes_observer = false   # aucun corridor ne part dans cette direction")
        else:
            lines.append("passes_observer = true")
            if link is not None and link.is_plausible:
                lines.append(
                    f"track_distance_m = {link.along_distance_m:.0f}"
                    f"   # corridor {corridor.corridor_id}, à {corridor.distance_m:.0f} m du point"
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
            link = corridor.link_to(anchor)
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

    if anchor is not None:
        suggestions = suggest_branches(config, corridors)
        print("Correspondance branches ↔ corridors :\n")
        for branch, corridor, link in suggestions:
            if corridor is None:
                print(f"  {branch.branch_id:8s} (cap {branch.bearing_deg:5.1f}°) → aucun corridor")
            else:
                heading = link.outbound_bearing_deg if link else None
                print(
                    f"  {branch.branch_id:8s} (cap {branch.bearing_deg:5.1f}°) → "
                    f"{corridor.corridor_id} « {corridor.label} » "
                    f"(repart au cap {heading:.0f}°)"
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
        countdown = (passage.when - now).total_seconds() / 60
        print(f"  dans {countdown:5.1f} min  {passage.describe()}")

    if args.record:
        with Store(config.data.database) as store:
            saved = store.record_passages(config.site.name, passages, predicted_at=now)
        print(f"\n{saved} prédictions enregistrées dans {config.data.database}")
    return 0


def cmd_listen(args: argparse.Namespace) -> int:
    """Écoute le capteur et journalise les passages réellement observés."""
    config = _load(args)
    from .sensor.audio import AudioUnavailable, listen

    detector = PassageDetector(
        trigger_db=config.sensor.trigger_db,
        release_db=config.sensor.release_db,
        min_duration_s=config.sensor.min_duration_s,
        max_duration_s=config.sensor.max_duration_s,
        refractory_s=config.sensor.refractory_s,
        baseline_half_life_s=config.sensor.baseline_half_life_s,
    )

    try:
        samples = listen(
            sample_rate_hz=config.sensor.sample_rate_hz,
            block_seconds=config.sensor.block_seconds,
            device=config.sensor.device,
        )
    except AudioUnavailable as exc:
        print(exc, file=sys.stderr)
        return 2

    print(f"Écoute en cours sur « {config.site.name} ». Ctrl-C pour arrêter.\n")
    with Store(config.data.database) as store:
        try:
            for detection in detector.feed(samples):
                store.record_detection(config.site.name, detection)
                print(f"  passage observé : {detection.describe()}")
        except KeyboardInterrupt:
            leftover = detector.flush()
            if leftover is not None:
                store.record_detection(config.site.name, leftover)
            detections, predictions = store.counts(config.site.name)
            print(f"\nArrêt. {detections} détections et {predictions} prédictions en base.")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    """Rejoue un enregistrement de niveaux à travers le détecteur."""
    config = _load(args)
    detector = PassageDetector(
        trigger_db=config.sensor.trigger_db,
        release_db=config.sensor.release_db,
        min_duration_s=config.sensor.min_duration_s,
        max_duration_s=config.sensor.max_duration_s,
        refractory_s=config.sensor.refractory_s,
        baseline_half_life_s=config.sensor.baseline_half_life_s,
    )

    detections = list(detector.feed(read_levels(args.levels)))
    leftover = detector.flush()
    if leftover is not None:
        detections.append(leftover)

    print(f"{len(detections)} passage(s) détecté(s) dans {args.levels} :")
    for detection in detections:
        print(f"  {detection.describe()}")

    if args.record and detections:
        with Store(config.data.database) as store:
            for detection in detections:
                store.record_detection(config.site.name, detection)
        print(f"\nEnregistrées dans {config.data.database}")
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    """Confronte les passages observés aux passages prédits et recale le modèle."""
    config = _load(args)
    end = args.until.astimezone() if args.until is not None else datetime.now().astimezone()
    start = end - timedelta(days=args.days)

    with Store(config.data.database) as store:
        detections = store.detections_between(config.site.name, start, end)
        passages = store.passages_between(config.site.name, start, end, config.site.branches)

    if not detections:
        print("Aucune détection enregistrée : lancez d'abord `listen` ou `replay --record`.")
        return 1
    if not passages:
        print("Aucune prédiction enregistrée : lancez d'abord `next --record`.")
        return 1

    result = match_detections(detections, passages, tolerance_s=args.tolerance)
    print(f"Sur {args.days} jour(s) : {result.summary()}\n")

    if result.matches:
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
            print(f"  {detection.started_at.strftime('%d/%m %H:%M:%S')} · {detection.describe()}")
        print(
            "\nCe sont les candidats fret / haut-le-pied / travaux : ils ne figurent\n"
            "dans aucun flux ouvert, seul le capteur les voit."
        )
    return 0


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
    tracks.set_defaults(func=cmd_tracks)

    upcoming = subparsers.add_parser("next", help="prochains passages prédits")
    upcoming.add_argument("--horizon", type=int, default=120, help="fenêtre en minutes")
    upcoming.add_argument("--limit", type=int, default=20, help="nombre maximal de passages")
    upcoming.add_argument(
        "--no-realtime", action="store_true", help="horaires théoriques seulement"
    )
    upcoming.add_argument("--record", action="store_true", help="journaliser les prédictions")
    upcoming.add_argument(
        "--at",
        type=_moment,
        default=None,
        help="raisonner à cet instant (ISO 8601) plutôt qu'à maintenant",
    )
    upcoming.set_defaults(func=cmd_next)

    listening = subparsers.add_parser("listen", help="écouter le capteur audio")
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

    doctor = subparsers.add_parser("doctor", help="vérifier l'environnement")
    doctor.set_defaults(func=cmd_doctor)

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
