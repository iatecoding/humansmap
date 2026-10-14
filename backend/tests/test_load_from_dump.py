"""Unit tests for scripts/load_from_dump.py — the Wikidata dump ETL pipeline.

Covers the NT/JSON parsers, entity classifier, doc/edge builders, the
_apply_doc_updates/_lbzip2_lines helpers, pass-1 identification, and an
end-to-end load_dump() smoke test against a small in-memory fake DB.

Everything here is pure-function or fake-backed — no real ArangoDB
connection and no real Wikidata dump file are needed to run these.

Run in-container: ``docker compose exec backend pytest backend/tests/test_load_from_dump.py``
(host pytest is not installed — do not run on host).
"""

import json

import pytest

import scripts.load_from_dump as m
from scripts.load_from_dump import (
    PERSONS,
    ORGANIZATIONS,
    EVENTS,
    AFFILIATIONS,
    parse_nt_line,
    extract_qid,
    extract_statement_parent,
    parse_date,
    _json_snak_value,
    parse_json_entity,
    classify_entity,
    build_person_doc,
    build_org_doc,
    build_event_doc,
    build_edge_docs,
    build_reverse_edge_docs,
    ORG_REVERSE_PROPERTIES,
    sanitize_key,
    _apply_doc_updates,
    _lbzip2_lines,
    load_dump,
)


def _make_entity(labels=None, descriptions=None, p31=None, props=None,
                  direct_count=None, stmt_nodes=None, aliases_en=None, sitelinks=None):
    props = props or {}
    return {
        "labels": labels or {},
        "descriptions": descriptions or {},
        "p31": p31 or [],
        "props": props,
        "direct_count": direct_count if direct_count is not None else sum(len(v) for v in props.values()),
        "stmt_nodes": stmt_nodes or {},
        "sitelinks": sitelinks or {},
        "aliases_en": aliases_en or [],
    }


# ── parse_nt_line ─────────────────────────────────────────────────────────────

def test_parse_nt_line_uri_object():
    line = "<http://www.wikidata.org/entity/Q42> <http://www.wikidata.org/prop/direct/P31> <http://www.wikidata.org/entity/Q5> .\n"
    parsed = parse_nt_line(line)
    assert parsed == (
        "http://www.wikidata.org/entity/Q42",
        "http://www.wikidata.org/prop/direct/P31",
        "http://www.wikidata.org/entity/Q5",
        None,
    )


def test_parse_nt_line_language_tagged_literal():
    line = '<http://www.wikidata.org/entity/Q42> <http://www.w3.org/2000/01/rdf-schema#label> "Douglas Adams"@en .\n'
    subject, predicate, obj, meta = parse_nt_line(line)
    assert obj == "Douglas Adams"
    assert meta == "en"


def test_parse_nt_line_escaped_quote_and_unicode_escape():
    line = '<http://www.wikidata.org/entity/Q1> <http://schema.org/description> "He said \\"hi\\" caf\\u00e9"@en .\n'
    _, _, obj, meta = parse_nt_line(line)
    assert obj == 'He said "hi" café'
    assert meta == "en"


def test_parse_nt_line_full_unicode_escape():
    # \U0001F600 = grinning face emoji
    line = '<http://www.wikidata.org/entity/Q1> <http://schema.org/description> "emoji \\U0001F600 here"@en .\n'
    _, _, obj, _ = parse_nt_line(line)
    assert obj == "emoji \U0001F600 here"


def test_parse_nt_line_typed_literal():
    line = ('<http://www.wikidata.org/entity/Q1> <http://www.wikidata.org/prop/direct/P569> '
            '"1952-03-11T00:00:00Z"^^<http://www.w3.org/2001/XMLSchema#dateTime> .\n')
    _, _, obj, meta = parse_nt_line(line)
    assert obj == "1952-03-11T00:00:00Z"
    assert meta == "http://www.w3.org/2001/XMLSchema#dateTime"


@pytest.mark.parametrize("line", ["", "\n", "# a comment\n", "not a valid nt line\n"])
def test_parse_nt_line_returns_none_for_unparseable(line):
    assert parse_nt_line(line) is None


# ── QID / date extraction helpers ────────────────────────────────────────────

def test_extract_qid_from_entity_uri():
    assert extract_qid("http://www.wikidata.org/entity/Q42") == "Q42"


def test_extract_qid_returns_none_for_non_entity_uri():
    assert extract_qid("http://www.wikidata.org/prop/direct/P31") is None


def test_extract_qid_does_not_match_statement_uri():
    # Statement node URIs contain WD_ENTITY as a substring but aren't QIDs.
    assert extract_qid("http://www.wikidata.org/entity/statement/Q937-abc-def") is None


def test_extract_statement_parent():
    assert extract_statement_parent("http://www.wikidata.org/entity/statement/Q937-abc-def") == "Q937"


