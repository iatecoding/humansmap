"""Unit tests for Phase 4 edge extraction (plan §2a / §2b / 4.1 / 4.3).

Tests the new person↔person relationship types (P451, P184, P185, P1327) and
the org→org structure edge builder (build_org_structure_edge_docs).

Pure-function tests — no DB or dump needed.
Run in-container: ``docker compose exec backend pytest backend/tests/test_new_edges.py``
(host pytest is not installed — do not run on host).
"""

from scripts.load_from_dump import (
    EDGE_PROPERTIES,
    ORG_STRUCTURE_PROPERTIES,
    RELATIONSHIPS,
    AFFILIATIONS,
    ORG_STRUCTURE,
    build_org_structure_edge_docs,
)


# ── 4.1: EDGE_PROPERTIES contains the 4 new person↔person types ─────────────

def test_p451_unmarried_partner_in_edge_properties():
    assert "P451" in EDGE_PROPERTIES
    collection, edge_type = EDGE_PROPERTIES["P451"]
    assert collection == RELATIONSHIPS
    assert edge_type == "unmarried_partner"


def test_p184_doctoral_advisor_in_edge_properties():
    assert "P184" in EDGE_PROPERTIES
    collection, edge_type = EDGE_PROPERTIES["P184"]
    assert collection == RELATIONSHIPS
    assert edge_type == "doctoral_advisor"


def test_p185_doctoral_student_in_edge_properties():
    assert "P185" in EDGE_PROPERTIES
    collection, edge_type = EDGE_PROPERTIES["P185"]
    assert collection == RELATIONSHIPS
    assert edge_type == "doctoral_student"


def test_p1327_professional_collaborator_in_edge_properties():
    assert "P1327" in EDGE_PROPERTIES
    collection, edge_type = EDGE_PROPERTIES["P1327"]
    assert collection == RELATIONSHIPS
    assert edge_type == "professional_collaborator"


# ── 4.3: ORG_STRUCTURE_PROPERTIES map ───────────────────────────────────────

def test_org_structure_properties_keys():
    assert set(ORG_STRUCTURE_PROPERTIES.keys()) == {"P749", "P355", "P127", "P1830"}


def test_org_structure_property_directions():
    assert ORG_STRUCTURE_PROPERTIES["P749"] == (ORG_STRUCTURE, "parent_of")
    assert ORG_STRUCTURE_PROPERTIES["P355"] == (ORG_STRUCTURE, "subsidiary_of")
    assert ORG_STRUCTURE_PROPERTIES["P127"] == (ORG_STRUCTURE, "owned_by")
    assert ORG_STRUCTURE_PROPERTIES["P1830"] == (ORG_STRUCTURE, "owns")


# ── 4.3: build_org_structure_edge_docs ─────────────────────────────────────

def _make_entity(props: dict, stmt_nodes: dict | None = None) -> dict:
    """Build a minimal entity accumulator for testing."""
    return {
        "labels": {"en": "Test Org"},
        "descriptions": {},
        "p31": ["Q4830453"],  # business
        "props": props,
        "direct_count": 10,
        "stmt_nodes": stmt_nodes or {},
    }


def test_empty_entity_returns_no_edges():
    entity = _make_entity({})
    edges = build_org_structure_edge_docs("Q1", entity)
    assert edges == []


def test_p749_emits_parent_of_edge():
    entity = _make_entity({"P749": ["Q999"]})
    edges = build_org_structure_edge_docs("Q1", entity)
    assert len(edges) == 1
    e = edges[0]
    assert e["_from"] == "organizations/Q1"
    assert e["_to"] == "organizations/Q999"
    assert e["type"] == "parent_of"
    assert e["edge_collection"] == ORG_STRUCTURE
    assert e["wikidata_property"] == "P749"


def test_p355_emits_subsidiary_of_edge():
    entity = _make_entity({"P355": ["Q888"]})
    edges = build_org_structure_edge_docs("Q2", entity)
    assert len(edges) == 1
    e = edges[0]
    assert e["_from"] == "organizations/Q2"
    assert e["_to"] == "organizations/Q888"
    assert e["type"] == "subsidiary_of"
    assert e["edge_collection"] == ORG_STRUCTURE


def test_p127_emits_owned_by_edge():
    entity = _make_entity({"P127": ["Q777"]})
    edges = build_org_structure_edge_docs("Q3", entity)
    assert len(edges) == 1
    e = edges[0]
    assert e["_from"] == "organizations/Q3"
    assert e["_to"] == "organizations/Q777"
    assert e["type"] == "owned_by"
    assert e["edge_collection"] == ORG_STRUCTURE


def test_p1830_emits_owns_edge():
    entity = _make_entity({"P1830": ["Q666"]})
    edges = build_org_structure_edge_docs("Q4", entity)
    assert len(edges) == 1
    e = edges[0]
    assert e["_from"] == "organizations/Q4"
    assert e["_to"] == "organizations/Q666"
    assert e["type"] == "owns"
    assert e["edge_collection"] == ORG_STRUCTURE


def test_multiple_subsidiaries_emit_multiple_edges():
    entity = _make_entity({"P355": ["Q100", "Q200", "Q300"]})
    edges = build_org_structure_edge_docs("Q10", entity)
    assert len(edges) == 3
    types = {e["type"] for e in edges}
    assert types == {"subsidiary_of"}
    targets = {e["_to"] for e in edges}
    assert targets == {"organizations/Q100", "organizations/Q200", "organizations/Q300"}


def test_non_qid_targets_are_skipped():
    # Literal values (e.g. blank nodes, plain strings) should be ignored
    entity = _make_entity({"P749": ["not-a-qid", "http://something/else"]})
    edges = build_org_structure_edge_docs("Q5", entity)
    assert edges == []


def test_qualified_edges_carry_dates():
    stmt_nodes = {
        "http://www.wikidata.org/entity/statement/Q5-s1": {
            "prop": "P749",
            "value": "Q999",
            "quals": {"P580": "2001-01-01T00:00:00Z", "P582": "2010-12-31T00:00:00Z"},
        }
    }
    entity = _make_entity({"P749": ["Q999"]}, stmt_nodes=stmt_nodes)
    edges = build_org_structure_edge_docs("Q5", entity)
    # Qualified edge should dominate; no duplicate from direct props
    assert len(edges) == 1
    e = edges[0]
    assert e["start_date"] == "2001-01-01"
    assert e["end_date"] == "2010-12-31"
    assert e["statement_guid"] == "http://www.wikidata.org/entity/statement/Q5-s1"
    assert e["original_subject"] == "Q5"
    assert e["original_object"] == "Q999"


def test_edge_key_is_sanitized_string():
    entity = _make_entity({"P127": ["Q42"]})
    edges = build_org_structure_edge_docs("Q1", entity)
    assert len(edges) == 1
    key = edges[0]["_key"]
    # Key should only contain alphanumeric, _, -, :, .
    import re
    assert re.match(r'^[A-Za-z0-9_.:\-]+$', key), f"Bad key: {key!r}"


def test_all_four_props_in_one_entity():
    entity = _make_entity({
        "P749": ["Q10"],   # parent
        "P355": ["Q20"],   # subsidiary
        "P127": ["Q30"],   # owner
        "P1830": ["Q40"],  # owns
    })
    edges = build_org_structure_edge_docs("Q5", entity)
    assert len(edges) == 4
    edge_types = {e["type"] for e in edges}
    assert edge_types == {"parent_of", "subsidiary_of", "owned_by", "owns"}
