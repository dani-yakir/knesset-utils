"""Per-table sync progress, so a full crawl can resume after interruption and
incremental syncs know their watermark.

_sync_chunks holds the unfinished Id-range chunks of in-progress crawls (see
db/sync.py); a chunk's row is deleted once it has been fully fetched.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

SYNC_STATE_DDL = """
CREATE TABLE IF NOT EXISTS _sync_state (
    table_name TEXT PRIMARY KEY,
    last_id_seen INTEGER NOT NULL DEFAULT 0,
    max_last_updated_seen TEXT,
    full_sync_complete INTEGER NOT NULL DEFAULT 0,
    last_synced_at TEXT,
    rows_synced INTEGER NOT NULL DEFAULT 0
)
"""

SYNC_CHUNKS_DDL = """
CREATE TABLE IF NOT EXISTS _sync_chunks (
    table_name TEXT NOT NULL,
    lo INTEGER NOT NULL,      -- exclusive lower bound the chunk was planned at (its identity)
    hi INTEGER,               -- inclusive upper bound; NULL = open-ended tail
    cursor INTEGER NOT NULL,  -- last Id written; where the chunk resumes
    PRIMARY KEY (table_name, lo)
)
"""


@dataclass
class SyncState:
    table_name: str
    last_id_seen: int = 0
    max_last_updated_seen: str | None = None
    full_sync_complete: bool = False
    last_synced_at: str | None = None
    rows_synced: int = 0


def ensure_state_table(conn: sqlite3.Connection) -> None:
    conn.execute(SYNC_STATE_DDL)
    conn.execute(SYNC_CHUNKS_DDL)
    conn.commit()


def get_state(conn: sqlite3.Connection, table_name: str) -> SyncState:
    row = conn.execute(
        "SELECT table_name, last_id_seen, max_last_updated_seen, full_sync_complete, last_synced_at, rows_synced "
        "FROM _sync_state WHERE table_name = ?",
        (table_name,),
    ).fetchone()
    if row is None:
        return SyncState(table_name=table_name)
    return SyncState(
        table_name=row[0],
        last_id_seen=row[1],
        max_last_updated_seen=row[2],
        full_sync_complete=bool(row[3]),
        last_synced_at=row[4],
        rows_synced=row[5],
    )


def save_state(conn: sqlite3.Connection, state: SyncState) -> None:
    conn.execute(
        """
        INSERT INTO _sync_state
            (table_name, last_id_seen, max_last_updated_seen, full_sync_complete, last_synced_at, rows_synced)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(table_name) DO UPDATE SET
            last_id_seen=excluded.last_id_seen,
            max_last_updated_seen=excluded.max_last_updated_seen,
            full_sync_complete=excluded.full_sync_complete,
            last_synced_at=excluded.last_synced_at,
            rows_synced=excluded.rows_synced
        """,
        (
            state.table_name,
            state.last_id_seen,
            state.max_last_updated_seen,
            int(state.full_sync_complete),
            state.last_synced_at,
            state.rows_synced,
        ),
    )
    conn.commit()
