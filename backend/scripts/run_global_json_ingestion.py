#!/usr/bin/env python3
"""Run a resumable, candidate-only full Wikidata JSON import.

This program intentionally does not create, replace, or promote databases.
It reuses the completed capacity index, streams the pinned remote dump once for candidate materialization, and stores an auditable run-state document in INGESTION_STATE_DIR.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/app")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.load_from_dump import source_metadata, validate_dump_source

DEFAULT_MANIFEST = "/app/config/ingestion/global-json-v1.json"
DEFAULT_STATE_DIR = "/data/ingestion"
STATE_NAME = "global-json-ingestion-state.json"
CAPACITY_INDEX_NAME = "global-capacity.sqlite3"
CAPACITY_REPORT_NAME = "global-capacity-report.json"
EXPECTED_EDGE_COUNT_SEMANTICS = "materialized-truthy-typed-v1"
MIN_POST_IMPORT_FREE_GIB = 20
ENRICHMENT_WORKING_HEADROOM_GIB = 8


def assert_global_execution_enabled(manifest: dict) -> None:
    """Reject global runs until the backup and candidate-database gates are approved."""
    execution = manifest.get("execution") or {}
    if execution.get("enabled") is not True:
        reason = execution.get("blocked_reason", "global execution is not approved")
        raise RuntimeError(
            "Global ingestion is deliberately disabled: " + reason + ". "
            "See the project design notes before enabling it."
        )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Expected an object in {path}")
    return data


def write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def manifest_fingerprint(manifest: dict) -> str:
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_manifest(manifest: dict) -> None:
    if manifest.get("version") != "global-json-v1":
        raise ValueError("Manifest version must be global-json-v1")
    selection = manifest.get("selection")
    source = manifest.get("source")
    if not isinstance(selection, dict) or not isinstance(source, dict):
        raise ValueError("Manifest requires source and selection objects")
    if source.get("format") != "json" or source.get("url_env") != "INGESTION_DUMP_URL":
        raise ValueError("Manifest must use JSON and INGESTION_DUMP_URL")
    if selection.get("person_scope") != "global":
        raise ValueError("Global manifest must set selection.person_scope to global")
    if selection.get("min_statements") != 10:
        raise ValueError("Global manifest must set selection.min_statements to 10")


def candidate_database() -> str:
    database = os.getenv("ARANGO_DB", "").strip()
    if not database.startswith("world_graph_candidate_"):
        raise ValueError(
            "ARANGO_DB must name a disposable candidate database, for example "
            "world_graph_candidate_20260718; production is never an ingestion target"
        )
    return database


def source_identity(metadata: dict) -> dict:
    return {key: metadata.get(key) for key in ("source", "size", "etag", "last_modified")}


def resolve_source() -> tuple[str, dict]:
    source = os.getenv("INGESTION_DUMP_URL", "").strip()
    if not source:
        raise ValueError("INGESTION_DUMP_URL must contain a pinned HTTPS JSON dump URL")
    validate_dump_source(source)
    metadata = source_metadata(source)
    return source, metadata


def assert_capacity_ready(
    state_dir: Path,
    source: str,
    threshold: int,
) -> Path:
    """Require the completed capacity scan for this exact dump and threshold."""
    index_path = state_dir / CAPACITY_INDEX_NAME
    report_path = state_dir / CAPACITY_REPORT_NAME
    if not index_path.exists() or not report_path.exists():
        raise RuntimeError(
            "A completed capacity pass is required before materialization; "
            f"expected {index_path} and {report_path}"
        )
    report = read_json(report_path)
    if report.get("edge_count_semantics") != EXPECTED_EDGE_COUNT_SEMANTICS:
        raise RuntimeError(
            "Capacity report predates the corrected materialized-edge query; "
            "rerun the SQLite-only report before materialization"
        )
    source_record = report.get("source") or {}
    source_metadata = source_record.get("metadata") or source_record
    if (source_metadata.get("url") or source_metadata.get("source")) != source:
        raise RuntimeError(
            "Capacity report source does not match INGESTION_DUMP_URL; "
            "start a new capacity pass for this pinned dump"
        )
    threshold_reports = {
        int(item["threshold"]): item
        for item in report.get("thresholds", [])
        if isinstance(item, dict) and "threshold" in item
    }
    if threshold not in threshold_reports:
        raise RuntimeError(
            f"Capacity report has no threshold-{threshold} result; "
            "rerun the capacity pass with that threshold"
        )
    db = sqlite3.connect(index_path)
    try:
        metadata = {
            key: json.loads(value)
            for key, value in db.execute("SELECT key, value FROM metadata")
        }
    finally:
        db.close()
    if metadata.get("status") != "complete":
        raise RuntimeError("Capacity index is not complete")
    index_source = metadata.get("source") or {}
    index_source_metadata = index_source.get("metadata") or index_source
    if (index_source_metadata.get("url") or index_source_metadata.get("source")) != source:
        raise RuntimeError(
            "Capacity index source does not match INGESTION_DUMP_URL; "
            "start a new capacity pass for this pinned dump"
        )
    estimate = float(threshold_reports[threshold].get("estimated_graph_high_gib", 0))
    required_free_gib = (
        estimate + MIN_POST_IMPORT_FREE_GIB + ENRICHMENT_WORKING_HEADROOM_GIB
    )
    free_gib = shutil.disk_usage(state_dir).free / (1024 ** 3)
    if free_gib < required_free_gib:
        raise RuntimeError(
            "Insufficient disk headroom for candidate materialization: "
            f"{free_gib:.1f} GiB free, {required_free_gib:.1f} GiB required "
            f"({estimate:.1f} GiB high estimate + "
            f"{ENRICHMENT_WORKING_HEADROOM_GIB} GiB enrichment workspace + "
            f"{MIN_POST_IMPORT_FREE_GIB} GiB reserve)"
        )
    return index_path


def run_materializer(
    source: str,
    state_dir: Path,
    manifest: dict,
    index_path: Path,
    *,
    finalize_only: bool = False,
) -> None:
    threshold = int(manifest["selection"]["min_statements"])
    command = [
        sys.executable,
        "/app/scripts/materialize_global_candidate.py",
        "--source", source,
        "--capacity-index", str(index_path),
        "--threshold", str(threshold),
        "--state", str(state_dir / "materializer-state.json"),
        "--max-rss-mib", "900",
        "--min-free-gib", "20",
    ]
    if finalize_only:
        command.append("--finalize-only")
    print("Starting candidate materialization: " + " ".join(command), flush=True)
    subprocess.run(command, env=os.environ.copy(), check=True)


def assert_materializer_state_matches(
    state: dict, database: str, source: str, threshold: int
) -> None:
    """Reject a finalization checkpoint copied from any other candidate run."""
    expected = {"database": database, "source": source, "threshold": threshold}
    actual = {key: state.get(key) for key in expected}
    if actual != expected:
        raise RuntimeError(
            "Refusing a stale materializer recovery checkpoint: "
            f"expected {expected}, found {actual}"
        )


def assert_existing_state_matches(state: dict, fingerprint: str, metadata: dict, database: str) -> None:
    if state.get("manifest_sha256") != fingerprint:
        raise ValueError("Existing state belongs to a different manifest; use a new state directory")
    if state.get("database") != database:
        raise ValueError("Existing state belongs to a different candidate database")
    if state.get("source") != source_identity(metadata):
        raise ValueError("Dump source metadata changed between passes; start a new candidate run")


def main() -> None:
    parser = argparse.ArgumentParser(description="Candidate-only global Wikidata JSON importer")
    parser.add_argument("--manifest", default=os.getenv("INGESTION_MANIFEST", DEFAULT_MANIFEST))
    parser.add_argument("--preflight", action="store_true", help="Validate configuration without reading dump data or writing ArangoDB")
    args = parser.parse_args()

    manifest_path = Path(args.manifest)
    manifest = read_json(manifest_path)
    validate_manifest(manifest)
    assert_global_execution_enabled(manifest)
    database = candidate_database()
    source, metadata = resolve_source()
    fingerprint = manifest_fingerprint(manifest)
    state_dir = Path(os.getenv("INGESTION_STATE_DIR", DEFAULT_STATE_DIR))
    state_path = state_dir / STATE_NAME
    threshold = int(manifest["selection"]["min_statements"])
    capacity_index = assert_capacity_ready(state_dir, source, threshold)

    print(json.dumps({"database": database, "manifest": str(manifest_path), "source": source_identity(metadata)}, sort_keys=True), flush=True)
    if args.preflight:
        print("Preflight complete; no dump data or database was touched", flush=True)
        return

    state = read_json(state_path) if state_path.exists() else None
    if state:
        assert_existing_state_matches(state, fingerprint, metadata, database)
        phase = state.get("phase")
        if phase == "materialization_complete":
            print("Import already completed; refusing to run it again", flush=True)
            return
        if phase in {"materialization_running", "failed", "finalization_running"}:
            materializer_state_path = state_dir / "materializer-state.json"
            materializer_state = (
                read_json(materializer_state_path)
                if materializer_state_path.exists() else {}
            )
            materializer_phase = materializer_state.get("phase")
            if materializer_phase in {"finalization_pending", "complete"}:
                assert_materializer_state_matches(
                    materializer_state, database, source, threshold
                )
            if materializer_phase == "finalization_pending":
                state.update({"phase": "finalization_running", "updated_at": utc_now()})
                write_json_atomic(state_path, state)
                try:
                    run_materializer(
                        source, state_dir, manifest, capacity_index,
                        finalize_only=True,
                    )
                except Exception:
                    state.update({"phase": "failed", "updated_at": utc_now()})
                    write_json_atomic(state_path, state)
                    raise
                state.update({
                    "phase": "materialization_complete",
                    "completed_at": utc_now(),
                    "updated_at": utc_now(),
                })
                write_json_atomic(state_path, state)
                print("Candidate finalization resumed and completed; it has not been promoted", flush=True)
                return
            if materializer_phase == "complete":
                state.update({
                    "phase": "materialization_complete",
                    "completed_at": utc_now(),
                    "updated_at": utc_now(),
                })
                write_json_atomic(state_path, state)
                print("Candidate import was already finalized; repaired orchestrator state", flush=True)
                return
            raise RuntimeError(
                "Materialization stopped before the safe finalization checkpoint. "
                "Discard the partial candidate database and start a new run."
            )
    else:
        state = {
            "version": 1,
            "created_at": utc_now(),
            "database": database,
            "manifest_sha256": fingerprint,
            "source": source_identity(metadata),
            "phase": "created",
        }
        write_json_atomic(state_path, state)

    state.update({"phase": "materialization_running", "updated_at": utc_now()})
    write_json_atomic(state_path, state)
    try:
        run_materializer(source, state_dir, manifest, capacity_index)
    except Exception:
        state.update({"phase": "failed", "updated_at": utc_now()})
        write_json_atomic(state_path, state)
        raise
    state.update({"phase": "materialization_complete", "completed_at": utc_now(), "updated_at": utc_now()})
    write_json_atomic(state_path, state)
    print("Candidate import completed; it has not been promoted", flush=True)


if __name__ == "__main__":
    main()
