#!/bin/bash
# Monthly SEC EDGAR refresh — runs on host, orchestrates Docker containers.
# Installs as a cron job (Monday 03:00 UTC, first Monday of month):
#   0 3 1-7 * 1 /path/to/humansmap/scripts/sec_monthly_refresh.sh
# (Monday because the Sunday dump sync just refreshed Wikidata; this lets
# build_cik_map pick up new P5531 statements from the latest truthy dump.)
set -euo pipefail

LOG="/var/log/humansmap-sec-refresh.log"
COMPOSE_DIR="/path/to/humansmap"
COMPOSE="docker compose -f ${COMPOSE_DIR}/docker-compose.yml"

exec >> "$LOG" 2>&1
echo "=== SEC refresh started: $(date -Iseconds) ==="

# 1. Rebuild sec_cik_map (SPARQL against live query.wikidata.org)
echo "Step 1/5: build_cik_map.py"
$COMPOSE exec -T -u appuser backend \
  python /app/scripts/sec/build_cik_map.py

# 2. Latest-quarter Form 4 ingestion
CURRENT_YEAR=$(date +%Y)
echo "Step 2/5: fetch_form4_bulk.py --since-year $CURRENT_YEAR"
$COMPOSE exec -T -u appuser backend \
  python /app/scripts/sec/fetch_form4_bulk.py --since-year "$CURRENT_YEAR"

# 3. Per-CIK submissions delta (script internally diffs last_synced)
echo "Step 3/5: fetch_submissions.py"
$COMPOSE exec -T -u appuser backend \
  python /app/scripts/sec/fetch_submissions.py

# 4. 8-K / SC 13D-G deltas
echo "Step 4/5: fetch_material_events.py"
$COMPOSE exec -T -u appuser backend \
  python /app/scripts/sec/fetch_material_events.py

echo "Step 5/5: fetch_ownership.py"
$COMPOSE exec -T -u appuser backend \
  python /app/scripts/sec/fetch_ownership.py

echo "=== SEC refresh complete: $(date -Iseconds) ==="
