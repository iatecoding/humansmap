"""Unit tests for the bounded global candidate materializer."""

import argparse
import json

from scripts.global_capacity_pass import CapacityIndex
from scripts.materialize_enrichment import EnrichmentStore
from scripts.run_global_json_ingestion import assert_materializer_state_matches
from scripts.materialize_global_candidate import (
    QidBitset,
    _compact_reference_doc,
    _cleanup_orphans,
    _qid_number_from_line,
    _read_state,
    _write_state,
    build_chosen_table,
    finalize_existing,
)


def _claim(qid: str, rank: str = "normal") -> dict:
    return {
        "rank": rank,
        "mainsnak": {
            "snaktype": "value",
            "datavalue": {
                "type": "wikibase-entityid",
                "value": {"id": qid},
            },
        },
    }


def _entity(qid: str, p31: str, *, facts=None, statements=10) -> dict:
    claims = {"P31": [_claim(p31)]}
    for prop, targets in (facts or {}).items():
        claims[prop] = [_claim(target) for target in targets]
    current = sum(len(values) for values in claims.values())
    if current < statements:
        claims["P999999"] = [
            {"rank": "normal", "mainsnak": {"snaktype": "novalue"}}
            for _ in range(statements - current)
        ]
    return {
        "id": qid,
        "claims": claims,
        "labels": {"en": {"value": qid}},
    }


def test_qid_line_parser_only_reads_entity_id_prefix():
    assert _qid_number_from_line('{"id":"Q123","claims":{}}') == 123
    assert _qid_number_from_line("[") is None


def test_compact_reference_uses_qid_when_wikidata_has_no_label():
    doc = _compact_reference_doc(
        "Q123",
        {"labels": {}, "descriptions": {}, "p31": []},
        "2026-09-09T00:00:00+00:00",
    )

    assert doc["name_en"] == "Q123"


def test_chosen_table_and_bitset_are_exact_and_disk_backed(tmp_path):
    index_path = tmp_path / "capacity.sqlite3"
    index = CapacityIndex(index_path)
    try:
        # Q1 is directly eligible and points to Q100. Q100 points to the
        # lower-notability person Q2 through an organization reverse property.
        index.record(_entity("Q1", "Q5", facts={"P108": ["Q100"]}), 10)
        index.record(_entity("Q2", "Q5", statements=2), 10)
        index.record(
            _entity("Q100", "Q4830453", facts={"P169": ["Q1"], "P112": ["Q2"]}),
            10,
        )
        index.commit()
    finally:
        index.close()

    counts = build_chosen_table(index_path, 10)
    assert counts["people"] == 2
    assert counts["organizations"] == 1

    bitset_path = tmp_path / "selected.bitset"
    bitset = QidBitset.create_from_index(index_path, bitset_path)
    try:
        assert bitset.contains(1)
        assert bitset.contains(2)
        assert bitset.contains(100)
        assert not bitset.contains(101)
    finally:
        bitset.close()


def _parsed_entity(*, direct_count=100, props=None, en="Example", it=None):
    return {
        "labels": {"en": en, "it": it},
        "direct_count": direct_count,
        "props": props or {},
    }


def test_enrichment_store_skips_empty_pending_rows(tmp_path):
    store = EnrichmentStore(tmp_path / "enrichment.sqlite3")
    try:
        store.pending_for_entity("Q1", _parsed_entity(props={}), "person")
        store.pending_for_entity("Q2", _parsed_entity(props={}), "organization")
        store.pending_for_entity("Q3", _parsed_entity(props={}), "event")
        store.flush()
        assert store.db.execute("SELECT COUNT(*) FROM pending").fetchone()[0] == 0
    finally:
        store.close()


