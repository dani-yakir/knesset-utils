"""Sync entity sets from the live OData API into the SQLite mirror.

Work is planned into units, fetched by a pool of worker threads, and applied
by a single writer (the calling thread -- SQLite has one writer). The server
returns at most 100 rows per request whatever $top says, and nearly all the
time goes to waiting on it (0.2-2s per page vs ~18ms to write one locally),
so the fetchers are threads: the work is I/O-bound, not CPU-bound.

Three kinds of unit, by table state:

- crawl -- the table has LastUpdatedDate and its initial crawl isn't done.
  The Id range is split into chunks, each walked with keyset pagination
  (`$filter=Id gt {cursor} and Id le {hi}&$orderby=Id&$top=100`). $skip was
  measured to degrade badly with depth; keyset pagination doesn't, and it
  can't miss or duplicate rows whatever the Id gaps. Chunks are disjoint and
  cover [0, max Id] plus an open-ended tail for rows added mid-crawl, so that
  guarantee holds for the whole table. Each chunk's cursor is checkpointed in
  _sync_chunks in the same transaction as its rows, so an interrupted crawl
  resumes where it stopped. Ids are heavily clustered in some tables (85% of
  KNS_DocumentCommitteeSession sits in the first 5% of its Id range), so a
  worker hands off half its remaining range whenever another worker is idle.
  The watermark for later incremental syncs is the server's newest
  LastUpdatedDate *when the crawl was planned*, not the newest one seen:
  chunks finish out of order, and "newest seen" can jump past rows updated
  mid-crawl in chunks already done. Starting earlier only re-fetches rows.
- replace -- the table has no LastUpdatedDate (a few lookup tables and
  KNS_BroadcastCommitteSession), so every run re-reads all of it. Crawled
  like the above into a staging table, swapped in with one transaction only
  once every chunk succeeded; a failure leaves the live table untouched.
- incremental -- the initial crawl is done. One unit per table, paging on the
  compound cursor (LastUpdatedDate, Id). LastUpdatedDate alone skips rows:
  timestamp ties are real (1.2M KNS_PlenumVoteResult rows share one) and
  `gt {last seen}` jumps over the rest of a tie group that straddles a page
  boundary. The stored watermark has no Id, so each run starts at
  `ge {watermark}`, re-fetching the (usually single) row on the boundary.

A unit that fails after the client's own retries fails its table: the
table's queued units are skipped, other tables carry on.
"""
from __future__ import annotations

import dataclasses
import logging
import math
import queue
import sqlite3
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone

from knesset_utils.db import ddl, state as state_mod
from knesset_utils.odata.client import ODataClient
from knesset_utils.schema.metadata import EntityDef
from knesset_utils.timeutil import format_duration

PAGE_SIZE = 100  # the server's cap: a larger $top still returns 100 rows + a nextLink
PAGES_PER_CHUNK = 25  # planning granularity; work-stealing splits even out the rest
MIN_SPLIT_WIDTH = 2 * PAGE_SIZE  # never hand off an Id range narrower than this
COMMIT_EVERY_S = 2.0
PROGRESS_EVERY_S = 30.0
EPOCH_WATERMARK = "1900-01-01T00:00:00Z"
STAGING_PREFIX = "_staging_"
MODE_NAMES = {"crawl": "initial-crawl", "replace": "full-replace", "incremental": "incremental"}

logger = logging.getLogger(__name__)


def _upsert_rows(conn: sqlite3.Connection, entity: EntityDef, rows: list[dict]) -> int:
    if not rows:
        return 0
    cols = entity.column_names
    col_list = ", ".join(f'"{c}"' for c in cols)
    placeholders = ", ".join(["?"] * len(cols))
    sql = f'INSERT OR REPLACE INTO "{entity.entity_set}" ({col_list}) VALUES ({placeholders})'
    conn.executemany(sql, [[row.get(c) for c in cols] for row in rows])
    return len(rows)


# --------------------------------------------------------------------------
# Concurrency control
# --------------------------------------------------------------------------


