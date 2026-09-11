from __future__ import annotations

import sqlite3
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from knesset_utils.stats import queries as stats_queries


def _ro_connect(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def register_tools(mcp: MCPServer, db_path: Path) -> None:
    @mcp.tool()
    def list_tables() -> list[str]:
        """List all tables in the Knesset SQLite mirror."""
        conn = _ro_connect(db_path)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '\\_%' ESCAPE '\\' ORDER BY name"  # _sync_*, _staging_*
            ).fetchall()
            return [r[0] for r in rows]
        finally:
            conn.close()

    @mcp.tool()
    def describe_table(table: str) -> list[dict]:
        """Show column names and types for a table in the mirror."""
        conn = _ro_connect(db_path)
        try:
            rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
            return [{"name": r[1], "type": r[2], "notnull": bool(r[3]), "pk": bool(r[5])} for r in rows]
        finally:
            conn.close()

    @mcp.tool()
    def query_sql(sql: str, max_rows: int = 500) -> list[dict]:
        """Run a read-only SELECT against the mirror. Only SELECT statements are allowed."""
        stripped = sql.strip().rstrip(";")
        if not stripped.lower().startswith("select"):
            raise ValueError("Only SELECT statements are allowed.")
        conn = _ro_connect(db_path)
        try:
            cur = conn.execute(stripped)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchmany(max_rows)
            return [dict(zip(cols, row)) for row in rows]
        finally:
            conn.close()

    @mcp.tool()
    def sync_status() -> list[dict]:
        """Per-table sync freshness: whether the initial crawl completed, last sync time, row count."""
        conn = _ro_connect(db_path)
        try:
            rows = conn.execute(
                "SELECT table_name, full_sync_complete, last_synced_at, rows_synced FROM _sync_state ORDER BY table_name"
            ).fetchall()
            return [
                {"table": r[0], "full_sync_complete": bool(r[1]), "last_synced_at": r[2], "rows_synced": r[3]}
                for r in rows
            ]
        finally:
            conn.close()

    @mcp.tool()
    def mk_vote_agreement(mk_id_a: int, mk_id_b: int) -> dict:
        """Fraction of shared plenum votes where two MKs (by KNS_Person Id) voted the same way."""
        conn = _ro_connect(db_path)
        try:
            return stats_queries.mk_vote_agreement(conn, mk_id_a, mk_id_b)
        finally:
            conn.close()

    @mcp.tool()
    def vote_result_breakdown(vote_id: int) -> dict:
        """Tally of vote results (for/against/abstain/...) for a single plenum vote (KNS_PlenumVote Id)."""
        conn = _ro_connect(db_path)
        try:
            return stats_queries.vote_result_breakdown(conn, vote_id)
        finally:
            conn.close()
