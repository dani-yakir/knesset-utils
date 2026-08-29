from __future__ import annotations

import logging
import sqlite3
import sys
import time
from pathlib import Path

import typer

from knesset_utils.db import ddl, fk_check, state as state_mod, sync as sync_mod
from knesset_utils.odata.client import ODataClient
from knesset_utils.schema import metadata as schema_metadata
from knesset_utils.timeutil import format_duration

app = typer.Typer(help="Mirror the Knesset OData API into SQLite and query it.")

DEFAULT_DB_PATH = Path("data/knesset_mirror.sqlite")
DEFAULT_LOG_PATH = Path("data/sync.log")


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    state_mod.ensure_state_table(conn)
    return conn


def _setup_logging(log_path: Path) -> logging.Logger:
    """File gets full per-page detail (DEBUG); console gets table start/end
    banners and periodic checkpoints only (INFO), so a multi-hour run doesn't
    flood stdout while the log file keeps a complete audit trail.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("knesset_utils")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    return logger


def _refresh_schema() -> None:
    live_defs = schema_metadata.fetch_live_entity_defs()
    if schema_metadata.SNAPSHOT_PATH.exists():
        old_defs = schema_metadata.load_snapshot()
        diff = schema_metadata.diff_snapshot(old_defs, live_defs)
        if any(diff.values()):
            typer.echo(f"Schema changed: {diff}")
        else:
            typer.echo("No schema changes.")
    schema_metadata.write_snapshot(live_defs)
    typer.echo(f"Snapshot written: {schema_metadata.SNAPSHOT_PATH} ({len(live_defs)} tables)")


@app.command("schema-refresh")
def schema_refresh() -> None:
    """Fetch live $metadata, diff against the committed snapshot, and write it."""
    _refresh_schema()


def _run_sync(
    table: list[str] | None,
    db_path: Path,
    log_path: Path,
    *,
    fail_on_any: bool = False,
) -> None:
    """Sync entity sets from the live OData API into the SQLite mirror.

    A failure on one table (after the client's own retries are exhausted) is
    logged and skipped rather than aborting the whole run -- this matters
    for an all-tables run that can take hours.

    Exit code: 0 normally. Non-zero if *every* table failed, or if any table
    failed and `fail_on_any` is set (used by CI so a partial regression is
    visible even though the partially-synced mirror is still published).
    """
    logger = _setup_logging(log_path)

    entities = schema_metadata.load_snapshot()
    targets = table or sorted(entities.keys())
    unknown = set(targets) - set(entities.keys())
    if unknown:
        logger.error("Unknown table(s): %s", sorted(unknown))
        raise typer.Exit(1)

    conn = _connect(db_path)
    ddl.create_all_tables(conn, {name: entities[name] for name in targets})

    logger.info("=" * 78)
    logger.info("Sync run starting: %d table(s) -> %s", len(targets), db_path)
    logger.info("=" * 78)
    run_start = time.monotonic()
    succeeded: list[tuple[str, dict]] = []
    failed: list[str] = []

    with ODataClient() as client:
        for i, name in enumerate(targets, 1):
            entity = entities[name]
            logger.info("[%d/%d] %s", i, len(targets), name)
            try:
                result = sync_mod.sync_table(client, conn, entity)
                succeeded.append((name, result))
            except Exception:
                logger.exception("FAILED syncing %s -- skipping, continuing with remaining tables", name)
                failed.append(name)
    conn.close()

    elapsed = format_duration(time.monotonic() - run_start)
    total_rows = sum(r["rows"] for _, r in succeeded)
    logger.info("=" * 78)
    logger.info(
        "Sync run complete: %d/%d tables succeeded, %d rows synced this run, elapsed %s",
        len(succeeded), len(targets), total_rows, elapsed,
    )
    if failed:
        logger.warning("Failed table(s): %s", failed)
    logger.info("=" * 78)

    if failed and (fail_on_any or not succeeded):
        raise typer.Exit(1)


@app.command()
def sync(
    table: list[str] = typer.Option(None, "--table", help="Sync only these entity sets (repeatable). Default: all."),
    db_path: Path = typer.Option(DEFAULT_DB_PATH, "--db", help="SQLite mirror path"),
    log_path: Path = typer.Option(DEFAULT_LOG_PATH, "--log", help="Log file path"),
    fail_on_any: bool = typer.Option(
        False,
        "--fail-on-any/--no-fail-on-any",
        help="Exit non-zero if ANY table fails (for CI). Default: exit non-zero only if all fail.",
    ),
) -> None:
    """Sync entity sets from the live OData API into the SQLite mirror."""
    _run_sync(table, db_path, log_path, fail_on_any=fail_on_any)


@app.command()
def seed(
    db_path: Path = typer.Option(DEFAULT_DB_PATH, "--db", help="SQLite mirror path"),
    log_path: Path = typer.Option(DEFAULT_LOG_PATH, "--log", help="Log file path"),
) -> None:
    """Build a seed mirror from scratch: refresh the schema snapshot, then sync every table.

    This is the full pipeline (schema-refresh + sync --all) as a single entrypoint.
    On an empty/new database this is the ~14h initial crawl (dominated by
    KNS_PlenumVoteResult); re-running it against an existing mirror is a cheap
    incremental catch-up instead, since sync_table already routes each table
    accordingly. Meant to be run as an infrequent, standalone job -- not part of
    a normal deploy.
    """
    typer.echo("=== seed: refreshing schema snapshot ===")
    _refresh_schema()
    typer.echo("=== seed: syncing all tables ===")
    _run_sync(None, db_path, log_path)


@app.command("validate-fks")
def validate_fks(db_path: Path = typer.Option(DEFAULT_DB_PATH, "--db", help="SQLite mirror path")) -> None:
    """Check foreign-key integrity in the local mirror and print a summary.

    See schema/foreign_keys.py for how the checked FK set was derived (naming
    convention + a small manually-reviewed set) and docs/fk_integrity.md for
    full prior results and the columns deliberately left unvalidated.
    """
    entities = schema_metadata.load_snapshot()
    conn = _connect(db_path)
    results = fk_check.validate_all(conn, entities)
    conn.close()

    clean = [r for r in results if r.is_clean]
    dirty = [r for r in results if not r.is_clean]

    typer.echo(f"Checked {len(results)} foreign key(s): {len(clean)} clean, {len(dirty)} with orphans")
    for r in dirty:
        typer.echo(
            f"  {r.source_table}.{r.fk_column} -> {r.target_table}: "
            f"{r.orphan_rows}/{r.source_rows_with_value} rows orphaned, "
            f"{len(r.orphan_distinct_values)}+ distinct missing values (e.g. {r.orphan_distinct_values[:5]})"
        )


@app.command()
def status(db_path: Path = typer.Option(DEFAULT_DB_PATH, "--db", help="SQLite mirror path")) -> None:
    """Show per-table sync state."""
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT table_name, full_sync_complete, last_synced_at, rows_synced FROM _sync_state ORDER BY table_name"
    ).fetchall()
    if not rows:
        typer.echo("No sync history yet.")
    for table_name, complete, last_synced_at, rows_synced in rows:
        typer.echo(f"{table_name:35s} complete={bool(complete)!s:5s} last_synced={last_synced_at} rows={rows_synced}")
    conn.close()


if __name__ == "__main__":
    app()
