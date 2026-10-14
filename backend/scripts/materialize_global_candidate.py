#!/usr/bin/env python3
"""Materialize a measured global Wikidata candidate into a disposable DB.

The capacity pass has already done the expensive selection work in SQLite.
This phase deliberately does not rebuild that selection in Python: it creates
an exact, disk-backed QID bitset from the capacity index and streams the dump
once more.  Only selected entities are decoded and written to ArangoDB.

The target database must be a fresh ``world_graph_candidate_*`` database. A
load interrupted before its durable finalization checkpoint must be discarded;
cleanup, index, and search-view finalization can be resumed safely.
"""

from __future__ import annotations

import argparse
import json
import logging
import mmap
import os
import re
import resource
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

sys_path = Path(__file__).resolve().parents[1]
if str(sys_path) not in sys.path:
    sys.path.insert(0, str(sys_path))

from app.database import (  # noqa: E402
    EDGE_COLLECTIONS,
    EVENTS,
    ORGANIZATIONS,
    ORG_STRUCTURE,
    PERSONS,
    DOCUMENT_COLLECTIONS,
    ensure_collections,
    ensure_database,
    ensure_graph,
    ensure_indexes,
    ensure_search_view,
    get_arango_client,
    wait_for_arango,
)
from scripts.materialize_enrichment import EnrichmentStore  # noqa: E402
from scripts.dump_edges import (  # noqa: E402
    EVENT_REVERSE_PROPERTIES,
    ORG_REVERSE_PROPERTIES,
    build_edge_docs,
    build_org_structure_edge_docs,
    build_reverse_edge_docs,
)
from scripts.global_capacity_pass import (  # noqa: E402
    EVENT_KIND,
    ORGANIZATION_KIND,
    PERSON_KIND,
    REVERSE_PROPERTY_NUMBERS,
)
from scripts.load_from_dump import (  # noqa: E402
    best_label,
    build_event_doc,
    build_org_doc,
    build_person_doc,
    classify_entity,
    open_dump_stream,
    parse_json_entity,
    validate_dump_source,
)

try:  # Imported separately so local-file tests do not need a remote stream.
    from scripts.global_capacity_pass import open_hashed_dump
except ImportError:  # pragma: no cover - defensive for direct script loading
    open_hashed_dump = None


logger = logging.getLogger("materialize_global_candidate")

REFERENCE_ENTITIES = "reference_entities"
CHOSEN_TABLE = "materialization_chosen"
QID_RE = re.compile(r'"id"\s*:\s*"Q([1-9][0-9]*)"')

DEFAULT_BATCH_SIZE = 2_000
DEFAULT_EDGE_BATCH_SIZE = 5_000
DEFAULT_PROGRESS_EVERY = 100_000
DEFAULT_ARANGO_REQUEST_TIMEOUT = 1_800
DEFAULT_CLEANUP_BATCH_SIZE = 50_000


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_state(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"Expected an object in {path}")
    return value


