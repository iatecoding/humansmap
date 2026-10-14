"""Unit tests for Phase 5 incremental updater parity.

Tests the new SPARQL extraction additions (5.1) and upsert logic (5.2).
Pure-function tests — no DB, network, or dump needed.

Run in-container:
    docker compose exec backend pytest backend/tests/test_incremental_enrichment.py -v
(host pytest is not installed — do not run on host.)
"""

import sys
import os

# Ensure app is importable when run as a module from the backend container
sys.path.insert(0, "/app")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from scripts.incremental_update import (
    PROP_TO_EDGE,
    ORG_STRUCTURE_PROP_TO_TYPE,
    EDGE_TO_COLLECTION,
    _val,
    _qid,
    RELATIONSHIPS,
    AFFILIATIONS,
    INVOLVEMENTS,
    ORG_STRUCTURE,
    ORGANIZATIONS,
    PERSONS,
    EVENTS,
)


# ── 5.1: PROP_TO_EDGE contains the 4 new person↔person types (§2a / 4.1) ─────

class TestPersonRelationshipPropsInPropToEdge:
    def test_p451_unmarried_partner(self):
        assert "P451" in PROP_TO_EDGE
        col, edge_type = PROP_TO_EDGE["P451"]
        assert col == RELATIONSHIPS
        assert edge_type == "unmarried_partner"

    def test_p184_doctoral_advisor(self):
        assert "P184" in PROP_TO_EDGE
        col, edge_type = PROP_TO_EDGE["P184"]
        assert col == RELATIONSHIPS
        assert edge_type == "doctoral_advisor"

    def test_p185_doctoral_student(self):
        assert "P185" in PROP_TO_EDGE
        col, edge_type = PROP_TO_EDGE["P185"]
        assert col == RELATIONSHIPS
        assert edge_type == "doctoral_student"

    def test_p1327_professional_collaborator(self):
        assert "P1327" in PROP_TO_EDGE
        col, edge_type = PROP_TO_EDGE["P1327"]
        assert col == RELATIONSHIPS
        assert edge_type == "professional_collaborator"

    def test_existing_family_props_untouched(self):
        """Existing family property mapping must not be changed."""
        assert PROP_TO_EDGE["P26"] == ("relationships", "spouse")
        assert PROP_TO_EDGE["P22"] == ("relationships", "parent")
        assert PROP_TO_EDGE["P40"] == ("relationships", "child")
        assert PROP_TO_EDGE["P3373"] == ("relationships", "sibling")

    def test_existing_affiliation_props_untouched(self):
        assert PROP_TO_EDGE["P108"] == ("affiliations", "employee")
        assert PROP_TO_EDGE["P39"] == ("affiliations", "position_held")
        assert PROP_TO_EDGE["P102"] == ("affiliations", "member_of")

    def test_existing_involvement_props_untouched(self):
        assert PROP_TO_EDGE["P1344"] == ("involvements", "participant")
        assert PROP_TO_EDGE["P793"] == ("involvements", "significant_event")


# ── 5.2: ORG_STRUCTURE_PROP_TO_TYPE map (§2b / 4.3) ─────────────────────────

class TestOrgStructurePropToType:
    def test_all_four_props_present(self):
        assert set(ORG_STRUCTURE_PROP_TO_TYPE.keys()) == {"P749", "P355", "P127", "P1830"}

    def test_p749_parent_of(self):
        assert ORG_STRUCTURE_PROP_TO_TYPE["P749"] == "parent_of"

    def test_p355_subsidiary_of(self):
        assert ORG_STRUCTURE_PROP_TO_TYPE["P355"] == "subsidiary_of"

    def test_p127_owned_by(self):
        assert ORG_STRUCTURE_PROP_TO_TYPE["P127"] == "owned_by"

    def test_p1830_owns(self):
        assert ORG_STRUCTURE_PROP_TO_TYPE["P1830"] == "owns"


# ── Helper functions ──────────────────────────────────────────────────────────

class TestHelpers:
    def test_val_extracts_value(self):
        binding = {"key": {"value": "hello", "type": "literal"}}
        assert _val(binding, "key") == "hello"

    def test_val_returns_none_for_missing_key(self):
        assert _val({}, "missing") is None

    def test_val_returns_none_for_no_value_key(self):
        assert _val({"key": {"type": "literal"}}, "key") is None

    def test_qid_extracts_from_entity_uri(self):
        uri = "http://www.wikidata.org/entity/Q42"
        assert _qid(uri) == "Q42"

    def test_qid_returns_none_for_non_entity_uri(self):
        assert _qid("http://www.wikidata.org/prop/direct/P31") is None

    def test_qid_returns_none_for_none(self):
        assert _qid(None) is None


# ── Edge collection constants exported correctly ──────────────────────────────

class TestCollectionConstants:
    def test_org_structure_constant(self):
        assert ORG_STRUCTURE == "org_structure"

    def test_edge_to_collection_maps_relationships_to_persons(self):
        assert EDGE_TO_COLLECTION["relationships"] == PERSONS

    def test_edge_to_collection_maps_affiliations_to_organizations(self):
        assert EDGE_TO_COLLECTION["affiliations"] == ORGANIZATIONS

    def test_edge_to_collection_maps_involvements_to_events(self):
        assert EDGE_TO_COLLECTION["involvements"] == EVENTS


# ── award_qids / awards_top5 logic (pure-function) ───────────────────────────

