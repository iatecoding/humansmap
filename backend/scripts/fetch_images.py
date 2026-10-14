"""Fetch and cache images for persons or organizations, ordered by notability.

Two phases:
  Phase 1: Download thumbnails for entities that already have image_url
  Phase 2: SPARQL lookup for entities missing image_url, then download those too

Uses Wikimedia Commons thumb.php CDN endpoint (not Special:FilePath) to avoid
throttling.

Run inside the backend container:
  docker compose exec -u appuser backend python /app/scripts/fetch_images.py

Options via env vars:
  COLLECTION=persons    collection to process: "persons" or "organizations" (default persons)
  MIN_STATEMENTS=50     only fetch images for entities with >= N statements (default 50)
  WORKERS=5             concurrent download threads (default 5)
  SPARQL_BATCH=200      entities per SPARQL query (default 200)
  SPARQL_DELAY=2.0      seconds between SPARQL queries (default 2.0)
  LIMIT=0               max entities to process (0 = all)
  EXISTING_URLS_ONLY=0  skip SPARQL and process only stored image URLs
  IMAGE_MIN_FREE_GB=10  stop before available disk falls below this value
  DRY_RUN=1             only query, don't download
"""

import asyncio
import json
import logging
import os
import shutil
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.error import HTTPError

sys.path.insert(0, "/app")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import settings
from app.database import init_db, PERSONS, ORGANIZATIONS
from app.services.image_cache import (
    COMMONS_METADATA_BATCH_SIZE,
    _download_and_optimize_sync,
    _is_license_allowed,
    fetch_commons_metadata_batch,
    image_url_to_thumb_url,
)

from PIL import Image
from io import BytesIO
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("fetch_images")

# ── Configuration ────────────────────────────────────────────────────────────

COLLECTION = os.getenv("COLLECTION", PERSONS)
MIN_STATEMENTS = int(os.getenv("MIN_STATEMENTS", str(settings.image_min_statements)))
WORKERS = int(os.getenv("WORKERS", "3"))
SPARQL_BATCH = int(os.getenv("SPARQL_BATCH", "200"))
SPARQL_DELAY = float(os.getenv("SPARQL_DELAY", "2.0"))
IMAGE_DELAY = float(os.getenv("IMAGE_DELAY", "0.3"))  # seconds between downloads per worker
LIMIT = int(os.getenv("LIMIT", "0"))
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"
EXISTING_URLS_ONLY = os.getenv("EXISTING_URLS_ONLY", "0") == "1"
IMAGE_MIN_FREE_GB = float(os.getenv("IMAGE_MIN_FREE_GB", "10"))
IMAGE_MIN_FREE_BYTES = int(IMAGE_MIN_FREE_GB * 1024**3)
EMPTY_EXIT_CODE = int(os.getenv("EMPTY_EXIT_CODE", "0"))

SPARQL_ENDPOINT = os.getenv("SPARQL_ENDPOINT", settings.sparql_endpoint)
USER_AGENT = settings.sparql_user_agent
THUMB_WIDTH = settings.image_max_dimension_px
CACHE_DIR = Path(settings.image_cache_dir)


# ── SPARQL helper ────────────────────────────────────────────────────────────

def sparql_get_image_urls(qids: list[str], properties: list[str] | None = None) -> dict[str, str]:
    """Fetch image URLs for a batch of QIDs via SPARQL.
    properties: list of Wikidata property IDs to query (default ["P18"]).
                For organizations, pass ["P18", "P154"] to also try logo images.
    Returns {qid: image_url} — first matching property wins per entity.
    """
    if properties is None:
        properties = ["P18"]
    values = " ".join(f"wd:{q}" for q in qids)
    # Build UNION clauses for each property
    union_clauses = "\n  UNION\n  ".join(
        f"{{ ?entity wdt:{prop} ?image }}" for prop in properties
    )
    query = f"""
    SELECT ?entity ?image WHERE {{
      VALUES ?entity {{ {values} }}
      {union_clauses}
    }}
    """
    params = urllib.parse.urlencode({"query": query, "format": "json"})
    url = f"{SPARQL_ENDPOINT}?{params}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
    except HTTPError as e:
        if e.code == 429:
            logger.warning("Rate-limited (429) — sleeping 60s then retrying")
            time.sleep(60)
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
        else:
            raise

    result = {}
    for row in data["results"]["bindings"]:
        entity_uri = row.get("entity", {}).get("value", "")
        image_uri = row.get("image", {}).get("value", "")
        if "entity/" in entity_uri and image_uri:
            qid = entity_uri.split("entity/")[-1]
            # First match wins (SPARQL returns rows in property order)
            if qid not in result:
                result[qid] = image_uri
    return result


