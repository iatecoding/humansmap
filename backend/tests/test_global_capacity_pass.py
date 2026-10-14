"""Tests for the disk-backed global Wikidata capacity pass."""

import bz2
import hashlib
import io

from scripts.global_capacity_pass import (
    CapacityIndex,
    HashingReader,
    PERSON_KIND,
    claim_qids,
    fact_rows,
    qid_number,
    statement_qid,
)


def _claim(qid: str, *, rank: str = "normal") -> dict:
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


def _entity(
    qid: str,
    entity_class: str,
    *,
    facts: dict[str, list[str]] | None = None,
    statement_count: int = 1,
    label: str | None = "Example",
) -> dict:
    claims = {"P31": [_claim(entity_class)]}
    for property_id, targets in (facts or {}).items():
        claims[property_id] = [_claim(target) for target in targets]

    current_count = sum(len(statements) for statements in claims.values())
    if current_count < statement_count:
        claims["P999999"] = [
            {
                "rank": "normal",
                "mainsnak": {"snaktype": "novalue"},
            }
            for _ in range(statement_count - current_count)
        ]

    labels = {"en": {"value": label}} if label else {}
    return {"id": qid, "claims": claims, "labels": labels}


def test_qid_helpers_ignore_invalid_and_deprecated_values():
    assert qid_number("Q42") == 42
    assert qid_number("Q0") is None
    assert qid_number("P42") is None
    assert statement_qid(_claim("Q7")) == 7
    assert statement_qid(_claim("Q7", rank="deprecated")) is None
    assert claim_qids(
        {"P31": [_claim("Q5"), _claim("Q5"), _claim("Q7", rank="deprecated")]},
        "P31",
    ) == {5}


def test_claim_qids_uses_truthy_preferred_rank_selection():
    assert claim_qids(
        {"P31": [_claim("Q5"), _claim("Q4830453", rank="preferred")]},
        "P31",
    ) == {4830453}


def test_capacity_report_keeps_full_people_and_one_hop_connections(tmp_path):
    index = CapacityIndex(tmp_path / "capacity.sqlite3")
    try:
        # Q1 qualifies as a full person. Its employer pulls Q100 into the
        # selection. The selected organization then pulls its low-statement
        # founder Q2 and parent organization Q200 into the reusable graph.
        person = _entity(
            "Q1",
            "Q5",
            facts={"P108": ["Q100"]},
            statement_count=20,
        )
        person["claims"]["P108"].append(_claim("Q999", rank="deprecated"))
        assert index.record(person, min_floor=10)
        assert index.record(
            _entity("Q2", "Q5", statement_count=2),
            min_floor=10,
        )
        assert index.record(
            _entity(
                "Q100",
                "Q4830453",
                facts={"P169": ["Q1"], "P112": ["Q2"], "P749": ["Q200"]},
                statement_count=8,
            ),
            min_floor=10,
        )
        assert index.record(
            _entity("Q200", "Q4830453", statement_count=3),
            min_floor=10,
        )
        index.commit()

        report = index.report_for_threshold(20)

        assert report["full_people"] == 1
        assert report["connected_people"] == 1
        assert report["organizations"] == 2
        assert report["events"] == 0
        assert report["reference_entities"] == 0
        assert report["total_entities"] == 4
        assert report["edges"] == 4
        assert index.db.execute(
            "SELECT COUNT(*) FROM person_facts WHERE rank=0"
        ).fetchone()[0] == 1
    finally:
        index.close()


def test_capacity_report_requires_a_label_for_full_people(tmp_path):
    index = CapacityIndex(tmp_path / "capacity.sqlite3")
    try:
        index.record(
            _entity("Q1", "Q5", statement_count=50, label=None),
            min_floor=10,
        )
        index.commit()

        report = index.report_for_threshold(10)

        assert report["full_people"] == 0
        assert report["total_entities"] == 0
        kind = index.db.execute(
            "SELECT kind FROM entities WHERE qid=1"
        ).fetchone()[0]
        assert kind == PERSON_KIND
    finally:
        index.close()


