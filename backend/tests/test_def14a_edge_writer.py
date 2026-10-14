"""Unit tests for fetch_def14a.write_edges (§11.2.d) and global_name_fallback (§11.2.h).

Uses an in-memory DB shim — no ArangoDB import required.
Run inside the backend container:
    docker compose exec backend python -m pytest tests/test_def14a_edge_writer.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

# Ensure the backend package root is on sys.path (before any local imports)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.sec.fetch_def14a import write_edges  # noqa: E402
from scripts.sec._match import global_name_fallback  # noqa: E402


# ---------------------------------------------------------------------------
# Fake DB shim  (<80 LoC)
# ---------------------------------------------------------------------------


class _FakeCollection:
    """Minimal collection shim tracking inserts and updates."""

    def __init__(self, docs: dict | None = None):
        self._docs: dict[str, dict] = dict(docs or {})
        self.updates: list[dict] = []

    def get(self, key: str) -> dict | None:
        return self._docs.get(key)

    def update(self, doc: dict) -> None:
        key = doc.get("_key") or doc.get("_id", "").split("/")[-1]
        if key:
            existing = self._docs.setdefault(key, {})
            existing.update(doc)
        self.updates.append(doc)


class _FakeAql:
    """Records AQL executions; returns configured hits or empty by default."""

    def __init__(
        self,
        candidates_by_org: dict | None = None,
        global_persons: list[dict] | None = None,
    ):
        self._candidates = candidates_by_org or {}
        # persons returned when the global fallback AQL fires (keyed by last,first)
        self._global_persons: list[dict] = global_persons or []
        self.calls: list[dict] = []
        self._edges: list[dict] = []

    def execute(self, query: str, bind_vars: dict | None = None) -> list:
        self.calls.append({"query": query, "bind_vars": bind_vars})
        bv = bind_vars or {}

        # Candidates lookup
        if "sec_person_candidates" in query and "org_qid" in bv:
            return iter(self._candidates.get(bv["org_qid"], []))

        # Global fallback AQL: match by @last/@first
        if "FOR p IN persons" in query and "last" in bv and "first" in bv:
            last = bv["last"]
            first = bv["first"]
            results = []
            for p in self._global_persons:
                parts = p.get("name_en", "").upper().split()
                if parts and parts[-1] == last and parts[0] == first:
                    results.append({
                        "qid": p["qid"],
                        "occupations": p.get("occupations", []),
                        "nationalities": p.get("nationalities", []),
                    })
            return iter(results)

        # Edge upsert — capture edges
        if "INSERT doc INTO" in query and "batch" in bv:
            self._edges.extend(bv["batch"])
            return iter([])

        return iter([])


class _FakeDb:
    def __init__(
        self,
        candidates_by_org: dict | None = None,
        org_docs: dict | None = None,
        global_persons: list[dict] | None = None,
    ):
        self.aql = _FakeAql(candidates_by_org, global_persons)
        self._orgs = _FakeCollection(org_docs)

    def collection(self, name: str):
        if name == "organizations":
            return self._orgs
        raise ValueError(f"Unknown collection: {name}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_candidates(qid: str, name: str) -> dict:
    return {"person_qid": qid, "person_name_en": name, "name_variants": None, "source": "test"}


def _stub_tier1(name, candidates):
    """Simple exact-match stub: matches if any candidate's person_name_en == name."""
    for c in candidates:
        if c["person_name_en"].lower() == name.lower():
            return c["person_qid"], "def14a_scoped", "exact"
    return None, None, None


def _stub_last_name(token, *, candidates):
    """Simple last-name stub: matches candidates whose last word == token (case-insensitive)."""
    hits = [
        c for c in candidates
        if c["person_name_en"].split()[-1].upper() == token.upper()
    ]
    return hits


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


PATCH_T1 = "scripts.sec.fetch_def14a.tier1_match"
PATCH_LN = "scripts.sec.fetch_def14a.last_name_match"