class RequestGate:
    """Paces requests to what the Knesset edge will take.

    Its firewall answers "access denied" (HTTP 481) once an IP sends too many
    requests in a short window, then denies that IP everything for about two
    minutes. Measured from a home IP: ~6 req/s for 3 minutes went through,
    ~30 req/s was cut off within a minute; GitHub's (Azure) runners get a
    smaller budget. Concurrency alone doesn't bound the rate -- 0.2s pages at
    48 in flight would be 240 req/s -- so this caps both:

    - at most `max_in_flight` requests at once, started at most `rate` per second;
    - a throttle pauses every request for `penalty_s` (retrying inside the block
      only prolongs it), then resumes at half the rate; the ceiling for later
      increases drops to 80% of the rate that tripped it;
    - each `step_s` without a throttle adds `step` req/s, up to the ceiling.

    `throttled()` is the client's `on_throttle` hook: it registers the block
    and returns once the caller may retry, paced like any other request.
    """

    def __init__(
        self,
        max_in_flight: int,
        rate: float,
        *,
        min_rate: float = 0.2,
        penalty_s: float = 120.0,
        step: float = 0.5,
        step_s: float = 60.0,
    ) -> None:
        self.max_in_flight = max(1, max_in_flight)
        self.rate = self.ceiling = float(rate)
        self.min_rate = min_rate
        self.penalty_s = penalty_s
        self.step = step
        self.step_s = step_s
        self.throttles = 0  # blocks seen; doubles as the pause epoch
        self._in_flight = 0
        self._next_slot = 0.0
        self._paused_until = 0.0
        self._last_change = time.monotonic()
        self._cv = threading.Condition()

    def __enter__(self) -> "RequestGate":
        with self._cv:
            while self._in_flight >= self.max_in_flight:
                self._cv.wait()
            self._in_flight += 1
        try:
            self._wait_for_slot()
        except BaseException:
            self.__exit__(None)
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        with self._cv:
            self._in_flight -= 1
            self._cv.notify()

    def _wait_for_slot(self) -> None:
        while True:
            with self._cv:
                now = time.monotonic()
                if self.rate < self.ceiling and now - self._last_change >= self.step_s:
                    self.rate = min(self.ceiling, self.rate + self.step)
                    self._last_change = now
                slot = max(now, self._next_slot, self._paused_until)
                self._next_slot = slot + 1.0 / self.rate
                epoch = self.throttles
            while self.throttles == epoch and (wait := slot - time.monotonic()) > 0:
                time.sleep(min(wait, 0.5))
            if self.throttles == epoch:
                return
            # A block started while we waited: the slot is void, queue behind the pause.

    def throttled(self) -> None:
        with self._cv:
            now = time.monotonic()
            if now >= self._paused_until:  # the first report of this block
                self.throttles += 1
                self.ceiling = max(self.min_rate, min(self.ceiling, self.rate * 0.8))
                self.rate = max(self.min_rate, self.rate / 2)
                self._paused_until = self._next_slot = self._last_change = now + self.penalty_s
                logger.warning(
                    "throttled (block #%d): pausing all requests %.0fs, then %.2f req/s (ceiling %.2f)",
                    self.throttles, self.penalty_s, self.rate, self.ceiling,
                )
        self._wait_for_slot()


# --------------------------------------------------------------------------
# Units, messages, scheduling
# --------------------------------------------------------------------------


@dataclass
class Unit:
    table: str
    kind: str  # "crawl" | "replace" | "incremental"
    lo: int = 0  # chunk identity: the exclusive lower bound it was planned (or split) at
    hi: int | None = None  # inclusive upper bound; None = open-ended tail
    cursor: int = 0  # last Id fetched; where the chunk resumes
    watermark: str | None = None  # incremental only


# Worker -> writer messages. A worker puts _Split before pushing the split-off
# unit to the scheduler, so the writer always sees it before that unit's pages.
@dataclass(frozen=True)
class _Page:
    table: str
    lo: int
    rows: list
    cursor: object  # Id for crawl/replace, LastUpdatedDate for incremental


@dataclass(frozen=True)
class _Split:
    table: str
    lo: int
    mid: int
    hi: int


@dataclass(frozen=True)
class _Done:
    table: str
    lo: int
    completed: bool  # False: skipped because its table had already failed


@dataclass(frozen=True)
class _Failed:
    table: str
    lo: int
    error: BaseException


_WORKER_EXIT = object()