def _write_state(path: Path, payload: dict) -> None:
    """Persist a phase checkpoint without exposing a partially written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {**payload, "updated_at": utc_now()}
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent,
        prefix=f".{path.name}.", delete=False,
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def rss_mib() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def enforce_limits(path: Path, max_rss_mib: int, min_free_gib: int) -> None:
    current_rss = rss_mib()
    if current_rss > max_rss_mib:
        raise RuntimeError(
            f"RSS safety limit exceeded: {current_rss:.0f} MiB > {max_rss_mib} MiB"
        )
    free_gib = shutil.disk_usage(path.parent).free / (1024 ** 3)
    if free_gib < min_free_gib:
        raise RuntimeError(
            f"Disk safety reserve breached: {free_gib:.1f} GiB < {min_free_gib} GiB"
        )


def _eligible(alias: str) -> str:
    return (
        f"{alias}.kind={PERSON_KIND} AND {alias}.statements >= ? "
        f"AND {alias}.has_label=1"
    )


def build_chosen_table(index_path: Path, threshold: int) -> dict[str, int]:
    """Build the selected-QID table without materializing QIDs in Python."""
    db = sqlite3.connect(index_path)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA temp_store=FILE")
        db.execute("PRAGMA cache_size=-65536")
        db.executescript("""
            CREATE INDEX IF NOT EXISTS person_facts_object_rank_property
                ON person_facts(object, rank, property);
            CREATE INDEX IF NOT EXISTS container_facts_object_rank_property
                ON container_facts(object, rank, property);
            CREATE INDEX IF NOT EXISTS container_facts_subject_rank_property
                ON container_facts(subject, rank, property);
            """)
        db.executescript(
            f"""
            DROP TABLE IF EXISTS {CHOSEN_TABLE};
            CREATE TABLE {CHOSEN_TABLE} (
                qid INTEGER PRIMARY KEY,
                kind INTEGER NOT NULL
            ) WITHOUT ROWID;
            """
        )

        db.execute(
            f"""
            INSERT OR IGNORE INTO {CHOSEN_TABLE}(qid, kind)
            SELECT qid, kind
            FROM entities entity
            WHERE {_eligible('entity')}
            """,
            (threshold,),
        )
        db.execute(
            f"""
            INSERT OR IGNORE INTO {CHOSEN_TABLE}(qid, kind)
            SELECT pf.object, COALESCE(target.kind, 0)
            FROM person_facts pf
            JOIN entities person ON person.qid=pf.subject
            LEFT JOIN entities target ON target.qid=pf.object
            WHERE pf.rank > 0
              AND {_eligible('person')}
            """,
            (threshold,),
        )

        reverse_placeholders = ",".join("?" for _ in REVERSE_PROPERTY_NUMBERS)
        db.execute(
            f"""
            INSERT OR IGNORE INTO {CHOSEN_TABLE}(qid, kind)
            SELECT cf.subject, COALESCE(container.kind, 0)
            FROM container_facts cf
            JOIN entities person ON person.qid=cf.object
            JOIN entities container ON container.qid=cf.subject
            WHERE cf.property IN ({reverse_placeholders})
              AND cf.rank > 0
              AND {_eligible('person')}
            """,
            (*sorted(REVERSE_PROPERTY_NUMBERS), threshold),
        )

        db.execute(
            f"""
            INSERT OR IGNORE INTO {CHOSEN_TABLE}(qid, kind)
            SELECT cf.object, COALESCE(target.kind, 0)
            FROM container_facts cf
            JOIN {CHOSEN_TABLE} selected ON selected.qid=cf.subject
            JOIN entities container ON container.qid=cf.subject
            LEFT JOIN entities target ON target.qid=cf.object
            WHERE container.kind IN ({ORGANIZATION_KIND}, {EVENT_KIND})
              AND cf.rank > 0
            """
        )

        counts = {
            "selected": db.execute(
                f"SELECT COUNT(*) FROM {CHOSEN_TABLE}"
            ).fetchone()[0],
            "people": db.execute(
                f"SELECT COUNT(*) FROM {CHOSEN_TABLE} WHERE kind=?",
                (PERSON_KIND,),
            ).fetchone()[0],
            "organizations": db.execute(
                f"SELECT COUNT(*) FROM {CHOSEN_TABLE} WHERE kind=?",
                (ORGANIZATION_KIND,),
            ).fetchone()[0],
            "events": db.execute(
                f"SELECT COUNT(*) FROM {CHOSEN_TABLE} WHERE kind=?",
                (EVENT_KIND,),
            ).fetchone()[0],
            "references": db.execute(
                f"SELECT COUNT(*) FROM {CHOSEN_TABLE} WHERE kind=0"
            ).fetchone()[0],
        }
        db.execute(
            "INSERT INTO metadata(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("materialization", json.dumps({"threshold": threshold, **counts})),
        )
        db.commit()
        return {key: int(value) for key, value in counts.items()}
    finally:
        db.close()


class QidBitset:
    """Exact QID membership backed by a compact mmap file."""

    def __init__(self, path: Path, max_qid: int, *, writable: bool):
        self.path = path
        self.max_qid = max_qid
        self._file = path.open("r+b" if writable else "rb")
        self._map = mmap.mmap(
            self._file.fileno(), 0,
            access=mmap.ACCESS_WRITE if writable else mmap.ACCESS_READ,
        )

    @classmethod
    def create_from_index(cls, index_path: Path, path: Path) -> "QidBitset":
        db = sqlite3.connect(index_path)
        try:
            max_qid = int(
                db.execute(f"SELECT COALESCE(MAX(qid), 0) FROM {CHOSEN_TABLE}")
                .fetchone()[0]
            )
            size = max(1, (max_qid // 8) + 1)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w+b") as handle:
                handle.truncate(size)
            bitset = cls(path, max_qid, writable=True)
            for (qid,) in db.execute(f"SELECT qid FROM {CHOSEN_TABLE} ORDER BY qid"):
                bitset.add(int(qid))
            bitset.flush()
            bitset.close()
            return cls(path, max_qid, writable=False)
        finally:
            db.close()

    def add(self, qid: int) -> None:
        self._map[qid >> 3] |= 1 << (qid & 7)

    def contains(self, qid: int) -> bool:
        return qid > 0 and qid <= self.max_qid and bool(
            self._map[qid >> 3] & (1 << (qid & 7))
        )

    def flush(self) -> None:
        self._map.flush()

    def close(self) -> None:
        self._map.close()
        self._file.close()


def _qid_number_from_line(raw: str) -> int | None:
    match = QID_RE.search(raw, 0, min(len(raw), 128))
    return int(match.group(1)) if match else None


def _compact_reference_doc(qid: str, entity: dict, timestamp: str) -> dict:
    return {
        "_key": qid,
        "wikidata_id": qid,
        # Some referenced/deleted Wikidata items legitimately have no label in
        # any language. Keep the display-label invariant deterministic instead
        # of emitting null for those compact endpoint documents.
        "name_en": best_label(entity["labels"]) or qid,
        "name_it": entity["labels"].get("it"),
        "description_en": entity["descriptions"].get("en"),
        "description_it": entity["descriptions"].get("it"),
        "instance_of": entity["p31"][:12],
        "category": "reference",
        "ingestion_run_id": timestamp,
        "source": "wikidata_dump",
        "last_updated": timestamp,
    }


def _strip_edge_collection(edge: dict) -> tuple[str, dict]:
    collection = edge.pop("edge_collection", None)
    if collection not in EDGE_COLLECTIONS:
        raise RuntimeError(f"Unknown edge collection: {collection!r}")
    return collection, edge


class CandidateMaterializer:
    def __init__(
        self,
        db,
        selected: QidBitset,
        enrichment: EnrichmentStore,
        *,
        batch_size: int,
        edge_batch_size: int,
        max_rss_mib: int,
        min_free_gib: int,
    ):
        self.db = db
        self.selected = selected
        self.enrichment = enrichment
        self.batch_size = batch_size
        self.edge_batch_size = edge_batch_size
        self.max_rss_mib = max_rss_mib
        self.min_free_gib = min_free_gib
        self.timestamp = utc_now()
        self.node_batches = {
            PERSONS: [],
            ORGANIZATIONS: [],
            EVENTS: [],
            REFERENCE_ENTITIES: [],
        }
        self.edge_batches = {collection: [] for collection in EDGE_COLLECTIONS}
        self.stats = {
            "entities_seen": 0,
            "selected_entities": 0,
            "persons": 0,
            "organizations": 0,
            "events": 0,
            "references": 0,
            "skipped_without_name": 0,
            "edges_seen": 0,
            "edges_written": 0,
            "malformed": 0,
            "orphaned": 0,
            "batches": 0,
        }

    def _write_batch(self, collection: str, docs: list[dict]) -> None:
        if not docs:
            return
        result = self.db.collection(collection).import_bulk(
            docs, on_duplicate="error"
        )
        errors = int(result.get("errors", 0))
        if errors:
            raise RuntimeError(
                f"Candidate import failed for {collection}: "
                f"{errors} errors in {len(docs)} documents"
            )
        self.stats["batches"] += 1

    def _flush_nodes(self, collection: str, *, force: bool = False) -> None:
        batch = self.node_batches[collection]
        if force or len(batch) >= self.batch_size:
            self._write_batch(collection, batch)
            batch.clear()

    def _flush_edges(self, collection: str, *, force: bool = False) -> None:
        batch = self.edge_batches[collection]
        if force or len(batch) >= self.edge_batch_size:
            self._write_batch(collection, batch)
            batch.clear()

    def _queue_edges(self, edges: Iterable[dict]) -> None:
        for edge in edges:
            self.stats["edges_seen"] += 1
            collection, doc = _strip_edge_collection(edge)
            target = (doc.get("_to") or "").rsplit("/", 1)[-1]
            if not target.startswith("Q"):
                self.stats["orphaned"] += 1
                continue
            # The capacity selection includes all retained edge targets. The
            # bitset check protects against parser/property drift and avoids
            # writing known out-of-scope targets.
            try:
                target_number = int(target[1:])
            except ValueError:
                self.stats["orphaned"] += 1
                continue
            if not self.selected.contains(target_number):
                self.stats["orphaned"] += 1
                continue
            self.edge_batches[collection].append(doc)
            self.stats["edges_written"] += 1
            self._flush_edges(collection)

    def _queue_entity(self, qid: str, entity: dict) -> None:
        category, type_label = classify_entity(entity["p31"])
        self.enrichment.record_entity(qid, entity)
        # Builders start with empty cross-entity labels; the disk-backed
        # enrichment pass fills the same fields after all selected QIDs are
        # available, without retaining a multi-million-entry Python map.
        if category == "person":
            doc = build_person_doc(qid, entity, {}, {}, {})
            if doc is None:
                self.stats["skipped_without_name"] += 1
                return
            doc["last_updated"] = self.timestamp
            self.enrichment.pending_for_entity(qid, entity, category)
            self.node_batches[PERSONS].append(doc)
            self.stats["persons"] += 1
            self._queue_edges(build_edge_docs(qid, entity))
            self._flush_nodes(PERSONS)
        elif category == "organization":
            doc = build_org_doc(qid, entity, type_label or "organization", {})
            if doc is None:
                self.stats["skipped_without_name"] += 1
                return
            doc["last_updated"] = self.timestamp
            self.enrichment.pending_for_entity(qid, entity, category)
            self.node_batches[ORGANIZATIONS].append(doc)
            self.stats["organizations"] += 1
            self._queue_edges(build_reverse_edge_docs(
                qid, entity, ORGANIZATIONS, ORG_REVERSE_PROPERTIES
            ))
            self._queue_edges(build_org_structure_edge_docs(qid, entity))
            self._flush_nodes(ORGANIZATIONS)
        elif category == "event":
            doc = build_event_doc(qid, entity, type_label or "event", {})
            if doc is None:
                self.stats["skipped_without_name"] += 1
                return
            doc["last_updated"] = self.timestamp
            self.enrichment.pending_for_entity(qid, entity, category)
            self.node_batches[EVENTS].append(doc)
            self.stats["events"] += 1
            self._queue_edges(build_reverse_edge_docs(
                qid, entity, EVENTS, EVENT_REVERSE_PROPERTIES
            ))
            self._flush_nodes(EVENTS)
        else:
            self.node_batches[REFERENCE_ENTITIES].append(
                _compact_reference_doc(qid, entity, self.timestamp)
            )
            self.stats["references"] += 1
            self._flush_nodes(REFERENCE_ENTITIES)

    def finish(self) -> dict[str, int]:
        for collection in self.node_batches:
            self._flush_nodes(collection, force=True)
        for collection in self.edge_batches:
            self._flush_edges(collection, force=True)
        return {key: int(value) for key, value in self.stats.items()}


def _candidate_db(*, request_timeout: int | float = DEFAULT_ARANGO_REQUEST_TIMEOUT):
    database = os.getenv("ARANGO_DB", "").strip()
    if not database.startswith("world_graph_candidate_"):
        raise ValueError(
            "ARANGO_DB must name a disposable world_graph_candidate_* database"
        )
    client = get_arango_client(request_timeout=request_timeout)
    wait_for_arango(client)
    db = ensure_database(client)
    ensure_collections(db)
    ensure_graph(db)
    if not db.has_collection(REFERENCE_ENTITIES):
        db.create_collection(REFERENCE_ENTITIES)
    return db


def _assert_empty_candidate(db) -> None:
    for collection in (*DOCUMENT_COLLECTIONS, *EDGE_COLLECTIONS, REFERENCE_ENTITIES):
        if not db.has_collection(collection):
            continue
        count = int(db.collection(collection).count())
        if count:
            raise RuntimeError(
                f"Candidate database is not empty: {collection} contains {count} documents. "
                "Discard it and create a fresh candidate database."
            )


def _cleanup_collection(
    db, collection: str, *, after: str = "", batch_size: int,
    type_check: bool = False, on_progress=None,
) -> int:
    """Scan and clean one collection in bounded primary-key ranges."""
    removed_total = 0
    while True:
        keys = list(db.aql.execute(
            """
            FOR e IN @@collection
              FILTER e._key > @after
              SORT e._key
              LIMIT @batch_size
              RETURN e._key
            """,
            bind_vars={
                "@collection": collection,
                "after": after,
                "batch_size": batch_size,
            },
        ))
        if not keys:
            break
        through = keys[-1]
        if type_check:
            predicate = (
                f'NOT STARTS_WITH(e._from, "{ORGANIZATIONS}/") '
                f'OR NOT STARTS_WITH(e._to, "{ORGANIZATIONS}/")'
            )
        else:
            predicate = "DOCUMENT(e._from) == null OR DOCUMENT(e._to) == null"
        cursor = db.aql.execute(
            f"""
            FOR e IN @@collection
              FILTER e._key > @after AND e._key <= @through
              FILTER {predicate}
              REMOVE e IN @@collection
              COLLECT WITH COUNT INTO n
              RETURN n
            """,
            bind_vars={
                "@collection": collection,
                "after": after,
                "through": through,
            },
        )
        removed = int(next(iter(cursor), 0))
        removed_total += removed
        after = through
        if on_progress is not None:
            on_progress(after, removed)
        if len(keys) < batch_size:
            break
    # A final bounded sweep guarantees convergence. Range pagination is the
    # efficient primary pass, but RocksDB index churn during large deletes can
    # leave a small residual set behind the cursor.
    while True:
        cursor = db.aql.execute(
            f"""
            FOR e IN @@collection
              FILTER {predicate}
              LIMIT @batch_size
              REMOVE e IN @@collection
              COLLECT WITH COUNT INTO n
              RETURN n
            """,
            bind_vars={
                "@collection": collection,
                "batch_size": batch_size,
            },
        )
        removed = int(next(iter(cursor), 0))
        removed_total += removed
        if on_progress is not None and removed:
            on_progress(after, removed)
        if removed < batch_size:
            break
    return removed_total


def _cleanup_orphans(
    db, *, completed: set[str] | None = None, cursors: dict[str, str] | None = None,
    batch_size: int = DEFAULT_CLEANUP_BATCH_SIZE, on_complete=None, on_progress=None,
) -> int:
    """Remove invalid edges in bounded, durably checkpointable batches."""
    completed = set() if completed is None else completed
    cursors = {} if cursors is None else cursors
    removed_total = 0
    for collection in EDGE_COLLECTIONS:
        step = f"cleanup:{collection}"
        if step in completed:
            continue
        removed = _cleanup_collection(
            db, collection, after=cursors.get(step, ""), batch_size=batch_size,
            on_progress=(
                (lambda cursor, count, step=step: on_progress(step, cursor, count))
                if on_progress is not None else None
            ),
        )
        removed_total += removed
        if removed:
            logger.info("Removed %d orphan edges from %s", removed, collection)
        if on_complete is not None:
            on_complete(step, 0)

    step = "cleanup:org_structure_type"
    if step not in completed:
        removed = _cleanup_collection(
            db, ORG_STRUCTURE, after=cursors.get(step, ""), batch_size=batch_size,
            type_check=True,
            on_progress=(
                (lambda cursor, count: on_progress(step, cursor, count))
                if on_progress is not None else None
            ),
        )
        removed_total += removed
        if removed:
            logger.info("Removed %d invalid org_structure edges", removed)
        if on_complete is not None:
            on_complete(step, 0)
    return removed_total


def _finalize_candidate(
    db, *, state: dict | None = None, state_path: Path | None = None,
    cleanup_batch_size: int = DEFAULT_CLEANUP_BATCH_SIZE,
) -> dict[str, int]:
    """Run idempotent, per-batch checkpointed post-load work."""
    state = state if state is not None else {}
    completed = set(state.get("finalization_steps") or [])
    cursors = dict(state.get("cleanup_cursors") or {})

    def write_checkpoint() -> None:
        state["finalization_steps"] = sorted(completed)
        state["cleanup_cursors"] = cursors
        if state_path is not None:
            _write_state(state_path, state)

    def progress(step: str, cursor: str, removed: int) -> None:
        cursors[step] = cursor
        state["orphan_edges_removed"] = int(
            state.get("orphan_edges_removed", 0)
        ) + removed
        write_checkpoint()

    def checkpoint(step: str, _removed: int = 0) -> None:
        completed.add(step)
        cursors.pop(step, None)
        write_checkpoint()

    _cleanup_orphans(
        db, completed=completed, cursors=cursors, batch_size=cleanup_batch_size,
        on_complete=checkpoint, on_progress=progress,
    )
    if "indexes" not in completed:
        ensure_indexes(db)
        checkpoint("indexes")
    if "search_view" not in completed:
        ensure_search_view(db)
        checkpoint("search_view")
    return {"orphan_edges_removed": int(state.get("orphan_edges_removed", 0))}


def _state_identity(args: argparse.Namespace) -> dict[str, object]:
    return {
        "database": os.getenv("ARANGO_DB", "").strip(),
        "source": args.source,
        "threshold": args.threshold,
    }


def _assert_state_identity(state: dict, args: argparse.Namespace) -> None:
    expected = _state_identity(args)
    actual = {key: state.get(key) for key in expected}
    if actual != expected:
        raise RuntimeError(
            "Materializer state does not match this database/source/threshold: "
            f"expected {expected}, found {actual}"
        )


def finalize_existing(args: argparse.Namespace) -> dict[str, object]:
    """Resume only idempotent finalization; never repeat the dump stream."""
    state = _read_state(args.state)
    if not state:
        if not args.adopt_existing:
            raise RuntimeError(
                "No materializer checkpoint exists; use --adopt-existing only after "
                "verifying that bulk loading and enrichment completed"
            )
        db = _candidate_db(request_timeout=args.arango_request_timeout)
        populated = {
            collection: int(db.collection(collection).count())
            for collection in (PERSONS, ORGANIZATIONS, EVENTS, REFERENCE_ENTITIES)
        }
        if not all(populated.values()):
            raise RuntimeError(
                "Cannot adopt an incomplete candidate; expected every primary "
                f"collection to be populated, found {populated}"
            )
        state = {
            "version": 1,
            **_state_identity(args),
            "phase": "finalization_pending",
            "adopted_existing": True,
            "collection_counts": populated,
        }
        _write_state(args.state, state)
    else:
        _assert_state_identity(state, args)
        if state.get("phase") == "complete":
            return state.get("stats") or {}
        if state.get("phase") != "finalization_pending":
            raise RuntimeError(
                "Finalization is only safe after the finalization_pending checkpoint; "
                f"found phase {state.get('phase')!r}"
            )
        db = _candidate_db(request_timeout=args.arango_request_timeout)

    logger.info("Resuming idempotent candidate finalization")
    final_stats = _finalize_candidate(
        db, state=state, state_path=args.state,
        cleanup_batch_size=args.cleanup_batch_size,
    )
    stats = {**(state.get("stats") or {}), **final_stats}
    state.update({"phase": "complete", "completed_at": utc_now(), "stats": stats})
    _write_state(args.state, state)
    return stats


def _stream_entities(stream, materializer: CandidateMaterializer, args) -> None:
    started = time.time()
    for raw in stream:
        materializer.stats["entities_seen"] += 1
        qid_number = _qid_number_from_line(raw)
        if qid_number is None or not materializer.selected.contains(qid_number):
            continue
        stripped = raw.strip()
        if stripped in ("[", "]"):
            continue
        if stripped.endswith(","):
            stripped = stripped[:-1]
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError as exc:
            materializer.stats["malformed"] += 1
            raise RuntimeError(
                f"Malformed selected JSON entity near input entity "
                f"{materializer.stats['entities_seen']}"
            ) from exc
        qid, entity = parse_json_entity(obj)
        if not qid or entity is None:
            continue
        materializer.stats["selected_entities"] += 1
        materializer._queue_entity(qid, entity)

        if materializer.stats["entities_seen"] % args.progress_every == 0:
            elapsed = max(time.time() - started, 0.001)
            logger.info(
                "%dM input entities | %d selected | people=%d orgs=%d events=%d "
                "refs=%d edges=%d | %.0f entities/s | RSS %.0f MiB",
                materializer.stats["entities_seen"] // 1_000_000,
                materializer.stats["selected_entities"],
                materializer.stats["persons"],
                materializer.stats["organizations"],
                materializer.stats["events"],
                materializer.stats["references"],
                materializer.stats["edges_written"],
                materializer.stats["entities_seen"] / elapsed,
                rss_mib(),
            )
            enforce_limits(
                args.capacity_index,
                args.max_rss_mib,
                args.min_free_gib,
            )


def materialize(args: argparse.Namespace) -> dict[str, object]:
    if args.finalize_only:
        return finalize_existing(args)

    validate_dump_source(args.source)
    index_path = args.capacity_index.resolve()
    if not index_path.exists():
        raise FileNotFoundError(f"Capacity index not found: {index_path}")

    index_db = sqlite3.connect(index_path)
    try:
        metadata = {
            key: json.loads(value)
            for key, value in index_db.execute(
                "SELECT key, value FROM metadata"
            )
        }
    finally:
        index_db.close()
    if metadata.get("status") != "complete":
        raise RuntimeError("Capacity index is not complete")
    if int(metadata.get("min_floor", 0)) > args.threshold:
        raise RuntimeError(
            f"Capacity index only retained facts down to {metadata.get('min_floor')}; "
            f"threshold {args.threshold} needs a new capacity scan"
        )

    counts = build_chosen_table(index_path, args.threshold)
    bitset_path = args.bitset or index_path.with_name(
        f"global-capacity-selected-{args.threshold}.bitset"
    )
    selected = QidBitset.create_from_index(index_path, bitset_path)
    enrichment_path = args.state.parent / f"materialization-enrichment-{args.threshold}.sqlite3"
    enrichment = EnrichmentStore(enrichment_path)
    db = None
    stream = None
    materializer = None
    started = time.time()
    try:
        db = _candidate_db(request_timeout=args.arango_request_timeout)
        _assert_empty_candidate(db)
        run_state = {
            "version": 1,
            **_state_identity(args),
            "phase": "streaming",
            "started_at": utc_now(),
        }
        _write_state(args.state, run_state)
        materializer = CandidateMaterializer(
            db,
            selected,
            enrichment,
            batch_size=args.batch_size,
            edge_batch_size=args.edge_batch_size,
            max_rss_mib=args.max_rss_mib,
            min_free_gib=args.min_free_gib,
        )
        logger.info(
            "Materializing threshold %d: %s | bitset=%s (%.1f MiB)",
            args.threshold,
            counts,
            bitset_path,
            bitset_path.stat().st_size / (1024 ** 2),
        )
        enforce_limits(index_path, args.max_rss_mib, args.min_free_gib)

        expected_sha1 = metadata.get("source", {}).get("expected_sha1")
        if expected_sha1 and open_hashed_dump is not None and args.source.startswith("http"):
            stream, hasher = open_hashed_dump(args.source)
        else:
            stream = open_dump_stream(args.source)
            hasher = None
        _stream_entities(stream, materializer, args)
        stream.close()
        stream = None
        if hasher is not None:
            actual_sha1 = hasher.hexdigest()
            if actual_sha1.lower() != str(expected_sha1).lower():
                raise RuntimeError(
                    f"Materialization SHA1 mismatch: expected {expected_sha1}, "
                    f"got {actual_sha1}"
                )
        stats = materializer.finish()
        stats["enrichment_updates"] = enrichment.apply(db, args.batch_size)
        # Everything after this durable checkpoint is idempotent. If ArangoDB
        # or the client is interrupted, --finalize-only resumes here without
        # re-reading the dump or duplicating imported documents.
        run_state.update({"phase": "finalization_pending", "stats": stats})
        _write_state(args.state, run_state)
        stats.update(
            _finalize_candidate(
                db, state=run_state, state_path=args.state,
                cleanup_batch_size=args.cleanup_batch_size,
            )
        )
        stats["elapsed_seconds"] = round(time.time() - started, 3)
        stats["threshold"] = args.threshold
        stats["capacity_counts"] = counts
        run_state.update({
            "phase": "complete",
            "completed_at": utc_now(),
            "stats": stats,
        })
        _write_state(args.state, run_state)
        logger.info("Candidate materialization complete: %s", stats)
        return stats
    finally:
        if stream is not None:
            stream.close()
        selected.close()
        enrichment.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Materialize a capacity-measured global Wikidata candidate"
    )
    parser.add_argument("--source", required=True)
    parser.add_argument("--capacity-index", type=Path, required=True)
    parser.add_argument("--threshold", type=int, required=True)
    parser.add_argument("--bitset", type=Path)
    parser.add_argument("--state", type=Path, default=Path("/data/ingestion/materializer-state.json"))
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--edge-batch-size", type=int, default=DEFAULT_EDGE_BATCH_SIZE)
    parser.add_argument("--progress-every", type=int, default=DEFAULT_PROGRESS_EVERY)
    parser.add_argument("--max-rss-mib", type=int, default=900)
    parser.add_argument("--min-free-gib", type=int, default=20)
    parser.add_argument(
        "--cleanup-batch-size", type=int, default=DEFAULT_CLEANUP_BATCH_SIZE,
        help="Maximum edges per cleanup transaction",
    )
    parser.add_argument(
        "--arango-request-timeout", type=int,
        default=int(os.getenv("ARANGO_MAINTENANCE_TIMEOUT_SECONDS", DEFAULT_ARANGO_REQUEST_TIMEOUT)),
        help="HTTP timeout for long-running ArangoDB maintenance queries",
    )
    parser.add_argument(
        "--finalize-only", action="store_true",
        help="Resume idempotent cleanup/index/view creation after a completed load",
    )
    parser.add_argument(
        "--adopt-existing", action="store_true",
        help="Create a finalization checkpoint for a verified pre-checkpoint candidate",
    )
    args = parser.parse_args()
    if args.threshold < 1:
        parser.error("--threshold must be positive")
    if args.batch_size < 1 or args.edge_batch_size < 1:
        parser.error("batch sizes must be positive")
    if args.arango_request_timeout < 1:
        parser.error("--arango-request-timeout must be positive")
    if args.cleanup_batch_size < 1:
        parser.error("--cleanup-batch-size must be positive")
    if args.adopt_existing and not args.finalize_only:
        parser.error("--adopt-existing requires --finalize-only")
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    result = materialize(args)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