def test_director_exact_match_creates_edge():
    candidates = [_make_candidates("Q1", "Tim Cook")]
    db = _FakeDb(candidates_by_org={"QApple": candidates})

    parsed = {
        "directors": [
            {"name": "Tim Cook", "role": "director", "start_date_hint": "2011", "raw_blob": "..."},
        ],
        "peonames": [],
    }

    with patch(PATCH_T1, side_effect=_stub_tier1), patch(PATCH_LN, side_effect=_stub_last_name):
        result = write_edges("QApple", "0000320193", "2025-03-15", parsed, db)

    assert result["matched_directors"] == 1
    assert result["matched_officers"] == 0
    assert result["unmatched"] == 0

    edges = db.aql._edges
    assert len(edges) == 1
    e = edges[0]
    assert e["_key"] == "Q1__QApple__def14a"
    assert e["_from"] == "persons/Q1"
    assert e["_to"] == "organizations/QApple"
    assert e["type"] == "director_of"
    assert e["match_method"] == "def14a_scoped"


def test_director_start_date_hint_takes_priority():
    """start_date_hint from table/mini layout wins over filing_date fallback."""
    candidates = [_make_candidates("Q1", "Tim Cook")]
    db = _FakeDb(candidates_by_org={"QApple": candidates})

    parsed = {
        "directors": [
            {"name": "Tim Cook", "role": "director", "start_date_hint": "2011", "raw_blob": ""},
        ],
        "peonames": [],
    }

    with patch(PATCH_T1, side_effect=_stub_tier1), patch(PATCH_LN, side_effect=_stub_last_name):
        write_edges("QApple", "0000320193", "2025-03-15", parsed, db)

    assert db.aql._edges[0]["start_date"] == "2011"


def test_director_filing_date_fallback_when_no_hint():
    """Filing date is used when start_date_hint is None."""
    candidates = [_make_candidates("Q1", "Tim Cook")]
    db = _FakeDb(candidates_by_org={"QApple": candidates})

    parsed = {
        "directors": [
            {"name": "Tim Cook", "role": "director", "start_date_hint": None, "raw_blob": ""},
        ],
        "peonames": [],
    }

    with patch(PATCH_T1, side_effect=_stub_tier1), patch(PATCH_LN, side_effect=_stub_last_name):
        write_edges("QApple", "0000320193", "2025-03-15", parsed, db)

    assert db.aql._edges[0]["start_date"] == "2025-03-15"


def test_peo_last_name_resolves_to_officer_of():
    """PeoName 'Mr. Cook' (single token after prefix strip) → last_name_match → officer_of."""
    candidates = [_make_candidates("Q1", "Tim Cook")]
    db = _FakeDb(candidates_by_org={"QApple": candidates})

    parsed = {
        "directors": [],
        "peonames": [
            {"raw_name": "Mr. Cook", "context_element": "ecd:PeoName", "raw_blob": ""},
        ],
    }

    with patch(PATCH_T1, side_effect=_stub_tier1), patch(PATCH_LN, side_effect=_stub_last_name):
        result = write_edges("QApple", "0000320193", "2025-03-15", parsed, db)

    assert result["matched_officers"] == 1
    assert result["unmatched"] == 0
    edges = db.aql._edges
    assert len(edges) == 1
    assert edges[0]["type"] == "officer_of"
    assert edges[0]["start_date"] == "2025-03-15"


def test_peo_ambiguous_last_name_goes_unmatched():
    """Multiple candidates with same last name → unmatched."""
    candidates = [
        _make_candidates("Q1", "Tim Cook"),
        _make_candidates("Q2", "Jane Cook"),
    ]
    db = _FakeDb(candidates_by_org={"QOrg": candidates})

    parsed = {
        "directors": [],
        "peonames": [
            {"raw_name": "Mr. Cook", "context_element": "ecd:PeoName", "raw_blob": ""},
        ],
    }

    with patch(PATCH_T1, side_effect=_stub_tier1), patch(PATCH_LN, side_effect=_stub_last_name):
        result = write_edges("QOrg", "0000111111", "2025-01-01", parsed, db)

    assert result["matched_officers"] == 0
    assert result["unmatched"] == 1