class _Scheduler:
    """Work queue that knows when workers are idle, so busy ones can split."""

    def __init__(self, units: list[Unit]) -> None:
        self._units = deque(units)
        self._busy = 0
        self._idle = 0
        self._stopped = False
        self.failed_tables: set[str] = set()
        self._cv = threading.Condition()

    def get(self) -> Unit | None:
        with self._cv:
            while True:
                if self._stopped:
                    return None
                if self._units:
                    self._busy += 1
                    return self._units.popleft()
                if self._busy == 0:  # nothing queued and nothing running that could split
                    self._cv.notify_all()
                    return None
                self._idle += 1
                self._cv.wait()
                self._idle -= 1

    def task_done(self) -> None:
        with self._cv:
            self._busy -= 1
            self._cv.notify_all()

    def wants_split(self) -> bool:
        with self._cv:
            return self._idle > 0 and not self._units

    def push(self, unit: Unit) -> None:
        with self._cv:
            self._units.append(unit)
            self._cv.notify()

    def fail_table(self, table: str) -> None:
        with self._cv:
            self.failed_tables.add(table)

    def stop(self) -> None:
        with self._cv:
            self._stopped = True
            self._cv.notify_all()

    @property
    def queued(self) -> int:
        return len(self._units)


# --------------------------------------------------------------------------
# Fetching (worker threads)
# --------------------------------------------------------------------------


def _fetch_range(client, gate, sched: _Scheduler, unit: Unit, out: queue.Queue, stop: threading.Event) -> None:
    cursor = unit.cursor
    while not stop.is_set():
        flt = f"Id gt {cursor}" if unit.hi is None else f"Id gt {cursor} and Id le {unit.hi}"
        with gate:
            rows = client.get_entities(unit.table, filter=flt, orderby="Id", top=PAGE_SIZE)["value"]
        if rows:
            cursor = rows[-1]["Id"]
        out.put(_Page(unit.table, unit.lo, rows, cursor))
        if len(rows) < PAGE_SIZE:
            out.put(_Done(unit.table, unit.lo, completed=True))
            return
        if unit.hi is not None and unit.hi - cursor >= MIN_SPLIT_WIDTH and sched.wants_split():
            mid = cursor + (unit.hi - cursor) // 2
            out.put(_Split(unit.table, unit.lo, mid, unit.hi))
            sched.push(Unit(unit.table, unit.kind, lo=mid, hi=unit.hi, cursor=mid))
            unit.hi = mid


def _fetch_incremental(client, gate, unit: Unit, out: queue.Queue, stop: threading.Event) -> None:
    lud, last_id = unit.watermark, None
    while not stop.is_set():
        if last_id is None:
            flt = f"LastUpdatedDate ge {lud}"
        else:
            flt = f"LastUpdatedDate gt {lud} or (LastUpdatedDate eq {lud} and Id gt {last_id})"
        with gate:
            rows = client.get_entities(unit.table, filter=flt, orderby="LastUpdatedDate,Id", top=PAGE_SIZE)["value"]
        if rows:
            lud, last_id = rows[-1]["LastUpdatedDate"], rows[-1]["Id"]
        out.put(_Page(unit.table, unit.lo, rows, lud))
        if len(rows) < PAGE_SIZE:
            out.put(_Done(unit.table, unit.lo, completed=True))
            return


def _worker(client, gate, sched: _Scheduler, out: queue.Queue, stop: threading.Event) -> None:
    try:
        while (unit := sched.get()) is not None:
            try:
                if unit.table in sched.failed_tables:
                    out.put(_Done(unit.table, unit.lo, completed=False))
                elif unit.kind == "incremental":
                    _fetch_incremental(client, gate, unit, out, stop)
                else:
                    _fetch_range(client, gate, sched, unit, out, stop)
            except Exception as exc:
                out.put(_Failed(unit.table, unit.lo, exc))
            finally:
                sched.task_done()
    finally:
        out.put(_WORKER_EXIT)


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------


@dataclass
class _Plan:
    kind: str
    units: list[Unit]
    watermark: str | None = None
    live_count: int | None = None
    max_id: int | None = None
    resumed: bool = False


def _newest(client, gate, table: str, column: str):
    with gate:
        rows = client.get_entities(table, orderby=f"{column} desc", top=1, select=column)["value"]
    return rows[0][column] if rows else None


def _chunks(table: str, kind: str, max_id: int | None, live_count: int) -> list[Unit]:
    if not max_id:
        return [Unit(table, kind, lo=0, hi=None, cursor=0)]
    n = max(1, math.ceil(live_count / (PAGE_SIZE * PAGES_PER_CHUNK)))
    width = max(1, math.ceil(max_id / n))
    bounds = list(range(0, max_id, width)) + [max_id]
    units = [Unit(table, kind, lo=a, hi=b, cursor=a) for a, b in zip(bounds, bounds[1:])]
    units.append(Unit(table, kind, lo=max_id, hi=None, cursor=max_id))  # rows added mid-crawl
    return units


