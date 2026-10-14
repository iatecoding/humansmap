#!/bin/bash
# Weekly Wikidata dump sync — runs on host, orchestrates Docker containers.
# Installs as a cron job (Sunday 03:00 UTC):
#   0 3 * * 0 /path/to/humansmap/scripts/sync_dump.sh
set -euo pipefail

LOG="/var/log/humansmap-sync.log"
DUMP_DIR="/data/wikidump"
DUMP_URL="https://dumps.wikimedia.org/wikidatawiki/entities/latest-truthy.nt.bz2"
COMPOSE_DIR="/path/to/humansmap"
COMPOSE="docker compose -f ${COMPOSE_DIR}/docker-compose.yml"
SYNC_MARKER="${DUMP_DIR}/.last_sync_timestamp"

exec >> "$LOG" 2>&1
echo "=== Sync started: $(date -Iseconds) ==="

# ── Step 1: Download new dump (resume-capable, only re-downloads if newer) ──
echo "Downloading dump..."
mkdir -p "$DUMP_DIR"
wget -N -P "$DUMP_DIR" "$DUMP_URL" || { echo "ERROR: dump download failed"; exit 1; }

DUMP_FILE="${DUMP_DIR}/latest-truthy.nt.bz2"
echo "Dump size: $(du -sh "$DUMP_FILE" | cut -f1)"

# ── Freshness guard: skip processing if dump hasn't changed since last sync ──
if [[ -f "$SYNC_MARKER" && "$DUMP_FILE" -ot "$SYNC_MARKER" ]]; then
  echo "Dump unchanged since last sync (marker: $(cat "$SYNC_MARKER")). Skipping pass1+pass2."
  echo "=== Sync skipped (no new dump): $(date -Iseconds) ==="
  exit 0
fi

# ── Step 2: Pass 1 — identify target entities directly from the mounted dump ──
echo "Running loader pass 1 (identification)…"
$COMPOSE exec -T -u appuser backend \
  python /app/scripts/load_from_dump.py --pass1

# ── Step 3: Pass 2 — upsert the identified entities directly into ArangoDB ──
echo "Running loader pass 2 (ArangoDB extraction)…"
$COMPOSE exec -T -u appuser backend \
  python /app/scripts/load_from_dump.py --pass2

# ── Step 4: Rebuild derived Redis data from the newly loaded graph ──
echo "Flushing stale Redis cache…"
$COMPOSE exec -T redis redis-cli FLUSHDB
echo "Rebuilding connections feed…"
$COMPOSE exec -T -u appuser backend \
  python -m scripts.feed.generate_feed

# ── Update marker: record successful sync timestamp ──
date -Iseconds > "$SYNC_MARKER"

echo "=== Sync complete: $(date -Iseconds) ==="
