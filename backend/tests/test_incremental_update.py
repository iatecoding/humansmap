"""Focused tests for global incremental person selection."""

import scripts.incremental_update as updater


def test_fetch_global_person_qids_applies_bulk_threshold(monkeypatch):
    captured = {}

    def fake_sparql(query):
        captured["query"] = query
        return [
            {"person": {"value": "http://www.wikidata.org/entity/Q76"}},
            {"person": {"value": "http://www.wikidata.org/entity/Q567"}},
        ]

    monkeypatch.setattr(updater, "_sparql_get", fake_sparql)

    assert updater.fetch_global_person_qids(["Q76", "Q567"], 10) == {
        "Q76",
        "Q567",
    }
    assert "wdt:P31 wd:Q5" in captured["query"]
    assert "FILTER(?stmtCount >= 10)" in captured["query"]


def test_fetch_global_person_qids_fails_closed(monkeypatch):
    def fail(_query):
        raise RuntimeError("endpoint unavailable")

    monkeypatch.setattr(updater, "_sparql_get", fail)
    assert updater.fetch_global_person_qids(["Q76"], 10) == set()
