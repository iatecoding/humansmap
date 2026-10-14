"""Retry failed image/logo downloads for persons and organizations.

Re-attempts downloading images for all entities that have an image_url
but no image_cached_path (i.e., previous download failed or was rate-limited).

Run inside the backend container:
  docker compose exec backend python /app/scripts/retry_images.py

Options via env vars:
  DELAY=3.0       seconds between downloads (default 3.0, be polite)
  COLLECTION=     limit to 'persons' or 'organizations' (default: both)
  LIMIT=0         max entities to process (0 = all)
"""

import asyncio
import logging
import os
import sys
import time

sys.path.insert(0, "/app")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import settings
from app.database import init_db, PERSONS, ORGANIZATIONS
from app.services.image_cache import (
    _download_and_optimize_sync,
    _is_license_allowed,
    fetch_commons_metadata,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("retry_images")

DELAY = float(os.getenv("DELAY", "0.5"))
COLLECTION = os.getenv("COLLECTION", "")  # "persons", "organizations", or ""
LIMIT = int(os.getenv("LIMIT", "0"))
MIN_STATEMENTS = int(os.getenv("MIN_STATEMENTS", str(settings.image_min_statements)))


def get_pending(db, collection_name: str) -> list[dict]:
    """Return entities with image_url but no image_cached_path."""
    statements_filter = (
        f"AND (doc.wikidata_statements_count == null OR doc.wikidata_statements_count >= {MIN_STATEMENTS})"
        if collection_name == PERSONS
        else ""
    )
    aql = f"""
        FOR doc IN {collection_name}
            FILTER doc.image_url != null
              AND doc.image_url != ""
              AND (doc.image_cached_path == null OR doc.image_cached_path == "")
              AND doc.image_status != "rejected_license"
              {statements_filter}
            SORT doc.wikidata_statements_count DESC, doc._key
            RETURN {{_key: doc._key, image_url: doc.image_url}}
    """
    return list(db.aql.execute(aql))


async def retry_collection(db, collection_name: str) -> tuple[int, int]:
    """Download missing images for one collection. Returns (success, fail)."""
    pending = get_pending(db, collection_name)
    if LIMIT > 0:
        pending = pending[:LIMIT]

    logger.info(
        "%s: %d entities need image download", collection_name, len(pending)
    )

    success = 0
    fail = 0
    col = db.collection(collection_name)

    for i, doc in enumerate(pending):
        qid = doc["_key"]
        url = doc["image_url"]
        try:
            metadata = await asyncio.to_thread(fetch_commons_metadata, url)
            if not metadata:
                col.update({"_key": qid, "image_status": "metadata_error"})
                fail += 1
                continue
            stored_metadata = {
                key: value for key, value in metadata.items()
                if not key.startswith("_")
            }
            if not _is_license_allowed(metadata.get("image_license")):
                col.update({
                    "_key": qid,
                    **stored_metadata,
                    "image_status": "rejected_license",
                })
                fail += 1
                continue
            cached_path = await asyncio.to_thread(
                _download_and_optimize_sync,
                qid,
                url,
                metadata.get("image_license"),
                metadata.get("_image_download_url"),
            )
            if cached_path:
                col.update({
                    "_key": qid,
                    **stored_metadata,
                    "image_cached_path": cached_path,
                    "image_modified": True,
                    "image_status": "cached",
                })
                success += 1
                logger.info("[%d/%d] %s: cached %s", i + 1, len(pending), qid, cached_path)
            else:
                col.update({"_key": qid, **stored_metadata, "image_status": "download_error"})
                fail += 1
                logger.debug("[%d/%d] %s: download failed", i + 1, len(pending), qid)
        except Exception as e:
            fail += 1
            logger.warning("[%d/%d] %s: failed — %s", i + 1, len(pending), qid, e)

        if i < len(pending) - 1:
            time.sleep(DELAY)

    return success, fail


async def main():
    logger.info("=== Image Retry ===")
    logger.info("Settings: DELAY=%.1fs, COLLECTION=%r, LIMIT=%d, MIN_STATEMENTS=%d", DELAY, COLLECTION or "all", LIMIT, MIN_STATEMENTS)

    db = init_db()
    start = time.time()
    total_ok = total_fail = 0

    collections = []
    if not COLLECTION or COLLECTION == "persons":
        collections.append(PERSONS)
    if not COLLECTION or COLLECTION == "organizations":
        collections.append(ORGANIZATIONS)

    for col in collections:
        ok, fail = await retry_collection(db, col)
        total_ok += ok
        total_fail += fail
        logger.info("%s complete: %d cached, %d failed/skipped", col, ok, fail)

    elapsed = time.time() - start
    logger.info("=== Retry Complete ===")
    logger.info("  Cached:        %d", total_ok)
    logger.info("  Failed/skipped:%d", total_fail)
    logger.info("  Total time:    %.0f minutes", elapsed / 60)


if __name__ == "__main__":
    asyncio.run(main())
