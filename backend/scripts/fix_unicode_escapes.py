"""Fix literal \\uXXXX sequences in ArangoDB that should be real Unicode characters.

The dump loader previously didn't decode NTriples \\uXXXX / \\UXXXXXXXX escapes,
so names/descriptions were stored as e.g. "L\u00E9onie" instead of "Léonie".
This script corrects all affected documents in-place.

Run inside the backend container:
  docker compose exec backend python /app/scripts/fix_unicode_escapes.py
"""

import logging
import os
import re
import sys

sys.path.insert(0, "/app")
from app.database import get_arango_client, get_db, PERSONS, ORGANIZATIONS, EVENTS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("fix_unicode")

_U4 = re.compile(r"\\u([0-9A-Fa-f]{4})")
_U8 = re.compile(r"\\U([0-9A-Fa-f]{8})")

TEXT_FIELDS = ["name_en", "name_it", "description_en", "description_it"]
COLLECTIONS = [PERSONS, ORGANIZATIONS, EVENTS]
BATCH_SIZE = 2000


def fix_str(s):
    if s is None or "\\" not in s:
        return s
    s = _U4.sub(lambda m: chr(int(m.group(1), 16)), s)
    s = _U8.sub(lambda m: chr(int(m.group(1), 16)), s)
    return s


def fix_collection(db, col_name):
    col = db.collection(col_name)
    total_scanned = 0
    total_fixed = 0
    batch = []

    # Only fetch docs that have at least one affected field
    filter_clause = " OR ".join(
        f'CONTAINS(d.{f}, "\\\\u")' for f in TEXT_FIELDS
    )
    aql = f"""
        FOR d IN {col_name}
            FILTER {filter_clause}
            RETURN KEEP(d, "_key", {", ".join(f'"{f}"' for f in TEXT_FIELDS)})
    """
    cursor = db.aql.execute(aql, batch_size=BATCH_SIZE, ttl=3600)

    for doc in cursor:
        total_scanned += 1
        updates = {"_key": doc["_key"]}
        changed = False
        for field in TEXT_FIELDS:
            original = doc.get(field)
            fixed = fix_str(original)
            if fixed != original:
                updates[field] = fixed
                changed = True
        if changed:
            batch.append(updates)
            total_fixed += 1

        if len(batch) >= BATCH_SIZE:
            col.update_many(batch, silent=True)
            logger.info("%s: fixed %d/%d so far...", col_name, total_fixed, total_scanned)
            batch = []

    if batch:
        col.update_many(batch, silent=True)

    logger.info("%s: done. Scanned %d, fixed %d.", col_name, total_scanned, total_fixed)
    return total_fixed


def main():
    client = get_arango_client()
    db = get_db(client)
    total = 0
    for col_name in COLLECTIONS:
        total += fix_collection(db, col_name)
    logger.info("All done. Total documents fixed: %d", total)


if __name__ == "__main__":
    main()
