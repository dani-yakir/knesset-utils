import re
import sqlite3
import threading
import time

import pytest

from knesset_utils.db import ddl, state as state_mod, sync as sync_mod
from knesset_utils.schema.metadata import ColumnDef, EntityDef
from tests.fake_odata import FakeOData


def _entity(name: str, *, with_lud: bool = True) -> EntityDef:
    cols = [ColumnDef("Id", "Edm.Int32"), ColumnDef("Name", "Edm.String")]
    if with_lud:
        cols.append(ColumnDef("LastUpdatedDate", "Edm.DateTimeOffset"))
    return EntityDef(name, name, "Id", cols)


def _db(*entities: EntityDef) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    state_mod.ensure_state_table(conn)
    ddl.create_all_tables(conn, {e.entity_set: e for e in entities})
    return conn


def _row(i: int, lud: str | None = None) -> dict:
    return {"Id": i, "Name": f"n{i}", "LastUpdatedDate": lud or f"2024-01-{i % 28 + 1:02d}T00:00:00Z"}


def _clustered_rows() -> list[dict]:
    """Most rows packed at the bottom of a wide Id range, like KNS_DocumentCommitteeSession."""
    return [_row(i) for i in range(1, 3001)] + [_row(3000 + i * 997) for i in range(1, 501)]


def _ids(conn: sqlite3.Connection, table: str) -> list[int]:
    return [r[0] for r in conn.execute(f'SELECT Id FROM "{table}" ORDER BY Id')]


