#!/usr/bin/env bash
# One mirror-regeneration cycle: crawl, and once the rebuild is complete,
# publish it to the `latest` GitHub release that the MCP server serves.
#
# Used by the CronJob on the homelab cluster (deploy/k8s/regenerate-cronjob.yaml)
# and by the manual fallback workflow (.github/workflows/regenerate.yml).
#
# The mirror is always rebuilt from an empty database, never patched (see
# regenerate.yml for why). A rebuild may not finish in one run: every crawled
# chunk is checkpointed inside the sqlite file, so the next run continues it.
# Where that partial database lives between runs depends on the caller:
#   - cluster: WORK_DIR is a persistent volume; nothing else is needed.
#   - Actions: WIP_TAG names a prerelease the partial is parked in and
#     restored from, since a runner's disk does not survive the job.
# After a successful publish the database is deleted so the next run starts
# from scratch.
#
# Env:
#   GH_TOKEN, REPO (owner/name)  required
#   WORK_DIR=.                   where the db, asset and sync.log live
#   WIP_TAG                      park/restore partial rebuilds in this release (Actions only)
#   WORKERS=8 MAX_RATE=2 RUNTIME_MIN=0 (0 = run to completion)
#   ZSTD_LONG=27                 must equal MIRROR_ZSTD_LONG in render.yaml
#   ZSTD_THREADS=0               0 = one per core; cap it under a container memory limit
#   RENDER_DEPLOY_HOOK           optional; POSTed after a publish
set -euo pipefail

: "${GH_TOKEN:?GH_TOKEN is required}"
: "${REPO:?REPO (owner/name) is required}"
WORK_DIR=${WORK_DIR:-.}
WIP_TAG=${WIP_TAG:-}
WORKERS=${WORKERS:-8}
MAX_RATE=${MAX_RATE:-2}
RUNTIME_MIN=${RUNTIME_MIN:-0}
ZSTD_LONG=${ZSTD_LONG:-27}
ZSTD_THREADS=${ZSTD_THREADS:-0}
export GH_TOKEN

cd "$WORK_DIR"
DB=knesset_mirror.sqlite
ASSET=knesset_mirror.sqlite.zst
STALE_WIP_TAG=rebuild-wip   # removed after a publish whichever path produced it

log() { echo "$(date -u +%FT%TZ) regenerate: $*"; }

if [ -n "$WIP_TAG" ] && [ ! -f "$DB" ]; then
  if gh release download "$WIP_TAG" --repo "$REPO" --pattern "$ASSET" --dir . --clobber; then
    zstd -d -f --long="$ZSTD_LONG" "$ASSET" -o "$DB"
    rm -f "$ASSET"
    log "continuing the rebuild parked in $WIP_TAG"
  fi
fi
if [ -f "$DB" ]; then
  log "continuing the rebuild in $WORK_DIR/$DB"
else
  log "no rebuild in progress -- starting from an empty database"
fi

crawl_rc=0
knesset-utils sync --db "$DB" --log sync.log \
  --workers "$WORKERS" --max-rate "$MAX_RATE" --max-runtime "$RUNTIME_MIN" || crawl_rc=$?

complete=false
if knesset-utils status --db "$DB" --check-complete; then
  complete=true
fi

if [ "$complete" = true ]; then
  knesset-utils validate-fks --db "$DB" || log "FK report failed (non-blocking)"

  rm -f "$ASSET"
  zstd -19 --long="$ZSTD_LONG" -T"$ZSTD_THREADS" "$DB" -o "$ASSET"
  ls -la "$ASSET"

  date_tag=$(date -u +%Y-%m-%d)
  notes="full rebuild $(date -u +%FT%TZ)"
  gh release view latest --repo "$REPO" >/dev/null 2>&1 \
    || gh release create latest --repo "$REPO" --title "latest mirror" --notes "moving pointer"
  gh release upload latest "$ASSET" --repo "$REPO" --clobber
  gh release edit latest --repo "$REPO" --notes "$notes"
  gh release create "mirror-$date_tag" "$ASSET" --repo "$REPO" --title "mirror $date_tag" --notes "$notes" \
    || gh release upload "mirror-$date_tag" "$ASSET" --repo "$REPO" --clobber
  gh release delete "$STALE_WIP_TAG" --repo "$REPO" --yes --cleanup-tag 2>/dev/null || true
  log "published $ASSET to 'latest' and 'mirror-$date_tag'"

  # Only now: a mirror that failed to publish is retried next run instead of lost.
  rm -f "$DB" "$DB-wal" "$DB-shm" "$ASSET"

  if [ -n "${RENDER_DEPLOY_HOOK:-}" ]; then
    curl -fsS -X POST "$RENDER_DEPLOY_HOOK" >/dev/null && log "Render deploy hook triggered"
  else
    log "RENDER_DEPLOY_HOOK not set; the server's 6h refresh will pick the mirror up"
  fi
elif [ -n "$WIP_TAG" ]; then
  rm -f "$ASSET"
  zstd -19 --long="$ZSTD_LONG" -T"$ZSTD_THREADS" "$DB" -o "$ASSET"
  gh release view "$WIP_TAG" --repo "$REPO" >/dev/null 2>&1 \
    || gh release create "$WIP_TAG" --repo "$REPO" --prerelease \
         --title "rebuild in progress" --notes "partial mirror -- not for use"
  gh release upload "$WIP_TAG" "$ASSET" --repo "$REPO" --clobber
  gh release edit "$WIP_TAG" --repo "$REPO" --notes "partial rebuild $(date -u +%FT%TZ); continues on the next run"
  log "rebuild incomplete; parked in $WIP_TAG"
else
  log "rebuild incomplete; continues from $WORK_DIR/$DB on the next run"
fi

if [ "$crawl_rc" -ne 0 ]; then
  log "ERROR: crawl exited $crawl_rc; its tables keep their chunk checkpoints and are retried next run"
  exit 1
fi
