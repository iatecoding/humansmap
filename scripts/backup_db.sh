#!/bin/bash
# Weekly ArangoDB backup — runs on host before sync_dump.sh.
# Backs up the world_graph_all database via arangodump inside the arangodb container,
# copies the dump to the host, and retains only the last 3 backups.
#
# Install as a cron job (Sunday 02:00 UTC, one hour before sync_dump.sh):
#   0 2 * * 0 /path/to/humansmap/scripts/backup_db.sh
set -euo pipefail

LOG="/var/log/humansmap-backup.log"
HOST_BACKUP_ROOT="/data/backups"
COMPOSE_DIR="/path/to/humansmap"
COMPOSE="docker compose -f ${COMPOSE_DIR}/docker-compose.yml"
DB_NAME="world_graph_all"
RETAIN=3

exec >> "$LOG" 2>&1
echo "=== DB backup started: $(date -Iseconds) ==="

# ── Resolve ARANGO_PASSWORD (env var takes precedence over .env file) ──
if [[ -z "${ARANGO_PASSWORD:-}" ]]; then
  ENV_FILE="${COMPOSE_DIR}/.env"
  if [[ -f "$ENV_FILE" ]]; then
    ARANGO_PASSWORD=$(grep -E '^ARANGO_PASSWORD=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' | tr -d "'")
  fi
fi

if [[ -z "${ARANGO_PASSWORD:-}" ]]; then
  echo "ERROR: ARANGO_PASSWORD not set and could not be read from .env"
  exit 1
fi

# ── Destination directory (named by date) ──
DATE_TAG=$(date +%Y%m%d)
BACKUP_DIR="${HOST_BACKUP_ROOT}/db_backup_${DATE_TAG}"
mkdir -p "$BACKUP_DIR"

# ── Run arangodump inside the arangodb container ──
CONTAINER_DUMP_PATH="/tmp/arangodump_${DATE_TAG}"

echo "Running arangodump for database '${DB_NAME}'..."
$COMPOSE exec -T arangodb arangodump \
  --server.database "$DB_NAME" \
  --server.username root \
  --server.password "$ARANGO_PASSWORD" \
  --output-directory "$CONTAINER_DUMP_PATH" \
  --overwrite true

# ── Copy dump from container to host ──
echo "Copying dump to host: ${BACKUP_DIR}"
CONTAINER_ID=$($COMPOSE ps -q arangodb)
docker cp "${CONTAINER_ID}:${CONTAINER_DUMP_PATH}/." "$BACKUP_DIR/"

# ── Clean up temp dump inside container ──
$COMPOSE exec -T arangodb rm -rf "$CONTAINER_DUMP_PATH"

echo "Backup written to: ${BACKUP_DIR}"

# ── Retain only the last N backups ──
echo "Pruning old backups (keeping last ${RETAIN})..."
BACKUP_COUNT=$(ls -1d "${HOST_BACKUP_ROOT}/db_backup_"* 2>/dev/null | wc -l)
if [[ "$BACKUP_COUNT" -gt "$RETAIN" ]]; then
  TO_DELETE=$(ls -1dt "${HOST_BACKUP_ROOT}/db_backup_"* | tail -n +"$((RETAIN + 1))")
  for OLD_BACKUP in $TO_DELETE; do
    echo "  Deleting old backup: ${OLD_BACKUP}"
    rm -rf "$OLD_BACKUP"
  done
fi

echo "=== DB backup complete: $(date -Iseconds) ==="
