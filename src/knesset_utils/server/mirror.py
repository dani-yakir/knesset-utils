"""Fetch the current SQLite mirror from a GitHub Release and swap it into place.

The rebuild job (`.github/workflows/regenerate.yml`) publishes each finished mirror as a
zstd-compressed asset on a moving `latest` release. The HTTP server calls
`ensure_mirror()` on boot and, optionally, from a background thread, to keep its
local copy current.

Design points:
- A public mirror repo needs no credentials; a private one uses `cfg.github_token`.
- Freshness is tracked by a small marker file next to the DB (`<db>.release`)
  holding `<release id>:<asset id>:<asset updated_at>`. If it matches the live
  release, nothing is downloaded.
- The new file is written to a temp path in the *same directory* and moved onto
  `db_path` with `os.replace()` (atomic on one filesystem). `server/tools.py`
  opens a fresh read-only connection per call, so a swap between calls is safe.
- A transient failure never deletes or truncates a working local DB.
"""
from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
from pathlib import Path

import httpx

from knesset_utils.server.config import ServerConfig

log = logging.getLogger("knesset_utils.mirror")

_API = "https://api.github.com"
_CHUNK = 1 << 20


def _headers(cfg: ServerConfig, *, octet: bool = False) -> dict[str, str]:
    headers = {
        "Accept": "application/octet-stream" if octet else "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if cfg.github_token:
        headers["Authorization"] = f"Bearer {cfg.github_token}"
    return headers


def _get_release(cfg: ServerConfig) -> dict:
    url = f"{_API}/repos/{cfg.mirror_repo}/releases/tags/{cfg.mirror_release_tag}"
    resp = httpx.get(url, headers=_headers(cfg), timeout=30.0, follow_redirects=True)
    resp.raise_for_status()
    return resp.json()


def _pick_asset(release: dict, name: str) -> dict:
    for asset in release.get("assets", []):
        if asset.get("name") == name:
            return asset
    raise RuntimeError(f"release {release.get('tag_name')!r} has no asset {name!r}")


def _marker_path(db_path: Path) -> Path:
    return db_path.with_name(db_path.name + ".release")


def _marker(release: dict, asset: dict) -> str:
    return f"{release.get('id')}:{asset.get('id')}:{asset.get('updated_at')}"


def _decompress(src: Path, dst: Path, window_log: int) -> None:
    import zstandard

    if window_log:
        dctx = zstandard.ZstdDecompressor(max_window_size=1 << window_log)
    else:
        dctx = zstandard.ZstdDecompressor()
    with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
        dctx.copy_stream(fsrc, fdst, read_size=_CHUNK, write_size=_CHUNK)


def _download_and_swap(cfg: ServerConfig, release: dict, asset: dict) -> None:
    db_path = cfg.db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if cfg.github_token:
        url = f"{_API}/repos/{cfg.mirror_repo}/releases/assets/{asset['id']}"
        dl_headers = _headers(cfg, octet=True)
    else:
        url = asset["browser_download_url"]
        dl_headers = {}

    fd_z, tmp_z = tempfile.mkstemp(dir=db_path.parent, suffix=".zst")
    os.close(fd_z)
    fd_d, tmp_d = tempfile.mkstemp(dir=db_path.parent, suffix=".sqlite.part")
    os.close(fd_d)
    try:
        with httpx.stream("GET", url, headers=dl_headers, timeout=None, follow_redirects=True) as resp:
            resp.raise_for_status()
            with open(tmp_z, "wb") as f:
                for chunk in resp.iter_bytes(_CHUNK):
                    f.write(chunk)
        _decompress(Path(tmp_z), Path(tmp_d), cfg.zstd_long_window_log)
        os.replace(tmp_d, db_path)  # atomic on the same filesystem
        _marker_path(db_path).write_text(_marker(release, asset))
    finally:
        for path in (tmp_z, tmp_d):
            try:
                os.unlink(path)
            except OSError:
                pass


def ensure_mirror(cfg: ServerConfig) -> None:
    """Download the mirror if the local copy is missing or older than the release.

    No-op when `cfg.mirror_repo` is unset. Never raises if a usable DB already
    exists locally -- it just logs and keeps serving the old data.
    """
    if not cfg.mirror_repo:
        return
    try:
        release = _get_release(cfg)
        asset = _pick_asset(release, cfg.mirror_asset)
        want = _marker(release, asset)
        marker_file = _marker_path(cfg.db_path)
        have = marker_file.read_text().strip() if marker_file.exists() else None
        if cfg.db_path.exists() and have == want:
            log.info("mirror up to date (%s)", want)
            return
        log.info("fetching mirror %s -> %s", want, cfg.db_path)
        _download_and_swap(cfg, release, asset)
        log.info("mirror ready: %s (%d bytes)", cfg.db_path, cfg.db_path.stat().st_size)
    except Exception:
        if cfg.db_path.exists():
            log.exception("mirror refresh failed; keeping existing DB")
            return
        raise


def start_refresh_thread(cfg: ServerConfig) -> threading.Thread:
    """Spawn a daemon thread that re-checks the release every `cfg.refresh_interval` seconds."""
    interval = max(cfg.refresh_interval, 60)

    def _loop() -> None:
        while True:
            time.sleep(interval)
            try:
                ensure_mirror(cfg)
            except Exception:
                log.exception("background mirror refresh crashed; will retry next tick")

    thread = threading.Thread(target=_loop, name="mirror-refresh", daemon=True)
    thread.start()
    return thread
