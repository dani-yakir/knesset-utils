"""Generate SQLite DDL from a parsed OData EntityDef."""
from __future__ import annotations

import sqlite3

from knesset_utils.schema.metadata import EntityDef

EDM_TO_SQLITE = {
    "Edm.Int16": "INTEGER",
    "Edm.Int32": "INTEGER",
    "Edm.Int64": "INTEGER",
    "Edm.String": "TEXT",
    "Edm.Boolean": "INTEGER",
    "Edm.DateTimeOffset": "TEXT",
    "Edm.Double": "REAL",
    "Edm.Decimal": "REAL",
    "Edm.Guid": "TEXT",
}


def sqlite_type(edm_type: str) -> str:
    return EDM_TO_SQLITE.get(edm_type, "TEXT")


def build_create_table(entity: EntityDef) -> str:
    col_sql = []
    for col in entity.columns:
        affinity = sqlite_type(col.edm_type)
        if col.name == entity.key:
            col_sql.append(f'"{col.name}" {affinity} PRIMARY KEY')
        else:
            col_sql.append(f'"{col.name}" {affinity}')
    return f'CREATE TABLE IF NOT EXISTS "{entity.entity_set}" ({", ".join(col_sql)})'


def build_indexes(entity: EntityDef) -> list[str]:
    """Index LastUpdatedDate (incremental sync filters on it) and any
    foreign-key-shaped column (join target for the stats layer). The primary
    key itself is already indexed as part of PRIMARY KEY.
    """
    statements = []
    for col in entity.columns:
        if col.name == entity.key:
            continue
        is_last_updated = col.name == "LastUpdatedDate"
        is_fk_shaped = col.name.endswith("Id") or col.name.endswith("ID")
        if is_last_updated or is_fk_shaped:
            idx_name = f"idx_{entity.entity_set}_{col.name}"
            statements.append(f'CREATE INDEX IF NOT EXISTS "{idx_name}" ON "{entity.entity_set}" ("{col.name}")')
    return statements


def create_all_tables(conn: sqlite3.Connection, entities: dict[str, EntityDef]) -> None:
    cur = conn.cursor()
    for entity in entities.values():
        cur.execute(build_create_table(entity))
        for idx_sql in build_indexes(entity):
            cur.execute(idx_sql)
    conn.commit()