# ── Thumbnail download ───────────────────────────────────────────────────────

def _fetch_bytes(url: str, timeout: int = 30) -> bytes:
    """Fetch raw bytes from a URL, following redirects. Raises on error."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _bytes_to_jpeg(raw_bytes: bytes) -> bytes | None:
    """Convert raw image bytes to JPEG bytes, handling palette/transparency correctly."""
    img = Image.open(BytesIO(raw_bytes))
    if img.mode == "P":
        img = img.convert("RGBA")
    img = img.convert("RGB")
    out = BytesIO()
    img.save(out, "JPEG", quality=settings.image_jpeg_quality)
    return out.getvalue()


def download_thumbnail(qid: str, image_url: str) -> str | None:
    """Download a thumbnail via thumb.php and save as JPEG. Returns path or None."""
    output_path = CACHE_DIR / f"{qid}.jpg"
    if output_path.exists():
        return str(output_path)

    thumb_url = image_url_to_thumb_url(image_url, width=THUMB_WIDTH)
    raw_bytes = None

    for attempt in range(3):
        try:
            raw_bytes = _fetch_bytes(thumb_url)
            break
        except HTTPError as e:
            if e.code == 429:
                wait = 60 * (attempt + 1)
                logger.warning("Rate-limited (429) for %s — sleeping %ds then retrying", qid, wait)
                time.sleep(wait)
            elif e.code == 500:
                # thumb.php can't render this file (tif, complex svg, etc.) — skip it
                logger.debug("thumb.php 500 for %s, skipping unthumbnailable file", qid)
                return None
            elif e.code == 404:
                logger.debug("404 not found for %s", qid)
                return None
            else:
                logger.warning("HTTP %d for %s: %s", e.code, qid, thumb_url)
                return None
        except Exception as e:
            logger.warning("Download failed for %s (%s): %s", qid, thumb_url, e)
            return None
    else:
        logger.warning("Giving up on %s after 3 rate-limit retries", qid)
        return None

    try:
        jpeg_bytes = _bytes_to_jpeg(raw_bytes)
        output_path.write_bytes(jpeg_bytes)
        return str(output_path)
    except Exception as e:
        logger.warning("Image conversion failed for %s: %s", qid, e)
        if output_path.exists():
            output_path.unlink()
        return None


def download_thumbnail_throttled(qid: str, image_url: str) -> str | None:
    """Download with per-request delay to avoid 429s."""
    result = download_thumbnail(qid, image_url)
    time.sleep(IMAGE_DELAY)
    return result


# ── Download batch ───────────────────────────────────────────────────────────

def download_licensed_image(qid: str, image_url: str, metadata: dict) -> dict:
    """Cache one image whose Commons metadata has already been checked."""
    try:
        cached_path = _download_and_optimize_sync(
            qid,
            image_url,
            metadata.get("image_license"),
            thumbnail_url=metadata.get("_image_download_url"),
        )
        stored_metadata = {
            key: value for key, value in metadata.items() if not key.startswith("_")
        }
        if not cached_path:
            return {**stored_metadata, "image_status": "download_error"}
        return {
            **stored_metadata,
            "image_cached_path": cached_path,
            "image_modified": True,
            "image_status": "cached",
        }
    finally:
        time.sleep(IMAGE_DELAY)


def download_batch(
    db,
    urls: dict[str, str],
    label: str,
    collection_name: str = PERSONS,
) -> tuple[int, int]:
    """Fetch metadata serially in small batches, then cache approved thumbnails."""
    if not urls:
        return 0, 0

    logger.info(
        "  Downloading %d thumbnails (%s, %d workers, metadata batches of %d)...",
        len(urls),
        label,
        WORKERS,
        COMMONS_METADATA_BATCH_SIZE,
    )
    start = time.time()
    success = 0
    fail = 0
    col = db.collection(collection_name)
    items = list(urls.items())

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for offset in range(0, len(items), COMMONS_METADATA_BATCH_SIZE):
            free_bytes = shutil.disk_usage(CACHE_DIR).free
            if free_bytes < IMAGE_MIN_FREE_BYTES:
                raise RuntimeError(
                    f"Stopping image import with {free_bytes / 1024**3:.1f} GiB free "
                    f"(safety floor: {IMAGE_MIN_FREE_GB:.1f} GiB)"
                )
            batch_items = items[offset : offset + COMMONS_METADATA_BATCH_SIZE]
            batch_urls = dict(batch_items)
            try:
                metadata_by_qid = fetch_commons_metadata_batch(batch_urls)
            except Exception as exc:
                logger.warning("Commons metadata batch failed: %s", exc)
                metadata_by_qid = {qid: None for qid in batch_urls}

            futures = {}
            for qid, image_url in batch_items:
                metadata = metadata_by_qid.get(qid)
                if metadata is None:
                    col.update({"_key": qid, "image_status": "metadata_error"})
                    fail += 1
                    continue

                stored_metadata = {
                    key: value
                    for key, value in metadata.items()
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

                future = pool.submit(
                    download_licensed_image,
                    qid,
                    image_url,
                    metadata,
                )
                futures[future] = qid

            for future in as_completed(futures):
                qid = futures[future]
                try:
                    image_result = future.result()
                    col.update({"_key": qid, **image_result})
                    if image_result.get("image_status") == "cached":
                        success += 1
                    else:
                        fail += 1
                except Exception as exc:
                    fail += 1
                    logger.warning("Download error for %s: %s", qid, exc)

            total = success + fail
            if total % 100 < COMMONS_METADATA_BATCH_SIZE:
                elapsed = time.time() - start
                rate = total / elapsed if elapsed > 0 else 0
                logger.info(
                    "  Progress: %d/%d (%.1f/s) — %d cached, %d failed",
                    total,
                    len(urls),
                    rate,
                    success,
                    fail,
                )

    elapsed = time.time() - start
    rate = (success + fail) / elapsed if elapsed > 0 else 0
    logger.info(
        "  %s done: %d cached, %d failed (%.1f/s, %.0fs)",
        label,
        success,
        fail,
        rate,
        elapsed,
    )
    return success, fail


# ── Main ─────────────────────────────────────────────────────────────────────

def get_entities_needing_images(db, collection_name: str) -> list[dict]:
    """Get entities without cached images, ordered by notability."""
    existing_url_filter = (
        'AND p.image_url != null AND p.image_url != ""'
        if EXISTING_URLS_ONLY
        else ""
    )
    aql = f"""
        FOR p IN {collection_name}
            FILTER p.wikidata_statements_count >= {MIN_STATEMENTS}
              AND (p.image_cached_path == null OR p.image_cached_path == "")
              AND p.image_status NOT IN [
                  "rejected_license",
                  "metadata_error",
                  "download_error"
              ]
              {existing_url_filter}
            SORT p.wikidata_statements_count DESC, p._key
            {"LIMIT " + str(LIMIT) if LIMIT > 0 else ""}
            RETURN {{
                _key: p._key,
                image_url: p.image_url,
                stmts: p.wikidata_statements_count
            }}
    """
    return list(db.aql.execute(aql))


async def main():
    # Determine SPARQL properties based on collection
    sparql_props = ["P18", "P154"] if COLLECTION == ORGANIZATIONS else ["P18"]

    logger.info("=== Fetch Images ===")
    logger.info(
        "Settings: COLLECTION=%s, MIN_STATEMENTS=%d, WORKERS=%d, "
        "IMAGE_DELAY=%.1fs, LIMIT=%d, DRY_RUN=%s, EXISTING_URLS_ONLY=%s, "
        "IMAGE_MIN_FREE_GB=%.1f",
        COLLECTION,
        MIN_STATEMENTS,
        WORKERS,
        IMAGE_DELAY,
        LIMIT,
        DRY_RUN,
        EXISTING_URLS_ONLY,
        IMAGE_MIN_FREE_GB,
    )

    db = init_db()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # Step 1: Get entities needing images
    logger.info("Step 1: Querying ArangoDB for %s needing images (statements >= %d)...",
                COLLECTION, MIN_STATEMENTS)
    entities = get_entities_needing_images(db, COLLECTION)
    logger.info("  Found %d %s needing images", len(entities), COLLECTION)

    if not entities:
        logger.info("Nothing to do.")
        if EMPTY_EXIT_CODE:
            raise SystemExit(EMPTY_EXIT_CODE)
        return

    # Split into those with and without image_url
    have_url = {p["_key"]: p["image_url"] for p in entities if p.get("image_url")}
    need_url = [p["_key"] for p in entities if not p.get("image_url")]
    logger.info("  %d already have image_url, %d need SPARQL lookup", len(have_url), len(need_url))

    # ── Phase 1: Download thumbnails for entities that already have image_url ──
    logger.info("Phase 1: Downloading images for %d %s with known URLs...", len(have_url), COLLECTION)
    total_success = 0
    total_fail = 0

    if not DRY_RUN:
        s, f = download_batch(db, have_url, "phase 1", COLLECTION)
        total_success += s
        total_fail += f
    else:
        logger.info("  DRY_RUN: would download %d images", len(have_url))

    # ── Phase 2: SPARQL lookup for missing image_url, then download ───────────
    sparql_found: dict[str, str] = {}
    if need_url:
        logger.info("Phase 2: SPARQL lookup for %d %s without image_url (%d batches, props=%s)...",
                     len(need_url), COLLECTION,
                     (len(need_url) + SPARQL_BATCH - 1) // SPARQL_BATCH,
                     sparql_props)
        for i in range(0, len(need_url), SPARQL_BATCH):
            batch = need_url[i:i + SPARQL_BATCH]
            try:
                urls = sparql_get_image_urls(batch, properties=sparql_props)
                for qid, url in urls.items():
                    sparql_found[qid] = url
                    db.collection(COLLECTION).update({"_key": qid, "image_url": url})
                batch_num = i // SPARQL_BATCH + 1
                total_batches = (len(need_url) + SPARQL_BATCH - 1) // SPARQL_BATCH
                if batch_num % 10 == 0 or batch_num == total_batches:
                    logger.info("  SPARQL batch %d/%d — found %d image URLs so far",
                                batch_num, total_batches, len(sparql_found))
            except Exception as e:
                logger.error("  SPARQL batch %d failed: %s", i // SPARQL_BATCH + 1, e)
            time.sleep(SPARQL_DELAY)

        logger.info("  SPARQL found %d additional image URLs", len(sparql_found))

        if sparql_found and not DRY_RUN:
            s, f = download_batch(db, sparql_found, "phase 2", COLLECTION)
            total_success += s
            total_fail += f
    else:
        logger.info("Phase 2: Skipped (all %s already have image_url)", COLLECTION)

    no_image = len(entities) - len(have_url) - len(sparql_found)
    logger.info("=== Fetch Images Complete ===")
    logger.info("  Cached:        %d", total_success)
    logger.info("  Failed:        %d", total_fail)
    logger.info("  No image on WD:%d", no_image)


if __name__ == "__main__":
    asyncio.run(main())
