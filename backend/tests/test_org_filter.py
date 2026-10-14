"""Unit tests for the top-N-per-country org filter.

Pure-function tests — no DB / dump needed. Tests the logic that drops
long-tail organizations before they reach ArangoDB (plan §1e / 3.4).

Run in-container: ``docker compose exec backend pytest backend/tests/test_org_filter.py``
(host pytest is not installed — do not run on host).
"""

from scripts.load_from_dump import filter_orgs_top_n


def _org(qid: str, country_id: str | None, statements: int) -> dict:
    return {
        "_key": qid,
        "country_id": country_id,
        "wikidata_statements_count": statements,
    }


def test_empty_input_returns_empty_list():
    assert filter_orgs_top_n([], top_n_per_country=100, no_country_cap=500) == []


def test_under_cap_keeps_everything():
    orgs = [_org(f"Q{i}", "Q38", 10 + i) for i in range(5)]
    kept = filter_orgs_top_n(orgs, top_n_per_country=100, no_country_cap=500)
    assert len(kept) == 5
    assert {d["_key"] for d in kept} == {o["_key"] for o in orgs}


def test_at_cap_ranks_by_statement_count_desc():
    # 3 orgs in the same country, cap=2 — keep the two with highest counts.
    orgs = [
        _org("Q1", "Q38", 10),
        _org("Q2", "Q38", 100),
        _org("Q3", "Q38", 50),
    ]
    kept = filter_orgs_top_n(orgs, top_n_per_country=2, no_country_cap=500)
    kept_keys = {d["_key"] for d in kept}
    assert kept_keys == {"Q2", "Q3"}


def test_per_country_cap_is_independent():
    # Two countries, cap=1 each — each country keeps its top.
    orgs = [
        _org("Q1", "Q38", 100),
        _org("Q2", "Q38", 10),
        _org("Q3", "Q142", 100),
        _org("Q4", "Q142", 10),
    ]
    kept = filter_orgs_top_n(orgs, top_n_per_country=1, no_country_cap=500)
    kept_keys = {d["_key"] for d in kept}
    assert kept_keys == {"Q1", "Q3"}


def test_no_country_bucket_uses_separate_cap():
    # Two country-less orgs and two in a country. no_country_cap=1, top_n=10.
    orgs = [
        _org("Q1", None, 100),
        _org("Q2", None, 10),
        _org("Q3", "Q38", 100),
        _org("Q4", "Q38", 10),
    ]
    kept = filter_orgs_top_n(orgs, top_n_per_country=10, no_country_cap=1)
    kept_keys = {d["_key"] for d in kept}
    # Both Q38 orgs kept (under top_n=10), only top-1 of no-country bucket.
    assert kept_keys == {"Q1", "Q3", "Q4"}


def test_country_id_none_coalesces_with_missing_field():
    # Orgs whose country_id is missing AND orgs whose country_id is explicitly
    # None must share the same "no-country" bucket.
    orgs = [
        {"_key": "Q1", "wikidata_statements_count": 100},  # missing field
        _org("Q2", None, 50),                              # explicit None
        _org("Q3", None, 10),
    ]
    kept = filter_orgs_top_n(orgs, top_n_per_country=100, no_country_cap=2)
    kept_keys = {d["_key"] for d in kept}
    assert kept_keys == {"Q1", "Q2"}


def test_tie_broken_by_qid_lexical():
    # Two orgs with identical statement counts — stable deterministic order.
    orgs = [
        _org("Q9", "Q38", 50),
        _org("Q1", "Q38", 50),
        _org("Q5", "Q38", 50),
    ]
    kept = filter_orgs_top_n(orgs, top_n_per_country=2, no_country_cap=500)
    assert [d["_key"] for d in kept] == ["Q1", "Q5"]


def test_missing_statements_count_treated_as_zero():
    orgs = [
        {"_key": "QA", "country_id": "Q38"},  # no statements count
        _org("QB", "Q38", 1),
    ]
    kept = filter_orgs_top_n(orgs, top_n_per_country=1, no_country_cap=500)
    assert [d["_key"] for d in kept] == ["QB"]


def test_default_thresholds_from_plan():
    # Sanity: 150 orgs in one country, default cap=100 → 100 kept.
    orgs = [_org(f"Q{i:04d}", "Q38", i) for i in range(150)]
    kept = filter_orgs_top_n(orgs, top_n_per_country=100, no_country_cap=500)
    assert len(kept) == 100
    # The 100 highest-statement orgs survive (i >= 50).
    surviving_ids = {d["_key"] for d in kept}
    assert all(int(q[1:]) >= 50 for q in surviving_ids)
