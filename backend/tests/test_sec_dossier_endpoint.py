"""
Tests for GET /api/organizations/{qid}/filings endpoint (10.6.a).

Run inside the backend container:
    docker compose exec backend pytest tests/test_sec_dossier_endpoint.py -v
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_org_doc(qid="Q9999", sec_metadata=None, unmatched=None):
    """Build a minimal org document as ArangoDB would return it."""
    doc = {
        "_key": qid,
        "_id": f"organizations/{qid}",
        "name_en": "Test Corp",
        "sec_metadata": sec_metadata if sec_metadata is not None else {
            "cik": "0001234567",
            "entity_type": "operating",
            "tickers": ["TC"],
            "exchanges": ["NYSE"],
            "sic_description": "Computer Hardware",
            "state_of_incorporation": "DE",
            "fiscal_year_end": "1231",
            "former_names": [{"name": "Old Corp", "date": "2010-01-01"}],
        },
        "sec_unmatched_reporters": unmatched if unmatched is not None else [],
        "sec_unmatched_directors": [],
    }
    return doc


def _make_affiliation_edge(person_qid, org_qid, edge_type="insider_of", **extra):
    base = {
        "_key": f"{person_qid}_{org_qid}_{edge_type}",
        "_from": f"persons/{person_qid}",
        "_to": f"organizations/{org_qid}",
        "type": edge_type,
    }
    base.update(extra)
    return base


def _make_event_doc(key, org_qid, subtype="exec_departure", filing_date="2026-02-12"):
    return {
        "_key": key,
        "type": "material_event",
        "subtype": subtype,
        "filing_date": filing_date,
        "accession": f"0001234567-26-{key}",
        "filing_url": f"https://www.sec.gov/Archives/edgar/data/1234567/{key}/",
        "org_qid": org_qid,
        "org_cik": "0001234567",
        "items": ["5.02"],
        "is_amendment": False,
    }


# ── Helper: build a mock db that returns pre-canned data ─────────────────────

def _mock_db(org_doc, affiliations=None, persons=None, events=None, event_total=None, subtypes=None):
    """
    Return a mock ArangoDB `db` object whose collection/aql behave
    predictably for the filings endpoint.
    """
    affiliations = affiliations or []
    persons = persons or []
    events = events or []
    event_total = event_total if event_total is not None else len(events)
    subtypes = subtypes or {}

    mock_db_obj = MagicMock()

    # collection.get(qid) returns org_doc
    mock_collection = MagicMock()
    mock_collection.get.return_value = org_doc
    mock_db_obj.collection.return_value = mock_collection

    # aql.execute: distinguish queries by their AQL text snippet
    def aql_execute(query, bind_vars=None, **kwargs):
        q = query.strip()
        if '"insider_of"' in q and '"officer_of"' in q:
            # affiliations query
            return iter(affiliations)
        if "FILTER p._key IN @keys" in q:
            # persons lookup
            return iter(persons)
        if "COLLECT WITH COUNT INTO total" in q:
            return iter([event_total])
        if "COLLECT subtype" in q:
            return iter([{"subtype": k, "count": v} for k, v in subtypes.items()])
        if "SORT e.filing_date DESC" in q:
            return iter(events)
        return iter([])

    mock_db_obj.aql.execute.side_effect = aql_execute
    return mock_db_obj


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestFilingsEndpoint404:
    """Endpoint returns 404 when the org QID does not exist."""

    def test_404_on_missing_qid(self, client: TestClient):
        with patch("app.routers.organizations._get_db") as mock_get_db, \
             patch("app.routers.organizations.get_cached", return_value=None), \
             patch("app.routers.organizations.set_cached"):
            mock_db_obj = MagicMock()
            mock_collection = MagicMock()
            mock_collection.get.return_value = None  # not found
            mock_db_obj.collection.return_value = mock_collection
            mock_get_db.return_value = mock_db_obj

            r = client.get("/api/organizations/Q999999999/filings")

        assert r.status_code == 404

    def test_invalid_qid_rejected(self, client: TestClient):
        r = client.get("/api/organizations/not-a-qid/filings")
        assert r.status_code == 422


class TestFilingsEndpointEmptyEvents:
    """Org has sec_metadata but no events and no affiliations."""

    def test_empty_events_structure(self, client: TestClient):
        org_doc = _make_org_doc()
        db = _mock_db(org_doc)

        with patch("app.routers.organizations._get_db", return_value=db), \
             patch("app.routers.organizations.get_cached", return_value=None), \
             patch("app.routers.organizations.set_cached"):
            r = client.get("/api/organizations/Q9999/filings")

        assert r.status_code == 200
        body = r.json()

        # Top-level keys present
        assert body["qid"] == "Q9999"
        assert "sec_metadata" in body
        assert "insiders" in body
        assert "material_events" in body

        # sec_metadata passes through
        assert body["sec_metadata"]["cik"] == "0001234567"

        # Empty insiders
        assert body["insiders"]["matched"] == []
        assert body["insiders"]["unmatched"] == []
        assert body["insiders"]["totals"]["matched_persons"] == 0
        assert body["insiders"]["totals"]["matched_transactions"] == 0

        # Empty material events
        assert body["material_events"]["items"] == []
        assert body["material_events"]["page"]["total"] == 0
        assert body["material_events"]["by_subtype"] == {}

        # Directors section present and empty
        assert body["directors"]["matched"] == []
        assert body["directors"]["unmatched"] == []
        assert body["directors"]["totals"]["matched"] == 0
        assert body["directors"]["totals"]["unmatched"] == 0

    def test_missing_sec_metadata_returns_empty_dict(self, client: TestClient):
        org_doc = _make_org_doc(sec_metadata={})
        db = _mock_db(org_doc)

        with patch("app.routers.organizations._get_db", return_value=db), \
             patch("app.routers.organizations.get_cached", return_value=None), \
             patch("app.routers.organizations.set_cached"):
            r = client.get("/api/organizations/Q9999/filings")

        assert r.status_code == 200
        assert r.json()["sec_metadata"] == {}


class TestFilingsEndpointFullData:
    """Org with all three sections populated."""

    def _setup(self):
        org_doc = _make_org_doc(
            unmatched=[
                {
                    "reporter_name": "DOE JOHN A",
                    "filing_date": "2026-02-10",
                    "form_type": "4",
                    "accession": "0001234567-26-000001",
                    "cik": "0000112233",
                }
            ]
        )

        aff_edges = [
            _make_affiliation_edge(
                "Q100", "Q9999", "officer_of",
                role="Chief Executive Officer",
                start_date="2020-01-01",
                last_seen="2026-02-01",
                source_accession="0001234567-26-000010",
            ),
            _make_affiliation_edge(
                "Q100", "Q9999", "insider_of",
                transaction_date="2026-03-15",
                shares=511000.0,
                price_usd=178.4,
                direction="D",
                accession="0001234567-26-000020",
                form_type="4",
            ),
        ]

        person_docs = [{"_key": "Q100", "label_en": "Tim Cook", "has_image": True}]

        events = [_make_event_doc("abc001", "Q9999")]
        subtypes = {"exec_departure": 1}

        db = _mock_db(
            org_doc,
            affiliations=aff_edges,
            persons=person_docs,
            events=events,
            event_total=1,
            subtypes=subtypes,
        )
        return db

    def test_full_response_shape(self, client: TestClient):
        db = self._setup()

        with patch("app.routers.organizations._get_db", return_value=db), \
             patch("app.routers.organizations.get_cached", return_value=None), \
             patch("app.routers.organizations.set_cached"):
            r = client.get("/api/organizations/Q9999/filings")

        assert r.status_code == 200
        body = r.json()

        # Matched insider present
        assert len(body["insiders"]["matched"]) == 1
        person = body["insiders"]["matched"][0]
        assert person["person_qid"] == "Q100"
        assert person["name"] == "Tim Cook"
        assert person["has_image"] is True
        assert len(person["roles"]) == 1
        assert person["roles"][0]["type"] == "officer_of"
        assert person["transaction_summary"]["count"] == 1
        assert person["transaction_summary"]["directions"]["D"] == 1
        assert len(person["recent_transactions"]) == 1
        txn = person["recent_transactions"][0]
        assert txn["transaction_date"] == "2026-03-15"
        assert txn["direction"] == "D"
        assert txn["filing_url"].startswith("https://www.sec.gov/Archives/edgar/data/")

        # Unmatched present
        assert len(body["insiders"]["unmatched"]) == 1
        unmatched = body["insiders"]["unmatched"][0]
        assert unmatched["reporter_name"] == "DOE JOHN A"
        assert "filing_url" in unmatched

        # Totals
        totals = body["insiders"]["totals"]
        assert totals["matched_persons"] == 1
        assert totals["matched_transactions"] == 1
        assert totals["unmatched"] == 1

        # Material events
        me = body["material_events"]
        assert me["page"]["total"] == 1
        assert me["by_subtype"]["exec_departure"] == 1
        assert len(me["items"]) == 1
        ev = me["items"][0]
        assert ev["subtype"] == "exec_departure"
        assert ev["key"] == "abc001"


class TestFilingsDirectorsSection:
    """11.5.a: directors section exposes non-transacting directors + unmatched names."""

    def test_non_transacting_director_surfaces_in_directors_section(self, client: TestClient):
        """Al Gore: director_of edge from DEF 14A, no insider_of edges."""
        org_doc = _make_org_doc()
        org_doc["sec_unmatched_directors"] = ["Wanda Austin", "James Bell"]

        aff_edges = [
            _make_affiliation_edge(
                "Q19673", "Q9999", "director_of",
                role="director_of",
                start_date="2003",
                source="sec_def14a",
                match_method="wd_p108_scoped",
            ),
        ]
        person_docs = [{"_key": "Q19673", "label_en": "Al Gore", "has_image": True}]
        db = _mock_db(org_doc, affiliations=aff_edges, persons=person_docs)

        with patch("app.routers.organizations._get_db", return_value=db), \
             patch("app.routers.organizations.get_cached", return_value=None), \
             patch("app.routers.organizations.set_cached"):
            r = client.get("/api/organizations/Q9999/filings")

        assert r.status_code == 200
        body = r.json()

        # Not in insiders (no transactions)
        assert body["insiders"]["matched"] == []
        # In directors
        assert body["directors"]["totals"]["matched"] == 1
        assert body["directors"]["totals"]["unmatched"] == 2
        d = body["directors"]["matched"][0]
        assert d["person_qid"] == "Q19673"
        assert d["name"] == "Al Gore"
        assert d["roles"][0]["type"] == "director_of"
        assert d["roles"][0]["source"] == "sec_def14a"
        assert d["roles"][0]["start_date"] == "2003"
        assert body["directors"]["unmatched"] == ["Wanda Austin", "James Bell"]

    def test_officer_with_transactions_appears_in_both_sections(self, client: TestClient):
        """Tim Cook: officer_of role AND insider_of txns → shows in both."""
        org_doc = _make_org_doc()
        aff_edges = [
            _make_affiliation_edge(
                "Q265852", "Q9999", "officer_of",
                role="CEO",
                start_date="2011",
                source="sec_def14a",
            ),
            _make_affiliation_edge(
                "Q265852", "Q9999", "insider_of",
                transaction_date="2026-03-15",
                shares=100.0,
                direction="D",
                accession="0001234567-26-000099",
            ),
        ]
        person_docs = [{"_key": "Q265852", "label_en": "Tim Cook", "has_image": True}]
        db = _mock_db(org_doc, affiliations=aff_edges, persons=person_docs)

        with patch("app.routers.organizations._get_db", return_value=db), \
             patch("app.routers.organizations.get_cached", return_value=None), \
             patch("app.routers.organizations.set_cached"):
            r = client.get("/api/organizations/Q9999/filings")

        body = r.json()
        assert len(body["insiders"]["matched"]) == 1
        assert len(body["directors"]["matched"]) == 1
        assert body["insiders"]["matched"][0]["person_qid"] == "Q265852"
        assert body["directors"]["matched"][0]["person_qid"] == "Q265852"


class TestFilingsPagination:
    """events_offset and events_limit are forwarded to the AQL query."""

    def test_pagination_params_accepted(self, client: TestClient):
        org_doc = _make_org_doc()
        db = _mock_db(org_doc, event_total=100, events=[])

        with patch("app.routers.organizations._get_db", return_value=db), \
             patch("app.routers.organizations.get_cached", return_value=None), \
             patch("app.routers.organizations.set_cached"):
            r = client.get("/api/organizations/Q9999/filings?events_offset=10&events_limit=5")

        assert r.status_code == 200
        page = r.json()["material_events"]["page"]
        assert page["offset"] == 10
        assert page["limit"] == 5
        assert page["total"] == 100

    def test_events_limit_clamped_at_100(self, client: TestClient):
        org_doc = _make_org_doc()
        db = _mock_db(org_doc)

        with patch("app.routers.organizations._get_db", return_value=db), \
             patch("app.routers.organizations.get_cached", return_value=None), \
             patch("app.routers.organizations.set_cached"):
            r = client.get("/api/organizations/Q9999/filings?events_limit=999")

        # FastAPI Query(le=100) rejects values over 100 with 422
        assert r.status_code == 422
