"""Serveur HTTP local : sert la PWA et l'API décrite par le contrat du projet.

Repose entièrement sur `http.server` de la bibliothèque standard
(`ThreadingHTTPServer`), fidèle au principe « noyau sans dépendance » du
projet — voir `docs/app-v0.md` § 1. Toute la logique de prédiction et de
rattachement vit dans `service.PassageService` ; ce module ne fait que
traduire des requêtes HTTP en appels à ce service, et ses réponses en JSON.
"""

from __future__ import annotations

import json
import logging
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .service import PassageService

log = logging.getLogger(__name__)

#: `webapp/` à la racine du dépôt : .../src/nexttraintosee/server.py -> dépôt.
DEFAULT_WEBAPP_DIR = Path(__file__).resolve().parents[2] / "webapp"


class RequestHandler(BaseHTTPRequestHandler):
    """Traduit les routes du contrat d'API, sert le reste en statique.

    `server` est une instance de `Server` (voir plus bas) : `self.server`
    porte donc `.service` et `.webapp_dir`, ce que mypy/le typage statique
    n'exprime pas ici faute d'attribut typé sur `BaseHTTPRequestHandler`.
    """

    server_version = "NextTrainToSee/0.1"

    # -- journalisation : sobre, une ligne par requête API seulement ---------

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - imposé par la stdlib
        """Réduit au silence la journalisation par défaut de la stdlib.

        Chaque route API journalise explicitement via `_log_api`, une fois sa
        réponse connue ; le statique, lui, ne journalise jamais.
        """
        return

    def _log_api(self, status: int) -> None:
        log.info("%s %s -> %d", self.command, self.path, status)

    # -- réponses --------------------------------------------------------

    def _send_json(self, payload: dict, status: int) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- routage -----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - nom imposé par la stdlib
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)

        if path == "/api/next":
            self._handle_next(parse_qs(parsed.query))
        elif path == "/api/histogram":
            self._handle_histogram()
        elif path == "/api/health":
            self._handle_health()
        elif path.startswith("/api/"):
            self._send_json({"error": "route inconnue"}, 404)
            self._log_api(404)
        else:
            self._serve_static(path)

    def do_POST(self) -> None:  # noqa: N802 - nom imposé par la stdlib
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)

        if path == "/api/observe":
            self._handle_observe()
        else:
            self._send_json({"error": "route inconnue"}, 404)
            self._log_api(404)

    # -- API -----------------------------------------------------------------

    @property
    def _service(self) -> PassageService:
        return self.server.service  # type: ignore[attr-defined]

    def _handle_next(self, query: dict) -> None:
        raw_limit = query.get("limit", ["2"])[0]
        try:
            limit = int(raw_limit)
        except ValueError:
            limit = 2
        payload = self._service.next_response(limit=limit)
        self._send_json(payload, 200)
        self._log_api(200)

    def _handle_histogram(self) -> None:
        payload = self._service.histogram_response()
        self._send_json(payload, 200)
        self._log_api(200)

    def _handle_health(self) -> None:
        payload = self._service.health_response()
        self._send_json(payload, 200)
        self._log_api(200)

    def _handle_observe(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json({"error": "corps JSON invalide"}, 400)
            self._log_api(400)
            return

        try:
            result = self._service.observe(payload)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, 400)
            self._log_api(400)
            return
        self._send_json(result, 200)
        self._log_api(200)

    # -- statique --------------------------------------------------------

    def _serve_static(self, path: str) -> None:
        relative = path.lstrip("/") or "index.html"
        if relative.endswith("/"):
            relative += "index.html"

        webapp_dir: Path = self.server.webapp_dir  # type: ignore[attr-defined]
        candidate = (webapp_dir / relative).resolve()
        try:
            candidate.relative_to(webapp_dir)
        except ValueError:
            # Chemin qui sort du dossier statique (« .. » après résolution).
            self._send_json({"error": "chemin refusé"}, 404)
            return

        if not candidate.is_file():
            self._send_json({"error": "ressource introuvable"}, 404)
            return

        content_type, _ = mimetypes.guess_type(candidate.name)
        data = candidate.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class Server(ThreadingHTTPServer):
    """`ThreadingHTTPServer` portant le service et le dossier statique.

    Un thread par requête : le calcul d'une réponse (ou la lecture d'un
    fichier) ne bloque jamais les autres clients — utile depuis un téléphone
    qui rafraîchit `/api/next` toutes les 30 s pendant qu'on ouvre la bottom
    sheet de l'histogramme.
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self, address: tuple[str, int], service: PassageService, webapp_dir: Path
    ) -> None:
        self.service = service
        self.webapp_dir = Path(webapp_dir).resolve()
        super().__init__(address, RequestHandler)


def create_server(
    service: PassageService,
    *,
    host: str = "0.0.0.0",
    port: int = 8770,
    webapp_dir: Path = DEFAULT_WEBAPP_DIR,
) -> Server:
    """Construit le serveur, sans le démarrer.

    `port=0` laisse le système en choisir un — c'est ce que font les tests
    pour ne jamais entrer en conflit avec un port déjà occupé ; le port
    effectivement lié se lit ensuite sur `server.server_address[1]`.
    """
    return Server((host, port), service, webapp_dir)