def test_unmatched_appended_to_org_doc():
    """Unmatched names accumulate on sec_unmatched_directors."""
    db = _FakeDb(candidates_by_org={"QOrg": []})

    parsed = {
        "directors": [
            {"name": "Unknown Person", "role": "director", "start_date_hint": None, "raw_blob": ""},
        ],
        "peonames": [],
    }

    with patch(PATCH_T1, return_value=(None, None, None)), \
         patch(PATCH_LN, return_value=[]):
        result = write_edges("QOrg", "0000111111", "2025-01-01", parsed, db)

    assert result["unmatched"] == 1
    assert result["updated_unmatched_list"] is True
    org_doc = db._orgs._docs.get("QOrg", {})
    assert "Unknown Person" in org_doc.get("sec_unmatched_directors", [])


def test_unmatched_cap_at_100():
    """sec_unmatched_directors is capped at 100 entries."""
    # Pre-populate with 99 existing entries
    existing = [f"Name{i}" for i in range(99)]
    db = _FakeDb(
        candidates_by_org={"QOrg": []},
        org_docs={"QOrg": {"_key": "QOrg", "sec_unmatched_directors": existing}},
    )

    parsed = {
        "directors": [
            {"name": "New Director A", "role": "director", "start_date_hint": None, "raw_blob": ""},
            {"name": "New Director B", "role": "director", "start_date_hint": None, "raw_blob": ""},
        ],
        "peonames": [],
    }

    with patch(PATCH_T1, return_value=(None, None, None)), \
         patch(PATCH_LN, return_value=[]):
        write_edges("QOrg", "0000111111", "2025-01-01", parsed, db)

    org_doc = db._orgs._docs.get("QOrg", {})
    directors_list = org_doc.get("sec_unmatched_directors", [])
    assert len(directors_list) == 100


def test_dry_run_no_writes():
    """dry_run=True returns correct counts but performs zero DB writes."""
    candidates = [_make_candidates("Q1", "Tim Cook")]
    db = _FakeDb(candidates_by_org={"QApple": candidates})

    parsed = {
        "directors": [
            {"name": "Tim Cook", "role": "director", "start_date_hint": "2011", "raw_blob": ""},
            {"name": "Unknown Director", "role": "director", "start_date_hint": None, "raw_blob": ""},
        ],
        "peonames": [],
    }

    def _t1(name, candidates):
        if name == "Tim Cook":
            return "Q1", "def14a_scoped", "exact"
        return None, None, None

    with patch(PATCH_T1, side_effect=_t1), patch(PATCH_LN, side_effect=_stub_last_name):
        result = write_edges("QApple", "0000320193", "2025-03-15", parsed, db, dry_run=True)

    assert result["matched_directors"] == 1
    assert result["unmatched"] == 1
    # No AQL calls for upsert (candidates AQL is skipped too in dry_run)
    upsert_calls = [c for c in db.aql.calls if "INSERT doc INTO" in c["query"]]
    assert len(upsert_calls) == 0
    # No org collection updates
    assert len(db._orgs.updates) == 0


# ---------------------------------------------------------------------------
# global_name_fallback unit tests (§11.2.h)
# ---------------------------------------------------------------------------


def _make_global_person(qid: str, name_en: str, occupations=None, nationalities=None) -> dict:
    return {
        "qid": qid,
        "name_en": name_en,
        "occupations": occupations or [],
        "nationalities": nationalities or [],
    }


class _GlobalFallbackDb:
    """Minimal DB shim for global_name_fallback — only needs aql.execute."""

    def __init__(self, persons: list[dict]):
        self.aql = _FakeAql(global_persons=persons)


def test_global_fallback_unique_name_matches():
    """Exactly one hit in persons → global_unique confidence."""
    db = _GlobalFallbackDb(persons=[
        _make_global_person("Q999", "Alice Unique", ["businessperson"], ["US"]),
    ])
    qid, method, confidence = global_name_fallback("Alice Unique", db)
    assert qid == "Q999"
    assert method == "def14a_global"
    assert confidence == "global_unique"


def test_global_fallback_no_hit_returns_none():
    """No persons match → None."""
    db = _GlobalFallbackDb(persons=[
        _make_global_person("Q1", "Bob Smith", ["businessperson"], ["US"]),
    ])
    qid, method, confidence = global_name_fallback("Alice Unique", db)
    assert qid is None
    assert method is None
    assert confidence is None