def test_extract_statement_parent_returns_none_for_entity_uri():
    assert extract_statement_parent("http://www.wikidata.org/entity/Q937") is None


@pytest.mark.parametrize("raw,expected", [
    ("1936-09-29T00:00:00Z", "1936-09-29"),
    ("1936-09-29", "1936-09-29"),
    (None, None),
    ("", None),
])
def test_parse_date(raw, expected):
    assert parse_date(raw) == expected


# ── JSON snak value extraction ───────────────────────────────────────────────

def test_json_snak_wikibase_entityid_with_id():
    snak = {"snaktype": "value", "datavalue": {"type": "wikibase-entityid", "value": {"id": "Q42"}}}
    assert _json_snak_value(snak) == "Q42"


def test_json_snak_wikibase_entityid_numeric_id_item():
    snak = {"snaktype": "value", "datavalue": {
        "type": "wikibase-entityid", "value": {"numeric-id": 42, "entity-type": "item"},
    }}
    assert _json_snak_value(snak) == "Q42"


def test_json_snak_wikibase_entityid_numeric_id_property():
    snak = {"snaktype": "value", "datavalue": {
        "type": "wikibase-entityid", "value": {"numeric-id": 31, "entity-type": "property"},
    }}
    assert _json_snak_value(snak) == "P31"


def test_json_snak_commons_media_builds_filepath_url():
    snak = {"snaktype": "value", "datatype": "commonsMedia",
            "datavalue": {"type": "string", "value": "Test image.jpg"}}
    assert _json_snak_value(snak) == "http://commons.wikimedia.org/wiki/Special:FilePath/Test%20image.jpg"


def test_json_snak_plain_string():
    snak = {"snaktype": "value", "datavalue": {"type": "string", "value": "hello"}}
    assert _json_snak_value(snak) == "hello"


def test_json_snak_time_strips_leading_plus():
    snak = {"snaktype": "value", "datavalue": {"type": "time", "value": {"time": "+1952-03-11T00:00:00Z"}}}
    assert _json_snak_value(snak) == "1952-03-11T00:00:00Z"


def test_json_snak_monolingualtext():
    snak = {"snaktype": "value", "datavalue": {"type": "monolingualtext", "value": {"text": "ciao", "language": "it"}}}
    assert _json_snak_value(snak) == "ciao"


def test_json_snak_quantity_strips_sign():
    snak = {"snaktype": "value", "datavalue": {"type": "quantity", "value": {"amount": "+123"}}}
    assert _json_snak_value(snak) == "123"


def test_json_snak_novalue_returns_none():
    assert _json_snak_value({"snaktype": "novalue"}) is None


def test_json_snak_none_returns_none():
    assert _json_snak_value(None) is None


# ── parse_json_entity ─────────────────────────────────────────────────────────

def _claim(prop_id, qid, rank="normal", qualifiers=None):
    return {
        "mainsnak": {"snaktype": "value", "datatype": "wikibase-item",
                     "datavalue": {"type": "wikibase-entityid", "value": {"id": qid}}},
        "rank": rank,
        "qualifiers": qualifiers or {},
    }


def test_parse_json_entity_basic_fields():
    obj = {
        "id": "Q42",
        "type": "item",
        "labels": {"en": {"value": "Douglas Adams"}, "it": {"value": "Douglas Adams IT"}},
        "descriptions": {"en": {"value": "writer"}},
        "aliases": {"en": [{"value": "DNA"}]},
        "sitelinks": {"enwiki": {"title": "Douglas Adams"}},
        "claims": {
            "P31": [_claim("P31", "Q5")],
            "P106": [_claim("P106", "Q36180")],
        },
    }
    qid, entity = parse_json_entity(obj)
    assert qid == "Q42"
    assert entity["labels"]["en"] == "Douglas Adams"
    assert entity["labels"]["it"] == "Douglas Adams IT"
    assert entity["descriptions"]["en"] == "writer"
    assert entity["aliases_en"] == ["DNA"]
    assert entity["sitelinks"]["en"] == "https://en.wikipedia.org/wiki/Douglas_Adams"
    assert entity["p31"] == ["Q5"]
    assert entity["props"]["P106"] == ["Q36180"]
    assert entity["direct_count"] == 2


def test_parse_json_entity_preserves_multilingual_label():
    obj = {"id": "Q1", "type": "item", "labels": {"mul": {"value": "Universal name"}}, "claims": {}}
    _, entity = parse_json_entity(obj)
    assert entity["labels"]["mul"] == "Universal name"


def test_parse_json_entity_preserves_deterministic_any_language_fallback():
    obj = {"id": "Q1", "type": "item", "labels": {"fr": {"value": "Nom"}, "de": {"value": "Name"}}, "claims": {}}
    _, entity = parse_json_entity(obj)
    assert entity["labels"]["fallback"] == "Name"


