"""Pure tests for the global candidate validator."""

import json
import sqlite3

from scripts.validate_global_candidate import (
    completed_materialization,
    expected_counts,
    expected_edge_contract,
    scan_edge_collection,
)


def test_expected_counts_reads_materialization_metadata(tmp_path):
    path = tmp_path / "capacity.sqlite3"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    expected = {
        "threshold": 10,
        "selected": 4,
        "people": 1,
        "organizations": 1,
        "events": 1,
        "references": 1,
    }
    db.execute(
        "INSERT INTO metadata(key, value) VALUES (?, ?)",
        ("materialization", json.dumps(expected)),
    )
    db.commit()
    db.close()

    assert expected_counts(path) == expected


def test_completed_materialization_reads_realized_counts(tmp_path):
    path = tmp_path / "materializer-state.json"
    path.write_text(json.dumps({
        "phase": "complete",
        "database": "world_graph_candidate_20260908",
        "stats": {
            "persons": 3,
            "organizations": 2,
            "events": 1,
            "references": 4,
        },
    }))

    counts, stats = completed_materialization(
        path,
        "world_graph_candidate_20260908",
    )

    assert counts == {
        "persons": 3,
        "organizations": 2,
        "events": 1,
        "references": 4,
    }
    assert stats["persons"] == 3


def test_expected_edge_contract_covers_all_enabled_collections():
    contract = expected_edge_contract()

    assert {name: len(values) for name, values in contract.items()} == {
        "relationships": 10,
        "affiliations": 11,
        "involvements": 3,
        "org_structure": 4,
    }
    assert ("persons", "persons", "spouse", "P26") in contract["relationships"]
    assert ("persons", "organizations", "chief_executive", "P169") in contract["affiliations"]
    assert ("persons", "events", "participant", "P710") in contract["involvements"]
    assert ("organizations", "organizations", "owned_by", "P127") in contract["org_structure"]


def test_edge_scan_aggregates_bounded_cursor_batches():
    class FakeAql:
        def __init__(self):
            self.calls = []

        def execute(self, _query, bind_vars):
            self.calls.append(bind_vars)
            if bind_vars["after"] == "":
                return iter([{
                    "last_key": "b",
                    "scanned": 2,
                    "orphans": 1,
                    "groups": [{
                        "from_collection": "persons",
                        "to_collection": "persons",
                        "edge_type": "spouse",
                        "property": "P26",
                        "count": 2,
                    }],
                }])
            return iter([{
                "last_key": "c",
                "scanned": 1,
                "orphans": 0,
                "groups": [{
                    "from_collection": "persons",
                    "to_collection": "persons",
                    "edge_type": "spouse",
                    "property": "P26",
                    "count": 1,
                }],
            }])

    class FakeDb:
        aql = FakeAql()

    fake_db = FakeDb()
    result = scan_edge_collection(fake_db, "relationships", 2)

    assert result["scanned"] == 3
    assert result["orphans"] == 1
    assert result["batches"] == 2
    assert result["groups"][0]["count"] == 3
    assert [call["after"] for call in fake_db.aql.calls] == ["", "b"]
