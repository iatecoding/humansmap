#!/bin/bash
# Run inside the backend container. Fetches org images first, then remaining persons.
# Usage: docker compose exec -d -u appuser backend bash /app/scripts/fetch_all_images.sh
set -e

echo "=== Phase 1: Organization images ==="
COLLECTION=organizations MIN_STATEMENTS=0 python /app/scripts/fetch_images.py 2>&1 | tee /tmp/fetch_orgs.log

echo "=== Phase 2: Remaining person images ==="
COLLECTION=persons python /app/scripts/fetch_images.py 2>&1 | tee /tmp/fetch_persons.log

echo "=== All done ==="
