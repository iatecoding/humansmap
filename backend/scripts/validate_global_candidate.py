#!/usr/bin/env python3
"""Read-only validation gates for a completed global candidate database."""

from __future__ import annotations

import argparse
from collections import Counter
import json
import sqlite3
import sys
import time
from pathlib import Path

sys_path = Path(__file__).resolve().parents[1]
if str(sys_path) not in sys.path:
    sys.path.insert(0, str(sys_path))

from app.config import settings  # noqa: E402
from app.database import get_arango_client  # noqa: E402
from scripts.dump_edges import (  # noqa: E402
    EDGE_PROPERTIES,
    EVENT_REVERSE_PROPERTIES,
    ORG_REVERSE_PROPERTIES,
    ORG_STRUCTURE_PROPERTIES,
)


NODE_EXPECTATIONS = {
    "persons": "persons",
    "organizations": "organizations",
    "events": "events",
    "reference_entities": "references",
}
EDGE_PREFIXES = {
    "relationships": ("persons", "persons"),
    "affiliations": ("persons", "organizations"),
    "involvements": ("persons", "events"),
    "org_structure": ("organizations", "organizations"),
}
SMOKE_QIDS = ("Q76", "Q567", "Q11860", "Q9685")


def scan_edge_collection(db, collection: str, batch_size: int) -> dict:
    """Validate endpoints and contracts in bounded primary-key batches."""
    after = ""
    scanned = 0
    orphans = 0
    batches = 0
    groups: Counter[tuple[str, str, str, str]] = Counter()
    started = time.monotonic()
    query = """
        LET batch = (
          FOR edge IN @@collection
            FILTER edge._key > @after
            SORT edge._key
            LIMIT @batch_size
            RETURN edge
        )
        LET grouped = (
          FOR edge IN batch
            COLLECT from_collection=SPLIT(edge._from, '/')[0],
                    to_collection=SPLIT(edge._to, '/')[0],
                    edge_type=edge.type,
                    property=edge.wikidata_property
              WITH COUNT INTO count
            RETURN {from_collection, to_collection, edge_type, property, count}
        )
        LET orphan_count = LENGTH(
          FOR edge IN batch
            FILTER DOCUMENT(edge._from) == null OR DOCUMENT(edge._to) == null
            RETURN 1
        )
        RETURN {
          last_key: LENGTH(batch) == 0 ? null : LAST(batch)._key,
          scanned: LENGTH(batch),
          orphans: orphan_count,
          groups: grouped
        }
    """
    while True:
        row = next(iter(db.aql.execute(
            query,
            bind_vars={
                "@collection": collection,
                "after": after,
                "batch_size": batch_size,
            },
        )))
        batch_count = int(row["scanned"])
        if batch_count == 0:
            break
        last_key = row["last_key"]
        if not isinstance(last_key, str) or last_key <= after:
            raise RuntimeError(
                f"{collection}: non-advancing validation cursor {last_key!r}"
            )
        after = last_key
        scanned += batch_count
        orphans += int(row["orphans"])
        batches += 1
        for group in row["groups"]:
            key = (
                group["from_collection"],
                group["to_collection"],
                group["edge_type"],
                group["property"],
            )
            groups[key] += int(group["count"])
        if batches % 20 == 0:
            print(
                f"validated {collection}: {scanned} edges in {batches} batches",
                file=sys.stderr,
                flush=True,
            )
        if batch_count < batch_size:
            break

    return {
        "scanned": scanned,
        "orphans": orphans,
        "batches": batches,
        "seconds": round(time.monotonic() - started, 3),
        "groups": [
            {
                "from_collection": key[0],
                "to_collection": key[1],
                "edge_type": key[2],
                "property": key[3],
                "count": count,
            }
            for key, count in sorted(groups.items())
        ],
    }


def expected_counts(index_path: Path) -> dict[str, int]:
    db = sqlite3.connect(index_path)
    try:
        row = db.execute(
            "SELECT value FROM metadata WHERE key='materialization'"
        ).fetchone()
        if not row:
            raise RuntimeError("capacity index has no materialization metadata")
        return {key: int(value) for key, value in json.loads(row[0]).items()}
    finally:
        db.close()


def completed_materialization(
    state_path: Path,
    database: str,
) -> tuple[dict[str, int], dict]:
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    if payload.get("phase") != "complete":
        raise RuntimeError("materializer state is not complete")
    if payload.get("database") != database:
        raise RuntimeError(
            f"materializer state database {payload.get('database')!r} "
            f"does not match {database!r}"
        )
    stats = payload.get("stats")
    if not isinstance(stats, dict):
        raise RuntimeError("materializer state has no stats object")
    counts = {
        "persons": int(stats["persons"]),
        "organizations": int(stats["organizations"]),
        "events": int(stats["events"]),
        "references": int(stats["references"]),
    }
    return counts, stats


def expected_edge_contract() -> dict[str, set[tuple[str, str, str, str]]]:
    contract = {name: set() for name in EDGE_PREFIXES}
    for prop, (collection, edge_type) in EDGE_PROPERTIES.items():
        contract[collection].add(("persons", "persons" if collection == "relationships" else "organizations" if collection == "affiliations" else "events", edge_type, prop))
    for prop, (collection, edge_type) in ORG_REVERSE_PROPERTIES.items():
        contract[collection].add(("persons", "organizations", edge_type, prop))
    for prop, (collection, edge_type) in EVENT_REVERSE_PROPERTIES.items():
        contract[collection].add(("persons", "events", edge_type, prop))
    for prop, (collection, edge_type) in ORG_STRUCTURE_PROPERTIES.items():
        contract[collection].add(("organizations", "organizations", edge_type, prop))
    return contract


