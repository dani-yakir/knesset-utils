# knesset-utils

Mirrors the Israeli Knesset's v4 OData API (`https://knesset.gov.il/OdataV4/ParliamentInfo/`)
into a local SQLite database, and serves statistical analyses over that mirror via an MCP server.

The live OData API is row-capped (100/page) and slow for ad-hoc filtering/counting on large
tables, so this project does the expensive work once (a sync) and serves everything else from
the local SQLite copy.

## Setup

```
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
```

## Usage

```
# Fetch live $metadata and write/refresh the committed schema snapshot
knesset-utils schema-refresh

# Sync one or more tables (omit --table to sync everything in the snapshot)
knesset-utils sync --table KNS_Faction --table KNS_Committee

# Check per-table sync freshness
knesset-utils status

# More concurrency. The Knesset firewall, not the server, sets the pace: it blocks an IP
# for ~2 min past roughly 1,000 requests per 2 min, so --max-rate caps requests/s and is
# halved automatically on each block. Defaults: 4 workers, 3 req/s.
knesset-utils sync --workers 32 --max-rate 6

# Build a full seed mirror from scratch (schema-refresh + sync everything).
# ~1.5h on an empty database (~34k requests at ~6 req/s; 14h when it ran sequentially);
# re-running against an existing mirror is an incremental catch-up instead.
knesset-utils seed

# Check foreign-key integrity in the local mirror (see docs/fk_integrity.md)
knesset-utils validate-fks

# Run the MCP server (stdio transport)
python -m knesset_utils.server.mcp_server
```

## Deployment

The MCP server is deployed as a Docker web service on **Render** (`render.yaml`); the mirror is
**rebuilt from scratch** by a scheduled **GitHub Actions** workflow
(`.github/workflows/regenerate.yml`), not a long-running server. The two are decoupled through
**GitHub Releases**: a moving `latest` release holds the current `knesset_mirror.sqlite.zst`,
and the server downloads it on boot and re-checks every 6h. Because `db/sync.py` checkpoints
progress into the `_sync_state` and `_sync_chunks` tables *inside* the sqlite file, the release
asset is simultaneously the distributed artifact and the crawl state store.

There is no incremental refresh: `LastUpdatedDate` does not reliably change when the data does
(see Design notes), so each cycle re-crawls everything. A rebuild is ~34k requests and the
Knesset firewall paces it to hours, which is longer than a GitHub-hosted job may run, so the
crawl takes a `--max-runtime` budget and the partial mirror is parked in a `rebuild-wip`
prerelease that the next run continues. `latest` only moves when a rebuild is **complete**
(`knesset-utils status --check-complete`), so what the server serves is never a half-crawl.

### Server configuration (env vars)

The server reads everything from the environment (see `server/config.py`). With no env set it
behaves exactly as before: stdio, `data/knesset_mirror.sqlite`, no auth.

