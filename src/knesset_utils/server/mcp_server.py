from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse

from knesset_utils.server.config import ServerConfig
from knesset_utils.server.guide import INSTRUCTIONS
from knesset_utils.server.tools import register_tools

DEFAULT_DB_PATH = Path("data/knesset_mirror.sqlite")

log = logging.getLogger("knesset_utils.server")


def _register_health_route(mcp: MCPServer, db_path: Path) -> None:
    """Unauthenticated `GET /healthz` for Docker/Render probes.

    Custom routes are not wrapped by the auth middleware, so this stays reachable
    without a token. Reports whether the DB file is present and its freshness.
    """

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> JSONResponse:  # noqa: ARG001
        info: dict = {"status": "ok", "db_exists": db_path.exists()}
        if db_path.exists():
            try:
                uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
                conn = sqlite3.connect(uri, uri=True)
                try:
                    row = conn.execute("SELECT max(last_synced_at) FROM _sync_state").fetchone()
                    info["last_synced_at"] = row[0] if row else None
                finally:
                    conn.close()
            except Exception as exc:  # noqa: BLE001
                info["status"] = "degraded"
                info["error"] = str(exc)
        else:
            info["status"] = "degraded"
        return JSONResponse(info, status_code=200 if info["status"] == "ok" else 503)


def build_server(db_path: Path = DEFAULT_DB_PATH) -> MCPServer:
    """Plain stdio-ready server: tools + health route, no auth."""
    mcp = MCPServer("knesset-utils", instructions=INSTRUCTIONS)
    register_tools(mcp, db_path)
    _register_health_route(mcp, db_path)
    return mcp


def _build_http_server(cfg: ServerConfig) -> MCPServer:
    if not cfg.native_auth:
        return build_server(cfg.db_path)

    # Opt-in: mcp's native OAuth-style bearer middleware.
    from mcp.server.auth.settings import AuthSettings

    from knesset_utils.server.auth import StaticTokenVerifier

    if not cfg.public_url:
        raise SystemExit("MCP_NATIVE_AUTH=1 requires MCP_PUBLIC_URL")
    if not cfg.auth_token:
        raise SystemExit("MCP_NATIVE_AUTH=1 requires MCP_AUTH_TOKEN")

    mcp = MCPServer(
        "knesset-utils",
        instructions=INSTRUCTIONS,
        token_verifier=StaticTokenVerifier(cfg.auth_token),
        auth=AuthSettings(issuer_url=cfg.public_url, resource_server_url=cfg.public_url),
    )
    register_tools(mcp, cfg.db_path)
    _register_health_route(mcp, cfg.db_path)
    return mcp


def _run_http(cfg: ServerConfig) -> None:
    import uvicorn

    from knesset_utils.server import mirror

    if cfg.download_on_boot:
        mirror.ensure_mirror(cfg)  # no-op unless MIRROR_REPO is set

    mcp = _build_http_server(cfg)
    app = mcp.streamable_http_app(
        host=cfg.host,
        streamable_http_path="/mcp",
        stateless_http=cfg.stateless_http,
    )

    if cfg.native_auth:
        pass  # auth is enforced inside the mcp app
    elif cfg.auth_token:
        from knesset_utils.server.auth import StaticBearerMiddleware

        app.add_middleware(StaticBearerMiddleware, token=cfg.auth_token)
    else:
        log.warning("MCP_AUTH_TOKEN is not set -- the HTTP server is UNAUTHENTICATED")

    if cfg.refresh_interval > 0 and cfg.mirror_repo:
        mirror.start_refresh_thread(cfg)
        log.info("mirror refresh thread started (every %ds)", cfg.refresh_interval)

    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info", access_log=False)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = ServerConfig.from_env()
    if cfg.transport == "stdio":
        build_server(cfg.db_path).run()
    else:
        _run_http(cfg)


if __name__ == "__main__":
    main()
