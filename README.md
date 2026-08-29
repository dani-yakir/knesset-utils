# knesset_utils

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

# Build a full seed mirror from scratch (schema-refresh + sync everything).
# ~14h on an empty database (dominated by KNS_PlenumVoteResult's 1.95M rows);
# re-running against an existing mirror is a cheap incremental catch-up instead.
knesset-utils seed

# Check foreign-key integrity in the local mirror (see docs/fk_integrity.md)
knesset-utils validate-fks

# Run the MCP server (stdio transport)
python -m knesset_utils.server.mcp_server
```

## Deployment

The MCP server is deployed as a Docker web service on **Render** (`render.yaml`); the mirror is
refreshed by a scheduled **GitHub Actions** workflow (`.github/workflows/sync.yml`), not a
long-running server. The two are decoupled through **GitHub Releases**: a moving `latest`
release holds the current `knesset_mirror.sqlite.zst`, and the server downloads it on boot and
re-checks every 6h. Because `db/sync.py` checkpoints progress into the `_sync_state` table
*inside* the sqlite file, the release asset is simultaneously the distributed artifact and the
sync state store.

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
| `MIRROR_ZSTD_LONG` | must equal `zstd --long=NN` in `sync.yml` (currently `27`) |
| `MIRROR_REFRESH_INTERVAL_SECONDS` | background re-check cadence; `0` disables |

`GET /healthz` is unauthenticated and reports `db_exists` + `last_synced_at`.

### Seeding the first release (one-time, local)

The cloud never runs the ~14h full seed. Build it locally, then publish the seed once:

```
knesset-utils seed                       # or reuse an existing data/knesset_mirror.sqlite
zstd -19 --long=27 -T0 data/knesset_mirror.sqlite -o knesset_mirror.sqlite.zst
gh release create latest        knesset_mirror.sqlite.zst --title "latest mirror"
gh release create mirror-$(date -u +%F) knesset_mirror.sqlite.zst --title "mirror seed"
```

### Schema drift is not handled in the cloud (by design)

`sync.yml` never runs `schema-refresh`, so new/renamed upstream columns are ignored until
someone runs `knesset-utils schema-refresh` locally, commits the updated
`src/knesset_utils/schema/snapshot.json`, **re-seeds locally**, and uploads a fresh `latest`.
Adding a table is therefore a deliberate PR + manual re-seed, not something a nightly run can
do silently.

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
  faster, confirming incremental sync is the right model for routine refreshes. `knesset-utils seed`
  runs the full pipeline (schema-refresh + sync everything) as one command — meant to be run as an
  infrequent, standalone job, not part of every deploy. The deployment story for shipping/refreshing
  that seed artifact itself is still an open design question.
- **Foreign keys are not reliable in the source data** — not a mirror bug, confirmed against the
  live API directly. E.g. `KNS_PlenumVoteResult.MkId` has no corresponding `KNS_Person` row for
  32.3% of vote rows (171 distinct MK ids), and the gap is current, not historical (orphaned votes
  run through 2026-07-28). `KNS_DocumentAgenda.AgendaID` is worse — 62.7% of rows point at an
  `AgendaID` absent from `KNS_Agenda`. Some other FK-shaped columns showing "orphans" turned out to
  be sentinel values (`KNS_Bill.CommitteeID = -1` means "unassigned," not corruption) or an
  incomplete lookup table, not real dangling references. Full methodology, the complete per-FK
  breakdown, and which columns were deliberately left unvalidated (ambiguous/polymorphic-looking):
  see [`docs/fk_integrity.md`](docs/fk_integrity.md). Run `knesset-utils validate-fks` to
  reproduce. Nothing consumes or enforces this yet — it's a read-only report; anything doing a
  `JOIN` against the mirror (including the stats layer) needs to know an inner join can silently
  drop a meaningful fraction of rows.
