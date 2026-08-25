from __future__ import annotations

import sqlite3
from pathlib import Path

import typer

from knesset_utils.db import ddl, state as state_mod, sync as sync_mod
from knesset_utils.odata.client import ODataClient
from knesset_utils.schema import metadata as schema_metadata

app = typer.Typer(help="Mirror the Knesset OData API into SQLite and query it.")

DEFAULT_DB_PATH = Path("data/knesset_mirror.sqlite")


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    state_mod.ensure_state_table(conn)
    return conn


@app.command("schema-refresh")
def schema_refresh() -> None:
    """Fetch live $metadata, diff against the committed snapshot, and write it."""
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


@app.command()
def sync(
    table: list[str] = typer.Option(None, "--table", help="Sync only these entity sets (repeatable). Default: all."),
    db_path: Path = typer.Option(DEFAULT_DB_PATH, "--db", help="SQLite mirror path"),
) -> None:
    """Sync entity sets from the live OData API into the SQLite mirror."""
    entities = schema_metadata.load_snapshot()
    targets = table or sorted(entities.keys())
    unknown = set(targets) - set(entities.keys())
    if unknown:
        typer.echo(f"Unknown table(s): {sorted(unknown)}", err=True)
        raise typer.Exit(1)

    conn = _connect(db_path)
    ddl.create_all_tables(conn, {name: entities[name] for name in targets})

    with ODataClient() as client:
        for name in targets:
            entity = entities[name]
            typer.echo(f"Syncing {name} ...")
            result = sync_mod.sync_table(client, conn, entity)
            typer.echo(f"  {result['mode']}: {result['rows']} rows")
    conn.close()


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
