"""Validate foreign-key integrity in the local mirror.

The OData API gives no guarantee these hold -- this exists because it was
discovered empirically that they don't always: e.g. KNS_PlenumVoteResult.MkId
doesn't always correspond to an extant KNS_Person.Id. See docs/fk_integrity.md
for the full results and schema/foreign_keys.py for how the FK set itself was
derived.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from knesset_utils.schema.foreign_keys import find_foreign_keys
from knesset_utils.schema.metadata import EntityDef

EXAMPLE_LIMIT = 5


@dataclass
class FkResult:
    source_table: str
    fk_column: str
    target_table: str
    source_rows_with_value: int
    orphan_rows: int
    orphan_distinct_values: list
    example_orphan_rows: list[dict] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return self.orphan_rows == 0


def check_foreign_key(conn: sqlite3.Connection, source: str, fk_col: str, target: str, target_key: str) -> FkResult:
    total_with_value = conn.execute(
        f'SELECT COUNT(*) FROM "{source}" WHERE "{fk_col}" IS NOT NULL'
    ).fetchone()[0]

    orphan_rows = conn.execute(
        f'SELECT COUNT(*) FROM "{source}" s '
        f'WHERE s."{fk_col}" IS NOT NULL '
        f'AND NOT EXISTS (SELECT 1 FROM "{target}" t WHERE t."{target_key}" = s."{fk_col}")'
    ).fetchone()[0]

    orphan_values: list = []
    example_rows: list[dict] = []
    if orphan_rows:
        orphan_values = [
            r[0]
            for r in conn.execute(
                f'SELECT DISTINCT s."{fk_col}" FROM "{source}" s '
                f'WHERE s."{fk_col}" IS NOT NULL '
                f'AND NOT EXISTS (SELECT 1 FROM "{target}" t WHERE t."{target_key}" = s."{fk_col}") '
                f'LIMIT 20'
            ).fetchall()
        ]
        cur = conn.execute(
            f'SELECT * FROM "{source}" s '
            f'WHERE s."{fk_col}" IS NOT NULL '
            f'AND NOT EXISTS (SELECT 1 FROM "{target}" t WHERE t."{target_key}" = s."{fk_col}") '
            f'LIMIT {EXAMPLE_LIMIT}'
        )
        cols = [d[0] for d in cur.description]
        example_rows = [dict(zip(cols, row)) for row in cur.fetchall()]

    return FkResult(
        source_table=source,
        fk_column=fk_col,
        target_table=target,
        source_rows_with_value=total_with_value,
        orphan_rows=orphan_rows,
        orphan_distinct_values=orphan_values,
        example_orphan_rows=example_rows,
    )


def validate_all(conn: sqlite3.Connection, entities: dict[str, EntityDef]) -> list[FkResult]:
    fks = find_foreign_keys(entities)
    results = []
    for source, fk_col, target in fks:
        target_key = entities[target].key
        results.append(check_foreign_key(conn, source, fk_col, target, target_key))
    return results
