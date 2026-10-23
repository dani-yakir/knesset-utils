# knesset-utils

Mirrors the Israeli Knesset's v4 OData API (`https://knesset.gov.il/OdataV4/ParliamentInfo/`)
into a SQLite database and serves it to LLM agents through an MCP server.

The live API returns at most 100 rows per request and is slow to filter or count, so the
mirror is crawled once a day and every query runs against the local copy.

## How it fits together

```
homelab K3S CronJob (daily, Israeli IP)          GitHub Releases              Render
  deploy/regenerate.sh                     -->   `latest` release      -->   MCP server
  crawl all 45 tables from scratch               knesset_mirror.sqlite.zst   (downloads on boot
  publish only when complete                     + dated `mirror-YYYY-MM-DD`  and every 6h)
```

- **Regeneration** runs on Alon's homelab K3S cluster, because the Knesset API only answers
  Israeli IPs. Each cycle rebuilds the whole mirror from an empty database, about 34k requests
  or ~2h. It publishes only once `knesset-utils status --check-complete` passes, so the server
  never gets a partial crawl.
- **Serving** is a Docker web service on Render (`render.yaml`). It downloads the `latest`
  release asset to its persistent disk, swaps it in atomically, and re-checks every 6h.
  `GET /healthz` reports which mirror it is serving (`last_synced_at`).
- **Clients** (Claude Code, n8n, ...) connect over Streamable HTTP with a bearer token.

## MCP tools

| Tool | What it does |
|---|---|
| `list_tables` | every table with its purpose and row count |
| `describe_table` | columns, foreign keys, value distributions; decodes code columns to Hebrew labels |
| `query_sql` | read-only SQL against the mirror |
| `find_person` | name → `KNS_Person.Id` |
| `sync_status` | per-table crawl state and `last_synced_at` |
| `mk_vote_agreement` | how often two MKs voted the same way |
| `vote_result_breakdown` | for/against/abstain/... tally for one plenum vote |
| `get_vote_official_link` / `get_bill_official_link` / `get_law_official_link` | official knesset.gov.il URLs for citing |

The server's own instructions (`server/guide.py`) explain the data model to the agent.

## Connecting a client

**Deployed server.** The committed `.mcp.json` points at `https://knesset-mcp.onrender.com/mcp`
and reads the token from `$KNESSET_MCP_TOKEN`:

```
export KNESSET_MCP_TOKEN=<MCP_AUTH_TOKEN from Render>   # PowerShell: $env:KNESSET_MCP_TOKEN="..."
```

For other clients, send `Authorization: Bearer <token>` to the same URL:

```
claude mcp add --transport http knesset https://knesset-mcp.onrender.com/mcp \
  --header "Authorization: Bearer $KNESSET_MCP_TOKEN"
```

**Local server** (for working on the tools), over stdio against `data/knesset_mirror.sqlite`:

```
claude mcp add --scope local knesset-local -- .venv/Scripts/python -m knesset_utils.server.mcp_server
```

Use `.venv/bin/python` on macOS/Linux. Local scope keeps it from clashing with the committed entry.

## Local setup and CLI

```
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"          # add ,server for the HTTP transport and mirror download
```

Crawling needs an Israeli IP.

```
knesset-utils seed                          # schema-refresh + sync everything into data/knesset_mirror.sqlite
knesset-utils sync --table KNS_Faction      # sync specific tables (omit --table for all)
knesset-utils sync --workers 8 --max-rate 6 # concurrency and request-rate cap (defaults 4 / 3 req/s)
knesset-utils status [--check-complete]     # per-table crawl state
knesset-utils validate-fks                  # foreign-key integrity report (docs/fk_integrity.md)
knesset-utils schema-refresh                # re-read live $metadata into schema/snapshot.json
python -m knesset_utils.server.mcp_server   # MCP server; stdio unless MCP_TRANSPORT is set
```