def test_parse_json_entity_non_item_type_is_skipped():
    assert parse_json_entity({"id": "P31", "type": "property"}) == (None, None)


def test_parse_json_entity_missing_id_is_skipped():
    assert parse_json_entity({"type": "item"}) == (None, None)


def test_parse_json_entity_deprecated_claim_skipped():
    obj = {"id": "Q1", "type": "item", "claims": {"P106": [_claim("P106", "Q36180", rank="deprecated")]}}
    _, entity = parse_json_entity(obj)
    assert entity["props"].get("P106", []) == []
    assert entity["direct_count"] == 0


def test_parse_json_entity_preferred_rank_shadows_normal():
    obj = {"id": "Q1", "type": "item", "claims": {
        "P106": [_claim("P106", "Q36180", rank="normal"), _claim("P106", "Q1930187", rank="preferred")],
    }}
    _, entity = parse_json_entity(obj)
    assert entity["props"]["P106"] == ["Q1930187"]


def test_parse_json_entity_edge_prop_creates_stmt_node_with_qualifiers():
    quals = {"P580": [{"snaktype": "value", "datavalue": {"type": "time", "value": {"time": "+2001-01-01T00:00:00Z"}}}]}
    obj = {"id": "Q1", "type": "item", "claims": {"P108": [_claim("P108", "Q100", qualifiers=quals)]}}
    _, entity = parse_json_entity(obj)
    assert len(entity["stmt_nodes"]) == 1
    stmt = next(iter(entity["stmt_nodes"].values()))
    assert stmt["prop"] == "P108"
    assert stmt["value"] == "Q100"
    assert stmt["quals"]["P580"] == "2001-01-01T00:00:00Z"


# ── classify_entity ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("p31,expected", [
    (["Q5"], ("person", None)),
    (["Q4830453"], ("organization", "company")),
    (["Q178561"], ("event", "battle")),
    (["Q16521"], ("skip", None)),
    (["Q999999999"], ("unknown", None)),
    ([], ("unknown", None)),
])
def test_classify_entity(p31, expected):
    assert classify_entity(p31) == expected


# ── build_person_doc ──────────────────────────────────────────────────────────

def test_build_person_doc_basic_fields():
    entity = _make_entity(
        labels={"en": "Alice Example"},
        p31=["Q5"],
        props={
            "P106": ["Q82955"],       # politician
            "P21": ["Q6581097"],      # male
            "P27": ["Q30"],           # US
            "P569": ["1950-01-01T00:00:00Z"],
        },
    )
    doc = build_person_doc("Q1", entity, place_labels={})
    assert doc["name_en"] == "Alice Example"
    assert doc["occupations"] == ["politician"]
    assert doc["primary_occupation"] == "politician"
    assert doc["gender"] == "male"
    assert doc["nationalities"] == ["US"]
    assert doc["birth_date"] == "1950-01-01"


def test_build_person_doc_uses_italian_label_as_display_fallback():
    entity = _make_entity(labels={"it": "Solo italiano"}, p31=["Q5"])
    assert build_person_doc("Q1", entity, place_labels={})["name_en"] == "Solo italiano"


def test_build_person_doc_returns_none_without_any_label():
    entity = _make_entity(labels={}, p31=["Q5"])
    assert build_person_doc("Q1", entity, place_labels={}) is None


def test_build_person_doc_rich_fields_gated_by_statement_threshold():
    entity = _make_entity(
        labels={"en": "Low Statement Person"},
        p31=["Q5"],
        props={"P509": ["Q12345"]},  # cause of death
        direct_count=5,  # below rich_fields_min_statements (default 50)
    )
    doc = build_person_doc("Q1", entity, place_labels={}, entity_labels={"Q12345": {"en": "heart attack"}})
    assert doc["cause_of_death"] is None


def test_build_person_doc_rich_fields_populated_above_threshold():
    entity = _make_entity(
        labels={"en": "Notable Person"},
        p31=["Q5"],
        props={"P509": ["Q12345"]},
        direct_count=50,
    )
    doc = build_person_doc("Q1", entity, place_labels={}, entity_labels={"Q12345": {"en": "heart attack"}})
    assert doc["cause_of_death"] == "heart attack"


def test_build_person_doc_birth_place_resolved_via_place_labels():
    entity = _make_entity(labels={"en": "Someone"}, p31=["Q5"], props={"P19": ["Q220"]})
    doc = build_person_doc("Q1", entity, place_labels={"Q220": {"en": "Rome", "it": "Roma"}})
    assert doc["birth_place_id"] == "Q220"
    assert doc["birth_place"] == "Roma"  # place_labels prefers it, falls back to en


# ── build_org_doc ─────────────────────────────────────────────────────────────

