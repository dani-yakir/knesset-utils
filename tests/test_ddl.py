from knesset_utils.db.ddl import build_create_table, build_indexes, sqlite_type
from knesset_utils.schema.metadata import ColumnDef, EntityDef


def test_sqlite_type_mapping():
    assert sqlite_type("Edm.Int32") == "INTEGER"
    assert sqlite_type("Edm.String") == "TEXT"
    assert sqlite_type("Edm.DateTimeOffset") == "TEXT"
    assert sqlite_type("Edm.Boolean") == "INTEGER"
    assert sqlite_type("Edm.Double") == "REAL"
    assert sqlite_type("Edm.SomethingUnknown") == "TEXT"


def test_build_create_table_uses_key_as_primary_key():
    entity = EntityDef(
        entity_type="KNS_Foo",
        entity_set="KNS_Foo",
        key="Id",
        columns=[ColumnDef("Id", "Edm.Int32"), ColumnDef("Name", "Edm.String")],
    )
    sql = build_create_table(entity)
    assert '"Id" INTEGER PRIMARY KEY' in sql
    assert '"Name" TEXT' in sql


def test_build_indexes_targets_last_updated_and_fk_shaped_columns_only():
    entity = EntityDef(
        entity_type="KNS_Foo",
        entity_set="KNS_Foo",
        key="Id",
        columns=[
            ColumnDef("Id", "Edm.Int32"),
            ColumnDef("CommitteeID", "Edm.Int32"),
            ColumnDef("LastUpdatedDate", "Edm.DateTimeOffset"),
            ColumnDef("Name", "Edm.String"),
        ],
    )
    idx_sql = build_indexes(entity)
    joined = " ".join(idx_sql)
    assert "CommitteeID" in joined
    assert "LastUpdatedDate" in joined
    assert "Name" not in joined
    assert not any(sql.endswith('("Id")') for sql in idx_sql)
