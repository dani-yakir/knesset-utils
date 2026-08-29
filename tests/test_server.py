import asyncio
import sqlite3

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from knesset_utils.server.auth import StaticBearerMiddleware, StaticTokenVerifier
from knesset_utils.server.config import ServerConfig
from knesset_utils.server.mcp_server import build_server


def _app(token: str) -> Starlette:
    app = Starlette(routes=[
        Route("/mcp", lambda r: PlainTextResponse("ok")),
        Route("/healthz", lambda r: PlainTextResponse("healthy")),
    ])
    app.add_middleware(StaticBearerMiddleware, token=token)
    return app


def test_config_defaults_to_stdio_with_no_env(monkeypatch):
    for key in list(ServerConfig.__annotations__):
        monkeypatch.delenv(key, raising=False)
    for key in ("MCP_TRANSPORT", "MCP_HOST", "MCP_PORT", "PORT", "MCP_AUTH_TOKEN", "MCP_DB_PATH",
                "MIRROR_REPO", "MCP_NATIVE_AUTH", "MIRROR_DOWNLOAD_ON_BOOT"):
        monkeypatch.delenv(key, raising=False)
    cfg = ServerConfig.from_env()
    assert cfg.transport == "stdio"
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 8000
    assert cfg.auth_token is None
    assert cfg.mirror_repo is None
    assert cfg.download_on_boot is True


def test_config_http_reads_port_and_mirror_env(monkeypatch):
    monkeypatch.setenv("MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("PORT", "10000")
    monkeypatch.setenv("MCP_AUTH_TOKEN", "  secret  ")
    monkeypatch.setenv("MIRROR_REPO", "owner/repo")
    monkeypatch.setenv("MIRROR_DOWNLOAD_ON_BOOT", "0")
    monkeypatch.delenv("MCP_HOST", raising=False)
    cfg = ServerConfig.from_env()
    assert cfg.transport == "streamable-http"
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 10000
    assert cfg.auth_token == "secret"
    assert cfg.mirror_repo == "owner/repo"
    assert cfg.download_on_boot is False


def test_bearer_middleware_rejects_missing_and_wrong_token():
    client = TestClient(_app("right"))
    assert client.get("/mcp").status_code == 401
    assert client.get("/mcp", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/mcp", headers={"Authorization": "right"}).status_code == 401


def test_bearer_middleware_allows_correct_token_and_exempts_health():
    client = TestClient(_app("right"))
    assert client.get("/mcp", headers={"Authorization": "Bearer right"}).status_code == 200
    assert client.get("/healthz").status_code == 200


def test_bearer_middleware_requires_nonempty_token():
    with pytest.raises(ValueError):
        StaticBearerMiddleware(_app("x"), token="")


def test_static_token_verifier_matches_only_exact_token():
    v = StaticTokenVerifier("tok")
    assert asyncio.run(v.verify_token("tok")) is not None
    assert asyncio.run(v.verify_token("nope")) is None
    assert asyncio.run(v.verify_token("")) is None


def test_healthz_route_reports_db_freshness(tmp_path):
    db = tmp_path / "mirror.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE _sync_state (table_name TEXT, last_synced_at TEXT)")
    conn.execute("INSERT INTO _sync_state VALUES ('KNS_Person', '2026-08-29T00:00:00Z')")
    conn.commit()
    conn.close()

    mcp = build_server(db)
    client = TestClient(mcp.streamable_http_app())
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["db_exists"] is True
    assert body["last_synced_at"] == "2026-08-29T00:00:00Z"


def test_healthz_route_degraded_when_db_missing(tmp_path):
    mcp = build_server(tmp_path / "absent.sqlite")
    client = TestClient(mcp.streamable_http_app())
    resp = client.get("/healthz")
    assert resp.status_code == 503
    assert resp.json()["db_exists"] is False
