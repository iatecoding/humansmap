#!/usr/bin/env python3
"""Backfill source statement context onto a completed global candidate.

The capacity index retains the Wikidata statement GUID, rank, original
subject/object, and supported qualifiers. This maintenance pass joins that
source-of-truth to already-materialized candidate edges without reading the
remote dump again. Work is bounded, idempotent, and durably checkpointed at
complete source/target/property groups so suffix assignment stays stable.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys_path = Path(__file__).resolve().parents[1]
if str(sys_path) not in sys.path:
    sys.path.insert(0, str(sys_path))

from app.config import settings  # noqa: E402
from app.database import EVENTS, ORGANIZATIONS, PERSONS, get_arango_client  # noqa: E402
from scripts.dump_edges import (  # noqa: E402
    EDGE_PROPERTIES,
    EVENT_REVERSE_PROPERTIES,
    ORG_REVERSE_PROPERTIES,
    ORG_STRUCTURE_PROPERTIES,
    sanitize_key,
)


RANK_NAMES = {1: "normal", 2: "preferred"}
TABLES = ("person_facts", "container_facts")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def edge_identity(subject: int, property_number: int, target: int) -> tuple[str, str, str, str] | None:
    property_id = f"P{property_number}"
    subject_qid = f"Q{subject}"
    target_qid = f"Q{target}"
    if property_id in EDGE_PROPERTIES:
        collection, edge_type = EDGE_PROPERTIES[property_id]
        from_qid, to_qid = subject_qid, target_qid
    elif property_id in ORG_REVERSE_PROPERTIES:
        collection, edge_type = ORG_REVERSE_PROPERTIES[property_id]
        from_qid, to_qid = target_qid, subject_qid
    elif property_id in EVENT_REVERSE_PROPERTIES:
        collection, edge_type = EVENT_REVERSE_PROPERTIES[property_id]
        from_qid, to_qid = target_qid, subject_qid
    elif property_id in ORG_STRUCTURE_PROPERTIES:
        collection, edge_type = ORG_STRUCTURE_PROPERTIES[property_id]
        from_qid, to_qid = subject_qid, target_qid
    else:
        return None
    key = sanitize_key(f"{from_qid}_{to_qid}_{edge_type}_{property_id}")
    return collection, key, subject_qid, target_qid


def load_state(path: Path, database: str) -> dict:
    if not path.exists():
        return {
            "version": 1,
            "database": database,
            "phase": "running",
            "tables": {},
            "started_at": utc_now(),
        }
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("database") != database:
        raise RuntimeError("edge-context state belongs to a different database")
    return state


def write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def update_patches(db, patches: dict[str, list[dict]]) -> tuple[int, int]:
    attempted = updated = 0
    query = """
        LET changed = (
          FOR patch IN @patches
            LET existing = DOCUMENT(CONCAT(@collection, '/', patch._key))
            FILTER existing != null
            UPDATE patch IN @@collection
            RETURN 1
        )
        RETURN LENGTH(changed)
    """
    for collection, documents in patches.items():
        if not documents:
            continue
        attempted += len(documents)
        updated += int(next(iter(db.aql.execute(
            query,
            bind_vars={
                "@collection": collection,
                "collection": collection,
                "patches": documents,
            },
        )), 0))
    return attempted, updated


def iter_groups(sqlite_db: sqlite3.Connection, table: str, cursor: tuple) -> object:
    properties = sorted(
        int(value[1:])
        for value in (
            set(EDGE_PROPERTIES)
            | set(ORG_REVERSE_PROPERTIES)
            | set(EVENT_REVERSE_PROPERTIES)
            | set(ORG_STRUCTURE_PROPERTIES)
        )
    )
    placeholders = ",".join("?" for _ in properties)
    sql = f"""
        SELECT fact.subject, fact.property, fact.object, fact.statement_key,
               fact.rank, fact.start_date, fact.end_date, fact.point_in_time,
               fact.named_as, fact.ordinal
        FROM {table} fact
        JOIN materialization_chosen chosen ON chosen.qid=fact.subject
        WHERE fact.property IN ({placeholders})
          AND fact.rank > 0
          AND (fact.subject, fact.property, fact.object, fact.statement_key)
              > (?, ?, ?, ?)
          AND (
            fact.rank=2 OR NOT EXISTS (
              SELECT 1 FROM {table} preferred
              WHERE preferred.subject=fact.subject
                AND preferred.property=fact.property
                AND preferred.rank=2
            )
          )
        ORDER BY fact.subject, fact.property, fact.object, fact.statement_key
    """
    rows = sqlite_db.execute(sql, (*properties, *cursor))
    current_key = None
    current_rows = []
    for row in rows:
        key = (row[0], row[1], row[2])
        if current_key is not None and key != current_key:
            yield current_rows
            current_rows = []
        current_key = key
        current_rows.append(row)
    if current_rows:
        yield current_rows


def reconcile_table(
    sqlite_db: sqlite3.Connection,
    arango_db,
    table: str,
    state: dict,
    state_path: Path,
    batch_size: int,
) -> dict:
    table_state = state["tables"].setdefault(table, {
        "cursor": [0, 0, 0, ""],
        "attempted": 0,
        "updated": 0,
        "missing": 0,
        "complete": False,
    })
    if table_state.get("complete"):
        return table_state

    cursor = tuple(table_state["cursor"])
    patches: dict[str, list[dict]] = defaultdict(list)
    pending = 0
    last_cursor = cursor

    def flush() -> None:
        nonlocal patches, pending
        if not pending:
            return
        attempted, updated = update_patches(arango_db, patches)
        table_state["attempted"] += attempted
        table_state["updated"] += updated
        table_state["missing"] += attempted - updated
        table_state["cursor"] = list(last_cursor)
        table_state["updated_at"] = utc_now()
        write_state(state_path, state)
        print(
            f"{table}: attempted={table_state['attempted']} "
            f"updated={table_state['updated']} missing={table_state['missing']}",
            flush=True,
        )
        patches = defaultdict(list)
        pending = 0

    for group in iter_groups(sqlite_db, table, cursor):
        subject, property_number, target = group[0][:3]
        identity = edge_identity(subject, property_number, target)
        if identity:
            collection, base_key, original_subject, original_object = identity
            for position, row in enumerate(group, 1):
                suffix = f"_{position}" if position > 1 else ""
                patches[collection].append({
                    "_key": base_key + suffix,
                    "statement_guid": row[3],
                    "rank": RANK_NAMES[int(row[4])],
                    "original_subject": original_subject,
                    "original_object": original_object,
                    "start_date": row[5],
                    "end_date": row[6],
                    "point_in_time": row[7],
                    "named_as": row[8],
                    "ordinal": row[9],
                })
                pending += 1
        last_row = group[-1]
        last_cursor = (last_row[0], last_row[1], last_row[2], last_row[3])
        if pending >= batch_size:
            flush()

    flush()
    table_state["complete"] = True
    table_state["completed_at"] = utc_now()
    write_state(state_path, state)
    return table_state


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument(
        "--capacity-index",
        type=Path,
        default=Path("/data/ingestion/global-capacity.sqlite3"),
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=Path("/data/ingestion/edge-context-reconciliation.json"),
    )
    parser.add_argument("--batch-size", type=int, default=5_000)
    args = parser.parse_args()
    if not args.database.startswith("world_graph_candidate_"):
        raise ValueError("--database must name a disposable candidate database")
    if args.batch_size <= 0:
        raise ValueError("--batch-size must be positive")

    state = load_state(args.state, args.database)
    client = get_arango_client(request_timeout=1800)
    arango_db = client.db(
        args.database,
        username=settings.arango_username,
        password=settings.arango_password,
    )
    arango_db.properties()
    sqlite_db = sqlite3.connect(args.capacity_index)
    try:
        for table in TABLES:
            reconcile_table(
                sqlite_db,
                arango_db,
                table,
                state,
                args.state,
                args.batch_size,
            )
    finally:
        sqlite_db.close()
    state["phase"] = "complete"
    state["completed_at"] = utc_now()
    write_state(args.state, state)
    print(json.dumps(state, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