def _plan(client, gate, entity: EntityDef, st: state_mod.SyncState, resume: list[tuple]) -> _Plan:
    """Network only (runs on the pool); the writer persists the result."""
    name = entity.entity_set
    if entity.has_last_updated and st.full_sync_complete:
        return _Plan("incremental", [Unit(name, "incremental", watermark=st.max_last_updated_seen or EPOCH_WATERMARK)])
    kind = "crawl" if entity.has_last_updated else "replace"
    if kind == "crawl" and resume:
        units = [Unit(name, kind, lo=lo, hi=hi, cursor=cur) for lo, hi, cur in resume]
        return _Plan(kind, units, watermark=st.max_last_updated_seen, resumed=True)
    # A partial crawl left by the old sequential engine (last_id_seen > 0, no
    # chunks) restarts from 0: it has no plan-time watermark to resume against.
    with gate:
        live_count = client.get_count(name)
    max_id = _newest(client, gate, name, "Id")
    watermark = (_newest(client, gate, name, "LastUpdatedDate") or EPOCH_WATERMARK) if kind == "crawl" else None
    return _Plan(kind, _chunks(name, kind, max_id, live_count), watermark, live_count, max_id)


def _persist_plan(conn: sqlite3.Connection, entity: EntityDef, plan: _Plan) -> None:
    name = entity.entity_set
    if plan.kind == "replace":
        conn.execute(f'DROP TABLE IF EXISTS "{STAGING_PREFIX}{name}"')
        conn.execute(ddl.build_create_table(dataclasses.replace(entity, entity_set=STAGING_PREFIX + name)))
    elif plan.kind == "crawl" and not plan.resumed:
        conn.execute("DELETE FROM _sync_chunks WHERE table_name = ?", (name,))
        conn.executemany(
            "INSERT INTO _sync_chunks (table_name, lo, hi, cursor) VALUES (?, ?, ?, ?)",
            [(name, u.lo, u.hi, u.cursor) for u in plan.units],
        )
        st = state_mod.get_state(conn, name)
        state_mod.save_state(
            conn,
            dataclasses.replace(st, last_id_seen=0, max_last_updated_seen=plan.watermark, full_sync_complete=False),
        )


# --------------------------------------------------------------------------
# Writing (calling thread)
# --------------------------------------------------------------------------


@dataclass
class _TableRun:
    entity: EntityDef
    plan: _Plan
    pending: int  # units not yet over (done, skipped or failed)
    rows: int = 0
    units_run: int = 0
    max_id: int = 0
    watermark: str | None = None
    failed: bool = False
    finished: bool = False
    started: float | None = None  # when its first page arrived

    @property
    def staging(self) -> EntityDef:
        return dataclasses.replace(self.entity, entity_set=STAGING_PREFIX + self.entity.entity_set)