class TestRichFieldsLogic:
    """Tests for the award ranking / rich fields accumulation logic.

    These test the *logic* of ranking without a live SPARQL endpoint.
    fetch_person_rich_fields() is not tested here (it calls _sparql_get).
    """

    def _compute_awards_top5(
        self, awards_map: dict[str, str], award_counts: dict[str, int]
    ) -> list[str]:
        """Mirror the ranking logic from fetch_person_rich_fields."""
        sorted_awards = sorted(
            awards_map.items(),
            key=lambda kv: award_counts.get(kv[0], 0),
            reverse=True,
        )
        return [label for _, label in sorted_awards[:5]]

    def test_awards_top5_sorted_by_count_desc(self):
        awards_map = {
            "Q1": "Nobel Prize",
            "Q2": "Regional Medal",
            "Q3": "Grammy Award",
        }
        award_counts = {"Q1": 5000, "Q2": 10, "Q3": 3000}
        top5 = self._compute_awards_top5(awards_map, award_counts)
        assert top5 == ["Nobel Prize", "Grammy Award", "Regional Medal"]

    def test_awards_top5_capped_at_5(self):
        awards_map = {f"Q{i}": f"Award {i}" for i in range(10)}
        award_counts = {f"Q{i}": i * 100 for i in range(10)}
        top5 = self._compute_awards_top5(awards_map, award_counts)
        assert len(top5) == 5

    def test_awards_top5_unknown_award_counts_as_zero(self):
        awards_map = {"Q1": "Known Award", "Q2": "Unknown Award"}
        award_counts = {"Q1": 500}  # Q2 not in counts
        top5 = self._compute_awards_top5(awards_map, award_counts)
        assert top5[0] == "Known Award"
        assert top5[1] == "Unknown Award"

    def test_empty_awards_map_returns_empty(self):
        top5 = self._compute_awards_top5({}, {})
        assert top5 == []

    def test_notable_works_cap_10(self):
        """Verify the work_labels cap-10 logic."""
        works = []
        labels = [f"Work {i}" for i in range(15)]
        for label in labels:
            if label not in works and len(works) < 10:
                works.append(label)
        assert len(works) == 10

    def test_notable_works_no_duplicates(self):
        works: list[str] = []
        for label in ["A", "B", "A", "C"]:
            if label not in works and len(works) < 10:
                works.append(label)
        assert works == ["A", "B", "C"]


# ── Org-upsert doc shape (pure construction test) ────────────────────────────

class TestOrgUpsertDocShape:
    """Verify the fields assembled by upsert_org() are correct.

    We do not call upsert_org() directly (it needs a DB), but we can verify
    the field extraction logic from SPARQL row dicts.
    """

    def _make_org_row(self, **overrides) -> dict:
        row = {
            "org": {"value": "http://www.wikidata.org/entity/Q123"},
            "orgLabel": {"value": "Acme Corp"},
            "orgLabelIt": {"value": "Acme Corp IT"},
            "orgDescription": {"value": "A corporation"},
            "orgDescriptionIt": {"value": "Una società"},
            "inception": {"value": "2000-01-01T00:00:00Z"},
            "dissolved": {"value": "2020-01-01T00:00:00Z"},
            "website": {"value": "https://acme.example.com"},
            "hq": {"value": "http://www.wikidata.org/entity/Q64"},
            "hqLabel": {"value": "Berlin"},
            "employees": {"value": "5000"},
            "country": {"value": "http://www.wikidata.org/entity/Q183"},
            "countryLabel": {"value": "Germany"},
        }
        row.update(overrides)
        return row

    def test_org_fields_extracted(self):
        row = self._make_org_row()
        assert _val(row, "website") == "https://acme.example.com"
        assert _val(row, "hqLabel") == "Berlin"
        assert _qid(_val(row, "hq")) == "Q64"
        assert _val(row, "employees") == "5000"
        assert _val(row, "countryLabel") == "Germany"
        assert _qid(_val(row, "country")) == "Q183"

    def test_employees_count_parsed_as_int(self):
        """Employees numeric literal from SPARQL is a string — must parse to int."""
        row = self._make_org_row()
        employees_raw = _val(row, "employees")
        assert int(float(employees_raw)) == 5000

    def test_employees_count_handles_float_literal(self):
        row = self._make_org_row(employees={"value": "12345.0"})
        employees_raw = _val(row, "employees")
        assert int(float(employees_raw)) == 12345

    def test_missing_website_is_none(self):
        row = self._make_org_row()
        del row["website"]
        assert _val(row, "website") is None


# ── Event-upsert doc shape ────────────────────────────────────────────────────

class TestEventUpsertDocShape:
    def _make_event_row(self, **overrides) -> dict:
        row = {
            "event": {"value": "http://www.wikidata.org/entity/Q999"},
            "eventLabel": {"value": "Some Event"},
            "date": {"value": "2001-09-11T00:00:00Z"},
            "location": {"value": "http://www.wikidata.org/entity/Q60"},
            "locationLabel": {"value": "New York City"},
            "deaths": {"value": "2977"},
            "country": {"value": "http://www.wikidata.org/entity/Q30"},
            "countryLabel": {"value": "United States"},
        }
        row.update(overrides)
        return row

    def test_event_fields_extracted(self):
        row = self._make_event_row()
        assert _val(row, "locationLabel") == "New York City"
        assert _qid(_val(row, "location")) == "Q60"
        assert _val(row, "deaths") == "2977"
        assert int(float(_val(row, "deaths"))) == 2977
        assert _val(row, "countryLabel") == "United States"
        assert _qid(_val(row, "country")) == "Q30"

    def test_deaths_parsed_as_int(self):
        row = self._make_event_row(deaths={"value": "100.0"})
        assert int(float(_val(row, "deaths"))) == 100

    def test_missing_location_returns_none(self):
        row = self._make_event_row()
        del row["location"]
        assert _val(row, "location") is None