def test_capacity_edge_count_only_uses_chosen_sources(tmp_path):
    index = CapacityIndex(tmp_path / "capacity.sqlite3")
    try:
        index.record(
            _entity("Q1", "Q5", facts={"P108": ["Q100"]}, statement_count=10),
            min_floor=1,
        )
        index.record(
            _entity("Q2", "Q5", facts={"P108": ["Q100"]}, statement_count=2),
            min_floor=1,
        )
        index.record(_entity("Q100", "Q4830453"), min_floor=1)
        index.commit()

        report = index.report_for_threshold(10)

        assert report["full_people"] == 1
        assert report["edges"] == 1
    finally:
        index.close()


def test_capacity_edge_count_matches_truthy_rank_selection(tmp_path):
    index = CapacityIndex(tmp_path / "capacity.sqlite3")
    try:
        person = _entity("Q1", "Q5", statement_count=10)
        person["claims"]["P108"] = [
            _claim("Q100"),
            _claim("Q200", rank="preferred"),
        ]
        index.record(person, min_floor=1)
        index.record(_entity("Q100", "Q4830453"), min_floor=1)
        index.record(_entity("Q200", "Q4830453"), min_floor=1)
        index.commit()

        report = index.report_for_threshold(10)

        # parse_json_entity/build_edge_docs suppress the normal P108 statement
        # when the same property has a preferred statement.
        assert report["edges"] == 1
    finally:
        index.close()


def test_capacity_edge_count_requires_materializable_endpoint_types(tmp_path):
    index = CapacityIndex(tmp_path / "capacity.sqlite3")
    try:
        person = _entity(
            "Q1",
            "Q5",
            facts={"P26": ["Q100"], "P108": ["Q200"], "P1344": ["Q300"]},
            statement_count=10,
        )
        index.record(person, min_floor=1)
        # Every target has the wrong kind for its edge collection.
        index.record(_entity("Q100", "Q4830453"), min_floor=1)
        index.record(_entity("Q200", "Q5"), min_floor=1)
        index.record(_entity("Q300", "Q4830453"), min_floor=1)
        index.commit()

        report = index.report_for_threshold(10)

        assert report["edges"] == 0
    finally:
        index.close()


def test_hashing_reader_hashes_the_exact_compressed_stream():
    compressed = bz2.compress(b'[{"id":"Q1"}]\n')
    reader = HashingReader(io.BytesIO(compressed))

    with bz2.BZ2File(reader) as stream:
        assert stream.read() == b'[{"id":"Q1"}]\n'

    assert reader.hexdigest() == hashlib.sha1(compressed).hexdigest()


def test_fact_rows_preserve_statement_identity_rank_and_qualifiers():
    statement = _claim("Q100", rank="preferred")
    statement["id"] = "Q1$statement-guid"
    statement["qualifiers"] = {
        "P580": [{
            "snaktype": "value",
            "datavalue": {
                "type": "time",
                "value": {"time": "+2018-01-01T00:00:00Z"},
            },
        }],
        "P585": [{
            "snaktype": "value",
            "datavalue": {
                "type": "time",
                "value": {"time": "+2020-06-30T00:00:00Z"},
            },
        }],
        "P1810": [{
            "snaktype": "value",
            "datavalue": {
                "type": "monolingualtext",
                "value": {"language": "en", "text": "Example role"},
            },
        }],
        "P1545": [{
            "snaktype": "value",
            "datavalue": {"type": "string", "value": "2"},
        }],
    }

    rows = fact_rows({"P108": [statement]})

    assert rows == [
        (
            108,
            100,
            "Q1$statement-guid",
            2,
            "2018-01-01",
            None,
            "2020-06-30",
            "Example role",
            "2",
        )
    ]