def test_build_org_doc_basic_fields():
    entity = _make_entity(labels={"en": "Acme Corp"}, p31=["Q4830453"], props={"P17": ["Q30"]})
    doc = build_org_doc("Q100", entity, "company", entity_labels={"Q30": {"en": "United States"}})
    assert doc["name_en"] == "Acme Corp"
    assert doc["type"] == "company"
    assert doc["country"] == "United States"
    assert doc["country_id"] == "Q30"


def test_build_org_doc_returns_none_without_any_label():
    entity = _make_entity(labels={}, p31=["Q4830453"])
    assert build_org_doc("Q100", entity, "company") is None


def test_build_org_doc_uses_multilingual_label_as_display_fallback():
    entity = _make_entity(labels={"mul": "Universal Corp"}, p31=["Q4830453"])
    assert build_org_doc("Q100", entity, "company")["name_en"] == "Universal Corp"


# ── build_event_doc ───────────────────────────────────────────────────────────

def test_build_event_doc_basic_fields():
    entity = _make_entity(labels={"en": "D-Day"}, p31=["Q178561"],
                           props={"P585": ["1944-06-06T00:00:00Z"], "P276": ["Q1000"]})
    doc = build_event_doc("Q200", entity, "battle", entity_labels={"Q1000": {"en": "Normandy"}})
    assert doc["name_en"] == "D-Day"
    assert doc["type"] == "battle"
    assert doc["date"] == "1944-06-06"
    assert doc["location"] == "Normandy"


def test_build_event_doc_uses_any_language_label_as_display_fallback():
    entity = _make_entity(labels={"fallback": "Evento"}, p31=["Q178561"])
    assert build_event_doc("Q200", entity, "event")["name_en"] == "Evento"


# ── build_edge_docs ───────────────────────────────────────────────────────────

def test_build_edge_docs_fallback_direct_property():
    entity = _make_entity(props={"P108": ["Q100"]})
    edges = build_edge_docs("Q1", entity)
    assert len(edges) == 1
    e = edges[0]
    assert e["_from"] == "persons/Q1"
    assert e["_to"] == "organizations/Q100"
    assert e["type"] == "employee"
    assert e["edge_collection"] == AFFILIATIONS
    assert e["start_date"] is None


def test_build_edge_docs_qualified_edge_carries_dates_and_suppresses_fallback_dup():
    stmt_nodes = {
        "stmt1": {"prop": "P108", "value": "Q100", "rank": "preferred", "quals": {"P580": "2001-01-01T00:00:00Z", "P582": "2010-12-31T00:00:00Z"}},
    }
    entity = _make_entity(props={"P108": ["Q100"]}, stmt_nodes=stmt_nodes)
    edges = build_edge_docs("Q1", entity)
    # Only one edge — the qualified path handles it, the fallback pass must not duplicate it.
    assert len(edges) == 1
    e = edges[0]
    assert e["start_date"] == "2001-01-01"
    assert e["end_date"] == "2010-12-31"
    assert e["statement_guid"] == "stmt1"
    assert e["rank"] == "preferred"
    assert e["original_subject"] == "Q1"
    assert e["original_object"] == "Q100"


def test_build_edge_docs_keeps_point_in_time_separate_from_end_date():
    stmt_nodes = {
        "stmt1": {
            "prop": "P108",
            "value": "Q100",
            "quals": {"P585": "2020-06-30T00:00:00Z"},
        },
    }
    entity = _make_entity(props={"P108": ["Q100"]}, stmt_nodes=stmt_nodes)

    edge = build_edge_docs("Q1", entity)[0]

    assert edge["point_in_time"] == "2020-06-30"
    assert edge["end_date"] is None


def test_build_edge_docs_duplicate_targets_get_suffix():
    stmt_nodes = {
        "stmt1": {"prop": "P108", "value": "Q100", "quals": {"P580": "2001-01-01T00:00:00Z"}},
        "stmt2": {"prop": "P108", "value": "Q100", "quals": {"P580": "2015-01-01T00:00:00Z"}},
    }
    entity = _make_entity(props={"P108": ["Q100", "Q100"]}, stmt_nodes=stmt_nodes)
    edges = build_edge_docs("Q1", entity)
    assert len(edges) == 2
    keys = {e["_key"] for e in edges}
    assert len(keys) == 2  # suffix keeps keys unique


def test_build_edge_docs_person_to_person_spouse():
    entity = _make_entity(props={"P26": ["Q2"]})
    edges = build_edge_docs("Q1", entity)
    assert len(edges) == 1
    assert edges[0]["_to"] == "persons/Q2"
    assert edges[0]["type"] == "spouse"


def test_build_edge_docs_non_qid_target_skipped():
    entity = _make_entity(props={"P108": ["not-a-qid"]})
    assert build_edge_docs("Q1", entity) == []


