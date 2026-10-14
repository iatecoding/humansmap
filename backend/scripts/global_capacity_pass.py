#!/usr/bin/env python3
"""Build a bounded, reusable capacity index from a full Wikidata JSON dump.

This phase never connects to ArangoDB. It stores only numeric entity IDs and
selected QID-to-QID facts in SQLite, then reports candidate graph sizes for
several statement thresholds. The database is deliberately useful to later
materialization phases so the expensive first pass is not throwaway work.
"""

from __future__ import annotations

import argparse
import bz2
import hashlib
import io
import json
import logging
import os
import re
import resource
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

sys.path.insert(0, "/app")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.dump_edges import (
    EDGE_PROPERTIES,
    EVENT_REVERSE_PROPERTIES,
    ORG_REVERSE_PROPERTIES,
    ORG_STRUCTURE_PROPERTIES,
)
from scripts.load_from_dump import EVENT_QIDS, LABEL_REF_PROPERTIES, ORG_QIDS


logger = logging.getLogger("global_capacity_pass")

SCHEMA_VERSION = 2
EDGE_COUNT_SEMANTICS = "materialized-truthy-typed-v1"
PERSON_KIND = 1
ORGANIZATION_KIND = 2
EVENT_KIND = 3

# Facts that are cheap to retain now and likely to become useful connections.
AUDIT_CONNECTION_PROPERTIES = {
    "P1416",  # affiliation
    "P1037",  # director / manager
    "P286",   # head coach
    "P6087",  # coach
    "P1066",  # student of
    "P802",   # student
    "P10449",  # trained by
    "P1290",  # godparent
}

PERSON_FACT_PROPERTIES = set(EDGE_PROPERTIES) | set(LABEL_REF_PROPERTIES)
CONTAINER_FACT_PROPERTIES = (
    set(ORG_REVERSE_PROPERTIES)
    | set(ORG_STRUCTURE_PROPERTIES)
    | set(EVENT_REVERSE_PROPERTIES)
    | set(LABEL_REF_PROPERTIES)
)
RETAINED_PROPERTIES = (
    PERSON_FACT_PROPERTIES
    | CONTAINER_FACT_PROPERTIES
    | AUDIT_CONNECTION_PROPERTIES
)
REVERSE_PROPERTY_NUMBERS = {
    int(prop[1:])
    for prop in set(ORG_REVERSE_PROPERTIES) | set(EVENT_REVERSE_PROPERTIES)
}
RETAINED_PROPERTY_NUMBERS = {int(prop[1:]) for prop in RETAINED_PROPERTIES}
PERSON_CLASS_NUMBERS = {5}
ORGANIZATION_CLASS_NUMBERS = {int(qid[1:]) for qid in ORG_QIDS}
EVENT_CLASS_NUMBERS = {int(qid[1:]) for qid in EVENT_QIDS}
_CLASS_NUMBER_PATTERN = "|".join(
    str(number)
    for number in sorted(
        PERSON_CLASS_NUMBERS | ORGANIZATION_CLASS_NUMBERS | EVENT_CLASS_NUMBERS
    )
)
CLASS_MARKER_RE = re.compile(rf'"Q(?:{_CLASS_NUMBER_PATTERN})"')