`sync` resumes from the checkpoints stored in the database (`_sync_state`, `_sync_chunks`).
On a table whose first crawl is done it fetches only rows with a newer `LastUpdatedDate`.
That misses some upstream changes (see [Data caveats](#data-caveats)), which is why the
deployed mirror is always rebuilt from scratch.

## Deployment

### Regeneration (homelab cluster)

- `deploy/k8s/regenerate-cronjob.yaml`: a CronJob in namespace `dani`, daily at 04:17
  Asia/Jerusalem. A run missed while the cluster was off starts once it is back, within 12h.
  The manifest header has the setup and manual-run commands.
- `deploy/regenerate.sh`: one cycle. It crawls with a 300-minute budget. If the rebuild is
  complete, it compresses it, uploads it to `latest` and `mirror-<date>`, and deletes the
  database so the next cycle starts empty. Otherwise the partial database stays on the
  `knesset-regenerate-work` PVC and the next run continues it.
- The image (`deploy/Dockerfile.regenerate`) is built by `.github/workflows/regenerate-image.yml`
  and pushed to `ghcr.io/dani-yakir/knesset-utils-regenerate:main` on merges to `main` that
  touch `src/`, `pyproject.toml`, `deploy/regenerate.sh` or `deploy/Dockerfile.regenerate`.
  The CronJob pulls `:main` on every run.
- Publishing uses a fine-grained GitHub token (Contents: read & write) from the
  `knesset-regen-github` Secret. An optional `RENDER_DEPLOY_HOOK` in the same Secret makes
  Render pick up the new mirror immediately instead of on its next 6h check.
- `.github/workflows/regenerate.yml` runs the same script on a GitHub runner, manually only.
  It is currently useless because of the geo-block. Since a runner's disk does not persist,
  it parks partial rebuilds in a `rebuild-wip` prerelease.

### Server (Render)

`Dockerfile` + `render.yaml`: starter plan, Frankfurt, a 2 GB disk at `/data`. A refresh needs
room for the old DB and the new one (~800 MB each). Temp files left by an interrupted download
are deleted before the next one starts. Configuration is read from the environment
(`server/config.py`):

| Var | Default | Purpose |
|---|---|---|
| `MCP_TRANSPORT` | `stdio` | `stdio` or `streamable-http` |
| `MCP_HOST` / `PORT` / `MCP_PORT` | `0.0.0.0` (HTTP) / – / `8000` | bind address; `PORT` (Render) wins over `MCP_PORT` |
| `MCP_DB_PATH` | `data/knesset_mirror.sqlite` | mirror location (`/data/...` in the image) |
| `MCP_AUTH_TOKEN` | – | bearer token required on `/mcp`; `/healthz` stays open |
| `MCP_NATIVE_AUTH` / `MCP_PUBLIC_URL` | off / – | opt-in OAuth-style auth path instead of the static bearer |
| `MIRROR_REPO` | – | `owner/repo` holding the releases; unset disables downloading |
| `MIRROR_RELEASE_TAG` / `MIRROR_ASSET` | `latest` / `knesset_mirror.sqlite.zst` | which release asset to serve |
| `MIRROR_ZSTD_LONG` | `0` | must equal `ZSTD_LONG` in the CronJob (both `27`) |
| `MIRROR_REFRESH_INTERVAL_SECONDS` | `0` (off) | background re-check cadence (`21600` on Render) |
| `MIRROR_DOWNLOAD_ON_BOOT` | on | fetch the mirror at startup |
| `GITHUB_TOKEN` / `GH_TOKEN` | – | only needed if the mirror repo is private |

### Schema snapshot

Table and column definitions come from the committed `src/knesset_utils/schema/snapshot.json`.
They are used to create the tables, to pick which fields are stored, and for the server's
foreign-key hints. Nothing updates the snapshot automatically: the regeneration job never
runs `schema-refresh`, so a new upstream column or table is ignored until someone runs
`knesset-utils schema-refresh` locally and merges the change. That rebuilds the regeneration
image, and the next rebuild that starts from an empty database picks the change up.

## Data caveats

- **`LastUpdatedDate` is not a reliable change signal.** Upstream fixes have rewritten hundreds
  of thousands of rows without touching it. `KNS_PlmSessionItem.Id` is a row number that gets
  renumbered, and deletions are never exposed. Only a full re-crawl sees these changes.
- **Foreign keys are unreliable in the source data.** For example, 63% of `KNS_DocumentAgenda`
  rows point at an `AgendaID` missing from `KNS_Agenda`. Some apparent orphans are sentinels
  (`KNS_Bill.CommitteeID = -1` means unassigned). Prefer `LEFT JOIN` when counting. Details are
  in [`docs/fk_integrity.md`](docs/fk_integrity.md).
- **`$count=true` cannot be trusted.** It can disagree with a real crawl at the same moment,
  so completeness is checked by crawling, never by `$count`.
- **Three entity sets are excluded.** `KNS_DocumentQuerie`, `V_Lobbyists` and
  `V_LobbyistsClients` are listed in `$metadata` but return 404 on every request.

## Crawl engine

Every entity set has a numeric `Id` key, so `db/sync.py` uses keyset pagination
(`Id gt {cursor}`, 100 rows per page) rather than `$skip`, which slows badly with depth. Each
table's Id range is split into chunks that a thread pool crawls, with idle workers taking over
half of a busy worker's remaining range and a single SQLite writer.

The Knesset firewall limits the crawl rate, not the server. It answers HTTP 481 "access denied"
once an IP exceeds roughly 1,000 requests in 2 minutes, then blocks it for up to ~2 minutes.
Requests are therefore capped by rate (`--max-rate`). On a block, every worker pauses 120s,
the rate halves, and later increases stop at 80% of the rate that tripped the block. The
cluster runs at 5 req/s.

## MCP ergonomics eval

`eval/` measures how easily a fresh LLM agent answers real questions through the MCP tools.
`eval/questions.json` has 22 questions with gold answers computed against the frozen
`data/scratch-2026-09-11.sqlite`, and `eval/split.json` is a seeded train/test split. Each run
is a headless `claude -p` agent with only this MCP server attached (stdio, from a chosen source
tree), so two server versions can be A/B tested on identical data. A blind LLM judge grades
the answers.

```
python eval/run_eval.py run --label A-train-1 --src <baseline-worktree>/src --split train
python eval/run_eval.py run --label B-train-1 --split train          # working tree
python eval/run_eval.py grade eval/runs/A-train-1 eval/runs/B-train-1
python eval/run_eval.py report eval/runs/*-train-*
```

Tune tools on `train`; use `test` only to confirm a change generalizes.
