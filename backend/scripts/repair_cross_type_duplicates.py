#!/usr/bin/env python3
"""Remove incremental-update vertex copies whose QID already has a primary type.

Dry-run by default. Run with --apply only after all ingestion/update jobs stop.
The repair is deliberately narrow: it removes only duplicate documents written
by the SPARQL incremental updater, plus edges incident to those exact documents.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

app_root = Path(__file__).resolve().parents[1]
if str(app_root) not in sys.path:
    sys.path.insert(0, str(app_root))

from app.config import settings
from app.database import (
    AFFILIATIONS,
    EVENTS,
    INVOLVEMENTS,
    ORGANIZATIONS,
    ORG_STRUCTURE,
    PERSONS,
    RELATIONSHIPS,
    get_arango_client,
    get_db,
)

EDGE_COLLECTIONS = (RELATIONSHIPS, AFFILIATIONS, INVOLVEMENTS, ORG_STRUCTURE)


def find_duplicates(db) -> list[dict]:
    query = f"""
        LET duplicate_orgs = (
            FOR doc IN {ORGANIZATIONS}
                FILTER doc.source == "wikidata_sparql"
                FILTER DOCUMENT(CONCAT("{PERSONS}/", doc._key)) != null
                RETURN {{qid: doc._key, remove_id: doc._id, keep: "person"}}
        )
        LET duplicate_events = (
            FOR doc IN {EVENTS}
                FILTER doc.source == "wikidata_sparql"
                LET person = DOCUMENT(CONCAT("{PERSONS}/", doc._key))
                LET org = DOCUMENT(CONCAT("{ORGANIZATIONS}/", doc._key))
                FILTER person != null OR org != null
                RETURN {{
                    qid: doc._key,
                    remove_id: doc._id,
                    keep: person != null ? "person" : "organization"
                }}
        )
        RETURN APPEND(duplicate_orgs, duplicate_events)
    """
    rows = list(db.aql.execute(query))
    return rows[0] if rows else []


def incident_edge_counts(db, document_ids: list[str]) -> dict[str, int]:
    counts = {}
    for collection in EDGE_COLLECTIONS:
        query = """
            FOR edge IN @@collection
                FILTER edge._from IN @ids OR edge._to IN @ids
                COLLECT WITH COUNT INTO count
                RETURN count
        """
        rows = list(db.aql.execute(query, bind_vars={"@collection": collection, "ids": document_ids}))
        counts[collection] = int(rows[0]) if rows else 0
    return counts


def apply_repair(db, duplicates: list[dict]) -> dict[str, int]:
    ids_by_collection: dict[str, list[str]] = defaultdict(list)
    duplicate_ids = [row["remove_id"] for row in duplicates]

    removed = {}
    for collection in EDGE_COLLECTIONS:
        query = """
            LET doomed = (
                FOR edge IN @@collection
                    FILTER edge._from IN @ids OR edge._to IN @ids
                    RETURN edge
            )
            FOR edge IN doomed
                REMOVE edge IN @@collection
                RETURN OLD._key
        """
        rows = list(db.aql.execute(query, bind_vars={"@collection": collection, "ids": duplicate_ids}))
        removed[f"edges:{collection}"] = len(rows)

    for row in duplicates:
        collection, _ = row["remove_id"].split("/", 1)
        ids_by_collection[collection].append(row["remove_id"])

    for collection, ids in ids_by_collection.items():
        query = """
            FOR document_id IN @ids
                LET document_key = PARSE_IDENTIFIER(document_id).key
                REMOVE document_key IN @@collection
                RETURN OLD._key
        """
        rows = list(db.aql.execute(query, bind_vars={"@collection": collection, "ids": ids}))
        removed[f"vertices:{collection}"] = len(rows)
    return removed


def invalidate_caches(qids: set[str]) -> int:
    import redis

    client = redis.Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=3,
    )
    patterns = {"search:*", "leaderboard:*", "featured:*"}
    for qid in qids:
        patterns.update({
            f"person:{qid}",
            f"organization:{qid}",
            f"event:{qid}",
            f"neighborhood:{qid}:*",
            f"neighborhood:v2:{qid}:*",
            f"person_page:*:{qid}:*",
            f"organization_page:*:{qid}:*",
            f"sp:{qid}:*",
            f"sp:*:{qid}",
        })
    keys = {key for pattern in patterns for key in client.scan_iter(match=pattern, count=500)}
    return client.delete(*keys) if keys else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    db = get_db(get_arango_client(request_timeout=300))
    duplicates = find_duplicates(db)
    ids = [row["remove_id"] for row in duplicates]
    report = {
        "mode": "apply" if args.apply else "dry-run",
        "duplicates": duplicates,
        "incident_edges": incident_edge_counts(db, ids) if ids else {},
    }
    if args.apply and duplicates:
        report["removed"] = apply_repair(db, duplicates)
        report["cache_keys_deleted"] = invalidate_caches({row["qid"] for row in duplicates})
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