# ── build_reverse_edge_docs ───────────────────────────────────────────────────

def test_build_reverse_edge_docs_org_ceo():
    entity = _make_entity(props={"P169": ["Q1"]})  # chief executive officer = Q1
    edges = build_reverse_edge_docs("Q100", entity, ORGANIZATIONS, ORG_REVERSE_PROPERTIES)
    assert len(edges) == 1
    e = edges[0]
    assert e["_from"] == "persons/Q1"
    assert e["_to"] == "organizations/Q100"
    assert e["type"] == "chief_executive"


def test_build_reverse_edge_docs_org_ceo_preserves_qualifier_dates():
    entity = _make_entity(
        props={"P169": ["Q1"]},
        stmt_nodes={
            "statement-1": {
                "prop": "P169",
                "value": "Q1",
                "quals": {
                    "P580": "2018-01-01T00:00:00Z",
                    "P582": "2020-12-31T00:00:00Z",
                },
            },
        },
    )

    edges = build_reverse_edge_docs("Q100", entity, ORGANIZATIONS, ORG_REVERSE_PROPERTIES)

    assert len(edges) == 1
    assert edges[0]["start_date"] == "2018-01-01"
    assert edges[0]["end_date"] == "2020-12-31"


def test_build_reverse_edge_docs_supports_co_ceos():
    entity = _make_entity(props={"P169": ["Q1", "Q2"]})

    edges = build_reverse_edge_docs("Q100", entity, ORGANIZATIONS, ORG_REVERSE_PROPERTIES)

    assert len(edges) == 2
    assert {edge["_from"] for edge in edges} == {"persons/Q1", "persons/Q2"}


# ── sanitize_key ──────────────────────────────────────────────────────────────

def test_sanitize_key_leaves_safe_chars():
    assert sanitize_key("Q1_Q2-spouse:P26.x") == "Q1_Q2-spouse:P26.x"


def test_sanitize_key_replaces_unsafe_chars():
    assert sanitize_key("Q1/Q2 test!") == "Q1_Q2_test_"


# ── _apply_doc_updates ────────────────────────────────────────────────────────

class _RaisingCollection:
    def __init__(self, fail_keys):
        self.fail_keys = set(fail_keys)
        self.updated = []

    def update(self, doc):
        if doc["_key"] in self.fail_keys:
            raise RuntimeError("boom")
        self.updated.append(doc["_key"])


def test_apply_doc_updates_counts_successes_and_swallows_individual_failures():
    col = _RaisingCollection(fail_keys={"Q2"})
    updates = [{"_key": "Q1", "x": 1}, {"_key": "Q2", "x": 2}, {"_key": "Q3", "x": 3}]
    applied = _apply_doc_updates(col, updates, "test-label")
    assert applied == 2
    assert col.updated == ["Q1", "Q3"]


def test_apply_doc_updates_all_succeed():
    col = _RaisingCollection(fail_keys=set())
    updates = [{"_key": "Q1"}, {"_key": "Q2"}]
    assert _apply_doc_updates(col, updates, "test-label") == 2


# ── _lbzip2_lines ─────────────────────────────────────────────────────────────

class _FakeStdout:
    def __init__(self, lines):
        self._lines = lines
        self.closed = False

    def __iter__(self):
        return iter(self._lines)

    def close(self):
        self.closed = True


class _FakeProc:
    def __init__(self, lines, returncode=0):
        self.stdout = _FakeStdout(lines)
        self._returncode = returncode

    def wait(self):
        return self._returncode


def test_lbzip2_lines_yields_decoded_lines_on_clean_exit():
    proc = _FakeProc([b"line one\n", b"line two\n"], returncode=0)
    lines = list(_lbzip2_lines(proc))
    assert lines == ["line one\n", "line two\n"]
    assert proc.stdout.closed


def test_lbzip2_lines_raises_on_nonzero_exit():
    proc = _FakeProc([b"partial line\n"], returncode=1)
    with pytest.raises(RuntimeError, match="lbzip2 exited with code 1"):
        list(_lbzip2_lines(proc))


# ── pass 1 identification ─────────────────────────────────────────────────────

def test_pass1_nt_identifies_target_country_person_above_threshold(monkeypatch):
    monkeypatch.setattr(m, "PASS1_MIN_STATEMENTS", 2)
    lines = [
        "<http://www.wikidata.org/entity/Q1> <http://www.wikidata.org/prop/direct/P31> <http://www.wikidata.org/entity/Q5> .\n",
        "<http://www.wikidata.org/entity/Q1> <http://www.wikidata.org/prop/direct/P27> <http://www.wikidata.org/entity/Q30> .\n",
        "<http://www.wikidata.org/entity/Q1> <http://www.wikidata.org/prop/direct/P108> <http://www.wikidata.org/entity/Q100> .\n",
    ]
    target_persons, needed_refs = set(), set()
    m._run_pass1_nt(iter(lines), target_persons, needed_refs)
    assert target_persons == {"Q1"}
    assert "Q100" in needed_refs


