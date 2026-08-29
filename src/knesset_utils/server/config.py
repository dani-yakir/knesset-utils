"""Runtime configuration for the MCP server, read from the environment.

Defaults are chosen so that `python -m knesset_utils.server.mcp_server` with no
environment set behaves exactly as before this module existed: stdio transport,
DB at `data/knesset_mirror.sqlite`, no auth, no release fetching. The HTTP
deployment (Docker/Render) sets `MCP_TRANSPORT=streamable-http` plus the
`MCP_*` / `MIRROR_*` vars below.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_FALSEY = {"", "0", "false", "no", "off"}


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in _FALSEY


@dataclass(frozen=True)
class ServerConfig:
    db_path: Path
    transport: str            # "stdio" | "streamable-http"
    host: str
    port: int
    auth_token: str | None
    native_auth: bool         # use mcp's OAuth-style RequireAuthMiddleware instead of the static one
    public_url: str | None    # required by mcp's native AuthSettings; e.g. https://knesset-mcp.onrender.com
    stateless_http: bool

    mirror_repo: str | None   # "owner/repo"; None disables all release fetching
    mirror_release_tag: str   # e.g. "latest"
    mirror_asset: str         # asset filename in the release
    github_token: str | None  # only needed for a PRIVATE mirror repo
    zstd_long_window_log: int # must match sync.yml's `zstd --long=NN`; 0 = no --long
    refresh_interval: int     # seconds between background mirror re-checks; 0 = disabled
    download_on_boot: bool

    @classmethod
    def from_env(cls) -> "ServerConfig":
        transport = (os.getenv("MCP_TRANSPORT") or "stdio").strip()
        default_host = "127.0.0.1" if transport == "stdio" else "0.0.0.0"
        return cls(
            db_path=Path(os.getenv("MCP_DB_PATH") or "data/knesset_mirror.sqlite"),
            transport=transport,
            host=(os.getenv("MCP_HOST") or default_host).strip(),
            # Render (and most PaaS) inject the listen port as $PORT.
            port=int(os.getenv("PORT") or os.getenv("MCP_PORT") or "8000"),
            auth_token=(os.getenv("MCP_AUTH_TOKEN") or "").strip() or None,
            native_auth=_flag("MCP_NATIVE_AUTH", False),
            public_url=(os.getenv("MCP_PUBLIC_URL") or "").rstrip("/") or None,
            stateless_http=_flag("MCP_STATELESS", False),
            mirror_repo=(os.getenv("MIRROR_REPO") or "").strip() or None,
            mirror_release_tag=(os.getenv("MIRROR_RELEASE_TAG") or "latest").strip(),
            mirror_asset=(os.getenv("MIRROR_ASSET") or "knesset_mirror.sqlite.zst").strip(),
            github_token=(os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN") or "").strip() or None,
            zstd_long_window_log=int(os.getenv("MIRROR_ZSTD_LONG") or "0"),
            refresh_interval=int(os.getenv("MIRROR_REFRESH_INTERVAL_SECONDS") or "0"),
            download_on_boot=_flag("MIRROR_DOWNLOAD_ON_BOOT", True),
        )
