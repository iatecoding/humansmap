#!/usr/bin/env bash
set -uo pipefail

cd /path/to/humansmap || exit 1

echo "Prioritizing sitemap-eligible people (>=50 Wikidata statements) by notability"

while true; do
    docker compose exec -T \
        -e COLLECTION=persons \
        -e MIN_STATEMENTS=50 \
        -e EXISTING_URLS_ONLY=1 \
        -e IMAGE_MIN_FREE_GB=10 \
        -e WORKERS=3 \
        -e IMAGE_DELAY=0.3 \
        -e LIMIT=5000 \
        -e EMPTY_EXIT_CODE=10 \
        backend python /app/scripts/fetch_images.py
    job_status=$?

    if [ "$job_status" -eq 10 ]; then
        echo "ALL_INDEXABLE_IMAGE_BATCHES_COMPLETE"
        exit 0
    fi
    if [ "$job_status" -ne 0 ]; then
        echo "IMAGE_BATCH_FAILED_STATUS=$job_status"
        exit "$job_status"
    fi
done
