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

A single table's sync failing (after the client's own retries are
exhausted) does not need to be fatal to a multi-table run -- sync_table
raises on failure and the caller decides whether to isolate that per table.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timezone

from knesset_utils.db import state as state_mod
from knesset_utils.odata.client import ODataClient
from knesset_utils.schema.metadata import EntityDef
from knesset_utils.timeutil import format_duration

PAGE_SIZE = 100
CHECKPOINT_EVERY_PAGES = 25  # how often page-level progress is promoted from DEBUG to INFO

logger = logging.getLogger(__name__)


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
    page_num = 0
    while True:
        page_num += 1
        data = client.get_entities(entity.entity_set, filter=f"{key} gt {cursor}", orderby=key, top=PAGE_SIZE)
        rows = data.get("value", [])
        total += _upsert_rows(conn, entity, rows)
        if rows:
            cursor = rows[-1][key]
        logger.debug("%s: full-replace page %d, +%d rows, %d total", entity.entity_set, page_num, len(rows), total)
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
    total_hint: int | None = None,
    already_synced: int = 0,
) -> tuple[int, int, str | None]:
    """Crawl an entity set in ascending Id order from resume_from, upserting every row.
    `already_synced` is rows synced in prior (interrupted) runs, used only to make
    the logged percent-complete/ETA correct across a resume. Returns
    (rows_this_run, last_id_seen, max_last_updated_seen).
    """
    key = entity.key
    cursor = resume_from
    total = 0
    max_last_updated = initial_watermark
    start = time.monotonic()
    page_num = 0
    while True:
        page_num += 1
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

        elapsed = time.monotonic() - start
        rate = total / elapsed if elapsed > 0 else 0.0  # rows/s this run only (ETA denominator)
        is_last_page = len(rows) < PAGE_SIZE
        is_checkpoint = page_num == 1 or page_num % CHECKPOINT_EVERY_PAGES == 0 or is_last_page

        if total_hint:
            overall_done = already_synced + total
            pct = min(100.0, 100.0 * overall_done / total_hint)
            remaining = max(total_hint - overall_done, 0)
            eta = format_duration(remaining / rate) if rate > 0 else "?"
            msg = (
                f"{entity.entity_set}: page {page_num} +{len(rows)} rows | "
                f"{overall_done}/{total_hint} ({pct:.1f}%) | cursor={cursor} | "
                f"{rate:.1f} rows/s | elapsed {format_duration(elapsed)} | eta {eta}"
            )
        else:
            msg = (
                f"{entity.entity_set}: page {page_num} +{len(rows)} rows | {total} total | "
                f"cursor={cursor} | {rate:.1f} rows/s | elapsed {format_duration(elapsed)}"
            )
        logger.info(msg) if is_checkpoint else logger.debug(msg)

        if is_last_page:
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
    page_num = 0
    while True:
        page_num += 1
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
        logger.debug("%s: incremental page %d, +%d rows, %d total", entity.entity_set, page_num, len(rows), total)
        if len(rows) < PAGE_SIZE:
            break
    conn.commit()
    return total, cursor_lud


def sync_table(client: ODataClient, conn: sqlite3.Connection, entity: EntityDef) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()

    if not entity.has_last_updated:
        logger.info("=== %s: full-replace sync starting ===", entity.entity_set)
        rows = full_replace_sync(client, conn, entity)
        elapsed = format_duration(time.monotonic() - t0)
        logger.info("=== %s: full-replace done, %d rows, %s ===", entity.entity_set, rows, elapsed)
        state_mod.save_state(
            conn,
            state_mod.SyncState(table_name=entity.entity_set, full_sync_complete=True, last_synced_at=now, rows_synced=rows),
        )
        return {"table": entity.entity_set, "mode": "full-replace", "rows": rows}

    st = state_mod.get_state(conn, entity.entity_set)

    if not st.full_sync_complete:
        total_hint = None
        try:
            total_hint = client.get_count(entity.entity_set)
        except Exception as exc:
            logger.warning("%s: could not fetch live $count for ETA (%s) -- continuing without one", entity.entity_set, exc)

        logger.info(
            "=== %s: initial keyset crawl starting (resume_from=%d, already_synced=%d, live_count=%s) ===",
            entity.entity_set, st.last_id_seen, st.rows_synced, total_hint if total_hint is not None else "unknown",
        )
        rows, last_id, watermark = keyset_full_sync(
            client, conn, entity, resume_from=st.last_id_seen, initial_watermark=st.max_last_updated_seen,
            total_hint=total_hint, already_synced=st.rows_synced,
        )
        elapsed = format_duration(time.monotonic() - t0)
        logger.info(
            "=== %s: initial crawl done, +%d rows this run (%d total), %s ===",
            entity.entity_set, rows, st.rows_synced + rows, elapsed,
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
    logger.info("=== %s: incremental sync starting (watermark=%s) ===", entity.entity_set, watermark)
    rows, new_watermark = incremental_sync(client, conn, entity, watermark)
    elapsed = format_duration(time.monotonic() - t0)
    logger.info("=== %s: incremental done, +%d rows, %s ===", entity.entity_set, rows, elapsed)
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
