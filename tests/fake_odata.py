"""In-memory stand-in for the Knesset OData service, for driving db/sync.py.

Interprets exactly the query shapes the sync engine sends (anything else is a
test failure), caps pages at 100 rows like the real server, and is safe to
call from many threads at once.
"""
from __future__ import annotations

import re
import threading
import time
from collections import Counter

SERVER_PAGE_CAP = 100

_RANGE = re.compile(r"Id gt (-?\d+)(?: and Id le (-?\d+))?")
_LUD_GE = re.compile(r"LastUpdatedDate ge (\S+)")
_LUD_AFTER = re.compile(r"LastUpdatedDate gt (\S+) or \(LastUpdatedDate eq (\S+) and Id gt (-?\d+)\)")


def _predicate(flt: str | None):
    if flt is None:
        return lambda r: True
    if m := _RANGE.fullmatch(flt):
        lo, hi = int(m[1]), m[2]
        return lambda r: r["Id"] > lo and (hi is None or r["Id"] <= int(hi))
    if m := _LUD_GE.fullmatch(flt):
        return lambda r: r["LastUpdatedDate"] >= m[1]
    if m := _LUD_AFTER.fullmatch(flt):
        assert m[1] == m[2], flt
        return lambda r: r["LastUpdatedDate"] > m[1] or (r["LastUpdatedDate"] == m[1] and r["Id"] > int(m[3]))
    raise AssertionError(f"unexpected $filter shape: {flt!r}")


def _sort_key(orderby: str):
    if orderby == "Id":
        return (lambda r: r["Id"]), False
    if orderby == "Id desc":
        return (lambda r: r["Id"]), True
    if orderby == "LastUpdatedDate desc":
        return (lambda r: r["LastUpdatedDate"]), True
    if orderby == "LastUpdatedDate,Id":
        return (lambda r: (r["LastUpdatedDate"], r["Id"])), False
    raise AssertionError(f"unexpected $orderby: {orderby!r}")


class FakeOData:
    def __init__(self, tables: dict[str, list[dict]], *, latency: float = 0.0):
        self.tables = tables
        self.latency = latency
        self.fail = None  # optional callable(entity_set, filter) -> bool: raise for this request
        self.count_calls = 0
        self.returned = Counter()  # (table, Id) -> times served by a data (non-$select) query
        self._lock = threading.Lock()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_count(self, entity_set: str) -> int:
        with self._lock:
            self.count_calls += 1
        return len(self.tables[entity_set])

    def get_entities(self, entity_set, *, filter=None, orderby=None, top=None, count=False, select=None):
        if self.fail is not None and self.fail(entity_set, filter):
            raise RuntimeError(f"injected failure: {entity_set} {filter}")
        if self.latency:
            time.sleep(self.latency)
        pred = _predicate(filter)
        rows = [r for r in self.tables[entity_set] if pred(r)]
        if orderby:
            key, reverse = _sort_key(orderby)
            rows.sort(key=key, reverse=reverse)
        rows = rows[: min(top or SERVER_PAGE_CAP, SERVER_PAGE_CAP)]
        if select:
            return {"value": [{select: r[select]} for r in rows]}
        with self._lock:
            self.returned.update((entity_set, r["Id"]) for r in rows)
        return {"value": [dict(r) for r in rows]}
