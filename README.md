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

# Run the MCP server (stdio transport)
python -m knesset_utils.server.mcp_server
```

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
- No delete detection: the OData API exposes no delete feed, so the mirror can only grow/update,
  not detect rows removed upstream. Out of scope for now.
- A full initial crawl of the largest table (`KNS_PlenumVoteResult`, ~1.95M rows) takes on the
  order of hours, not minutes. This is meant to be run as an infrequent, separate seed-build step
  (not part of every deploy) — the deployment story for shipping/refreshing that seed artifact is
  still an open design question.
