from __future__ import annotations

import re
import sqlite3
from functools import lru_cache
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from knesset_utils.schema.foreign_keys import find_foreign_keys
from knesset_utils.schema.metadata import load_snapshot
from knesset_utils.server.guide import TABLE_DESCRIPTIONS
from knesset_utils.stats import queries as stats_queries

# describe_table lists every value of a column with at most this many distinct values,
# and the most common ones for code columns pointing at a small lookup table.
_MAX_LISTED_VALUES = 25
# Text columns with at most this many distinct values show their most common ones.
_MAX_TOP_VALUES_DISTINCT = 500
_TOP_VALUES = 12
# Lookup tables whose codes describe_table decodes into their label column.
_LOOKUP_LABELS = {"KNS_Position": "Description", "KNS_Status": "Desc", "KNS_ItemType": "Desc"}
_MK_POSITIONS = (43, 61)  # חבר הכנסת / חברת הכנסת
_ORIGINAL_LAW_BINDING = 6012  # KNS_LawBinding.BindingType 'החוק המקורי': the bill that enacted the law

VOTE_URL = "https://main.knesset.gov.il/Activity/plenum/Votes/Pages/vote.aspx?voteId={}"
BILL_URL = "https://main.knesset.gov.il/apps/legislation/main/bills/{}"
LAW_URL = "https://main.knesset.gov.il/apps/legislation/main/laws/{}"


def _enacted_law(conn: sqlite3.Connection, bill_id: int) -> dict:
    """The law (KNS_IsraelLaw) a bill originally enacted, as link fields; nulls if none."""
    row = conn.execute(
        "SELECT l.Id FROM KNS_LawBinding lb JOIN KNS_IsraelLaw l ON l.Id = lb.IsraelLawID "
        "WHERE lb.LawID = ? AND lb.BindingType = ? ORDER BY l.Id LIMIT 1",
        (bill_id, _ORIGINAL_LAW_BINDING),
    ).fetchone()
    return {"law_id": row[0] if row else None, "law_url": LAW_URL.format(row[0]) if row else None}


def _as_list(ids: int | list[int] | None) -> list[int]:
    if ids is None:
        return []
    return [ids] if isinstance(ids, int) else list(ids)


def _ro_connect(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _user_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '\\_%' ESCAPE '\\' ORDER BY name"  # _sync_*, _staging_*
    ).fetchall()
    return [r[0] for r in rows]


@lru_cache(maxsize=1)
def _foreign_keys() -> list[tuple[str, str, str]]:
    return find_foreign_keys(load_snapshot())


def _column_values(conn: sqlite3.Connection, table: str, col: str, limit: int) -> list[tuple]:
    return conn.execute(
        f'SELECT "{col}", COUNT(*) FROM "{table}" WHERE "{col}" IS NOT NULL '
        f'GROUP BY 1 ORDER BY 2 DESC LIMIT {limit}'
    ).fetchall()


def _lookup_labels(conn: sqlite3.Connection, target: str) -> dict:
    label = _LOOKUP_LABELS[target]
    return dict(conn.execute(f'SELECT Id, "{label}" FROM "{target}"').fetchall())


@lru_cache(maxsize=128)
def _describe(db_path: Path, mtime: float, table: str) -> str:  # mtime keys the cache to one mirror
    conn = _ro_connect(db_path)
    try:
        cols = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        n_rows = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        fks = _foreign_keys()
        out_fk = {c: t for s, c, t in fks if s == table}
        in_fk = sorted(f"{s}.{c}" for s, c, t in fks if t == table)

        lines = [f"{table} ({n_rows:,} rows) -- {TABLE_DESCRIPTIONS.get(table, '')}"]
        for _, name, ctype, _, _, pk in cols:
            line = f"  {name} {ctype}"
            if pk:
                lines.append(line + " primary key")
                continue
            target = out_fk.get(name)
            if target:
                line += f" -> {target}.Id"
            n_distinct, n_null = conn.execute(
                f'SELECT COUNT(DISTINCT "{name}"), SUM("{name}" IS NULL) FROM "{table}"'
            ).fetchone()
            notes = []
            if n_rows and n_null:
                notes.append(f"{100 * n_null / n_rows:.4g}% NULL")
            if target in _LOOKUP_LABELS:
                labels = _lookup_labels(conn, target)
                vals = _column_values(conn, table, name, _MAX_LISTED_VALUES)
                listed = ", ".join(f"{v}={labels.get(v, '?')} ({c:,})" for v, c in vals)
                more = f" ... {n_distinct} distinct" if n_distinct > len(vals) else ""
                notes.append(f"values: {listed}{more}")
            elif target or name.endswith(("Id", "ID")) or name == "LastUpdatedDate":
                pass  # identifiers and sync timestamps: value lists are noise
            elif n_distinct <= _MAX_LISTED_VALUES:
                vals = _column_values(conn, table, name, _MAX_LISTED_VALUES)
                notes.append("values: " + ", ".join(f"{v!r} ({c:,})" for v, c in vals))
            elif ctype.upper() == "TEXT" and n_distinct <= _MAX_TOP_VALUES_DISTINCT:
                vals = _column_values(conn, table, name, _TOP_VALUES)
                notes.append(f"{n_distinct:,} distinct, most common: " + ", ".join(f"{v!r} ({c:,})" for v, c in vals))
            elif ctype.upper() == "INTEGER":
                lo, hi = conn.execute(f'SELECT MIN("{name}"), MAX("{name}") FROM "{table}"').fetchone()
                notes.append(f"range {lo}..{hi}, {n_distinct:,} distinct")
            else:
                example = conn.execute(
                    f'SELECT "{name}" FROM "{table}" WHERE "{name}" IS NOT NULL LIMIT 1'
                ).fetchone()
                if example:
                    notes.append(f"{n_distinct:,} distinct, e.g. {str(example[0])[:80]!r}")
            if notes:
                line += "  | " + "; ".join(notes)
            lines.append(line)
        if in_fk:
            lines.append("  referenced by: " + ", ".join(in_fk))
        return "\n".join(lines)
    finally:
        conn.close()