class _Writer:
    def __init__(self, conn: sqlite3.Connection, sched: _Scheduler, runs: dict[str, _TableRun]) -> None:
        self.conn = conn
        self.sched = sched
        self.runs = runs
        self.pages = 0
        self.rows = 0
        self.succeeded: list[dict] = []
        self.failed: list[str] = []

    def handle(self, msg) -> None:
        tr = self.runs[msg.table]
        if tr.started is None:
            tr.started = time.monotonic()
        if isinstance(msg, _Page):
            self._page(tr, msg)
        elif isinstance(msg, _Split):
            tr.pending += 1
            if tr.plan.kind == "crawl":
                self.conn.execute(
                    "UPDATE _sync_chunks SET hi = ? WHERE table_name = ? AND lo = ?", (msg.mid, msg.table, msg.lo)
                )
                self.conn.execute(
                    "INSERT INTO _sync_chunks (table_name, lo, hi, cursor) VALUES (?, ?, ?, ?)",
                    (msg.table, msg.mid, msg.hi, msg.mid),
                )
        elif isinstance(msg, _Done):
            if msg.completed:
                tr.units_run += 1
                if tr.plan.kind == "crawl":
                    self.conn.execute("DELETE FROM _sync_chunks WHERE table_name = ? AND lo = ?", (msg.table, msg.lo))
            self._unit_over(tr)
        elif isinstance(msg, _Failed):
            if not tr.failed:
                tr.failed = True
                self.sched.fail_table(msg.table)
                logger.error(
                    "FAILED syncing %s -- skipping, continuing with remaining tables", msg.table, exc_info=msg.error
                )
            self._unit_over(tr)

    def _page(self, tr: _TableRun, msg: _Page) -> None:
        self.pages += 1
        kind = tr.plan.kind
        if kind == "replace":
            n = 0 if tr.failed else _upsert_rows(self.conn, tr.staging, msg.rows)
        else:
            n = _upsert_rows(self.conn, tr.entity, msg.rows)
        if kind == "crawl":
            self.conn.execute(
                "UPDATE _sync_chunks SET cursor = ? WHERE table_name = ? AND lo = ?", (msg.cursor, msg.table, msg.lo)
            )
        if msg.rows:
            if kind == "incremental":
                tr.watermark = msg.cursor
            else:
                tr.max_id = max(tr.max_id, msg.cursor)
        tr.rows += n
        self.rows += n

    def _unit_over(self, tr: _TableRun) -> None:
        tr.pending -= 1
        if tr.pending == 0:
            self._finish(tr)

    def _finish(self, tr: _TableRun) -> None:
        tr.finished = True
        name = tr.entity.entity_set
        if tr.failed:
            if tr.plan.kind == "replace":
                self.conn.execute(f'DROP TABLE IF EXISTS "{tr.staging.entity_set}"')
            self.conn.commit()
            self.failed.append(name)
            return
        now = datetime.now(timezone.utc).isoformat()
        st = state_mod.get_state(self.conn, name)
        if tr.plan.kind == "replace":
            cols = ", ".join(f'"{c}"' for c in tr.entity.column_names)
            self.conn.execute(f'DELETE FROM "{name}"')
            self.conn.execute(f'INSERT INTO "{name}" ({cols}) SELECT {cols} FROM "{tr.staging.entity_set}"')
            self.conn.execute(f'DROP TABLE "{tr.staging.entity_set}"')
            st = dataclasses.replace(st, full_sync_complete=True, last_synced_at=now, rows_synced=self._count(name))
        elif tr.plan.kind == "crawl":
            st = dataclasses.replace(
                st,
                last_id_seen=max(st.last_id_seen, tr.max_id),
                max_last_updated_seen=tr.plan.watermark,
                full_sync_complete=True,
                last_synced_at=now,
                rows_synced=self._count(name),
            )
        else:
            st = dataclasses.replace(
                st,
                max_last_updated_seen=tr.watermark or st.max_last_updated_seen,
                last_synced_at=now,
                rows_synced=st.rows_synced + tr.rows,
            )
        state_mod.save_state(self.conn, st)  # commits -- atomically with a replace's swap
        chunks = f", {tr.units_run} chunks" if tr.plan.kind != "incremental" else ""
        elapsed = format_duration(time.monotonic() - tr.started)
        logger.info("=== %s: %s done, %d rows%s, %s ===", name, tr.plan.kind, tr.rows, chunks, elapsed)
        self.succeeded.append({"table": name, "mode": MODE_NAMES[tr.plan.kind], "rows": tr.rows, "chunks": tr.units_run})

    def _count(self, table: str) -> int:
        return self.conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]

    def progress(self, gate: RequestGate, elapsed: float) -> None:
        done = sum(1 for tr in self.runs.values() if tr.finished)
        biggest_open = sorted(
            (tr for tr in self.runs.values() if not tr.finished and tr.plan.live_count),
            key=lambda tr: -tr.plan.live_count,
        )[:3]
        detail = ", ".join(f"{tr.entity.entity_set} {tr.rows}/{tr.plan.live_count}" for tr in biggest_open)
        logger.info(
            "progress: %d pages (%.1f/s), %d rows, rate %.2f req/s (ceiling %.2f, %d blocks), "
            "%d units queued, %d/%d tables done%s",
            self.pages, self.pages / max(elapsed, 1e-9), self.rows, gate.rate, gate.ceiling, gate.throttles,
            self.sched.queued, done, len(self.runs), f" | {detail}" if detail else "",
        )


