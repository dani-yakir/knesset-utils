import sqlite3

import pytest
import typer

from knesset_utils import cli
from knesset_utils.db import ddl
from knesset_utils.schema.metadata import ColumnDef, EntityDef


@pytest.fixture
def two_tables(monkeypatch, tmp_path):
    entities = {
        "KNS_A": EntityDef("KNS_A", "KNS_A", "Id", [ColumnDef("Id", "Edm.Int32")]),
        "KNS_B": EntityDef("KNS_B", "KNS_B", "Id", [ColumnDef("Id", "Edm.Int32")]),
    }
    monkeypatch.setattr(cli.schema_metadata, "load_snapshot", lambda: entities)

    class _NoopClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(cli, "ODataClient", _NoopClient)
    return tmp_path


def _run(monkeypatch, tmp_path, results, *, fail_on_any):
    """results: dict table_name -> "ok" | Exception to raise."""

    def fake_sync_table(client, conn, entity):
        outcome = results[entity.entity_set]
        if isinstance(outcome, Exception):
            raise outcome
        return {"table": entity.entity_set, "mode": "test", "rows": 1}

    monkeypatch.setattr(cli.sync_mod, "sync_table", fake_sync_table)
    cli._run_sync(
        None,
        tmp_path / "m.sqlite",
        tmp_path / "sync.log",
        fail_on_any=fail_on_any,
    )


def test_run_sync_all_ok_exits_zero(monkeypatch, two_tables):
    _run(monkeypatch, two_tables, {"KNS_A": "ok", "KNS_B": "ok"}, fail_on_any=True)


def test_run_sync_partial_failure_default_is_tolerated(monkeypatch, two_tables):
    _run(monkeypatch, two_tables, {"KNS_A": "ok", "KNS_B": RuntimeError("boom")}, fail_on_any=False)


def test_run_sync_partial_failure_with_fail_on_any_raises(monkeypatch, two_tables):
    with pytest.raises(typer.Exit) as exc:
        _run(monkeypatch, two_tables, {"KNS_A": "ok", "KNS_B": RuntimeError("boom")}, fail_on_any=True)
    assert exc.value.exit_code == 1


def test_run_sync_total_failure_always_raises(monkeypatch, two_tables):
    with pytest.raises(typer.Exit) as exc:
        _run(monkeypatch, two_tables, {"KNS_A": RuntimeError("x"), "KNS_B": RuntimeError("y")}, fail_on_any=False)
    assert exc.value.exit_code == 1


def test_run_sync_writes_log_file(monkeypatch, two_tables):
    _run(monkeypatch, two_tables, {"KNS_A": "ok", "KNS_B": "ok"}, fail_on_any=False)
    log = (two_tables / "sync.log").read_text(encoding="utf-8")
    assert "Sync run complete" in log


def test_created_db_is_a_valid_sqlite_file(monkeypatch, two_tables):
    _run(monkeypatch, two_tables, {"KNS_A": "ok", "KNS_B": "ok"}, fail_on_any=False)
    conn = sqlite3.connect(two_tables / "m.sqlite")
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert {"KNS_A", "KNS_B", "_sync_state"} <= names


def test_run_sync_rolls_back_a_table_that_fails_mid_full_replace(monkeypatch, two_tables):
    """A full-replace that dies after its DELETE must keep its old rows, not have
    the DELETE + partial crawl committed by the next table's sync."""
    db = two_tables / "m.sqlite"
    conn = sqlite3.connect(db)
    ddl.create_all_tables(conn, cli.schema_metadata.load_snapshot())
    conn.executemany('INSERT INTO "KNS_A" (Id) VALUES (?)', [(i,) for i in range(1, 251)])
    conn.commit()
    conn.close()

    class FlakyClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get_entities(self, entity_set, filter=None, orderby=None, top=None, count=False):
            if entity_set == "KNS_A":
                if filter == "Id gt 0":
                    return {"value": [{"Id": i} for i in range(1, 101)]}  # full page -> crawl continues
                raise RuntimeError("481 mid-crawl")
            return {"value": [{"Id": 1}]}

    monkeypatch.setattr(cli, "ODataClient", FlakyClient)
    cli._run_sync(None, db, two_tables / "sync.log", fail_on_any=False)

    conn = sqlite3.connect(db)
    try:
        assert conn.execute('SELECT COUNT(*) FROM "KNS_A"').fetchone()[0] == 250
        assert conn.execute('SELECT COUNT(*) FROM "KNS_B"').fetchone()[0] == 1
    finally:
        conn.close()
