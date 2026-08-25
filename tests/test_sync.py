import sqlite3

from knesset_utils.db import ddl, state as state_mod, sync as sync_mod
from knesset_utils.schema.metadata import ColumnDef, EntityDef


class FakeClient:
    """Serves pre-canned pages per entity set, one page per call, in order."""

    def __init__(self, pages: dict[str, list[list[dict]]]):
        self._pages = pages
        self._calls: dict[str, int] = {}

    def get_entities(self, entity_set, filter=None, orderby=None, top=None, count=False):
        idx = self._calls.get(entity_set, 0)
        pages = self._pages[entity_set]
        page = pages[idx] if idx < len(pages) else []
        self._calls[entity_set] = idx + 1
        return {"value": page}


def _entity_with_last_updated():
    return EntityDef(
        entity_type="KNS_Foo",
        entity_set="KNS_Foo",
        key="Id",
        columns=[ColumnDef("Id", "Edm.Int32"), ColumnDef("LastUpdatedDate", "Edm.DateTimeOffset")],
    )


def _entity_without_last_updated():
    return EntityDef(
        entity_type="KNS_Lookup",
        entity_set="KNS_Lookup",
        key="Id",
        columns=[ColumnDef("Id", "Edm.Int32"), ColumnDef("Name", "Edm.String")],
    )


def test_keyset_full_sync_terminates_and_upserts_all_rows_across_a_gap():
    entity = _entity_with_last_updated()
    conn = sqlite3.connect(":memory:")
    state_mod.ensure_state_table(conn)
    ddl.create_all_tables(conn, {"KNS_Foo": entity})

    # deliberate gap between page 1 and page 2 (ids 101-149 missing) --
    # keyset pagination must not care.
    page1 = [{"Id": i, "LastUpdatedDate": "2024-01-01T00:00:00Z"} for i in range(1, 101)]
    page2 = [{"Id": i, "LastUpdatedDate": "2024-01-02T00:00:00Z"} for i in range(150, 175)]
    client = FakeClient({"KNS_Foo": [page1, page2]})

    total, last_id, watermark = sync_mod.keyset_full_sync(client, conn, entity)

    assert total == 125
    assert last_id == 174
    assert watermark == "2024-01-02T00:00:00Z"
    count = conn.execute('SELECT COUNT(*) FROM "KNS_Foo"').fetchone()[0]
    assert count == 125


def test_keyset_full_sync_resumes_from_checkpoint():
    entity = _entity_with_last_updated()
    conn = sqlite3.connect(":memory:")
    state_mod.ensure_state_table(conn)
    ddl.create_all_tables(conn, {"KNS_Foo": entity})

    page1 = [{"Id": i, "LastUpdatedDate": "2024-01-01T00:00:00Z"} for i in range(1, 101)]
    client = FakeClient({"KNS_Foo": [page1]})
    sync_mod.keyset_full_sync(client, conn, entity)  # simulate an interrupted first run

    page2 = [{"Id": i, "LastUpdatedDate": "2024-01-02T00:00:00Z"} for i in range(101, 121)]
    client2 = FakeClient({"KNS_Foo": [page2]})
    total, last_id, _ = sync_mod.keyset_full_sync(client2, conn, entity, resume_from=100)

    assert total == 20
    assert last_id == 120
    count = conn.execute('SELECT COUNT(*) FROM "KNS_Foo"').fetchone()[0]
    assert count == 120


def test_incremental_sync_advances_watermark_and_upserts_only_new_rows():
    entity = _entity_with_last_updated()
    conn = sqlite3.connect(":memory:")
    ddl.create_all_tables(conn, {"KNS_Foo": entity})

    new_rows = [{"Id": 200, "LastUpdatedDate": "2024-06-01T00:00:00Z"}]
    client = FakeClient({"KNS_Foo": [new_rows]})

    rows, new_watermark = sync_mod.incremental_sync(client, conn, entity, "2024-05-01T00:00:00Z")

    assert rows == 1
    assert new_watermark == "2024-06-01T00:00:00Z"
    count = conn.execute('SELECT COUNT(*) FROM "KNS_Foo"').fetchone()[0]
    assert count == 1


def test_full_replace_sync_clears_table_first():
    entity = _entity_without_last_updated()
    conn = sqlite3.connect(":memory:")
    ddl.create_all_tables(conn, {"KNS_Lookup": entity})
    conn.execute('INSERT INTO "KNS_Lookup" (Id, Name) VALUES (999, "stale")')
    conn.commit()

    fresh_rows = [{"Id": 1, "Name": "a"}, {"Id": 2, "Name": "b"}]
    client = FakeClient({"KNS_Lookup": [fresh_rows]})

    total = sync_mod.full_replace_sync(client, conn, entity)

    assert total == 2
    ids = [r[0] for r in conn.execute('SELECT Id FROM "KNS_Lookup" ORDER BY Id').fetchall()]
    assert ids == [1, 2]


def test_sync_table_routes_by_has_last_updated():
    conn = sqlite3.connect(":memory:")
    state_mod.ensure_state_table(conn)

    with_lud = _entity_with_last_updated()
    without_lud = _entity_without_last_updated()
    ddl.create_all_tables(conn, {"KNS_Foo": with_lud, "KNS_Lookup": without_lud})

    client = FakeClient(
        {
            "KNS_Foo": [[{"Id": 1, "LastUpdatedDate": "2024-01-01T00:00:00Z"}]],
            "KNS_Lookup": [[{"Id": 1, "Name": "a"}]],
        }
    )

    result_a = sync_mod.sync_table(client, conn, with_lud)
    result_b = sync_mod.sync_table(client, conn, without_lud)

    assert result_a["mode"] == "initial-keyset-crawl"
    assert result_b["mode"] == "full-replace"