def validate(args: argparse.Namespace) -> dict:
    if not args.database.startswith("world_graph_candidate_"):
        raise ValueError("--database must name a disposable world_graph_candidate_* database")

    capacity_counts = expected_counts(args.capacity_index)
    counts, materializer_stats = completed_materialization(
        args.materializer_state,
        args.database,
    )
    client = get_arango_client(request_timeout=args.timeout)
    db = client.db(
        args.database,
        username=settings.arango_username,
        password=settings.arango_password,
    )
    db.properties()

    report: dict[str, object] = {
        "database": args.database,
        "expected": counts,
        "capacity_expected": capacity_counts,
        "nodes": {},
        "edges": {},
        "smoke_qids": {},
        "errors": [],
    }
    errors: list[str] = report["errors"]  # type: ignore[assignment]

    state_capacity = materializer_stats.get("capacity_counts")
    indexed_capacity = {
        key: value for key, value in capacity_counts.items() if key != "threshold"
    }
    if state_capacity != indexed_capacity:
        errors.append("materializer capacity counts do not match the capacity index")
    if int(materializer_stats.get("threshold", -1)) != int(capacity_counts["threshold"]):
        errors.append("materializer threshold does not match the capacity index")
    realized_total = sum(counts.values())
    selected_entities = int(materializer_stats.get("selected_entities", -1))
    skipped_without_name = int(materializer_stats.get("skipped_without_name", -1))
    if selected_entities != realized_total + skipped_without_name:
        errors.append(
            "materializer selected-entity accounting does not reconcile: "
            f"selected={selected_entities}, realized={realized_total}, "
            f"skipped_without_name={skipped_without_name}"
        )
    unseen_selected = int(capacity_counts["selected"]) - selected_entities
    if unseen_selected < 0:
        errors.append(
            "materializer saw more selected entities than the capacity index contains"
        )
    report["selection_accounting"] = {
        "capacity_selected": int(capacity_counts["selected"]),
        "stream_selected_entities": selected_entities,
        "selected_qids_not_present_as_dump_items": unseen_selected,
        "skipped_without_name": skipped_without_name,
        "realized_documents": realized_total,
        "capacity_to_realized_deltas": {
            "persons": counts["persons"] - int(capacity_counts["people"]),
            "organizations": counts["organizations"] - int(capacity_counts["organizations"]),
            "events": counts["events"] - int(capacity_counts["events"]),
            "references": counts["references"] - int(capacity_counts["references"]),
        },
    }

    for collection, expected_key in NODE_EXPECTATIONS.items():
        col = db.collection(collection)
        actual = int(col.count())
        expected = counts[expected_key]
        missing_labels = int(next(iter(db.aql.execute(
            """
            RETURN LENGTH(
              FOR doc IN @@collection
                FILTER !IS_STRING(doc.name_en) OR LENGTH(TRIM(doc.name_en)) == 0
                RETURN 1
            )
            """,
            bind_vars={"@collection": collection},
        )), 0))
        report["nodes"][collection] = {  # type: ignore[index]
            "actual": actual,
            "expected": expected,
            "missing_display_labels": missing_labels,
            "secondary_indexes": [
                index.get("fields", [])
                for index in col.indexes()
                if index.get("type") != "primary"
            ],
        }
        if actual != expected:
            errors.append(f"{collection}: expected {expected}, found {actual}")
        if missing_labels:
            errors.append(f"{collection}: {missing_labels} missing display labels")

    contract = expected_edge_contract()
    for collection in EDGE_PREFIXES:
        print(f"validating {collection} endpoints", file=sys.stderr, flush=True)
        scan = scan_edge_collection(db, collection, args.edge_batch_size)
        orphans = scan["orphans"]
        groups = scan["groups"]
        actual_contract = {
            (row["from_collection"], row["to_collection"], row["edge_type"], row["property"])
            for row in groups
        }
        unexpected = sorted(actual_contract - contract[collection])
        missing_types = sorted(contract[collection] - actual_contract)
        report["edges"][collection] = {  # type: ignore[index]
            "count": int(db.collection(collection).count()),
            "orphans": orphans,
            "scanned": scan["scanned"],
            "batches": scan["batches"],
            "seconds": scan["seconds"],
            "groups": groups,
            "unexpected_contracts": unexpected,
            "missing_contracts": missing_types,
            "primary_key_enforced": True,
        }
        if orphans:
            errors.append(f"{collection}: {orphans} orphan edges")
        if unexpected:
            errors.append(f"{collection}: unexpected contracts {unexpected}")
        if missing_types:
            errors.append(f"{collection}: missing contracts {missing_types}")

    views = sorted(view["name"] for view in db.views())
    report["views"] = views
    if "search_view" not in views:
        errors.append("search_view is missing")

    persons = db.collection("persons")
    for qid in SMOKE_QIDS:
        document = persons.get(qid)
        report["smoke_qids"][qid] = (  # type: ignore[index]
            {"present": True, "name_en": document.get("name_en")}
            if document
            else {"present": False}
        )
        if not document:
            errors.append(f"required smoke person {qid} is missing")

    report["ok"] = not errors
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument(
        "--capacity-index",
        type=Path,
        default=Path("/data/ingestion/global-capacity.sqlite3"),
    )
    parser.add_argument(
        "--materializer-state",
        type=Path,
        default=Path("/data/ingestion/materializer-state.json"),
    )
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--edge-batch-size", type=int, default=50_000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = validate(args)
    payload = json.dumps(report, indent=2, sort_keys=True)
    print(payload)
    if args.output:
        args.output.write_text(payload + "\n", encoding="utf-8")
    raise SystemExit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