def test_pass1_nt_skips_below_notability_threshold(monkeypatch):
    monkeypatch.setattr(m, "PASS1_MIN_STATEMENTS", 5)
    lines = [
        "<http://www.wikidata.org/entity/Q2> <http://www.wikidata.org/prop/direct/P31> <http://www.wikidata.org/entity/Q5> .\n",
        "<http://www.wikidata.org/entity/Q2> <http://www.wikidata.org/prop/direct/P27> <http://www.wikidata.org/entity/Q30> .\n",
    ]
    target_persons, needed_refs = set(), set()
    m._run_pass1_nt(iter(lines), target_persons, needed_refs)
    assert target_persons == set()


def test_pass1_nt_skips_non_target_country(monkeypatch):
    monkeypatch.setattr(m, "PERSON_SCOPE", "countries")
    monkeypatch.setattr(m, "PASS1_MIN_STATEMENTS", 1)
    lines = [
        "<http://www.wikidata.org/entity/Q3> <http://www.wikidata.org/prop/direct/P31> <http://www.wikidata.org/entity/Q5> .\n",
        "<http://www.wikidata.org/entity/Q3> <http://www.wikidata.org/prop/direct/P27> <http://www.wikidata.org/entity/Q148> .\n",  # China
    ]
    target_persons, needed_refs = set(), set()
    m._run_pass1_nt(iter(lines), target_persons, needed_refs)
    assert target_persons == set()



def test_pass1_nt_keeps_reverse_ceo_org_regardless_of_entity_order(monkeypatch):
    monkeypatch.setattr(m, "PERSON_SCOPE", "global")
    monkeypatch.setattr(m, "PASS1_MIN_STATEMENTS", 1)
    lines = [
        "<http://www.wikidata.org/entity/Q100> <http://www.wikidata.org/prop/direct/P31> <http://www.wikidata.org/entity/Q4830453> .\n",
        "<http://www.wikidata.org/entity/Q100> <http://www.wikidata.org/prop/direct/P169> <http://www.wikidata.org/entity/Q1> .\n",
        "<http://www.wikidata.org/entity/Q100> <http://www.wikidata.org/prop/direct/P749> <http://www.wikidata.org/entity/Q200> .\n",
        "<http://www.wikidata.org/entity/Q1> <http://www.wikidata.org/prop/direct/P31> <http://www.wikidata.org/entity/Q5> .\n",
    ]
    target_persons, needed_refs = set(), set()

    m._run_pass1_nt(iter(lines), target_persons, needed_refs)

    assert target_persons == {"Q1"}
    assert {"Q100", "Q200"}.issubset(needed_refs)

# ── load_dump end-to-end (fake DB, no real ArangoDB / dump file) ─────────────

class _FakeCollection:
    def __init__(self, name):
        self.name = name
        self.docs: dict = {}

    def has(self, key):
        return key in self.docs

    def get(self, key):
        return self.docs.get(key)

    def insert(self, doc):
        self.docs[doc["_key"]] = dict(doc)

    def update(self, doc):
        if doc["_key"] not in self.docs:
            raise KeyError(doc["_key"])
        self.docs[doc["_key"]].update(doc)

    def import_bulk(self, docs, on_duplicate="update"):
        created = updated = 0
        for doc in docs:
            key = doc["_key"]
            if key in self.docs:
                self.docs[key].update(doc)
                updated += 1
            else:
                self.docs[key] = dict(doc)
                created += 1
        return {"created": created, "updated": updated, "errors": 0}


class _FakeAQL:
    """Minimal shim covering only the AQL shapes load_dump()/merge_occupations
    actually issue for this smoke test — not a general AQL interpreter."""

    def __init__(self, db):
        self.db = db

    def execute(self, query, bind_vars=None):
        if "RETURN d._key" in query:
            for name, col in self.db.collections.items():
                if f"FOR d IN {name}" in query:
                    return iter(list(col.docs.keys()))
            return iter([])
        if "birth_place_id" in query:
            rows = []
            for doc in self.db.collections[PERSONS].docs.values():
                if (doc.get("birth_place_id") and not doc.get("birth_place")) or \
                   (doc.get("death_place_id") and not doc.get("death_place")):
                    rows.append({"k": doc["_key"], "bp": doc.get("birth_place_id"), "dp": doc.get("death_place_id")})
            return iter(rows)
        return iter([])


