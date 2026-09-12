import sqlite3

import pytest
import typer

from knesset_utils import cli
from knesset_utils.db import ddl, state as state_mod
from knesset_utils.schema.metadata import ColumnDef, EntityDef
from tests.fake_odata import FakeOData


@pytest.fixture
def two_tables(monkeypatch, tmp_path):
    entities = {
        "KNS_A": EntityDef("KNS_A", "KNS_A", "Id", [ColumnDef("Id", "Edm.Int32")]),
        "KNS_B": EntityDef("KNS_B", "KNS_B", "Id", [ColumnDef("Id", "Edm.Int32")]),
    }
    monkeypatch.setattr(cli.schema_metadata, "load_snapshot", lambda: entities)

    class _NoopClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(cli, "ODataClient", _NoopClient)
    return tmp_path


def _run(monkeypatch, tmp_path, results, *, fail_on_any):
    """results: dict table_name -> "ok" | Exception to raise."""

    def fake_sync_tables(client, conn, entities, *, workers, gate, max_runtime_s=None):
        succeeded, failed = [], []
        for entity in entities:
            if isinstance(results[entity.entity_set], Exception):
                failed.append(entity.entity_set)
            else:
                succeeded.append({"table": entity.entity_set, "mode": "test", "rows": 1})
        return succeeded, failed

    monkeypatch.setattr(cli.sync_mod, "sync_tables", fake_sync_tables)
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


def test_run_sync_keeps_old_rows_of_a_full_replace_that_fails_mid_crawl(monkeypatch, two_tables):
    """End to end through the CLI with 4 workers: the failed table keeps its
    previous rows (no truncated table published), the other one still syncs."""
    db = two_tables / "m.sqlite"
    conn = sqlite3.connect(db)
    ddl.create_all_tables(conn, cli.schema_metadata.load_snapshot())
    conn.executemany('INSERT INTO "KNS_A" (Id) VALUES (?)', [(i,) for i in range(1, 251)])
    conn.commit()
    conn.close()

    fake = FakeOData({"KNS_A": [{"Id": i} for i in range(1, 301)], "KNS_B": [{"Id": 1}]})
    fake.fail = lambda table, flt: table == "KNS_A" and (flt or "").startswith("Id gt 100 ")
    monkeypatch.setattr(cli, "ODataClient", lambda **kwargs: fake)
    cli._run_sync(None, db, two_tables / "sync.log", fail_on_any=False, workers=4)

    conn = sqlite3.connect(db)
    try:
        assert conn.execute('SELECT COUNT(*) FROM "KNS_A"').fetchone()[0] == 250
        assert conn.execute('SELECT COUNT(*) FROM "KNS_B"').fetchone()[0] == 1
    finally:
        conn.close()


def _state_db(tmp_path, complete):
    db = tmp_path / "m.sqlite"
    conn = sqlite3.connect(db)
    state_mod.ensure_state_table(conn)
    ddl.create_all_tables(conn, cli.schema_metadata.load_snapshot())
    for name in complete:
        state_mod.save_state(conn, state_mod.SyncState(name, full_sync_complete=True))
    conn.commit()
    conn.close()
    return db


def test_status_check_complete_rejects_a_half_crawled_mirror(two_tables):
    db = _state_db(two_tables, complete=["KNS_A"])
    with pytest.raises(typer.Exit) as exc:
        cli.status(db_path=db, check_complete=True)
    assert exc.value.exit_code == 1


def test_status_check_complete_rejects_leftover_chunks(two_tables):
    db = _state_db(two_tables, complete=["KNS_A", "KNS_B"])
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO _sync_chunks (table_name, lo, hi, cursor) VALUES ('KNS_B', 500, 900, 700)")
    conn.commit()
    conn.close()
    with pytest.raises(typer.Exit) as exc:
        cli.status(db_path=db, check_complete=True)
    assert exc.value.exit_code == 1


def test_status_check_complete_accepts_a_finished_mirror(two_tables):
    db = _state_db(two_tables, complete=["KNS_A", "KNS_B"])
    cli.status(db_path=db, check_complete=True)  # no Exit raised
