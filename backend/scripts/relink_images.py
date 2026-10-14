"""Re-link already-downloaded images to their DB documents after a fresh DB restore.

Scans /app/images/ for Q*.jpg files and bulk-updates image_cached_path
on matching persons and organizations documents.

Run inside the backend container:
  docker compose exec backend python /app/scripts/relink_images.py

Env vars:
  DRY_RUN=1   report counts only, don't write
  BATCH=1000  update batch size (default 1000)
"""

import os
import sys
import logging

sys.path.insert(0, "/app")

from app.config import settings
from app.database import init_db, PERSONS, ORGANIZATIONS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("relink_images")

DRY_RUN = os.getenv("DRY_RUN", "0") == "1"
BATCH = int(os.getenv("BATCH", "1000"))


def scan_image_dir(cache_dir: str) -> dict[str, str]:
    """Return {qid: full_path} for every Q*.jpg in cache_dir."""
    result = {}
    for fname in os.listdir(cache_dir):
        if fname.startswith("Q") and fname.endswith(".jpg"):
            qid = fname[:-4]  # strip .jpg
            result[qid] = os.path.join(cache_dir, fname)
    return result


def bulk_update(col, updates: list[dict], dry_run: bool) -> int:
    """Update documents in batches; return count updated."""
    if dry_run or not updates:
        return 0
    count = 0
    for i in range(0, len(updates), BATCH):
        batch = updates[i : i + BATCH]
        col.import_bulk(batch, on_duplicate="update")
        count += len(batch)
    return count


def main():
    db = init_db()

    cache_dir = settings.image_cache_dir
    log.info("Scanning %s ...", cache_dir)
    qid_to_path = scan_image_dir(cache_dir)
    log.info("Found %d image files", len(qid_to_path))

    persons_col = db.collection(PERSONS)
    orgs_col = db.collection(ORGANIZATIONS)

    # Fetch all keys that currently have null image_cached_path from both collections
    log.info("Querying persons with missing image_cached_path ...")
    person_keys = set(
        db.aql.execute(
            "FOR p IN persons FILTER p.image_cached_path == null OR p.image_cached_path == '' RETURN p._key",
            batch_size=10000,
        )
    )
    log.info("  %d persons need relinking", len(person_keys))

    log.info("Querying organizations with missing image_cached_path ...")
    org_keys = set(
        db.aql.execute(
            "FOR o IN organizations FILTER o.image_cached_path == null OR o.image_cached_path == '' RETURN o._key",
            batch_size=10000,
        )
    )
    log.info("  %d organizations need relinking", len(org_keys))

    person_updates = [
        {"_key": qid, "image_cached_path": path}
        for qid, path in qid_to_path.items()
        if qid in person_keys
    ]
    org_updates = [
        {"_key": qid, "image_cached_path": path}
        for qid, path in qid_to_path.items()
        if qid in org_keys
    ]

    log.info(
        "Will update %d persons, %d orgs%s",
        len(person_updates),
        len(org_updates),
        " (DRY RUN)" if DRY_RUN else "",
    )

    if not DRY_RUN:
        n = bulk_update(persons_col, person_updates, DRY_RUN)
        log.info("Updated %d person documents", n)
        n = bulk_update(orgs_col, org_updates, DRY_RUN)
        log.info("Updated %d organization documents", n)
        log.info("Done.")
    else:
        log.info("Dry-run complete. Re-run without DRY_RUN=1 to apply.")


if __name__ == "__main__":
    main()