| Var | Purpose |
|---|---|
| `MCP_TRANSPORT` | `stdio` (default) or `streamable-http` |
| `MCP_HOST` / `PORT` | HTTP bind; `PORT` (Render's convention) wins over `MCP_PORT` |
| `MCP_DB_PATH` | mirror location (`/data/knesset_mirror.sqlite` in the container) |
| `MCP_AUTH_TOKEN` | shared secret required as `Authorization: Bearer <token>` on `/mcp` |
| `MCP_PUBLIC_URL` | public base URL (used by `MCP_NATIVE_AUTH`, the opt-in OAuth-style path) |
| `MIRROR_REPO` | `owner/repo` holding the mirror releases; unset disables all fetching |
| `MIRROR_ZSTD_LONG` | must equal `zstd --long=NN` in `regenerate.yml` (currently `27`) |
| `MIRROR_REFRESH_INTERVAL_SECONDS` | background re-check cadence; `0` disables |

`GET /healthz` is unauthenticated and reports `db_exists` + `last_synced_at`.

### Connecting a client

**Deployed server (default).** The committed `.mcp.json` points at the Render deployment over
Streamable HTTP and reads the bearer token from `$KNESSET_MCP_TOKEN` (so no secret is
committed). Export it before launching Claude Code:

```
export KNESSET_MCP_TOKEN=<the MCP_AUTH_TOKEN value from Render>   # PowerShell: $env:KNESSET_MCP_TOKEN=...
```

Or register it explicitly for any MCP client:

```
claude mcp add --transport http knesset https://knesset-mcp.onrender.com/mcp \
  --header "Authorization: Bearer $KNESSET_MCP_TOKEN"
```

**Local server (for hacking on the tools).** Run it against your local mirror over stdio and
add it at local scope so it does not clash with the committed remote entry:

```
claude mcp add --scope local knesset-local -- \
  .venv/Scripts/python -m knesset_utils.server.mcp_server
```

(`.venv/bin/python` on macOS/Linux. A bare `python` picks up the system interpreter, which
does not have the package installed.)

### Seeding the first release (one-time, local)

The cloud never runs the full seed (~34k requests: ~1.5h from a home IP, and the firewall
holds GitHub's runner IPs to a lower rate). Build it locally, then publish the seed once:

```
knesset-utils seed                       # or reuse an existing data/knesset_mirror.sqlite
zstd -19 --long=27 -T0 data/knesset_mirror.sqlite -o knesset_mirror.sqlite.zst
gh release create latest        knesset_mirror.sqlite.zst --title "latest mirror"
gh release create mirror-$(date -u +%F) knesset_mirror.sqlite.zst --title "mirror seed"
```

### Schema drift is not handled in the cloud (by design)

`regenerate.yml` never runs `schema-refresh`, so new/renamed upstream columns are ignored until
someone runs `knesset-utils schema-refresh` locally and commits the updated
`src/knesset_utils/schema/snapshot.json`. Adding a table stays a deliberate PR, not something a
nightly run does silently -- but once merged, the next rebuild that starts from an empty
database picks it up on its own (a rebuild already in flight keeps the schema it started with).

## Design notes

- Every v4 entity set uses a single numeric `Id` key. Sync uses **keyset pagination**
  (`$filter=Id gt {cursor}&$orderby=Id&$top=100`, looping until a page returns fewer than 100
  rows) rather than `$skip`/nextLink-following, which was measured to degrade badly with depth
  on large tables (20-40s per page near the end of a 2M-row table, vs. 1-13s for keyset at
  equivalent depth). This also makes sync resumable: progress is checkpointed per page in
  `_sync_state`.
- Tables with a `LastUpdatedDate` column get an incremental sync after their first full crawl
  completes (`$filter=LastUpdatedDate gt {watermark}`). Tables without one (a handful of small
  lookup tables) are fully replaced on every sync run instead.
- Three entity sets listed in `$metadata`/the service document 404 on every request and are
  permanently excluded: `KNS_DocumentQuerie`, `V_Lobbyists`, `V_LobbyistsClients`.
- **No delete/repair detection**, confirmed empirically, not just theoretically. The OData API
  exposes no delete feed, so incremental sync (`LastUpdatedDate gt watermark`) only ever adds or
  updates rows — it structurally cannot notice a row that disappeared, whether that's a genuine
  upstream deletion or local corruption, because a vanished row was never going to show up as
  "updated." Tested directly: after the initial full mirror, we deleted one row from every table
  and re-ran `sync`. Result — the 4 tables with **no** `LastUpdatedDate` (`KNS_ItemType`,
  `KNS_CmtSiteCode`, `KNS_BroadcastCommitteSession`, `KNS_MkSiteCode`) self-healed, because those
  are fully replaced every run regardless. All other tables (the incremental ones) stayed short by
  exactly the deleted row, as expected. No reconciliation/full-reverify mode exists yet; this is
  accepted as a known limitation rather than solved, since a real fix (a periodic full re-crawl to
  detect drift) costs close to the original full-crawl time, dominated by `KNS_PlenumVoteResult`.
- **`$count=true` on this API cannot be trusted as a verification oracle.** While investigating the
  above, a few tables' row counts appeared to have dropped on their own between the initial sync
  and ~6-17 hours later, which first looked like real upstream deletions. It wasn't: a full
  row-by-row crawl (not `$count`) against three of those tables (`KNS_CmtSiteCode`, `KNS_PlenumVote`,
  `KNS_DocumentAgenda`) found **zero missing and zero extra Ids** versus the local mirror — the
  actual data matched exactly. Querying `$count=true` and doing a real paginated crawl *at the same
  instant* on `KNS_CmtSiteCode` confirmed it directly: `$count=true` returned 720 while the crawl
  found 718 distinct rows, simultaneously, reproducibly. Combined with `$count=true` being
  unusually slow (10-16s cold) from the very first exploration of this API, it's very likely served
  from a cached or estimated path that can disagree with the real data. Practical takeaway: never
  use `$count=true` to check mirror completeness or drift — only a full paginated crawl is
  trustworthy for that.
- A full initial crawl of the largest table (`KNS_PlenumVoteResult`, ~1.95M rows) takes on the
  order of hours, not minutes — confirmed: the first full mirror of all 45 tables took **14h25m**
  end-to-end (3,362,911 rows), with `KNS_PlenumVoteResult` alone accounting for 7h. A subsequent
  incremental resync of all 45 tables (nothing changed upstream) took **21m13s** — roughly 40x
  faster -- but see the next bullet for why incremental refresh was dropped anyway. `knesset-utils seed`
  runs the full pipeline (schema-refresh + sync everything) as one command — meant to be run as an
  infrequent, standalone job, not part of every deploy. The deployment story for shipping/refreshing
  that seed artifact itself is still an open design question.
- **The sync engine is parallel, and the Knesset firewall is what limits it.** The server caps
  every response at 100 rows (a bigger `$top` still returns 100 plus a `nextLink`), so a full crawl
  is ~34k requests no matter what, and nearly all the time is spent waiting on the server (0.2–2s
  per page vs ~18ms to write it). `db/sync.py` therefore splits each table's Id range into chunks
  crawled by a thread pool (keyset pagination per chunk; idle workers take half of a busy worker's
  remaining range, because Ids are heavily clustered in some tables), with one SQLite writer. The
  server itself keeps scaling to ~85 req/s at 32–48 concurrent requests, but its firewall answers
  HTTP **481 "access denied"** once an IP sends more than roughly 1,000 requests in ~2 minutes,
  then blocks it for up to ~2 minutes. That applies to home IPs too (measured: 8–10 req/s blocked
  after ~2 min, 6.4 req/s held steady), and GitHub's runner IPs get a lower budget. So requests are
  paced by rate, not just concurrency: on a block, every request pauses for 120s, the rate halves,
  and later increases stop at 80% of the rate that tripped it. With that, a from-scratch crawl of
  all 45 tables took **1h34m** (2026-09-11: 3,388,474 rows, 2 blocks, no failures) instead of 14h25m.
- **`LastUpdatedDate` is not a reliable change signal**, which is why the mirror is rebuilt
  rather than patched. Comparing a from-scratch crawl (2026-09-11) against the incrementally
  synced mirror published the same day: `KNS_PlenumVoteResult.MkId` differed on **631,655 rows**
  (32%) with no timestamp change -- the live API agrees with the fresh crawl -- so an upstream
  data fix was invisible to incremental sync. `KNS_PlmSessionItem.Id` turned out to be a dense
  `1..N` row number that is **renumbered** when upstream rows disappear (the row published as Id
  50558 is now 50518), which makes upsert-by-Id unsound for it. Rows deleted upstream never left
  the mirror (191 of them). And a handful of FK columns were re-pointed with no timestamp change
  (`KNS_DocumentAgenda`, `KNS_DocumentBill`, `KNS_Bill`). None of these can be detected by
  filtering on `LastUpdatedDate`; only a full re-crawl sees them.
- **Foreign keys are not reliable in the source data** — not a mirror bug, confirmed against the
  live API directly. E.g. `KNS_PlenumVoteResult.MkId` had no corresponding `KNS_Person` row for
  32.3% of vote rows (171 distinct MK ids) in the 2026-08 seed -- though that particular gap was
  since fixed upstream: the 2026-09-11 from-scratch crawl has **zero** MkId orphans. `KNS_DocumentAgenda.AgendaID` is worse — 62.7% of rows point at an
  `AgendaID` absent from `KNS_Agenda`. Some other FK-shaped columns showing "orphans" turned out to
  be sentinel values (`KNS_Bill.CommitteeID = -1` means "unassigned," not corruption) or an
  incomplete lookup table, not real dangling references. Full methodology, the complete per-FK
  breakdown, and which columns were deliberately left unvalidated (ambiguous/polymorphic-looking):
  see [`docs/fk_integrity.md`](docs/fk_integrity.md). Run `knesset-utils validate-fks` to
  reproduce. Nothing consumes or enforces this yet — it's a read-only report; anything doing a
  `JOIN` against the mirror (including the stats layer) needs to know an inner join can silently
  drop a meaningful fraction of rows.

## MCP ergonomics eval

`eval/` measures how easily a fresh LLM agent can answer real questions through the MCP tools.
`eval/questions.json` holds 22 questions (themes from public Knesset FAQs/statistics) with gold
answers computed against the frozen `data/scratch-2026-09-11.sqlite` snapshot; `eval/split.json`
is a seeded random train/test split. Each run is a headless `claude -p` agent with every
built-in tool disabled and only this MCP server attached (over stdio, from a chosen source tree),
so two server versions can be A/B tested on identical data. A blind LLM judge grades the answers.

```
python eval/run_eval.py run --label A-train-1 --src <baseline-worktree>/src --split train
python eval/run_eval.py run --label B-train-1 --split train          # working tree
python eval/run_eval.py grade eval/runs/A-train-1 eval/runs/B-train-1
python eval/run_eval.py report eval/runs/*-train-*
```

Tune tools on `train`; use `test` only to confirm a change generalizes.
