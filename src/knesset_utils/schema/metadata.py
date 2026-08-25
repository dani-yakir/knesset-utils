"""Parse the Knesset v4 OData $metadata document into a schema we can generate
SQLite DDL from, and manage a committed snapshot of it so DDL generation and
tests don't require a live network call by default.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

from knesset_utils.odata.client import ODataClient

EDM_NS = "{http://docs.oasis-open.org/odata/ns/edm}"

# Confirmed empirically before this was written: these entity sets appear in
# $metadata and the service document but 404 on every request. Permanently
# excluded from sync.
BROKEN_ENTITY_SETS = frozenset({"KNS_DocumentQuerie", "V_Lobbyists", "V_LobbyistsClients"})

SNAPSHOT_PATH = Path(__file__).parent / "snapshot.json"


@dataclass
class ColumnDef:
    name: str
    edm_type: str
    nullable: bool = True


@dataclass
class EntityDef:
    entity_type: str
    entity_set: str
    key: str | None
    columns: list[ColumnDef] = field(default_factory=list)

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    @property
    def has_last_updated(self) -> bool:
        return "LastUpdatedDate" in self.column_names


def parse_metadata_xml(xml_bytes: bytes) -> tuple[dict[str, EntityDef], dict[str, str]]:
    """Returns (entity_types_by_type_name, entity_set_name -> entity_type_name)."""
    root = ET.fromstring(xml_bytes)
    entity_types: dict[str, EntityDef] = {}
    for et in root.iter(f"{EDM_NS}EntityType"):
        name = et.attrib["Name"]
        key_prop: str | None = None
        columns: list[ColumnDef] = []
        for child in et:
            tag = child.tag.split("}")[-1]
            if tag == "Key":
                for pref in child.iter(f"{EDM_NS}PropertyRef"):
                    key_prop = pref.attrib["Name"]
            elif tag == "Property":
                nullable = child.attrib.get("Nullable", "true").lower() != "false"
                columns.append(ColumnDef(child.attrib["Name"], child.attrib["Type"], nullable))
        entity_types[name] = EntityDef(entity_type=name, entity_set="", key=key_prop, columns=columns)

    entity_sets: dict[str, str] = {}
    for es in root.iter(f"{EDM_NS}EntitySet"):
        entity_sets[es.attrib["Name"]] = es.attrib["EntityType"].split(".")[-1]

    return entity_types, entity_sets


def build_entity_defs(entity_types: dict[str, EntityDef], entity_sets: dict[str, str]) -> dict[str, EntityDef]:
    """Keyed by entity SET name (what you actually query against), broken sets excluded."""
    result: dict[str, EntityDef] = {}
    for set_name, type_name in entity_sets.items():
        if set_name in BROKEN_ENTITY_SETS:
            continue
        et_def = entity_types.get(type_name)
        if et_def is None:
            continue
        result[set_name] = EntityDef(entity_type=type_name, entity_set=set_name, key=et_def.key, columns=et_def.columns)
    return result


def fetch_live_entity_defs(client: ODataClient | None = None) -> dict[str, EntityDef]:
    owns_client = client is None
    client = client or ODataClient()
    try:
        xml_bytes = client.get_metadata_xml()
    finally:
        if owns_client:
            client.close()
    entity_types, entity_sets = parse_metadata_xml(xml_bytes)
    return build_entity_defs(entity_types, entity_sets)


def _to_jsonable(defs: dict[str, EntityDef]) -> dict:
    return {
        name: {
            "entity_type": d.entity_type,
            "key": d.key,
            "columns": [{"name": c.name, "type": c.edm_type, "nullable": c.nullable} for c in d.columns],
        }
        for name, d in defs.items()
    }


def _from_jsonable(data: dict) -> dict[str, EntityDef]:
    result = {}
    for name, d in data.items():
        cols = [ColumnDef(c["name"], c["type"], c["nullable"]) for c in d["columns"]]
        result[name] = EntityDef(entity_type=d["entity_type"], entity_set=name, key=d["key"], columns=cols)
    return result


def load_snapshot(path: Path = SNAPSHOT_PATH) -> dict[str, EntityDef]:
    with open(path, "r", encoding="utf-8") as f:
        return _from_jsonable(json.load(f))


def write_snapshot(defs: dict[str, EntityDef], path: Path = SNAPSHOT_PATH) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(_to_jsonable(defs), f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def diff_snapshot(old: dict[str, EntityDef], new: dict[str, EntityDef]) -> dict[str, list[str]]:
    added_tables = sorted(set(new) - set(old))
    removed_tables = sorted(set(old) - set(new))
    changed_columns: list[str] = []
    for name in sorted(set(old) & set(new)):
        old_cols = {c.name for c in old[name].columns}
        new_cols = {c.name for c in new[name].columns}
        if old_cols != new_cols:
            changed_columns.append(f"{name}: +{sorted(new_cols - old_cols)} -{sorted(old_cols - new_cols)}")
    return {"added_tables": added_tables, "removed_tables": removed_tables, "changed_columns": changed_columns}