class _FakeDB:
    def __init__(self):
        from scripts.load_from_dump import RELATIONSHIPS, INVOLVEMENTS, ORG_STRUCTURE
        self.collections = {
            PERSONS: _FakeCollection(PERSONS),
            ORGANIZATIONS: _FakeCollection(ORGANIZATIONS),
            EVENTS: _FakeCollection(EVENTS),
            RELATIONSHIPS: _FakeCollection(RELATIONSHIPS),
            AFFILIATIONS: _FakeCollection(AFFILIATIONS),
            INVOLVEMENTS: _FakeCollection(INVOLVEMENTS),
            ORG_STRUCTURE: _FakeCollection(ORG_STRUCTURE),
        }
        self.aql = _FakeAQL(self)

    def collection(self, name):
        return self.collections[name]


def test_load_dump_end_to_end_person_and_org_with_edge(monkeypatch):
    monkeypatch.setattr(m, "MIN_STATEMENTS", 1)
    monkeypatch.setattr(m, "DRY_RUN", False)
    monkeypatch.setattr(m, "SKIP_EDGES", False)
    monkeypatch.setattr(m, "SKIP_EXISTING", False)
    monkeypatch.setattr(m, "BATCH_SIZE", 500)

    lines = [
        '<http://www.wikidata.org/entity/Q1> <http://www.w3.org/2000/01/rdf-schema#label> "Alice"@en .\n',
        "<http://www.wikidata.org/entity/Q1> <http://www.wikidata.org/prop/direct/P31> <http://www.wikidata.org/entity/Q5> .\n",
        "<http://www.wikidata.org/entity/Q1> <http://www.wikidata.org/prop/direct/P27> <http://www.wikidata.org/entity/Q30> .\n",
        "<http://www.wikidata.org/entity/Q1> <http://www.wikidata.org/prop/direct/P106> <http://www.wikidata.org/entity/Q82955> .\n",
        "<http://www.wikidata.org/entity/Q1> <http://www.wikidata.org/prop/direct/P108> <http://www.wikidata.org/entity/Q100> .\n",
        '<http://www.wikidata.org/entity/Q100> <http://www.w3.org/2000/01/rdf-schema#label> "Acme Corp"@en .\n',
        "<http://www.wikidata.org/entity/Q100> <http://www.wikidata.org/prop/direct/P31> <http://www.wikidata.org/entity/Q4830453> .\n",
    ]

    db = _FakeDB()
    stats = load_dump(iter(lines), db, allowed_qids=None, input_format="nt")

    assert stats["persons"] == 1
    assert stats["organizations"] == 1

    person = db.collections[PERSONS].docs["Q1"]
    assert person["name_en"] == "Alice"
    assert person["occupations"] == ["politician"]
    assert person["nationalities"] == ["US"]

    org = db.collections[ORGANIZATIONS].docs["Q100"]
    assert org["name_en"] == "Acme Corp"
    assert org["type"] == "company"

    aff_docs = list(db.collections[AFFILIATIONS].docs.values())
    assert len(aff_docs) == 1
    edge = aff_docs[0]
    assert edge["_from"] == "persons/Q1"
    assert edge["_to"] == "organizations/Q100"
    assert edge["type"] == "employee"


def test_load_dump_single_pass_drops_non_target_country_person(monkeypatch):
    monkeypatch.setattr(m, "PERSON_SCOPE", "countries")
    monkeypatch.setattr(m, "MIN_STATEMENTS", 1)
    monkeypatch.setattr(m, "DRY_RUN", False)
    monkeypatch.setattr(m, "SKIP_EDGES", False)
    monkeypatch.setattr(m, "SKIP_EXISTING", False)
    monkeypatch.setattr(m, "BATCH_SIZE", 500)

    lines = [
        '<http://www.wikidata.org/entity/Q9> <http://www.w3.org/2000/01/rdf-schema#label> "Not Included"@en .\n',
        "<http://www.wikidata.org/entity/Q9> <http://www.wikidata.org/prop/direct/P31> <http://www.wikidata.org/entity/Q5> .\n",
        # P27 target (Q148 = China) is not in TARGET_COUNTRY_QIDS
        "<http://www.wikidata.org/entity/Q9> <http://www.wikidata.org/prop/direct/P27> <http://www.wikidata.org/entity/Q148> .\n",
    ]
    db = _FakeDB()
    stats = load_dump(iter(lines), db, allowed_qids=None, input_format="nt")
    assert stats["persons"] == 0
    assert "Q9" not in db.collections[PERSONS].docs


# ── Global JSON source and scope safety ──────────────────────────────────────

def test_detect_input_format_accepts_remote_json_bzip2():
    assert m.detect_input_format(
        "https://dumps.wikimedia.org/wikidatawiki/entities/20260701/wikidata-20260701-all.json.bz2"
    ) == "json"


def test_validate_dump_source_rejects_unpinned_or_insecure_remote_urls():
    with pytest.raises(ValueError, match="HTTPS"):
        m.validate_dump_source("http://example.test/wikidata.json.bz2")
    with pytest.raises(ValueError, match="pinned"):
        m.validate_dump_source("https://example.test/latest-all.json.bz2")


