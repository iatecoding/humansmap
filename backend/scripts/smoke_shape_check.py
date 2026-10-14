"""Dump the first 5 docs from each core collection for shape inspection.

Used after the 100K-entity smoke test to eyeball-diff the JSON-parser output
against what the NT path produces. Does not assert anything — just prints.

Usage (inside backend container):
  python /app/scripts/smoke_shape_check.py
  ARANGO_DB=world_graph_smoke python /app/scripts/smoke_shape_check.py
"""

import json
import os
import sys

sys.path.insert(0, "/app")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.database import (
    init_db, PERSONS, ORGANIZATIONS, EVENTS,
    RELATIONSHIPS, AFFILIATIONS, INVOLVEMENTS, ORG_STRUCTURE,
)


def dump_collection(db, name: str, n: int = 5) -> None:
    print(f"\n── {name} ── (first {n})")
    try:
        cursor = db.aql.execute(f"FOR d IN {name} LIMIT @n RETURN d", bind_vars={"n": n})
        for doc in cursor:
            print(json.dumps(doc, indent=2, ensure_ascii=False, sort_keys=True))
            print("---")
    except Exception as e:
        print(f"  error: {e}")


def count_collection(db, name: str) -> int:
    try:
        cursor = db.aql.execute(f"FOR d IN {name} COLLECT WITH COUNT INTO n RETURN n")
        return next(iter(cursor), 0)
    except Exception as e:
        print(f"  {name} count error: {e}")
        return -1


def main() -> None:
    db = init_db()
    print("== Collection counts ==")
    for name in (PERSONS, ORGANIZATIONS, EVENTS, RELATIONSHIPS, AFFILIATIONS, INVOLVEMENTS, ORG_STRUCTURE):
        print(f"  {name}: {count_collection(db, name)}")
    for name in (PERSONS, ORGANIZATIONS, EVENTS, AFFILIATIONS, RELATIONSHIPS, INVOLVEMENTS, ORG_STRUCTURE):
        dump_collection(db, name)


if __name__ == "__main__":
    main()