_QID_RE = re.compile(r"^Q([1-9][0-9]*)$")
_PINNED_DUMP_RE = re.compile(
    r"^https://dumps\.wikimedia\.org/wikidatawiki/entities/"
    r"(?P<release>[0-9]{8})/wikidata-(?P=release)-all\.json\.bz2$"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def qid_number(value: object) -> int | None:
    match = _QID_RE.fullmatch(value) if isinstance(value, str) else None
    return int(match.group(1)) if match else None


def _statement_qid(statement: object, *, include_deprecated: bool) -> int | None:
    if not isinstance(statement, dict):
        return None
    if not include_deprecated and statement.get("rank") == "deprecated":
        return None
    mainsnak = statement.get("mainsnak")
    if not isinstance(mainsnak, dict) or mainsnak.get("snaktype") != "value":
        return None
    datavalue = mainsnak.get("datavalue")
    if not isinstance(datavalue, dict) or datavalue.get("type") != "wikibase-entityid":
        return None
    value = datavalue.get("value")
    if not isinstance(value, dict):
        return None
    entity_id = value.get("id")
    if isinstance(entity_id, str):
        return qid_number(entity_id)
    numeric_id = value.get("numeric-id")
    return numeric_id if isinstance(numeric_id, int) and numeric_id > 0 else None


def statement_qid(statement: object) -> int | None:
    return _statement_qid(statement, include_deprecated=False)


def claim_qids(claims: dict, property_id: str) -> set[int]:
    statements = claims.get(property_id) or []
    has_preferred = any(
        isinstance(statement, dict) and statement.get("rank") == "preferred"
        for statement in statements
    )
    return {
        target
        for statement in statements
        if not has_preferred or (
            isinstance(statement, dict)
            and statement.get("rank") == "preferred"
        )
        if (target := statement_qid(statement)) is not None
    }


def nondeprecated_statement_count(claims: dict) -> int:
    return sum(
        1
        for statements in claims.values()
        if isinstance(statements, list)
        for statement in statements
        if isinstance(statement, dict) and statement.get("rank") != "deprecated"
    )


def classify_claims(claims: dict) -> int | None:
    classes = claim_qids(claims, "P31")
    if classes.intersection(PERSON_CLASS_NUMBERS):
        return PERSON_KIND
    if classes.intersection(ORGANIZATION_CLASS_NUMBERS):
        return ORGANIZATION_KIND
    if classes.intersection(EVENT_CLASS_NUMBERS):
        return EVENT_KIND
    return None


def _qualifier_value(snak: object) -> str | None:
    if not isinstance(snak, dict) or snak.get("snaktype") != "value":
        return None
    datavalue = snak.get("datavalue")
    if not isinstance(datavalue, dict):
        return None
    value = datavalue.get("value")
    if datavalue.get("type") == "time" and isinstance(value, dict):
        raw_time = value.get("time")
        if isinstance(raw_time, str):
            return raw_time.lstrip("+").split("T", 1)[0]
    if datavalue.get("type") == "monolingualtext" and isinstance(value, dict):
        text = value.get("text")
        return text if isinstance(text, str) else None
    return value if isinstance(value, str) else None


def _first_qualifier(statement: dict, property_id: str) -> str | None:
    qualifiers = statement.get("qualifiers")
    if not isinstance(qualifiers, dict):
        return None
    return next(
        (
            value
            for snak in qualifiers.get(property_id) or []
            if (value := _qualifier_value(snak)) is not None
        ),
        None,
    )


FactRow = tuple[
    int,
    int,
    str,
    int,
    str | None,
    str | None,
    str | None,
    str | None,
    str | None,
]


def fact_rows(claims: dict) -> list[FactRow]:
    rows: list[
        tuple[
            int,
            int,
            str,
            int,
            str | None,
            str | None,
            str | None,
            str | None,
            str | None,
        ]
    ] = []
    for property_id in sorted(RETAINED_PROPERTIES):
        property_number = int(property_id[1:])
        for position, statement in enumerate(claims.get(property_id) or []):
            target = _statement_qid(statement, include_deprecated=True)
            if target is None or not isinstance(statement, dict):
                continue
            rank = {"deprecated": 0, "normal": 1, "preferred": 2}.get(
                statement.get("rank"),
                1,
            )
            statement_key = statement.get("id")
            if not isinstance(statement_key, str) or not statement_key:
                statement_key = f"{property_id}:{position}"
            rows.append(
                (
                    property_number,
                    target,
                    statement_key,
                    rank,
                    _first_qualifier(statement, "P580"),
                    _first_qualifier(statement, "P582"),
                    _first_qualifier(statement, "P585"),
                    _first_qualifier(statement, "P1810"),
                    _first_qualifier(statement, "P1545"),
                )
            )
    return rows


class HashingReader:
    """Hash compressed bytes while a decompressor consumes a non-seekable stream."""

    def __init__(self, raw):
        self.raw = raw
        self.digest = hashlib.sha1()

    def read(self, size: int = -1) -> bytes:
        data = self.raw.read(size)
        if data:
            self.digest.update(data)
        return data

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def hexdigest(self) -> str:
        return self.digest.hexdigest()


class CapacityIndex:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA temp_store=FILE")
        self.db.execute("PRAGMA cache_size=-65536")
        self.db.execute("PRAGMA mmap_size=134217728")
        self.db.execute("PRAGMA journal_size_limit=268435456")
        self.db.execute("PRAGMA wal_autocheckpoint=10000")
        self._create_schema()

    def _create_schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            ) WITHOUT ROWID;

            CREATE TABLE IF NOT EXISTS entities (
                qid INTEGER PRIMARY KEY,
                kind INTEGER NOT NULL,
                statements INTEGER NOT NULL,
                has_en INTEGER NOT NULL,
                has_it INTEGER NOT NULL,
                has_label INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS entities_kind_statements
                ON entities(kind, statements DESC);

            CREATE TABLE IF NOT EXISTS person_facts (
                subject INTEGER NOT NULL,
                property INTEGER NOT NULL,
                object INTEGER NOT NULL,
                statement_key TEXT NOT NULL,
                rank INTEGER NOT NULL,
                start_date TEXT,
                end_date TEXT,
                point_in_time TEXT,
                named_as TEXT,
                ordinal TEXT,
                PRIMARY KEY(subject, property, object, statement_key)
            ) WITHOUT ROWID;

            CREATE TABLE IF NOT EXISTS container_facts (
                subject INTEGER NOT NULL,
                property INTEGER NOT NULL,
                object INTEGER NOT NULL,
                statement_key TEXT NOT NULL,
                rank INTEGER NOT NULL,
                start_date TEXT,
                end_date TEXT,
                point_in_time TEXT,
                named_as TEXT,
                ordinal TEXT,
                PRIMARY KEY(subject, property, object, statement_key)
            ) WITHOUT ROWID;
            """
        )
        self.db.commit()

    def set_metadata(self, **values: object) -> None:
        self.db.executemany(
            """
            INSERT INTO metadata(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            [(key, json.dumps(value, sort_keys=True)) for key, value in values.items()],
        )

    def get_metadata(self) -> dict[str, object]:
        return {
            key: json.loads(value)
            for key, value in self.db.execute("SELECT key, value FROM metadata")
        }

    def record(self, obj: dict, min_floor: int) -> bool:
        qid = qid_number(obj.get("id"))
        claims = obj.get("claims")
        if qid is None or not isinstance(claims, dict):
            return False
        kind = classify_claims(claims)
        if kind is None:
            return False

        labels = obj.get("labels")
        labels = labels if isinstance(labels, dict) else {}
        has_en = bool((labels.get("en") or {}).get("value"))
        has_it = bool((labels.get("it") or {}).get("value"))
        has_label = any(
            isinstance(block, dict) and bool(block.get("value"))
            for block in labels.values()
        )
        statements = nondeprecated_statement_count(claims)
        self.db.execute(
            """
            INSERT INTO entities(qid, kind, statements, has_en, has_it, has_label)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(qid) DO UPDATE SET
                kind=excluded.kind,
                statements=excluded.statements,
                has_en=excluded.has_en,
                has_it=excluded.has_it,
                has_label=excluded.has_label
            """,
            (qid, kind, statements, has_en, has_it, has_label),
        )

        if kind == PERSON_KIND and statements < min_floor:
            return True

        rows = [(qid, *fact) for fact in fact_rows(claims)]
        if not rows:
            return True
        table = "person_facts" if kind == PERSON_KIND else "container_facts"
        self.db.executemany(
            f"""
            INSERT OR IGNORE INTO {table}(
                subject, property, object, statement_key, rank,
                start_date, end_date, point_in_time, named_as, ordinal
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        return True

    def commit(self) -> None:
        self.db.commit()

    def size_bytes(self) -> int:
        page_count = self.db.execute("PRAGMA page_count").fetchone()[0]
        page_size = self.db.execute("PRAGMA page_size").fetchone()[0]
        return int(page_count) * int(page_size)

    def report_for_threshold(self, threshold: int) -> dict[str, int | float]:
        db = self.db
        db.execute("DROP TABLE IF EXISTS temp.chosen")
        db.execute(
            "CREATE TEMP TABLE chosen(qid INTEGER PRIMARY KEY) WITHOUT ROWID"
        )

        def eligible(alias: str) -> str:
            return (
                f"{alias}.kind={PERSON_KIND} AND {alias}.statements >= ? "
                f"AND {alias}.has_label=1"
            )

        db.execute(
            f"INSERT OR IGNORE INTO chosen(qid) "
            f"SELECT qid FROM entities entity WHERE {eligible('entity')}",
            (threshold,),
        )
        db.execute(
            f"""
            INSERT OR IGNORE INTO chosen(qid)
            SELECT pf.object
            FROM person_facts pf
            JOIN entities person ON person.qid=pf.subject
            WHERE pf.rank > 0
              AND {eligible('person')}
            """,
            (threshold,),
        )

        reverse_placeholders = ",".join("?" for _ in REVERSE_PROPERTY_NUMBERS)
        db.execute(
            f"""
            INSERT OR IGNORE INTO chosen(qid)
            SELECT cf.subject
            FROM container_facts cf
            JOIN entities person ON person.qid=cf.object
            WHERE cf.property IN ({reverse_placeholders})
              AND cf.rank > 0
              AND {eligible('person')}
            """,
            (*sorted(REVERSE_PROPERTY_NUMBERS), threshold),
        )

        # Preserve one-hop endpoints and reusable facts for every selected
        # organization/event, whether selected from a person fact or reverse role.
        db.execute(
            f"""
            INSERT OR IGNORE INTO chosen(qid)
            SELECT cf.object
            FROM container_facts cf
            JOIN chosen selected ON selected.qid=cf.subject
            JOIN entities container ON container.qid=cf.subject
            WHERE container.kind IN ({ORGANIZATION_KIND}, {EVENT_KIND})
              AND cf.rank > 0
            """
        )

        def scalar(query: str, params: tuple = ()) -> int:
            return int(db.execute(query, params).fetchone()[0])

        full_people = scalar(
            f"SELECT COUNT(*) FROM entities entity WHERE {eligible('entity')}",
            (threshold,),
        )
        selected_people = scalar(
            f"""
            SELECT COUNT(*)
            FROM chosen c JOIN entities e ON e.qid=c.qid
            WHERE e.kind={PERSON_KIND}
            """
        )
        organizations = scalar(
            f"""
            SELECT COUNT(*)
            FROM chosen c JOIN entities e ON e.qid=c.qid
            WHERE e.kind={ORGANIZATION_KIND}
            """
        )
        events = scalar(
            f"""
            SELECT COUNT(*)
            FROM chosen c JOIN entities e ON e.qid=c.qid
            WHERE e.kind={EVENT_KIND}
            """
        )
        references = scalar(
            """
            SELECT COUNT(*)
            FROM chosen c LEFT JOIN entities e ON e.qid=c.qid
            WHERE e.qid IS NULL
            """
        )
        person_edge_numbers = {
            collection: sorted(
                int(property_id[1:])
                for property_id, (edge_collection, _edge_type) in EDGE_PROPERTIES.items()
                if edge_collection == collection
            )
            for collection in ("relationships", "affiliations", "involvements")
        }
        relationship_placeholders = ",".join(
            "?" for _ in person_edge_numbers["relationships"]
        )
        affiliation_placeholders = ",".join(
            "?" for _ in person_edge_numbers["affiliations"]
        )
        involvement_placeholders = ",".join(
            "?" for _ in person_edge_numbers["involvements"]
        )
        person_edges = scalar(
            f"""
            SELECT COUNT(*)
            FROM person_facts pf
            JOIN chosen chosen_source ON chosen_source.qid=pf.subject
            JOIN entities source ON source.qid=pf.subject
            JOIN chosen target ON target.qid=pf.object
            JOIN entities target_entity ON target_entity.qid=pf.object
            WHERE source.kind={PERSON_KIND}
              AND source.has_en=1
              AND target_entity.has_en=1
              AND (
                    (pf.property IN ({relationship_placeholders})
                     AND target_entity.kind={PERSON_KIND})
                    OR (pf.property IN ({affiliation_placeholders})
                        AND target_entity.kind={ORGANIZATION_KIND})
                    OR (pf.property IN ({involvement_placeholders})
                        AND target_entity.kind={EVENT_KIND})
                  )
              AND (
                    pf.rank=2
                    OR (
                        pf.rank=1
                        AND NOT EXISTS (
                            SELECT 1
                            FROM person_facts preferred
                            WHERE preferred.subject=pf.subject
                              AND preferred.property=pf.property
                              AND preferred.rank=2
                        )
                    )
                  )
            """,
            (
                *person_edge_numbers["relationships"],
                *person_edge_numbers["affiliations"],
                *person_edge_numbers["involvements"],
            ),
        )
        org_reverse_numbers = sorted(
            int(property_id[1:]) for property_id in ORG_REVERSE_PROPERTIES
        )
        org_structure_numbers = sorted(
            int(property_id[1:]) for property_id in ORG_STRUCTURE_PROPERTIES
        )
        event_reverse_numbers = sorted(
            int(property_id[1:]) for property_id in EVENT_REVERSE_PROPERTIES
        )
        org_reverse_placeholders = ",".join("?" for _ in org_reverse_numbers)
        org_structure_placeholders = ",".join("?" for _ in org_structure_numbers)
        event_reverse_placeholders = ",".join("?" for _ in event_reverse_numbers)
        container_edges = scalar(
            f"""
            SELECT COUNT(*)
            FROM container_facts cf
            JOIN chosen source ON source.qid=cf.subject
            JOIN chosen target ON target.qid=cf.object
            JOIN entities source_entity ON source_entity.qid=cf.subject
            JOIN entities target_entity ON target_entity.qid=cf.object
            WHERE source_entity.has_en=1
              AND target_entity.has_en=1
              AND (
                    (source_entity.kind={ORGANIZATION_KIND}
                     AND cf.property IN ({org_reverse_placeholders})
                     AND target_entity.kind={PERSON_KIND})
                    OR (source_entity.kind={ORGANIZATION_KIND}
                        AND cf.property IN ({org_structure_placeholders})
                        AND target_entity.kind={ORGANIZATION_KIND})
                    OR (source_entity.kind={EVENT_KIND}
                        AND cf.property IN ({event_reverse_placeholders})
                        AND target_entity.kind={PERSON_KIND})
                  )
              AND (
                    cf.rank=2
                    OR (
                        cf.rank=1
                        AND NOT EXISTS (
                            SELECT 1
                            FROM container_facts preferred
                            WHERE preferred.subject=cf.subject
                              AND preferred.property=cf.property
                              AND preferred.rank=2
                        )
                    )
                  )
            """,
            (*org_reverse_numbers, *org_structure_numbers, *event_reverse_numbers),
        )
        edges = person_edges + container_edges
        connected_people = max(0, selected_people - full_people)
        total_entities = selected_people + organizations + events + references

        # Planning estimate calibrated conservatively from the current graph.
        estimated_bytes = (
            full_people * 1200
            + connected_people * 500
            + organizations * 950
            + events * 750
            + references * 300
            + edges * 750
        )
        return {
            "threshold": threshold,
            "full_people": full_people,
            "connected_people": connected_people,
            "organizations": organizations,
            "events": events,
            "reference_entities": references,
            "total_entities": total_entities,
            "edges": edges,
            "estimated_graph_gib": round(estimated_bytes / (1024 ** 3), 2),
            "estimated_graph_high_gib": round(estimated_bytes * 1.6 / (1024 ** 3), 2),
        }

    def build_report(self, thresholds: list[int]) -> dict:
        self.db.execute("ANALYZE")
        self.db.commit()
        fact_counts: dict[str, dict[str, int]] = {}
        rank_names = {0: "deprecated", 1: "normal", 2: "preferred"}
        rows = self.db.execute(
            """
            SELECT property, rank, COUNT(*)
            FROM (
                SELECT property, rank FROM person_facts
                UNION ALL
                SELECT property, rank FROM container_facts
            )
            GROUP BY property, rank
            ORDER BY property, rank
            """
        )
        for property_number, rank, count in rows:
            property_id = f"P{property_number}"
            fact_counts.setdefault(property_id, {})[rank_names[rank]] = int(count)
        return {
            "generated_at": utc_now(),
            "schema_version": SCHEMA_VERSION,
            "edge_count_semantics": EDGE_COUNT_SEMANTICS,
            "index_size_gib": round(self.size_bytes() / (1024 ** 3), 3),
            "source": self.get_metadata().get("source"),
            "fact_statement_counts": fact_counts,
            "thresholds": [
                self.report_for_threshold(threshold)
                for threshold in sorted(set(thresholds))
            ],
        }

    def finish(self) -> None:
        self.db.execute("PRAGMA optimize")
        self.db.commit()
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def close(self) -> None:
        self.db.close()


def source_metadata(source: str) -> dict[str, object]:
    if not _PINNED_DUMP_RE.fullmatch(source):
        raise ValueError(
            "Source must be an official pinned Wikidata all.json.bz2 URL"
        )
    request = Request(
        source,
        method="HEAD",
        headers={"User-Agent": "HumansMap-capacity/1.0"},
    )
    with urlopen(request, timeout=120) as response:
        size = response.headers.get("Content-Length")
        return {
            "url": response.geturl(),
            "size": int(size) if size and size.isdigit() else None,
            "etag": response.headers.get("ETag"),
            "last_modified": response.headers.get("Last-Modified"),
        }


def open_hashed_dump(source: str) -> tuple[io.TextIOWrapper, HashingReader]:
    request = Request(source, headers={"User-Agent": "HumansMap-capacity/1.0"})
    response = urlopen(request, timeout=120)
    hashing_reader = HashingReader(response)
    decompressed = bz2.BZ2File(hashing_reader)
    return io.TextIOWrapper(decompressed, encoding="utf-8"), hashing_reader


def rss_mib() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def enforce_capacity(path: Path, max_rss_mib: int, min_free_gib: int) -> None:
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


def remove_index_files(path: Path) -> None:
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        if candidate.exists():
            candidate.unlink()


def run_capacity_pass(args: argparse.Namespace) -> dict:
    thresholds = sorted(set(args.thresholds))
    if not thresholds or min(thresholds) < 1:
        raise ValueError("At least one positive threshold is required")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", args.expected_sha1):
        raise ValueError("expected SHA1 must be exactly 40 hexadecimal characters")
    min_floor = min(thresholds)
    metadata = source_metadata(args.source)
    index_path = args.index.resolve()
    logger.info(
        "Starting capacity pass | source=%s | compressed_bytes=%s | "
        "thresholds=%s | index=%s",
        args.source,
        metadata.get("size"),
        ",".join(str(threshold) for threshold in thresholds),
        index_path,
    )

    if index_path.exists() and args.restart:
        remove_index_files(index_path)

    index = CapacityIndex(index_path)
    previous = index.get_metadata()
    identity = {
        "metadata": metadata,
        "expected_sha1": args.expected_sha1.lower(),
    }
    if previous:
        if previous.get("source") != identity:
            index.close()
            raise RuntimeError(
                "Existing index belongs to another or incomplete run; use a new path "
                "or explicitly pass --restart"
            )
        if previous.get("status") == "complete":
            indexed_floor = previous.get("min_floor")
            if not isinstance(indexed_floor, int) or min_floor < indexed_floor:
                index.close()
                raise RuntimeError(
                    f"Index retained person facts only down to {indexed_floor}; "
                    "use a new path or explicitly pass --restart"
                )
            report = index.build_report(thresholds)
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
            index.close()
            return report
        index.close()
        raise RuntimeError(
            "Existing capacity index is incomplete; explicitly pass --restart"
        )

    index.set_metadata(
        schema_version=SCHEMA_VERSION,
        status="running",
        started_at=utc_now(),
        source=identity,
        min_floor=min_floor,
    )
    index.commit()
    enforce_capacity(index_path, args.max_rss_mib, args.min_free_gib)

    total_entities = 0
    parsed_candidates = 0
    retained_entities = 0
    malformed = 0
    started = time.time()
    stream = None

    try:
        stream, hashing_reader = open_hashed_dump(args.source)
        for raw in stream:
            stripped = raw.strip()
            if not stripped or stripped in ("[", "]"):
                continue
            if stripped.endswith(","):
                stripped = stripped[:-1]
            total_entities += 1

            if '"P31"' not in stripped or not CLASS_MARKER_RE.search(stripped):
                if total_entities % args.progress_every == 0:
                    index.commit()
                    enforce_capacity(index_path, args.max_rss_mib, args.min_free_gib)
                    elapsed = max(time.time() - started, 0.001)
                    logger.info(
                        "%dM entities | %d candidates | %d retained | %.2f GiB index | "
                        "%.0f MiB RSS | %.0f entities/s",
                        total_entities // 1_000_000,
                        parsed_candidates,
                        retained_entities,
                        index.size_bytes() / (1024 ** 3),
                        rss_mib(),
                        total_entities / elapsed,
                    )
                continue

            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError as exc:
                malformed += 1
                raise RuntimeError(
                    f"Malformed JSON entity at input entity {total_entities}"
                ) from exc

            parsed_candidates += 1
            if index.record(obj, min_floor):
                retained_entities += 1

            if parsed_candidates % args.commit_every == 0:
                index.commit()

            if total_entities % args.progress_every == 0:
                index.commit()
                enforce_capacity(index_path, args.max_rss_mib, args.min_free_gib)

        index.commit()
        actual_sha1 = hashing_reader.hexdigest()
        if actual_sha1.lower() != args.expected_sha1.lower():
            raise RuntimeError(
                f"SHA1 mismatch: expected {args.expected_sha1}, got {actual_sha1}"
            )

        index.set_metadata(
            status="complete",
            completed_at=utc_now(),
            actual_sha1=actual_sha1,
            total_entities=total_entities,
            parsed_candidates=parsed_candidates,
            retained_entities=retained_entities,
            malformed_entities=malformed,
            elapsed_seconds=round(time.time() - started, 3),
        )
        index.finish()
        report = index.build_report(thresholds)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        return report
    except Exception:
        index.set_metadata(
            status="failed",
            failed_at=utc_now(),
            total_entities=total_entities,
            parsed_candidates=parsed_candidates,
            retained_entities=retained_entities,
            malformed_entities=malformed,
        )
        index.commit()
        raise
    finally:
        if stream is not None:
            stream.close()
        index.close()


def parse_thresholds(value: str) -> list[int]:
    try:
        thresholds = [int(part.strip()) for part in value.split(",") if part.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("thresholds must be comma-separated integers") from exc
    if not thresholds or any(threshold < 1 for threshold in thresholds):
        raise argparse.ArgumentTypeError("thresholds must contain positive integers")
    return thresholds


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a memory-bounded global Wikidata capacity index"
    )
    parser.add_argument("--source", required=True)
    parser.add_argument("--expected-sha1", required=True)
    parser.add_argument(
        "--index",
        type=Path,
        default=Path("/data/ingestion/global-capacity.sqlite3"),
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("/data/ingestion/global-capacity-report.json"),
    )
    parser.add_argument(
        "--log",
        type=Path,
        default=Path("/data/ingestion/global-capacity.log"),
    )
    parser.add_argument(
        "--thresholds",
        type=parse_thresholds,
        default=parse_thresholds("10,20,30,50"),
    )
    parser.add_argument("--commit-every", type=int, default=10_000)
    parser.add_argument("--progress-every", type=int, default=1_000_000)
    parser.add_argument("--max-rss-mib", type=int, default=900)
    parser.add_argument("--min-free-gib", type=int, default=25)
    parser.add_argument(
        "--restart",
        action="store_true",
        help="Delete an existing incomplete index and restart from entity zero",
    )
    args = parser.parse_args()

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if args.log:
        args.log.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(args.log, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    report = run_capacity_pass(args)
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
