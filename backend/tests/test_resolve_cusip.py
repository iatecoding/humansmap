"""Unit tests for scripts/sec/resolve_cusip.py (§11.4.b).

Run in the backend container:
    docker compose exec backend pytest tests/test_resolve_cusip.py -v
"""

import json

import pytest

from scripts.sec.resolve_cusip import (
    CusipResolver,
    normalize_issuer_name,
    _resolve_via_db,
)


# ── normalize_issuer_name ─────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("Apple Inc.", "APPLE"),
    ("APPLE INC", "APPLE"),
    ("ALPHABET INC CL A", "ALPHABET"),
    ("ALPHABET INC CLASS C", "ALPHABET"),
    ("Berkshire Hathaway Inc.", "BERKSHIRE HATHAWAY"),
    ("Berkshire Hathaway Inc. Class B", "BERKSHIRE HATHAWAY"),
    ("MICROSOFT CORP COM", "MICROSOFT"),
    ("The Coca-Cola Company", "COCA COLA"),
    ("Nestlé S.A.", "NESTLE"),
    ("JPMorgan Chase & Co.", "JPMORGAN CHASE"),
    ("VISA INC CLASS A", "VISA"),
    ("Tesla, Inc.", "TESLA"),
    ("Meta Platforms Inc Cl A", "META PLATFORMS"),
    ("", ""),
    ("   ", ""),
])
def test_normalize(raw, expected):
    assert normalize_issuer_name(raw) == expected


def test_normalize_idempotent():
    """Running normalization twice yields the same result."""
    for raw in ["Apple Inc.", "ALPHABET INC CL A", "The Coca-Cola Company"]:
        once = normalize_issuer_name(raw)
        twice = normalize_issuer_name(once)
        assert once == twice


# ── _resolve_via_db with fake db ──────────────────────────────────────────────

class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows
    def __iter__(self):
        return iter(self._rows)


class _FakeAQL:
    def __init__(self, rows):
        self.rows = rows
        self.last_bind_vars = None
    def execute(self, aql, bind_vars=None):
        self.last_bind_vars = bind_vars
        return _FakeCursor(self.rows)


class _FakeDB:
    def __init__(self, rows):
        self.aql = _FakeAQL(rows)


def test_resolve_unique_hit():
    db = _FakeDB([{"qid": "Q312", "name_en": "Apple Inc.", "aliases_en": None}])
    assert _resolve_via_db("APPLE INC COM", db) == "Q312"


def test_resolve_via_alias():
    db = _FakeDB([{"qid": "Q95", "name_en": "Alphabet Inc.", "aliases_en": ["Google", "Alphabet"]}])
    assert _resolve_via_db("ALPHABET INC CL A", db) == "Q95"


def test_resolve_ambiguous_returns_none():
    db = _FakeDB([
        {"qid": "Q1", "name_en": "Apex Inc.", "aliases_en": None},
        {"qid": "Q2", "name_en": "APEX Corp", "aliases_en": None},
    ])
    assert _resolve_via_db("APEX INC", db) is None


def test_resolve_no_hit():
    db = _FakeDB([])
    assert _resolve_via_db("UNKNOWN ISSUER INC", db) is None


def test_resolve_empty_name():
    db = _FakeDB([])
    assert _resolve_via_db("", db) is None


def test_resolve_noise_candidates_filtered():
    """AQL prefix filter returns noisy rows; python pass must discard them."""
    db = _FakeDB([
        {"qid": "Q1", "name_en": "Apple Bank for Savings", "aliases_en": None},
        {"qid": "Q312", "name_en": "Apple Inc.", "aliases_en": None},
    ])
    assert _resolve_via_db("APPLE INC", db) == "Q312"


# ── CusipResolver cache roundtrip ─────────────────────────────────────────────

def test_cache_roundtrip(tmp_path):
    cache = tmp_path / "cusip_cache.json"
    db = _FakeDB([{"qid": "Q312", "name_en": "Apple Inc.", "aliases_en": None}])
    r = CusipResolver(db, cache_path=cache)

    qid = r.resolve("037833100", "APPLE INC")
    assert qid == "Q312"
    r.save()

    # Re-load from disk — DB must not be re-queried
    dead_db = _FakeDB([])
    r2 = CusipResolver(dead_db, cache_path=cache)
    assert r2.resolve("037833100", "APPLE INC") == "Q312"

    saved = json.loads(cache.read_text())
    assert saved == {"037833100": "Q312"}


def test_cache_stores_misses(tmp_path):
    """Negative results are cached so we don't re-scan each quarter."""
    cache = tmp_path / "cache.json"
    db = _FakeDB([])
    r = CusipResolver(db, cache_path=cache)
    assert r.resolve("999999999", "UNKNOWN INC") is None
    r.save()
    saved = json.loads(cache.read_text())
    assert saved == {"999999999": None}


def test_cusip_uppercased(tmp_path):
    db = _FakeDB([{"qid": "Q312", "name_en": "Apple Inc.", "aliases_en": None}])
    r = CusipResolver(db, cache_path=tmp_path / "c.json")
    r.resolve("abc123def", "APPLE INC")
    assert "ABC123DEF" in r._cache


def test_stats(tmp_path):
    db = _FakeDB([{"qid": "Q312", "name_en": "Apple Inc.", "aliases_en": None}])
    r = CusipResolver(db, cache_path=tmp_path / "c.json")
    r.resolve("037833100", "APPLE INC")
    r.resolve("999999999", "UNKNOWN INC")  # miss
    s = r.stats()
    assert s == {"total": 2, "matched": 1, "unmatched": 1}
