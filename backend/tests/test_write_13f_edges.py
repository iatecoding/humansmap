"""Unit tests for scripts/sec/write_13f_edges.py (§11.4.c)."""

from scripts.sec.parse_13f import Holding
from scripts.sec.write_13f_edges import (
    build_edge_docs,
    merge_unmatched_holdings,
)


class _FakeResolver:
    def __init__(self, mapping: dict[str, str | None]):
        self.mapping = mapping
    def resolve(self, cusip, name_of_issuer):
        return self.mapping.get(cusip)


def _h(name, cusip, value=100, shares=10) -> Holding:
    return Holding(name, cusip, value, shares, "SH", "COM")


# ── build_edge_docs ───────────────────────────────────────────────────────────

def test_builds_edge_for_resolved_cusip():
    holdings = {"037833100": _h("APPLE INC", "037833100", 1_000_000, 500)}
    resolver = _FakeResolver({"037833100": "Q312"})
    edges, unmatched = build_edge_docs(
        manager_qid="Q217583",
        period_of_report="2024-12-31",
        filing_date="2025-02-14",
        accession="0001067983-25-000001",
        holdings_by_cusip=holdings,
        resolver=resolver,
    )
    assert len(edges) == 1
    e = edges[0]
    assert e["_key"] == "Q217583__Q312__2024-12-31"
    assert e["_from"] == "organizations/Q217583"
    assert e["_to"] == "organizations/Q312"
    assert e["type"] == "holds_shares_of"
    assert e["cusip"] == "037833100"
    assert e["shares"] == 500
    assert e["value_usd"] == 1_000_000
    assert e["form_type"] == "13F-HR"
    assert e["source"] == "sec_13f"
    assert unmatched == []


def test_unresolved_cusip_goes_to_unmatched():
    holdings = {"999999999": _h("UNKNOWN PRIVATE CO", "999999999", 5_000, 10)}
    resolver = _FakeResolver({"999999999": None})
    edges, unmatched = build_edge_docs(
        manager_qid="Q1",
        period_of_report="2024-12-31",
        filing_date="2025-02-14",
        accession="X",
        holdings_by_cusip=holdings,
        resolver=resolver,
    )
    assert edges == []
    assert unmatched == [{
        "cusip": "999999999",
        "name_of_issuer": "UNKNOWN PRIVATE CO",
        "value_usd": 5_000,
        "shares": 10,
        "title_of_class": "COM",
    }]


def test_self_holding_is_skipped():
    """Manager holding its own stock carries no graph info."""
    holdings = {"AAA": _h("SELF INC", "AAA")}
    resolver = _FakeResolver({"AAA": "Q1"})
    edges, unmatched = build_edge_docs(
        manager_qid="Q1",
        period_of_report="2024-12-31",
        filing_date="2025-02-14",
        accession="X",
        holdings_by_cusip=holdings,
        resolver=resolver,
    )
    assert edges == []
    assert unmatched == []


def test_amendment_flag_marks_form_type():
    holdings = {"AAA": _h("X", "AAA")}
    edges, _ = build_edge_docs(
        manager_qid="Q1",
        period_of_report="2024-12-31",
        filing_date="2025-02-14",
        accession="X",
        holdings_by_cusip=holdings,
        resolver=_FakeResolver({"AAA": "Q2"}),
        is_amendment=True,
    )
    assert edges[0]["form_type"] == "13F-HR/A"
    assert edges[0]["is_amendment"] is True


def test_key_stable_across_quarters():
    """Same (manager, issuer) but different quarter → different keys."""
    resolver = _FakeResolver({"AAA": "Q2"})
    args = dict(
        manager_qid="Q1", filing_date="2025-02-14", accession="X",
        holdings_by_cusip={"AAA": _h("X", "AAA")}, resolver=resolver,
    )
    e1, _ = build_edge_docs(period_of_report="2024-09-30", **args)
    e2, _ = build_edge_docs(period_of_report="2024-12-31", **args)
    assert e1[0]["_key"] != e2[0]["_key"]


def test_mixed_resolved_and_unresolved():
    holdings = {
        "AAA": _h("APPLE", "AAA", 1000, 10),
        "BBB": _h("BRIDGEWATER-HELD PRIVATE", "BBB", 500, 5),
        "CCC": _h("MICROSOFT", "CCC", 2000, 20),
    }
    resolver = _FakeResolver({"AAA": "Q312", "BBB": None, "CCC": "Q2283"})
    edges, unmatched = build_edge_docs(
        manager_qid="Q1",
        period_of_report="2024-12-31",
        filing_date="2025-02-14",
        accession="X",
        holdings_by_cusip=holdings,
        resolver=resolver,
    )
    assert {e["cusip"] for e in edges} == {"AAA", "CCC"}
    assert [u["cusip"] for u in unmatched] == ["BBB"]


# ── merge_unmatched_holdings ──────────────────────────────────────────────────

class _FakeCollection:
    def __init__(self, doc):
        self._doc = doc
        self.updates = []
    def get(self, key):
        return self._doc if self._doc and self._doc.get("_key") == key else None
    def update(self, doc):
        self.updates.append(doc)
        self._doc.update(doc)


class _FakeDB:
    def __init__(self, doc):
        self._col = _FakeCollection(doc)
    def collection(self, name):
        return self._col


def test_merge_dedups_by_cusip():
    db = _FakeDB({"_key": "Q1", "sec_unmatched_holdings": [
        {"cusip": "AAA", "name_of_issuer": "OLD NAME", "value_usd": 100, "shares": 1, "title_of_class": "COM"},
    ]})
    new = [
        {"cusip": "AAA", "name_of_issuer": "NEW NAME", "value_usd": 200, "shares": 2, "title_of_class": "COM"},
        {"cusip": "BBB", "name_of_issuer": "OTHER", "value_usd": 50, "shares": 1, "title_of_class": "COM"},
    ]
    n = merge_unmatched_holdings(db, "organizations", "Q1", new)
    assert n == 2
    final = {r["cusip"]: r for r in db._col._doc["sec_unmatched_holdings"]}
    assert final["AAA"]["name_of_issuer"] == "NEW NAME"  # last-write-wins
    assert "BBB" in final


def test_merge_caps_by_value():
    existing = [{"cusip": f"C{i:03d}", "name_of_issuer": "X", "value_usd": i, "shares": 1, "title_of_class": "COM"} for i in range(5)]
    db = _FakeDB({"_key": "Q1", "sec_unmatched_holdings": existing})
    new = [{"cusip": f"N{i:03d}", "name_of_issuer": "Y", "value_usd": 1000 + i, "shares": 1, "title_of_class": "COM"} for i in range(5)]
    n = merge_unmatched_holdings(db, "organizations", "Q1", new, cap=5)
    assert n == 5
    kept_cusips = {r["cusip"] for r in db._col._doc["sec_unmatched_holdings"]}
    # The 5 highest-value rows are the new ones
    assert kept_cusips == {f"N{i:03d}" for i in range(5)}


def test_merge_noop_on_empty():
    db = _FakeDB({"_key": "Q1"})
    assert merge_unmatched_holdings(db, "organizations", "Q1", []) == 0
    assert db._col.updates == []
