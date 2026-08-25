"""Canned statistical analyses over the SQLite mirror. Starting pattern --
expand as real questions come up; not meant to be exhaustive yet.
"""
from __future__ import annotations

import sqlite3

import pandas as pd


def mk_vote_agreement(conn: sqlite3.Connection, mk_id_a: int, mk_id_b: int) -> dict:
    """Fraction of shared plenum votes where two MKs cast the same ResultDesc."""
    query = """
        SELECT a.VoteID, a.ResultDesc AS result_a, b.ResultDesc AS result_b
        FROM KNS_PlenumVoteResult a
        JOIN KNS_PlenumVoteResult b ON a.VoteID = b.VoteID
        WHERE a.MkId = ? AND b.MkId = ?
    """
    df = pd.read_sql_query(query, conn, params=(mk_id_a, mk_id_b))
    if df.empty:
        return {"mk_id_a": mk_id_a, "mk_id_b": mk_id_b, "shared_votes": 0, "agreement_rate": None}
    agree = int((df["result_a"] == df["result_b"]).sum())
    return {
        "mk_id_a": mk_id_a,
        "mk_id_b": mk_id_b,
        "shared_votes": len(df),
        "agreement_rate": agree / len(df),
    }


def vote_result_breakdown(conn: sqlite3.Connection, vote_id: int) -> dict:
    """Tally of ResultDesc values (for/against/abstain/...) for a single plenum vote."""
    query = 'SELECT ResultDesc, COUNT(*) as n FROM KNS_PlenumVoteResult WHERE VoteID = ? GROUP BY ResultDesc'
    df = pd.read_sql_query(query, conn, params=(vote_id,))
    return dict(zip(df["ResultDesc"], df["n"].astype(int)))
