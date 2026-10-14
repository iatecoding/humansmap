#!/usr/bin/env bash
# Create and authorize one disposable ArangoDB candidate database.
# Usage: ./scripts/create_candidate_database.sh [world_graph_candidate_YYYYMMDD]
set -euo pipefail

COMPOSE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE=(docker compose -f "${COMPOSE_DIR}/docker-compose.yml")
DB_NAME="${1:-world_graph_candidate_$(date +%Y%m%d)}"
APP_USER="${ARANGO_APP_USERNAME:-app_user}"

if [[ ! "$DB_NAME" =~ ^world_graph_candidate_[0-9]{8}$ ]]; then
  echo "Candidate name must match world_graph_candidate_YYYYMMDD" >&2
  exit 2
fi

ENV_FILE="${COMPOSE_DIR}/.env"
if [[ -z "${ARANGO_PASSWORD:-}" && -f "$ENV_FILE" ]]; then
  ARANGO_PASSWORD=$(grep -E '^ARANGO_PASSWORD=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' | tr -d "'")
fi
if [[ -z "${ARANGO_PASSWORD:-}" ]]; then
  echo "ARANGO_PASSWORD is required (or must be present in .env)" >&2
  exit 1
fi

javascript=$(cat <<EOF
// arangosh exposes db globally. Requiring the server-only @arangodb root
// module from this client context fails on ArangoDB 3.11.
const name = "${DB_NAME}";
const user = "${APP_USER}";
if (db._databases().indexOf(name) === -1) {
  db._createDatabase(name);
  print("Created database: " + name);
} else {
  print("Database already exists: " + name);
}
EOF
)

"${COMPOSE[@]}" exec -T arangodb arangosh \
  --server.database _system \
  --server.username root \
  --server.password "$ARANGO_PASSWORD" \
  --javascript.execute-string "$javascript"


"${COMPOSE[@]}" exec -T arangodb arangosh \
  --server.database _system \
  --server.username root \
  --server.password "$ARANGO_PASSWORD" \
  --javascript.execute-string \
  "require(\"@arangodb/users\").grantDatabase(\"${APP_USER}\", \"${DB_NAME}\", \"rw\");"

"${COMPOSE[@]}" exec -T arangodb arangosh \
  --server.database _system \
  --server.username root \
  --server.password "$ARANGO_PASSWORD" \
  --javascript.execute-string \
  "require(\"@arangodb/users\").grantCollection(\"${APP_USER}\", \"${DB_NAME}\", \"*\", \"rw\");"

echo "Granted ${APP_USER} rw access to ${DB_NAME}"
