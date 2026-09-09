"""Tests du serveur HTTP : contrat d'API, statique, erreurs.

Le serveur écoute sur le port 0 (choisi par le système) et est arrêté à la
fin de chaque test — aucun thread ne doit traverser d'un test à l'autre.
Aucune requête réseau externe : tout passe par `http.client` en boucle
locale, et le temps réel est désactivé sur le service sous-jacent.
"""

from __future__ import annotations

import http.client
import json
import threading
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from nexttraintosee.config import AppConfig, DataPaths
from nexttraintosee.server import create_server
from nexttraintosee.service import PassageService

PARIS = ZoneInfo("Europe/Paris")
MORNING = datetime(2026, 9, 9, 8, 0, tzinfo=PARIS)  # mercredi : jour ouvré du mini-GTFS


@pytest.fixture
def config(tmp_path: Path, gtfs_zip: Path, site) -> AppConfig:
    return AppConfig(
        site=site,
        data=DataPaths(gtfs_path=gtfs_zip, database=tmp_path / "journal.sqlite"),
    )


@pytest.fixture
def webapp_dir(tmp_path: Path) -> Path:
    """Dossier statique de test, disjoint du vrai `webapp/` du dépôt."""
    root = tmp_path / "webapp"
    root.mkdir()
    (root / "index.html").write_text("<!doctype html><title>accueil</title>", encoding="utf-8")
    (root / "app.js").write_text("console.log('ok');", encoding="utf-8")
    assets = root / "assets"
    assets.mkdir()
    (assets / "style.css").write_text("body { margin: 0; }", encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("ne doit jamais être servi", encoding="utf-8")
    return root


@pytest.fixture
def running_server(config: AppConfig, webapp_dir: Path):
    """Serveur démarré sur le port 0, arrêté proprement en fin de test."""
    service = PassageService(config, use_realtime=False, refresh_every_s=3600.0)
    service.refresh(now=MORNING)

    server = create_server(service, host="127.0.0.1", port=0, webapp_dir=webapp_dir)
    thread = threading.Thread(target=server.serve_forever, name="test-server", daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5.0)
        server.server_close()
        service.stop()
        assert not thread.is_alive()


def _connection(server) -> http.client.HTTPConnection:
    host, port = server.server_address[0], server.server_address[1]
    return http.client.HTTPConnection(host, port, timeout=5.0)


def _get_json(server, path: str) -> tuple[int, dict]:
    conn = _connection(server)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        body = response.read()
        return response.status, json.loads(body.decode("utf-8"))
    finally:
        conn.close()


def _post_json(server, path: str, payload) -> tuple[int, dict]:
    conn = _connection(server)
    try:
        body = json.dumps(payload).encode("utf-8")
        conn.request("POST", path, body=body, headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        return response.status, json.loads(response.read().decode("utf-8"))
    finally:
        conn.close()


# -- /api/next -------------------------------------------------------------------


def test_next_route_returns_the_contract_shape(running_server):
    status, payload = _get_json(running_server, "/api/next")
    assert status == 200
    assert set(payload) == {"generated_at", "site", "realtime", "passages"}
    assert len(payload["passages"]) <= 2  # limite par défaut


def test_next_route_honours_the_limit_query_parameter(running_server):
    status, payload = _get_json(running_server, "/api/next?limit=1")
    assert status == 200
    assert len(payload["passages"]) <= 1


def test_next_route_clamps_an_excessive_limit_to_ten(running_server):
    status, payload = _get_json(running_server, "/api/next?limit=999")
    assert status == 200
    assert len(payload["passages"]) <= 10


def test_next_route_computes_direction_label_and_look_server_side(running_server):
    _, payload = _get_json(running_server, "/api/next?limit=10")
    for passage in payload["passages"]:
        if passage["direction"] == "outbound":
            assert passage["direction_label"] == "From Matabiau"
            assert passage["look"] == "tunnel"
        else:
            assert passage["direction_label"] == "To Matabiau"
            assert passage["look"] == "sud"


# -- /api/histogram --------------------------------------------------------------


def test_histogram_route_returns_the_contract_shape(running_server):
    status, payload = _get_json(running_server, "/api/histogram")
    assert status == 200
    assert payload["first_hour"] == 5
    assert payload["last_hour"] == 23
    assert "peak" in payload
    for series in ("weekday", "weekend"):
        assert len(payload[series]["hours"]) == 18

    totals = [h["total"] for h in payload["weekday"]["hours"] + payload["weekend"]["hours"]]
    assert payload["peak"] == max(totals)


# -- /api/health -----------------------------------------------------------------


def test_health_route_reports_the_feed_window_and_cache(running_server):
    status, payload = _get_json(running_server, "/api/health")
    assert status == 200
    assert payload["ok"] is True
    assert payload["feed_start"] == "2026-01-01"
    assert payload["feed_end"] == "2026-12-31"
    assert payload["passages_cached"] > 0


# -- POST /api/observe ------------------------------------------------------------


def test_observe_route_binds_a_seen_passage(running_server):
    _, next_payload = _get_json(running_server, "/api/next?limit=1")
    target = datetime.fromisoformat(next_payload["passages"][0]["when"])
    observed_at = target + timedelta(seconds=10)

    status, payload = _post_json(
        running_server,
        "/api/observe",
        {"seen": True, "observed_at": observed_at.isoformat(), "precision_s": 3, "source": "app"},
    )
    assert status == 200
    assert payload["recorded"] is True
    assert payload["ambiguous"] is False
    assert payload["bound_to"]["trip_id"] == next_payload["passages"][0]["trip_id"]


def test_observe_route_accepts_a_not_seen_report(running_server):
    status, payload = _post_json(
        running_server, "/api/observe", {"seen": False, "anchor": "2026-09-09T03:00:00+02:00"}
    )
    assert status == 200
    assert payload["recorded"] is True
    assert payload["bound_to"] is None


def test_observe_route_rejects_a_body_without_seen(running_server):
    status, payload = _post_json(running_server, "/api/observe", {"observed_at": "2026-09-09T08:00:00+02:00"})
    assert status == 400
    assert "error" in payload


def test_observe_route_rejects_malformed_json(running_server):
    conn = _connection(running_server)
    try:
        conn.request(
            "POST", "/api/observe", body=b"{ceci n'est pas du json",
            headers={"Content-Type": "application/json"},
        )
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        assert response.status == 400
        assert "error" in payload
    finally:
        conn.close()


# -- routes inconnues et statique -------------------------------------------------


def test_an_unknown_api_route_is_a_404(running_server):
    status, payload = _get_json(running_server, "/api/does-not-exist")
    assert status == 404
    assert "error" in payload


def test_an_unknown_post_route_is_a_404(running_server):
    status, payload = _post_json(running_server, "/api/does-not-exist", {})
    assert status == 404
    assert "error" in payload


def test_the_index_is_served_at_the_root(running_server):
    conn = _connection(running_server)
    try:
        conn.request("GET", "/")
        response = conn.getresponse()
        body = response.read().decode("utf-8")
        assert response.status == 200
        assert "text/html" in response.getheader("Content-Type", "")
        assert "accueil" in body
    finally:
        conn.close()


def test_a_nested_static_asset_gets_the_right_mime_type(running_server):
    conn = _connection(running_server)
    try:
        conn.request("GET", "/assets/style.css")
        response = conn.getresponse()
        assert response.status == 200
        assert "text/css" in response.getheader("Content-Type", "")
    finally:
        conn.close()


def test_a_missing_static_file_is_a_404(running_server):
    status, payload = _get_json(running_server, "/does-not-exist.txt")
    assert status == 404
    assert "error" in payload


@pytest.mark.parametrize(
    "path",
    [
        "/../secret.txt",
        "/%2e%2e/secret.txt",
        "/assets/../../secret.txt",
        "/assets/%2e%2e/%2e%2e/secret.txt",
    ],
)
def test_path_traversal_outside_the_webapp_dir_is_refused(running_server, path):
    status, payload = _get_json(running_server, path)
    assert status == 404
    assert "error" in payload