def test_enrichment_preserves_legacy_person_org_and_event_fields(tmp_path):
    store = EnrichmentStore(tmp_path / "enrichment.sqlite3")
    try:
        labels = {
            "Q10": ("Rome", "Roma", 20),
            "Q11": ("Paris", "Parigi", 30),
            "Q12": ("natural causes", None, 5),
            "Q13": ("Award A", None, 10),
            "Q14": ("Award B", None, 50),
            "Q15": ("Work A", None, 10),
            "Q16": ("Christianity", None, 10),
            "Q17": ("Jazz", None, 10),
            "Q18": ("Piano", None, 10),
            "Q19": ("Italian", None, 10),
            "Q20": ("World War II", None, 10),
            "Q21": ("Milan", "Milano", 10),
            "Q22": ("Cemetery", "Cimitero", 10),
            "Q23": ("Studio", None, 10),
            "Q24": ("Technology", None, 10),
            "Q25": ("Widget", None, 10),
            "Q26": ("corporation", None, 10),
            "Q27": ("Italy", "Italia", 10),
        }
        for qid, (en, it, statements) in labels.items():
            store.record_entity(
                qid,
                _parsed_entity(direct_count=statements, en=en, it=it),
            )
        person = _parsed_entity(
            props={
                "P19": ["Q10"], "P20": ["Q11"], "P509": ["Q12"],
                "P166": ["Q13", "Q14"], "P800": ["Q15"],
                "P140": ["Q16"], "P136": ["Q17"], "P1303": ["Q18"],
                "P1412": ["Q19"], "P607": ["Q20"], "P551": ["Q21"],
                "P119": ["Q22"], "P937": ["Q23"],
            }
        )
        organization = _parsed_entity(
            props={
                "P159": ["Q21"], "P17": ["Q27"], "P452": ["Q24"],
                "P1056": ["Q25"], "P1454": ["Q26"],
            }
        )
        event = _parsed_entity(props={"P276": ["Q21"], "P495": ["Q27"]})
        store.pending_for_entity("Q1", person, "person")
        store.pending_for_entity("Q2", organization, "organization")
        store.pending_for_entity("Q3", event, "event")
        store.flush()

        pending = {
            f"Q{number}": json.loads(payload)
            for number, payload in store.db.execute("SELECT qid, payload FROM pending")
        }
        person_update = store._person_update("Q1", 100, pending["Q1"])
        assert person_update["cause_of_death"] == "natural causes"
        assert person_update["awards_count"] == 2
        assert person_update["awards_top5"] == ["Award B", "Award A"]
        assert person_update["notable_works"] == ["Work A"]
        assert person_update["religion"] == "Christianity"
        assert person_update["genres"] == ["Jazz"]
        assert person_update["instruments"] == ["Piano"]
        assert person_update["languages_spoken"] == ["Italian"]
        assert person_update["conflicts"] == ["World War II"]
        assert person_update["residence"] == "Milano"
        assert person_update["burial_place"] == "Cimitero"
        assert person_update["work_location"] == "Studio"

        org_update = store._org_update("Q2", pending["Q2"])
        assert org_update["headquarters"] == "Milan"
        assert org_update["country"] == "Italy"
        assert org_update["industries"] == ["Technology"]
        assert org_update["products"] == ["Widget"]
        assert org_update["legal_form"] == "corporation"

        event_update = store._event_update("Q3", pending["Q3"])
        assert event_update["location"] == "Milan"
        assert event_update["country"] == "Italy"
    finally:
        store.close()


def _finalize_args(tmp_path):
    return argparse.Namespace(
        source="https://example.test/wikidata.json.bz2",
        threshold=10,
        state=tmp_path / "materializer-state.json",
        arango_request_timeout=1800,
        cleanup_batch_size=50_000,
        adopt_existing=False,
    )


def test_finalization_checkpoint_is_atomic_and_resumable(tmp_path, monkeypatch):
    args = _finalize_args(tmp_path)
    monkeypatch.setenv("ARANGO_DB", "world_graph_candidate_test")
    _write_state(args.state, {
        "version": 1,
        "database": "world_graph_candidate_test",
        "source": args.source,
        "threshold": 10,
        "phase": "finalization_pending",
        "stats": {"persons": 3},
    })
    fake_db = object()
    monkeypatch.setattr(
        "scripts.materialize_global_candidate._candidate_db",
        lambda *, request_timeout: fake_db,
    )
    monkeypatch.setattr(
        "scripts.materialize_global_candidate._finalize_candidate",
        lambda db, **kwargs: {"orphan_edges_removed": 2},
    )

    result = finalize_existing(args)

    assert result == {"persons": 3, "orphan_edges_removed": 2}
    state = _read_state(args.state)
    assert state["phase"] == "complete"
    assert state["stats"] == result
    assert not list(tmp_path.glob(".materializer-state.json.*"))


def test_finalization_refuses_unsafe_streaming_checkpoint(tmp_path, monkeypatch):
    args = _finalize_args(tmp_path)
    monkeypatch.setenv("ARANGO_DB", "world_graph_candidate_test")
    _write_state(args.state, {
        "version": 1,
        "database": "world_graph_candidate_test",
        "source": args.source,
        "threshold": 10,
        "phase": "streaming",
    })

    import pytest
    with pytest.raises(RuntimeError, match="only safe after"):
        finalize_existing(args)


def test_cleanup_covers_every_edge_collection_and_org_type_contract():
    class FakeAql:
        def __init__(self):
            self.queries = []

        def execute(self, query, bind_vars=None):
            self.queries.append(query)
            if "RETURN e._key" in query:
                return iter(["edge-1"])
            return iter([0])

    class FakeDb:
        aql = FakeAql()

    db = FakeDb()
    assert _cleanup_orphans(db, batch_size=2) == 0
    assert len(db.aql.queries) == 15
    assert "FOR e IN @@collection" in db.aql.queries[-1]
    assert "organizations/" in db.aql.queries[-1]
    assert "LIMIT @batch_size" in db.aql.queries[-1]


def test_orchestrator_rejects_checkpoint_from_another_run():
    import pytest

    state = {
        "database": "world_graph_candidate_old",
        "source": "https://example.test/old.json.bz2",
        "threshold": 20,
    }
    with pytest.raises(RuntimeError, match="stale materializer"):
        assert_materializer_state_matches(
            state,
            "world_graph_candidate_new",
            "https://example.test/new.json.bz2",
            10,
        )
