"""
Unit tests for backend/scripts/sec/build_cik_map.py helper functions.

Covers normalisation logic only — no network or DB access required.

Run inside the Docker container:
    docker compose exec backend pytest tests/test_build_cik_map.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow import without the app on PYTHONPATH
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.sec.build_cik_map import (
    collapse_sparql_rows,
    load_overrides,
    merge_overrides,
    uri_to_qid,
    zero_pad_cik,
)


# ---------------------------------------------------------------------------
# uri_to_qid
# ---------------------------------------------------------------------------


def test_uri_to_qid_full_uri():
    assert uri_to_qid("http://www.wikidata.org/entity/Q312") == "Q312"


def test_uri_to_qid_already_qid():
    assert uri_to_qid("Q312") == "Q312"


def test_uri_to_qid_none_returns_none():
    assert uri_to_qid(None) is None


def test_uri_to_qid_empty_string_returns_empty():
    # Empty string is falsy — function returns None
    assert uri_to_qid("") is None


def test_uri_to_qid_person_entity():
    assert uri_to_qid("http://www.wikidata.org/entity/Q76") == "Q76"


# ---------------------------------------------------------------------------
# zero_pad_cik
# ---------------------------------------------------------------------------


def test_zero_pad_cik_string_short():
    assert zero_pad_cik("320193") == "0000320193"


def test_zero_pad_cik_int():
    assert zero_pad_cik(320193) == "0000320193"


def test_zero_pad_cik_already_padded():
    assert zero_pad_cik("0000320193") == "0000320193"


def test_zero_pad_cik_one_digit():
    assert zero_pad_cik("1") == "0000000001"


def test_zero_pad_cik_ten_digits_unchanged():
    assert zero_pad_cik("1234567890") == "1234567890"


def test_zero_pad_cik_strips_whitespace():
    assert zero_pad_cik("  320193  ") == "0000320193"


# ---------------------------------------------------------------------------
# collapse_sparql_rows
# ---------------------------------------------------------------------------


def _make_row(entity_qid: str, cik: str, ticker=None, instance_of_qid=None, label=None):
    """Build a fake SPARQL bindings row dict."""
    row: dict = {
        "entity": {"value": f"http://www.wikidata.org/entity/{entity_qid}"},
        "cik": {"value": cik},
        "entityLabel": {"value": label or entity_qid},
    }
    if ticker is not None:
        row["ticker"] = {"value": ticker}
    if instance_of_qid is not None:
        row["instanceOf"] = {"value": f"http://www.wikidata.org/entity/{instance_of_qid}"}
    return row


def test_collapse_single_row():
    rows = [_make_row("Q312", "320193", ticker="AAPL", instance_of_qid="Q891723")]
    result = collapse_sparql_rows(rows)
    assert "Q312" in result
    entry = result["Q312"]
    assert entry["cik"] == "0000320193"
    assert entry["ticker"] == "AAPL"
    assert entry["instance_of"] == "Q891723"
    assert entry["instance_of_list"] == ["Q891723"]


def test_collapse_multiple_instance_of_rows():
    """Two rows for the same entity (cartesian product from OPTIONAL instanceOf)."""
    rows = [
        _make_row("Q312", "320193", ticker="AAPL", instance_of_qid="Q891723"),
        _make_row("Q312", "320193", ticker="AAPL", instance_of_qid="Q4830453"),
    ]
    result = collapse_sparql_rows(rows)
    assert len(result) == 1
    entry = result["Q312"]
    # instance_of is the FIRST one seen
    assert entry["instance_of"] == "Q891723"
    assert set(entry["instance_of_list"]) == {"Q891723", "Q4830453"}


def test_collapse_duplicate_instance_of_not_doubled():
    """Same instanceOf repeated should appear only once in instance_of_list."""
    rows = [
        _make_row("Q312", "320193", instance_of_qid="Q891723"),
        _make_row("Q312", "320193", instance_of_qid="Q891723"),
    ]
    result = collapse_sparql_rows(rows)
    assert result["Q312"]["instance_of_list"] == ["Q891723"]


def test_collapse_ticker_prefers_first_non_null():
    """First row has ticker; second row should not overwrite it."""
    rows = [
        _make_row("Q312", "320193", ticker="AAPL"),
        _make_row("Q312", "320193", ticker=None),
    ]
    result = collapse_sparql_rows(rows)
    assert result["Q312"]["ticker"] == "AAPL"


def test_collapse_ticker_fills_in_when_first_null():
    """First row has no ticker; second row provides it."""
    rows = [
        _make_row("Q312", "320193", ticker=None),
        _make_row("Q312", "320193", ticker="AAPL"),
    ]
    result = collapse_sparql_rows(rows)
    assert result["Q312"]["ticker"] == "AAPL"


def test_collapse_multiple_entities():
    rows = [
        _make_row("Q312", "320193", ticker="AAPL"),
        _make_row("Q76", "1418091", ticker=None),
    ]
    result = collapse_sparql_rows(rows)
    assert len(result) == 2
    assert result["Q312"]["cik"] == "0000320193"
    assert result["Q76"]["cik"] == "0001418091"


def test_collapse_skips_missing_cik():
    rows = [
        {"entity": {"value": "http://www.wikidata.org/entity/Q999"}, "cik": {"value": ""}},
    ]
    result = collapse_sparql_rows(rows)
    assert "Q999" not in result


def test_collapse_skips_missing_entity_uri():
    rows = [
        {"entity": {"value": ""}, "cik": {"value": "12345"}},
    ]
    result = collapse_sparql_rows(rows)
    assert len(result) == 0


def test_collapse_empty_input():
    assert collapse_sparql_rows([]) == {}


# ---------------------------------------------------------------------------
# load_overrides / merge_overrides
# ---------------------------------------------------------------------------


def test_load_overrides_missing_file(tmp_path):
    assert load_overrides(tmp_path / "does-not-exist.json") == {}


def test_load_overrides_strips_comments_and_pads(tmp_path):
    p = tmp_path / "ov.json"
    p.write_text('{"_comment": "hi", "Q3884": "1018724"}')
    assert load_overrides(p) == {"Q3884": "0001018724"}


def test_load_overrides_rejects_non_qid_keys(tmp_path):
    p = tmp_path / "ov.json"
    p.write_text('{"not-a-qid": "123", "Q1": "456", "Qabc": "789"}')
    assert load_overrides(p) == {"Q1": "0000000456"}


def test_load_overrides_handles_malformed_json(tmp_path):
    p = tmp_path / "ov.json"
    p.write_text("{ this is not json }")
    assert load_overrides(p) == {}


def test_merge_overrides_inserts_missing():
    by_qid = {}
    added = merge_overrides(by_qid, {"Q3884": "0001018724"})
    assert added == 1
    assert by_qid["Q3884"]["cik"] == "0001018724"
    assert by_qid["Q3884"]["source"] == "override"


def test_merge_overrides_respects_wikidata():
    by_qid = {"Q312": {"qid": "Q312", "cik": "0000320193", "ticker": "AAPL",
                       "label": "Apple", "instance_of_list": [], "instance_of": None}}
    added = merge_overrides(by_qid, {"Q312": "9999999999"})
    assert added == 0
    assert by_qid["Q312"]["cik"] == "0000320193"  # Wikidata wins
