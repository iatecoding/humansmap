"""
Tests for GET /api/persons/{qid}/filings endpoint (10.6.b).

Run inside the backend container:
    docker compose exec backend pytest tests/test_sec_dossier_person_endpoint.py -v
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_person_doc(qid="Q100", sec_cik=None):
    """Build a minimal person document as ArangoDB would return it."""
    doc = {
        "_key": qid,
        "_id": f"persons/{qid}",
        "name_en": "Test Person",
    }
    if sec_cik is not None:
        doc["sec_cik"] = sec_cik
    return doc


def _make_org_doc(qid="Q9999", name_en="Test Corp", cik="0001234567", has_image=True):
    return {
        "_key": qid,
        "_id": f"organizations/{qid}",
        "name_en": name_en,
        "has_image": has_image,
        "sec_metadata": {"cik": cik},
    }


def _make_affiliation_edge(person_qid, org_qid, edge_type="insider_of", **extra):
    base = {
        "_key": f"{person_qid}_{org_qid}_{edge_type}",
        "_from": f"persons/{person_qid}",
        "_to": f"organizations/{org_qid}",
        "type": edge_type,
    }
    base.update(extra)
    return base


# ── Helper: build a mock db ───────────────────────────────────────────────────

def _mock_db(person_doc, affiliations=None, orgs=None):
    """
    Return a mock ArangoDB `db` object whose collection/aql behave
    predictably for the person filings endpoint.
    """
    affiliations = affiliations or []
    orgs = orgs or []

    mock_db_obj = MagicMock()

    # collection(PERSONS).get(qid) → person_doc
    mock_persons_col = MagicMock()
    mock_persons_col.get.return_value = person_doc

    def collection_side_effect(name):
        if name == "persons":
            return mock_persons_col
        col = MagicMock()
        col.get.return_value = None
        return col

    mock_db_obj.collection.side_effect = collection_side_effect

    # aql.execute: distinguish by query content
    def aql_execute(query, bind_vars=None, **kwargs):
        q = query.strip()
        if '"insider_of"' in q and '"officer_of"' in q:
            # affiliations query
            return iter(affiliations)
        if "FILTER o._key IN @keys" in q:
            # orgs lookup
            return iter(orgs)
        return iter([])

    mock_db_obj.aql.execute.side_effect = aql_execute
    return mock_db_obj


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestPersonFilingsEndpoint404:
    """Endpoint returns 404 when the person QID does not exist."""

    def test_404_on_missing_qid(self, client: TestClient):
        with patch("app.routers.persons._get_db") as mock_get_db, \
             patch("app.routers.persons.get_cached", return_value=None), \
             patch("app.routers.persons.set_cached"):
            mock_db_obj = MagicMock()
            mock_col = MagicMock()
            mock_col.get.return_value = None  # not found
            mock_db_obj.collection.return_value = mock_col
            mock_get_db.return_value = mock_db_obj

            r = client.get("/api/persons/Q999999999/filings")

        assert r.status_code == 404

    def test_invalid_qid_rejected(self, client: TestClient):
        r = client.get("/api/persons/not-a-qid/filings")
        assert r.status_code == 422


class TestPersonFilingsNoSecData:
    """Person exists but has no sec_cik → empty-but-valid response, not 404."""

    def test_no_cik_returns_empty_response(self, client: TestClient):
        person_doc = _make_person_doc(sec_cik=None)
        db = _mock_db(person_doc)

        with patch("app.routers.persons._get_db", return_value=db), \
             patch("app.routers.persons.get_cached", return_value=None), \
             patch("app.routers.persons.set_cached"):
            r = client.get("/api/persons/Q100/filings")

        assert r.status_code == 200
        body = r.json()

        assert body["qid"] == "Q100"
        assert body["sec_cik"] is None
        assert body["roles"] == []
        assert body["transactions"] == []
        assert body["transaction_summary"]["count"] == 0
        assert body["transaction_summary"]["directions"] == {}
        assert body["transaction_summary"]["by_org"] == []
        assert body["page"]["total"] == 0

    def test_person_with_sec_cik_but_no_affiliations(self, client: TestClient):
        person_doc = _make_person_doc(sec_cik="0001214156")
        # No affiliations → AQL returns empty
        db = _mock_db(person_doc, affiliations=[], orgs=[])

        with patch("app.routers.persons._get_db", return_value=db), \
             patch("app.routers.persons.get_cached", return_value=None), \
             patch("app.routers.persons.set_cached"):
            r = client.get("/api/persons/Q100/filings")

        assert r.status_code == 200
        body = r.json()
        assert body["sec_cik"] == "0001214156"
        assert body["roles"] == []
        assert body["transactions"] == []
        assert body["transaction_summary"]["count"] == 0


class TestPersonFilingsFullData:
    """Person with roles + transactions across two orgs."""

    def _setup(self):
        person_doc = _make_person_doc(sec_cik="0001214156")

        aff_edges = [
            # Role at Apple
            _make_affiliation_edge(
                "Q100", "Q9999", "officer_of",
                role="Chief Executive Officer",
                start_date="2011-08-24",
                last_seen="2026-02-01",
                source_accession="0001234567-26-000010",
            ),
            # Role at Apple (beneficial owner)
            _make_affiliation_edge(
                "Q100", "Q9999", "beneficial_owner_of",
                role=None,
                start_date="2012-01-01",
                last_seen="2026-01-01",
                source_accession="0001234567-26-000011",
            ),
            # Transaction at Apple
            _make_affiliation_edge(
                "Q100", "Q9999", "insider_of",
                transaction_date="2026-03-15",
                shares=511000.0,
                price_usd=178.4,
                direction="D",
                accession="0001234567-26-000020",
                form_type="4",
            ),
            # Transaction at Apple (older)
            _make_affiliation_edge(
                "Q100", "Q9999", "insider_of",
                transaction_date="2025-06-10",
                shares=100000.0,
                price_usd=150.0,
                direction="A",
                accession="0001234567-25-000005",
                form_type="4",
            ),
            # Transaction at a second org
            _make_affiliation_edge(
                "Q100", "Q8888", "insider_of",
                transaction_date="2024-01-05",
                shares=50000.0,
                price_usd=200.0,
                direction="D",
                accession="0009876543-24-000001",
                form_type="4",
            ),
        ]

        org_docs = [
            _make_org_doc("Q9999", "Apple Inc.", "0001234567"),
            _make_org_doc("Q8888", "Other Corp", "0009876543", has_image=False),
        ]

        db = _mock_db(person_doc, affiliations=aff_edges, orgs=org_docs)
        return db

    def test_full_response_shape(self, client: TestClient):
        db = self._setup()

        with patch("app.routers.persons._get_db", return_value=db), \
             patch("app.routers.persons.get_cached", return_value=None), \
             patch("app.routers.persons.set_cached"):
            r = client.get("/api/persons/Q100/filings")

        assert r.status_code == 200
        body = r.json()

        assert body["qid"] == "Q100"
        assert body["sec_cik"] == "0001214156"

        # Roles: officer_of + beneficial_owner_of
        assert len(body["roles"]) == 2
        role_types = {ro["type"] for ro in body["roles"]}
        assert "officer_of" in role_types
        assert "beneficial_owner_of" in role_types
        officer = next(ro for ro in body["roles"] if ro["type"] == "officer_of")
        assert officer["org_qid"] == "Q9999"
        assert officer["org_name"] == "Apple Inc."
        assert officer["org_has_image"] is True
        assert officer["role"] == "Chief Executive Officer"

        # Transaction summary: 3 insider edges total
        ts = body["transaction_summary"]
        assert ts["count"] == 3
        assert ts["first_date"] == "2024-01-05"
        assert ts["last_date"] == "2026-03-15"
        assert ts["directions"]["D"] == 2
        assert ts["directions"]["A"] == 1
        assert len(ts["by_org"]) == 2
        # by_org sorted by last_date desc — Apple first
        assert ts["by_org"][0]["org_qid"] == "Q9999"
        assert ts["by_org"][0]["count"] == 2

        # Transactions: first page (limit 50, all 3 returned)
        txns = body["transactions"]
        assert len(txns) == 3
        # Sorted desc: 2026-03-15 first
        assert txns[0]["transaction_date"] == "2026-03-15"
        assert txns[0]["direction"] == "D"
        assert txns[0]["org_qid"] == "Q9999"
        assert txns[0]["filing_url"].startswith("https://www.sec.gov/Archives/edgar/data/")
        # Last entry: 2024-01-05
        assert txns[2]["transaction_date"] == "2024-01-05"

        # Page metadata
        assert body["page"]["total"] == 3
        assert body["page"]["offset"] == 0

    def test_transactions_sorted_desc_by_date(self, client: TestClient):
        db = self._setup()
        with patch("app.routers.persons._get_db", return_value=db), \
             patch("app.routers.persons.get_cached", return_value=None), \
             patch("app.routers.persons.set_cached"):
            r = client.get("/api/persons/Q100/filings")

        txns = r.json()["transactions"]
        dates = [t["transaction_date"] for t in txns]
        assert dates == sorted(dates, reverse=True)

    def test_filing_url_constructed_from_org_cik(self, client: TestClient):
        db = self._setup()
        with patch("app.routers.persons._get_db", return_value=db), \
             patch("app.routers.persons.get_cached", return_value=None), \
             patch("app.routers.persons.set_cached"):
            r = client.get("/api/persons/Q100/filings")

        txns = r.json()["transactions"]
        apple_txn = next(t for t in txns if t["org_qid"] == "Q9999")
        assert "1234567" in apple_txn["filing_url"]
        assert "Archives/edgar" in apple_txn["filing_url"]


class TestPersonFilingsPagination:
    """transactions_offset and transactions_limit slice the result."""

    def _setup_many_txns(self):
        person_doc = _make_person_doc(sec_cik="0001214156")
        # 5 insider edges
        edges = [
            _make_affiliation_edge(
                "Q100", "Q9999", "insider_of",
                transaction_date=f"2026-0{i+1}-01",
                shares=float(i * 1000),
                price_usd=100.0,
                direction="D",
                accession=f"0001234567-26-00000{i}",
                form_type="4",
            )
            for i in range(1, 6)
        ]
        orgs = [_make_org_doc("Q9999", "Apple Inc.", "0001234567")]
        db = _mock_db(person_doc, affiliations=edges, orgs=orgs)
        return db

    def test_limit_applied(self, client: TestClient):
        db = self._setup_many_txns()
        with patch("app.routers.persons._get_db", return_value=db), \
             patch("app.routers.persons.get_cached", return_value=None), \
             patch("app.routers.persons.set_cached"):
            r = client.get("/api/persons/Q100/filings?transactions_limit=2")

        assert r.status_code == 200
        body = r.json()
        assert len(body["transactions"]) == 2
        assert body["page"]["limit"] == 2
        assert body["page"]["total"] == 5

    def test_offset_applied(self, client: TestClient):
        db = self._setup_many_txns()
        with patch("app.routers.persons._get_db", return_value=db), \
             patch("app.routers.persons.get_cached", return_value=None), \
             patch("app.routers.persons.set_cached"):
            r = client.get("/api/persons/Q100/filings?transactions_offset=3&transactions_limit=10")

        assert r.status_code == 200
        body = r.json()
        assert len(body["transactions"]) == 2  # 5 total, skip 3
        assert body["page"]["offset"] == 3

    def test_limit_clamped_at_100(self, client: TestClient):
        r = client.get("/api/persons/Q100/filings?transactions_limit=999")
        # FastAPI Query(le=100) rejects values over 100 with 422
        assert r.status_code == 422