def _sql_error_hint(conn: sqlite3.Connection, sql: str, exc: sqlite3.Error) -> str:
    msg = f"SQLite error: {exc}"
    if "no such column" in str(exc) or "syntax error" in str(exc):
        known = set(_user_tables(conn))
        mentioned = [t for t in dict.fromkeys(re.findall(r"KNS_\w+", sql)) if t in known]
        has_desc_col = False
        for t in mentioned:
            cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')]
            has_desc_col |= "Desc" in cols
            msg += f"\n{t} columns: {', '.join(cols)}"
        if has_desc_col and re.search(r'(?<!")\bDesc\b(?!")', sql):
            msg += '\nNote: DESC is an SQL keyword; quote the column as "Desc".'
    elif "no such table" in str(exc):
        msg += "\nCall list_tables for the available tables."
    return msg


def register_tools(mcp: MCPServer, db_path: Path) -> None:
    @mcp.tool()
    def list_tables() -> dict:
        """List the mirror's tables with row counts and a one-line description of each.

        Start here, then call describe_table on the tables you need.
        """
        conn = _ro_connect(db_path)
        try:
            tables = {}
            for t in _user_tables(conn):
                n = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                tables[t] = f"{n:,} rows -- {TABLE_DESCRIPTIONS.get(t, '')}"
            as_of = conn.execute("SELECT max(last_synced_at) FROM _sync_state").fetchone()[0]
            return {"data_as_of": as_of, "tables": tables}
        finally:
            conn.close()

    @mcp.tool()
    def describe_table(tables: list[str]) -> str:
        """Describe one or more tables (pass several names at once to save calls).

        For each column: type, foreign-key target, NULL share, and its values -- every value
        with counts for low-cardinality columns, code columns (PositionID, StatusID, ...)
        decoded into their Hebrew labels, ranges or an example otherwise. Also lists which
        columns elsewhere reference the table.
        """
        if isinstance(tables, str):
            tables = [tables]
        conn = _ro_connect(db_path)
        try:
            known = set(_user_tables(conn))
        finally:
            conn.close()
        unknown = [t for t in tables if t not in known]
        if unknown:
            raise ToolError(f"Unknown table(s): {', '.join(unknown)}. Call list_tables for the available tables.")
        mtime = db_path.stat().st_mtime
        return "\n\n".join(_describe(db_path, mtime, t) for t in tables)

    @mcp.tool()
    def query_sql(sql: str, max_rows: int = 500) -> dict:
        """Run a read-only SQLite SELECT (or WITH ... SELECT) against the mirror.

        Returns {"columns": [...], "rows": [[...], ...], "row_count": n, "truncated": bool}.
        Text values are Hebrew. Quote columns named like SQL keywords, e.g. "Desc".
        """
        stripped = sql.strip().rstrip(";")
        if not re.match(r"(?is)^\s*(select|with)\b", stripped):
            raise ToolError("Only SELECT (or WITH ... SELECT) statements are allowed.")
        conn = _ro_connect(db_path)
        try:
            try:
                cur = conn.execute(stripped)
            except sqlite3.Error as exc:
                raise ToolError(_sql_error_hint(conn, stripped, exc)) from exc
            cols = [d[0] for d in cur.description]
            rows = cur.fetchmany(max_rows + 1)
            truncated = len(rows) > max_rows
            rows = [list(r) for r in rows[:max_rows]]
            return {"columns": cols, "rows": rows, "row_count": len(rows), "truncated": truncated}
        finally:
            conn.close()

    @mcp.tool()
    def find_person(name: str, limit: int = 10) -> list[dict]:
        """Find people (MKs, ministers, ...) by name and return their KNS_Person Id with a role summary.

        Names are stored in Hebrew, so search in Hebrew (e.g. "נתניהו" or "בנימין נתניהו");
        each word must match part of the first or last name. Returns Id, name, gender,
        IsCurrent, the Knesset terms served as MK, and the positions held (with counts).
        """
        words = name.split()
        if not words:
            raise ToolError("Pass part of a first and/or last name.")
        if not re.search(r"[֐-׿]", name):
            raise ToolError("Names are stored in Hebrew; search with the Hebrew spelling, e.g. 'נתניהו'.")
        where = " AND ".join(["(FirstName || ' ' || LastName) LIKE ?"] * len(words))
        conn = _ro_connect(db_path)
        try:
            people = conn.execute(
                f"SELECT Id, FirstName, LastName, GenderDesc, IsCurrent FROM KNS_Person WHERE {where} "
                f"ORDER BY IsCurrent DESC, Id DESC LIMIT ?",
                [f"%{w}%" for w in words] + [limit],
            ).fetchall()
            result = []
            for pid, first, last, gender, current in people:
                knessets = [r[0] for r in conn.execute(
                    f"SELECT DISTINCT KnessetNum FROM KNS_PersonToPosition WHERE PersonID=? "
                    f"AND PositionID IN {_MK_POSITIONS} AND KnessetNum IS NOT NULL ORDER BY 1",
                    (pid,),
                )]
                positions = {
                    desc: n for desc, n in conn.execute(
                        "SELECT p.Description, COUNT(*) FROM KNS_PersonToPosition pp "
                        "LEFT JOIN KNS_Position p ON p.Id = pp.PositionID WHERE pp.PersonID=? "
                        "GROUP BY 1 ORDER BY 2 DESC",
                        (pid,),
                    )
                }
                result.append({
                    "Id": pid, "FirstName": first, "LastName": last, "Gender": gender,
                    "IsCurrent": bool(current), "mk_in_knessets": knessets, "positions": positions,
                })
            return result
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

    @mcp.tool()
    def get_vote_official_link(vote_ids: int | list[int]) -> list[dict]:
        """Official knesset.gov.il page for one or more plenum votes (KNS_PlenumVote Id).

        Include these links whenever an answer cites a vote. Each result also carries the
        vote's date, title and meaning; when the vote was on a bill, that bill's page; and when
        the bill enacted a law, the law's page.
        """
        conn = _ro_connect(db_path)
        try:
            result = []
            for vid in _as_list(vote_ids):
                row = conn.execute(
                    "SELECT v.VoteDateTime, v.VoteTitle, v.VoteSubject, v.ForOptionDesc, b.Id "
                    "FROM KNS_PlenumVote v LEFT JOIN KNS_Bill b ON b.Id = v.ItemID WHERE v.Id = ?",
                    (vid,),
                ).fetchone()
                link = {"vote_id": vid, "url": VOTE_URL.format(vid)}
                if row is None:
                    link["found_in_mirror"] = False
                else:
                    date, title, subject, for_option, bill_id = row
                    link.update(date=date, title=title, subject=subject, for_option=for_option,
                                bill_id=bill_id, bill_url=BILL_URL.format(bill_id) if bill_id else None)
                    if bill_id:
                        link.update(_enacted_law(conn, bill_id))
                result.append(link)
            return result
        finally:
            conn.close()

    @mcp.tool()
    def get_bill_official_link(bill_ids: int | list[int]) -> list[dict]:
        """Official knesset.gov.il legislation page for one or more bills (KNS_Bill Id).

        Include these links whenever an answer cites a bill. When the bill enacted a law
        (KNS_IsraelLaw), the law's page is included too.
        """
        conn = _ro_connect(db_path)
        try:
            result = []
            for bid in _as_list(bill_ids):
                row = conn.execute("SELECT Name, KnessetNum FROM KNS_Bill WHERE Id = ?", (bid,)).fetchone()
                link = {"bill_id": bid, "url": BILL_URL.format(bid)}
                if row is None:
                    link["found_in_mirror"] = False
                else:
                    link.update(name=row[0], knesset_num=row[1], **_enacted_law(conn, bid))
                result.append(link)
            return result
        finally:
            conn.close()

    @mcp.tool()
    def get_law_official_link(law_ids: int | list[int]) -> list[dict]:
        """Official knesset.gov.il page for one or more laws of Israel (KNS_IsraelLaw Id).

        Include these links whenever an answer cites a law. Each result also carries the
        law's name and, when it is in the mirror, the bill that originally enacted it and
        that bill's page (whose ItemID links it to its plenum votes).
        """
        conn = _ro_connect(db_path)
        try:
            result = []
            for lid in _as_list(law_ids):
                law = conn.execute("SELECT Name FROM KNS_IsraelLaw WHERE Id = ?", (lid,)).fetchone()
                link = {"law_id": lid, "url": LAW_URL.format(lid)}
                if law is None:
                    link["found_in_mirror"] = False
                else:
                    bill = conn.execute(
                        "SELECT b.Id FROM KNS_LawBinding lb JOIN KNS_Bill b ON b.Id = lb.LawID "
                        "WHERE lb.IsraelLawID = ? AND lb.BindingType = ? ORDER BY b.Id LIMIT 1",
                        (lid, _ORIGINAL_LAW_BINDING),
                    ).fetchone()
                    link.update(name=law[0], enacting_bill_id=bill[0] if bill else None,
                                enacting_bill_url=BILL_URL.format(bill[0]) if bill else None)
                result.append(link)
            return result
        finally:
            conn.close()