def _chunks_left(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM _sync_chunks WHERE table_name = ?", (table,)).fetchone()[0]


@pytest.mark.parametrize("workers", [1, 8])
def test_crawl_from_scratch_fetches_every_row_exactly_once(workers):
    entity = _entity("KNS_Foo")
    source = _clustered_rows()
    fake = FakeOData({"KNS_Foo": source}, latency=0.001)
    conn = _db(entity)

    succeeded, failed = sync_mod.sync_tables(fake, conn, [entity], workers=workers)

    assert failed == []
    assert succeeded[0]["mode"] == "initial-crawl"
    assert _ids(conn, "KNS_Foo") == [r["Id"] for r in source]
    assert set(fake.returned.values()) == {1}, "a row was fetched more than once"
    st = state_mod.get_state(conn, "KNS_Foo")
    assert st.full_sync_complete
    assert st.max_last_updated_seen == max(r["LastUpdatedDate"] for r in source)
    assert _chunks_left(conn, "KNS_Foo") == 0


def test_idle_workers_split_a_dense_chunk():
    entity = _entity("KNS_Foo")
    fake = FakeOData({"KNS_Foo": _clustered_rows()}, latency=0.001)
    conn = _db(entity)

    succeeded, _ = sync_mod.sync_tables(fake, conn, [entity], workers=8)

    # 3,500 rows plan as 2 chunks + the open tail; work stealing must add more.
    assert succeeded[0]["chunks"] > 3


def test_interrupted_crawl_resumes_from_chunk_checkpoints():
    entity = _entity("KNS_Foo")
    source = [_row(i) for i in range(1, 5001)]
    fake = FakeOData({"KNS_Foo": source})
    tripped = threading.Event()

    def fail_once_mid_crawl(table, flt):
        # the first page request at or past Id 1500 -- split points decide its exact cursor
        m = re.match(r"Id gt (\d+)", flt or "")
        if m and int(m[1]) >= 1500 and not tripped.is_set():
            tripped.set()
            return True
        return False

    fake.fail = fail_once_mid_crawl
    conn = _db(entity)
    _, failed = sync_mod.sync_tables(fake, conn, [entity], workers=4)
    assert failed == ["KNS_Foo"]
    assert not state_mod.get_state(conn, "KNS_Foo").full_sync_complete
    assert _chunks_left(conn, "KNS_Foo") > 0
    assert 0 < len(_ids(conn, "KNS_Foo")) < len(source)

    fake.fail = None
    counts_before = fake.count_calls
    succeeded, failed = sync_mod.sync_tables(fake, conn, [entity], workers=4)

    assert failed == []
    assert fake.count_calls == counts_before, "resume should reuse the persisted plan, not re-plan"
    assert _ids(conn, "KNS_Foo") == [r["Id"] for r in source]
    assert state_mod.get_state(conn, "KNS_Foo").full_sync_complete
    assert _chunks_left(conn, "KNS_Foo") == 0


def test_failed_full_replace_keeps_the_previous_rows():
    entity = _entity("KNS_Lookup", with_lud=False)
    conn = _db(entity)
    conn.executemany('INSERT INTO "KNS_Lookup" (Id, Name) VALUES (?, ?)', [(i, "old") for i in range(1, 251)])
    conn.commit()
    fake = FakeOData({"KNS_Lookup": [{"Id": i, "Name": "new"} for i in range(1, 401)]})
    fake.fail = lambda table, flt: flt is not None and flt.startswith("Id gt 100 ")

    _, failed = sync_mod.sync_tables(fake, conn, [entity], workers=1)

    assert failed == ["KNS_Lookup"]
    assert conn.execute('SELECT COUNT(*), MIN(Name), MAX(Name) FROM "KNS_Lookup"').fetchone() == (250, "old", "old")
    assert conn.execute("SELECT name FROM sqlite_master WHERE name LIKE '_staging_%'").fetchall() == []

    fake.fail = None
    succeeded, failed = sync_mod.sync_tables(fake, conn, [entity], workers=4)

    assert failed == [] and succeeded[0]["mode"] == "full-replace"
    assert conn.execute('SELECT COUNT(*), MIN(Name), MAX(Name) FROM "KNS_Lookup"').fetchone() == (400, "new", "new")


def test_full_replace_drops_rows_gone_from_the_source():
    entity = _entity("KNS_Lookup", with_lud=False)
    conn = _db(entity)
    conn.execute("INSERT INTO \"KNS_Lookup\" (Id, Name) VALUES (999, 'stale')")
    conn.commit()
    fake = FakeOData({"KNS_Lookup": [{"Id": 1, "Name": "a"}, {"Id": 2, "Name": "b"}]})

    sync_mod.sync_tables(fake, conn, [entity], workers=2)

    assert _ids(conn, "KNS_Lookup") == [1, 2]


def test_incremental_pages_through_a_timestamp_tie_group():
    """250 rows sharing one LastUpdatedDate span three pages; paging on the
    timestamp alone would stop after the first 100."""
    entity = _entity("KNS_Foo")
    conn = _db(entity)
    watermark = "2024-06-01T00:00:00Z"
    state_mod.save_state(
        conn, state_mod.SyncState("KNS_Foo", max_last_updated_seen=watermark, full_sync_complete=True)
    )
    old = [_row(i, "2024-05-01T00:00:00Z") for i in range(1, 51)]
    boundary = [_row(51, watermark)]
    tied = [_row(i, "2024-07-01T00:00:00Z") for i in range(52, 302)]
    fake = FakeOData({"KNS_Foo": old + boundary + tied})

    succeeded, failed = sync_mod.sync_tables(fake, conn, [entity], workers=2)

    assert failed == [] and succeeded[0]["mode"] == "incremental"
    assert _ids(conn, "KNS_Foo") == list(range(51, 302))
    assert state_mod.get_state(conn, "KNS_Foo").max_last_updated_seen == "2024-07-01T00:00:00Z"
    assert fake.count_calls == 0, "incremental runs shouldn't spend requests on planning"


def test_a_failing_table_does_not_stop_the_others():
    broken, healthy = _entity("KNS_Broken"), _entity("KNS_Healthy")
    fake = FakeOData({"KNS_Broken": [_row(i) for i in range(1, 400)], "KNS_Healthy": [_row(i) for i in range(1, 400)]})
    fake.fail = lambda table, flt: table == "KNS_Broken" and flt is not None and flt.startswith("Id gt")
    conn = _db(broken, healthy)

    succeeded, failed = sync_mod.sync_tables(fake, conn, [broken, healthy], workers=4)

    assert failed == ["KNS_Broken"]
    assert [r["table"] for r in succeeded] == ["KNS_Healthy"]
    assert len(_ids(conn, "KNS_Healthy")) == 399


def test_a_table_that_fails_planning_is_reported_failed():
    broken, healthy = _entity("KNS_Broken"), _entity("KNS_Healthy")
    fake = FakeOData({"KNS_Broken": [], "KNS_Healthy": [_row(1)]})
    fake.fail = lambda table, flt: table == "KNS_Broken"
    conn = _db(broken, healthy)

    succeeded, failed = sync_mod.sync_tables(fake, conn, [broken, healthy], workers=2)

    assert failed == ["KNS_Broken"]
    assert [r["table"] for r in succeeded] == ["KNS_Healthy"]


def test_gate_paces_requests_to_its_rate():
    gate = sync_mod.RequestGate(8, rate=50)  # a request every 20ms
    t0 = time.monotonic()
    for _ in range(11):
        with gate:
            pass
    assert time.monotonic() - t0 >= 0.18


def test_a_block_pauses_everyone_once_then_halves_the_rate():
    gate = sync_mod.RequestGate(8, rate=10, penalty_s=0.3)
    returned_at = []

    def report_throttle():
        gate.throttled()
        returned_at.append(time.monotonic())

    t0 = time.monotonic()
    threads = [threading.Thread(target=report_throttle) for _ in range(3)]  # three 481s from one block
    for t in threads:
        t.start()
        time.sleep(0.02)
    for t in threads:
        t.join()

    assert gate.throttles == 1
    assert gate.rate == 5 and gate.ceiling == 8
    assert min(returned_at) - t0 >= 0.3, "retried inside the block"
    assert max(returned_at) - min(returned_at) >= 2 * 0.2 - 0.05, "retries weren't paced after the block"


def test_rate_creeps_back_up_but_stops_below_the_rate_that_tripped():
    gate = sync_mod.RequestGate(8, rate=10, penalty_s=0, step=1, step_s=0.01)
    gate.throttled()
    for _ in range(10):
        time.sleep(0.015)
        with gate:
            pass
    assert gate.rate == 8


def test_gate_caps_requests_in_flight():
    gate = sync_mod.RequestGate(3, rate=float("inf"))
    in_flight = peak = 0
    lock = threading.Lock()

    def request():
        nonlocal in_flight, peak
        with gate:
            with lock:
                in_flight += 1
                peak = max(peak, in_flight)
            time.sleep(0.01)
            with lock:
                in_flight -= 1

    threads = [threading.Thread(target=request) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak == 3


def test_a_runtime_budget_stops_the_run_and_the_next_one_resumes():
    """A rebuild outlasts a capped CI job: it stops on the budget, keeps its
    chunk checkpoints, and the next run carries on from them."""
    entity = _entity("KNS_Foo")
    source = [_row(i) for i in range(1, 5001)]
    fake = FakeOData({"KNS_Foo": source}, latency=0.02)
    conn = _db(entity)

    succeeded, failed = sync_mod.sync_tables(fake, conn, [entity], workers=2, max_runtime_s=0.3)

    assert (succeeded, failed) == ([], [])  # nothing finished, but nothing failed either
    assert 0 < len(_ids(conn, "KNS_Foo")) < len(source)
    assert _chunks_left(conn, "KNS_Foo") > 0
    assert not state_mod.get_state(conn, "KNS_Foo").full_sync_complete

    succeeded, failed = sync_mod.sync_tables(fake, conn, [entity], workers=2)

    assert failed == []
    assert _ids(conn, "KNS_Foo") == [r["Id"] for r in source]
    assert state_mod.get_state(conn, "KNS_Foo").full_sync_complete
