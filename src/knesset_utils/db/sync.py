"""Sync entity sets from the live OData API into the SQLite mirror.

Two strategies, chosen per-table based on whether it has a LastUpdatedDate
column (most do; a handful of small lookup tables don't):

- Tables WITHOUT LastUpdatedDate: full replace every run (cheap, small tables).
- Tables WITH LastUpdatedDate: a one-time keyset-pagination crawl
  (`$filter=Id gt {cursor}&$orderby=Id&$top=100`, looping until a page
  returns fewer than PAGE_SIZE rows), checkpointed per page so it can resume
  after interruption; then, once complete, incremental syncs filtered on
  `LastUpdatedDate gt {watermark}`.

Keyset pagination was chosen over $skip/nextLink-following because $skip was
measured to degrade badly with depth on large tables (tens of seconds per
page near the end of a 2M-row table). It's also correctness-guaranteed
regardless of Id gaps/sparsity: each request just asks for the next
PAGE_SIZE rows above the cursor in Id order, so gaps are silently skipped
over rather than causing missed rows -- this was verified against live data
(a full crawl of a real table matched `$count=true` exactly, with zero
duplicates and strictly ascending Ids across every page boundary) before
this was written.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from knesset_utils.db import state as state_mod
from knesset_utils.odata.client import ODataClient
from knesset_utils.schema.metadata import EntityDef

PAGE_SIZE = 100


def _upsert_rows(conn: sqlite3.Connection, entity: EntityDef, rows: list[dict]) -> int:
    if not rows:
        return 0
    cols = entity.column_names
    col_list = ", ".join(f'"{c}"' for c in cols)
    placeholders = ", ".join(["?"] * len(cols))
    sql = f'INSERT OR REPLACE INTO "{entity.entity_set}" ({col_list}) VALUES ({placeholders})'
    conn.executemany(sql, [[row.get(c) for c in cols] for row in rows])
    return len(rows)


def full_replace_sync(client: ODataClient, conn: sqlite3.Connection, entity: EntityDef) -> int:
    """For small lookup tables without LastUpdatedDate: replace the whole table every run."""
    key = entity.key
    conn.execute(f'DELETE FROM "{entity.entity_set}"')
    total = 0
    cursor = 0
    while True:
        data = client.get_entities(entity.entity_set, filter=f"{key} gt {cursor}", orderby=key, top=PAGE_SIZE)
        rows = data.get("value", [])
        total += _upsert_rows(conn, entity, rows)
        if rows:
            cursor = rows[-1][key]
        if len(rows) < PAGE_SIZE:
            break
    conn.commit()
    return total


def keyset_full_sync(
    client: ODataClient,
    conn: sqlite3.Connection,
    entity: EntityDef,
    resume_from: int = 0,
    initial_watermark: str | None = None,
) -> tuple[int, int, str | None]:
    """Crawl an entity set in ascending Id order from resume_from, upserting every row.
    Returns (rows_this_run, last_id_seen, max_last_updated_seen).
    """
    key = entity.key
    cursor = resume_from
    total = 0
    max_last_updated = initial_watermark
    while True:
        data = client.get_entities(entity.entity_set, filter=f"{key} gt {cursor}", orderby=key, top=PAGE_SIZE)
        rows = data.get("value", [])
        total += _upsert_rows(conn, entity, rows)
        if rows:
            cursor = rows[-1][key]
            for row in rows:
                lud = row.get("LastUpdatedDate")
                if lud and (max_last_updated is None or lud > max_last_updated):
                    max_last_updated = lud
            conn.commit()
            state_mod.save_state(
                conn,
                state_mod.SyncState(
                    table_name=entity.entity_set,
                    last_id_seen=cursor,
                    max_last_updated_seen=max_last_updated,
                    full_sync_complete=False,
                    rows_synced=total,
                ),
            )
        if len(rows) < PAGE_SIZE:
            break
    return total, cursor, max_last_updated


def incremental_sync(
    client: ODataClient, conn: sqlite3.Connection, entity: EntityDef, watermark: str
) -> tuple[int, str]:
    """For a table whose initial crawl already completed: pull only rows updated since watermark.
    Returns (rows_synced, new_watermark).
    """
    total = 0
    cursor_lud = watermark
    while True:
        data = client.get_entities(
            entity.entity_set,
            filter=f"LastUpdatedDate gt {cursor_lud}",
            orderby="LastUpdatedDate",
            top=PAGE_SIZE,
        )
        rows = data.get("value", [])
        total += _upsert_rows(conn, entity, rows)
        if rows:
            cursor_lud = rows[-1]["LastUpdatedDate"]
        if len(rows) < PAGE_SIZE:
            break
    conn.commit()
    return total, cursor_lud


def sync_table(client: ODataClient, conn: sqlite3.Connection, entity: EntityDef) -> dict:
    now = datetime.now(timezone.utc).isoformat()

    if not entity.has_last_updated:
        rows = full_replace_sync(client, conn, entity)
        state_mod.save_state(
            conn,
            state_mod.SyncState(table_name=entity.entity_set, full_sync_complete=True, last_synced_at=now, rows_synced=rows),
        )
        return {"table": entity.entity_set, "mode": "full-replace", "rows": rows}

    st = state_mod.get_state(conn, entity.entity_set)

    if not st.full_sync_complete:
        rows, last_id, watermark = keyset_full_sync(
            client, conn, entity, resume_from=st.last_id_seen, initial_watermark=st.max_last_updated_seen
        )
        state_mod.save_state(
            conn,
            state_mod.SyncState(
                table_name=entity.entity_set,
                last_id_seen=last_id,
                max_last_updated_seen=watermark,
                full_sync_complete=True,
                last_synced_at=now,
                rows_synced=st.rows_synced + rows,
            ),
        )
        return {"table": entity.entity_set, "mode": "initial-keyset-crawl", "rows": rows}

    watermark = st.max_last_updated_seen or "1900-01-01T00:00:00Z"
    rows, new_watermark = incremental_sync(client, conn, entity, watermark)
    state_mod.save_state(
        conn,
        state_mod.SyncState(
            table_name=entity.entity_set,
            last_id_seen=st.last_id_seen,
            max_last_updated_seen=new_watermark,
            full_sync_complete=True,
            last_synced_at=now,
            rows_synced=st.rows_synced + rows,
        ),
    )
    return {"table": entity.entity_set, "mode": "incremental", "rows": rows}