def test_pass1_json_global_scope_keeps_person_without_citizenship(monkeypatch):
    monkeypatch.setattr(m, "PERSON_SCOPE", "global")
    monkeypatch.setattr(m, "PASS1_MIN_STATEMENTS", 1)
    person = {
        "id": "Q42",
        "type": "item",
        "claims": {
            "P31": [_claim("P31", "Q5")],
            "P106": [_claim("P106", "Q36180")],
        },
    }
    stream = iter(["[\n", json.dumps(person) + ",\n", "]\n"])
    persons, refs = set(), set()

    m._run_pass1_json(stream, persons, refs)

    assert persons == {"Q42"}
    assert refs == set()


def test_pass1_json_keeps_reverse_ceo_org_regardless_of_entity_order(monkeypatch):
    monkeypatch.setattr(m, "PERSON_SCOPE", "global")
    monkeypatch.setattr(m, "PASS1_MIN_STATEMENTS", 1)
    organization = {
        "id": "Q100",
        "type": "item",
        "claims": {
            "P31": [_claim("P31", "Q4830453")],
            "P169": [_claim("P169", "Q1")],
            "P749": [_claim("P749", "Q200")],
        },
    }
    person = {
        "id": "Q1",
        "type": "item",
        "claims": {"P31": [_claim("P31", "Q5")]},
    }
    stream = iter([
        "[\n",
        json.dumps(organization) + ",\n",
        json.dumps(person) + ",\n",
        "]\n",
    ])
    persons, refs = set(), set()

    m._run_pass1_json(stream, persons, refs)

    assert persons == {"Q1"}
    assert {"Q100", "Q200"}.issubset(refs)


def test_pass1_json_does_not_keep_org_for_unselected_person(monkeypatch):
    monkeypatch.setattr(m, "PERSON_SCOPE", "countries")
    monkeypatch.setattr(m, "PASS1_MIN_STATEMENTS", 1)
    organization = {
        "id": "Q100",
        "type": "item",
        "claims": {
            "P31": [_claim("P31", "Q4830453")],
            "P169": [_claim("P169", "Q9")],
        },
    }
    person = {
        "id": "Q9",
        "type": "item",
        "claims": {
            "P31": [_claim("P31", "Q5")],
            "P27": [_claim("P27", "Q148")],
        },
    }
    stream = iter([
        "[\n",
        json.dumps(organization) + ",\n",
        json.dumps(person) + ",\n",
        "]\n",
    ])
    persons, refs = set(), set()

    m._run_pass1_json(stream, persons, refs)

    assert persons == set()
    assert "Q100" not in refs


def test_orphan_cleanup_never_blanket_deletes_org_affiliations():
    class RecordingAql:
        def __init__(self):
            self.queries = []

        def execute(self, query):
            self.queries.append(query)
            return iter([0])

    class Db:
        def __init__(self):
            self.aql = RecordingAql()

    db = Db()

    assert m._cleanup_orphan_edges(db) == 0
    affiliation_queries = [
        query for query in db.aql.queries if "REMOVE e IN affiliations" in query
    ]
    assert len(affiliation_queries) == 1
    assert "DOCUMENT(e._from)" in affiliation_queries[0]
    assert 'STARTS_WITH(e._from, "organizations/")' not in affiliation_queries[0]


def test_iter_json_entities_fails_when_any_entity_is_malformed():
    valid = json.dumps({"id": "Q1", "type": "item"})
    stream = iter(["[\n", valid + ",\n", '{"broken":\n', "]\n"])

    with pytest.raises(RuntimeError, match="1 malformed entity"):
        list(m.iter_json_entities(stream))


def test_pass1_json_fails_when_a_candidate_line_is_malformed():
    stream = iter(["[\n", '{"P31":"Q5",\n', "]\n"])

    with pytest.raises(RuntimeError, match="pass 1 encountered 1 malformed"):
        m._run_pass1_json(stream, set(), set())


def test_batch_upsert_raises_if_individual_fallback_loses_a_document(monkeypatch):
    monkeypatch.setattr(m, "DRY_RUN", False)

    class FailingCollection:
        def import_bulk(self, docs, on_duplicate="update"):
            return {"created": 1, "updated": 0, "errors": 1}

        def has(self, key):
            return False

        def insert(self, doc):
            if doc["_key"] == "Q2":
                raise RuntimeError("cannot write Q2")

    class Db:
        def collection(self, name):
            return FailingCollection()

    with pytest.raises(RuntimeError, match="1/2 documents failed"):
        m.batch_upsert(
            Db(),
            PERSONS,
            [{"_key": "Q1"}, {"_key": "Q2"}],
            "safety-test",
        )