def test_global_fallback_ambiguous_names_no_occupation_returns_none():
    """Two hits with no distinguishing occupation → None (prefer unmatched)."""
    db = _GlobalFallbackDb(persons=[
        _make_global_person("Q1", "John Smith", [], ["US"]),
        _make_global_person("Q2", "John Smith", [], ["GB"]),
    ])
    qid, method, confidence = global_name_fallback("John Smith", db)
    assert qid is None


def test_global_fallback_occupation_disambiguates():
    """Two 'John Smith's but only one is a businessperson → occupation match."""
    db = _GlobalFallbackDb(persons=[
        _make_global_person("Q1", "John Smith", ["actor"], ["US"]),
        _make_global_person("Q2", "John Smith", ["businessperson", "executive"], ["US"]),
    ])
    qid, method, confidence = global_name_fallback("John Smith", db)
    assert qid == "Q2"
    assert method == "def14a_global_occupation"
    assert confidence == "global_occupation"


def test_global_fallback_occupation_still_ambiguous_returns_none():
    """Two businessperson hits → still ambiguous → None."""
    db = _GlobalFallbackDb(persons=[
        _make_global_person("Q1", "John Smith", ["businessperson"], ["US"]),
        _make_global_person("Q2", "John Smith", ["businessperson"], ["GB"]),
    ])
    qid, method, confidence = global_name_fallback("John Smith", db)
    assert qid is None


def test_global_fallback_country_disambiguates():
    """Two businessperson hits, org_country narrows to 1 → country match."""
    db = _GlobalFallbackDb(persons=[
        _make_global_person("Q1", "John Smith", ["businessperson"], ["US"]),
        _make_global_person("Q2", "John Smith", ["businessperson"], ["GB"]),
    ])
    qid, method, confidence = global_name_fallback("John Smith", db, org_country="US")
    assert qid == "Q1"
    assert method == "def14a_global_country"
    assert confidence == "global_country"


def test_global_fallback_short_name_returns_none():
    """Single-token name (no parseable last+first) → None."""
    db = _GlobalFallbackDb(persons=[])
    qid, method, confidence = global_name_fallback("Musk", db)
    assert qid is None


def test_write_edges_falls_back_to_global_after_scoped_miss():
    """write_edges: tier1_match misses, global_name_fallback resolves → edge written."""
    global_persons = [
        _make_global_person("Q999", "Alice Gore", ["businessperson"], ["US"]),
    ]
    db = _FakeDb(
        candidates_by_org={"QOrg": []},
        global_persons=global_persons,
    )
    parsed = {
        "directors": [
            {"name": "Alice Gore", "role": "director", "start_date_hint": None, "raw_blob": ""},
        ],
        "peonames": [],
    }

    with patch(PATCH_T1, return_value=(None, None, None)), \
         patch(PATCH_LN, return_value=[]):
        result = write_edges("QOrg", "0000111111", "2025-01-01", parsed, db)

    assert result["matched_directors"] == 1
    assert result["unmatched"] == 0
    edges = db.aql._edges
    assert len(edges) == 1
    assert edges[0]["match_method"].startswith("def14a_global")


def test_write_edges_global_fallback_peo_two_tokens():
    """PEO row with 2+ tokens falls back to global after scoped miss."""
    global_persons = [
        _make_global_person("Q888", "Bob Williams", ["executive"], ["US"]),
    ]
    db = _FakeDb(
        candidates_by_org={"QOrg": []},
        global_persons=global_persons,
    )
    parsed = {
        "directors": [],
        "peonames": [
            {"raw_name": "Mr. Bob Williams", "context_element": "ecd:PeoName", "raw_blob": ""},
        ],
    }

    with patch(PATCH_T1, return_value=(None, None, None)), \
         patch(PATCH_LN, return_value=[]):
        result = write_edges("QOrg", "0000111111", "2025-01-01", parsed, db)

    assert result["matched_officers"] == 1
    assert result["unmatched"] == 0
    edges = db.aql._edges
    assert len(edges) == 1
    assert edges[0]["type"] == "officer_of"
    assert edges[0]["match_method"].startswith("def14a_global")