def sync_tables(
    client: ODataClient,
    conn: sqlite3.Connection,
    entities: list[EntityDef],
    *,
    workers: int = 1,
    gate: RequestGate | None = None,
    max_runtime_s: float | None = None,
) -> tuple[list[dict], list[str]]:
    """Sync `entities` with `workers` concurrent fetchers. Returns
    (succeeded results, failed table names); a failed table never aborts the run.

    Pass the gate the client reports throttles to (ODataClient's `on_throttle`)
    so the request rate adapts; without one, requests aren't paced at all.

    `max_runtime_s` stops fetching once the budget is spent, for jobs with a
    time cap (a full rebuild outlasts a GitHub-hosted runner's 6h): finished
    pages and their chunk cursors are committed, so the next run resumes from
    them. Overshoot is bounded by the page in flight -- a worker parked in a
    firewall block only notices when the block lifts.
    """
    workers = max(1, workers)
    gate = gate or RequestGate(workers, rate=math.inf)
    state_mod.ensure_state_table(conn)
    states = {e.entity_set: state_mod.get_state(conn, e.entity_set) for e in entities}
    resume = {
        e.entity_set: conn.execute(
            "SELECT lo, hi, cursor FROM _sync_chunks WHERE table_name = ? ORDER BY lo", (e.entity_set,)
        ).fetchall()
        for e in entities
    }

    started = time.monotonic()
    failed_planning: list[str] = []
    plans: dict[str, _Plan] = {}

    def plan_one(entity: EntityDef):
        try:
            return entity, _plan(client, gate, entity, states[entity.entity_set], resume[entity.entity_set]), None
        except Exception as exc:
            return entity, None, exc

    with ThreadPoolExecutor(workers) as pool:
        for entity, plan, exc in pool.map(plan_one, entities):
            if exc is not None:
                logger.error("FAILED planning %s -- skipping", entity.entity_set, exc_info=exc)
                failed_planning.append(entity.entity_set)
                continue
            plans[entity.entity_set] = plan
            _persist_plan(conn, entity, plan)
            if plan.kind != "incremental":
                logger.info(
                    "=== %s: %s %s: %d chunks (live_count=%s, max Id=%s) ===",
                    entity.entity_set, plan.kind, "resumed" if plan.resumed else "planned",
                    len(plan.units), plan.live_count, plan.max_id,
                )
    conn.commit()
    logger.info("planned %d table(s) in %s", len(plans), format_duration(time.monotonic() - started))

    by_name = {e.entity_set: e for e in entities}
    runs = {name: _TableRun(by_name[name], plan, pending=len(plan.units)) for name, plan in plans.items()}
    # Biggest tables first so the long crawls don't start last; incrementals are tiny.
    order = sorted(plans, key=lambda n: (plans[n].kind == "incremental", -(plans[n].live_count or 0), n))
    sched = _Scheduler([u for name in order for u in plans[name].units])
    writer = _Writer(conn, sched, runs)
    out: queue.Queue = queue.Queue(maxsize=workers * 4)
    stop = threading.Event()
    threads = [
        threading.Thread(target=_worker, args=(client, gate, sched, out, stop), name=f"fetch-{i}", daemon=True)
        for i in range(workers)
    ]
    for t in threads:
        t.start()

    exits = 0
    stopping = False
    deadline = started + max_runtime_s if max_runtime_s else None
    last_commit = last_progress = time.monotonic()
    try:
        while exits < workers:
            try:
                msg = out.get(timeout=0.5)
            except queue.Empty:
                msg = None
            if msg is _WORKER_EXIT:
                exits += 1
            elif msg is not None:
                writer.handle(msg)
            now = time.monotonic()
            if deadline and not stopping and now >= deadline:
                stopping = True
                logger.warning(
                    "runtime budget spent after %s -- stopping; %d units still queued, "
                    "resume from the chunk checkpoints",
                    format_duration(now - started), sched.queued,
                )
                stop.set()
                sched.stop()
            if now - last_commit >= COMMIT_EVERY_S:
                conn.commit()
                last_commit = now
            if now - last_progress >= PROGRESS_EVERY_S:
                writer.progress(gate, now - started)
                last_progress = now
    finally:
        if exits < workers:  # interrupted: stop fetching and unblock workers stuck on a full queue
            stop.set()
            sched.stop()
            deadline = time.monotonic() + 10
            while exits < workers and time.monotonic() < deadline:
                try:
                    if out.get(timeout=0.5) is _WORKER_EXIT:
                        exits += 1
                except queue.Empty:
                    pass
        conn.commit()

    writer.progress(gate, time.monotonic() - started)
    return writer.succeeded, failed_planning + writer.failed
