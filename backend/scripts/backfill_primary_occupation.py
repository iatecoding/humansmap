"""One-shot backfill: set primary_occupation = occupations[0] for all persons.

Usage:
    docker compose exec backend python -m scripts.backfill_primary_occupation
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from arango import ArangoClient
from app.database import get_arango_client, get_db, PERSONS

BATCH_SIZE = 10_000


def main():
    client = get_arango_client()
    db = get_db(client)

    # Count how many need backfill
    cursor = db.aql.execute(f"""
        RETURN {{
            total: LENGTH(FOR p IN {PERSONS} RETURN 1),
            has_occ: LENGTH(FOR p IN {PERSONS} FILTER LENGTH(p.occupations) > 0 RETURN 1),
            already_set: LENGTH(FOR p IN {PERSONS} FILTER p.primary_occupation != null RETURN 1)
        }}
    """)
    stats = list(cursor)[0]
    print(f"Total persons:           {stats['total']}")
    print(f"With occupations:        {stats['has_occ']}")
    print(f"Already have primary:    {stats['already_set']}")
    to_update = stats['has_occ'] - stats['already_set']
    print(f"Need backfill:           {to_update}")

    if to_update == 0:
        print("Nothing to do.")
        return

    total_updated = 0
    batch = 1
    while True:
        cursor = db.aql.execute(f"""
            FOR p IN {PERSONS}
                FILTER LENGTH(p.occupations) > 0
                FILTER p.primary_occupation == null
                LIMIT @batch_size
                UPDATE p WITH {{ primary_occupation: p.occupations[0] }} IN {PERSONS}
                RETURN 1
        """, bind_vars={"batch_size": BATCH_SIZE})
        count = sum(1 for _ in cursor)
        total_updated += count
        print(f"Batch {batch}: {count} updated ({total_updated}/{to_update})")
        batch += 1
        if count < BATCH_SIZE:
            break

    print(f"Done. Total updated: {total_updated}")


if __name__ == "__main__":
    main()
