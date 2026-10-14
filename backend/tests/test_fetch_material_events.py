"""Unit tests for backend/scripts/sec/fetch_material_events.py helper functions.

Covers pure logic only — no network or DB access required.

Run inside the Docker container:
    docker compose exec backend pytest tests/test_fetch_material_events.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow import without the app on PYTHONPATH
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.sec.fetch_material_events import (
    build_event_doc,
    build_filing_url,
    build_involvement_doc,
    classify_8k_items,
    iter_filings_rows,
    parse_items_field,
)

# ---------------------------------------------------------------------------
# classify_8k_items — priority ordering and basic cases
# ---------------------------------------------------------------------------


def test_classify_bankruptcy_filed():
    """Item 1.03 → bankruptcy_filed."""
    assert classify_8k_items("1.03") == "bankruptcy_filed"


def test_classify_exec_departure():
    """Item 5.02 (with trailing 8.01) → exec_departure."""
    assert classify_8k_items("5.02, 8.01") == "exec_departure"


def test_classify_acquisition_via_1_01():
    """Item 1.01 (with 2.01) → acquisition_announced (1.01 is first match)."""
    assert classify_8k_items("1.01,2.01") == "acquisition_announced"


def test_classify_no_match():
    """Item 8.01 alone → None (not in our subtype set)."""
    assert classify_8k_items("8.01") is None


def test_classify_empty_string():
    """Empty items string → None."""
    assert classify_8k_items("") is None


def test_classify_priority_bankruptcy_over_departure():
    """When both 5.02 and 1.03 present, bankruptcy_filed wins (highest priority)."""
    assert classify_8k_items("5.02, 1.03") == "bankruptcy_filed"


def test_classify_acquisition_via_2_01_only():
    """Item 2.01 alone → acquisition_announced."""
    assert classify_8k_items("2.01") == "acquisition_announced"


def test_classify_whitespace_only():
    """Whitespace-only items string → None."""
    assert classify_8k_items("   ") is None


# ---------------------------------------------------------------------------
# parse_items_field
# ---------------------------------------------------------------------------


def test_parse_items_field_multiple_with_spaces():
    """Parse comma-separated items with varying whitespace."""
    result = parse_items_field("1.01, 2.01 , 8.01")
    assert result == ["1.01", "2.01", "8.01"]


def test_parse_items_field_single():
    """Single item without trailing comma."""
    assert parse_items_field("5.02") == ["5.02"]


def test_parse_items_field_empty():
    """Empty string → empty list."""
    assert parse_items_field("") == []


def test_parse_items_field_whitespace_only():
    """Whitespace-only string → empty list."""
    assert parse_items_field("   ") == []


def test_parse_items_field_no_extra_whitespace_in_output():
    """Trailing/leading spaces on individual items are stripped."""
    result = parse_items_field("  1.03  ,  5.02  ")
    assert result == ["1.03", "5.02"]


# ---------------------------------------------------------------------------
# iter_filings_rows
# ---------------------------------------------------------------------------

_THREE_FILING_PAYLOAD: dict = {
    "accessionNumber": [
        "0001193125-24-000001",
        "0001193125-24-000002",
        "0001193125-24-000003",
    ],
    "filingDate": ["2024-01-01", "2024-02-01", "2024-03-01"],
    "form": ["8-K", "10-K", "8-K/A"],
    "primaryDocument": ["doc1.htm", "doc2.htm", "doc3.htm"],
    "items": ["5.02,8.01", "", "1.03"],
}


def test_iter_filings_rows_yields_three_dicts():
    """Three-filing payload produces exactly 3 dicts."""
    rows = list(iter_filings_rows(_THREE_FILING_PAYLOAD))
    assert len(rows) == 3


def test_iter_filings_rows_correct_zipped_fields():
    """Each row has the expected key values zipped correctly."""
    rows = list(iter_filings_rows(_THREE_FILING_PAYLOAD))

    assert rows[0]["accessionNumber"] == "0001193125-24-000001"
    assert rows[0]["filingDate"] == "2024-01-01"
    assert rows[0]["form"] == "8-K"
    assert rows[0]["primaryDocument"] == "doc1.htm"
    assert rows[0]["items"] == "5.02,8.01"

    assert rows[1]["form"] == "10-K"
    assert rows[1]["items"] == ""

    assert rows[2]["accessionNumber"] == "0001193125-24-000003"
    assert rows[2]["form"] == "8-K/A"
    assert rows[2]["items"] == "1.03"


def test_iter_filings_rows_empty_payload():
    """Empty dict yields no rows (no error)."""
    rows = list(iter_filings_rows({}))
    assert rows == []


def test_iter_filings_rows_missing_items_column():
    """If items column is absent it defaults to empty strings."""
    payload = {
        "accessionNumber": ["0001193125-24-000001"],
        "filingDate": ["2024-01-01"],
        "form": ["8-K"],
        "primaryDocument": ["doc1.htm"],
        # "items" key intentionally absent
    }
    rows = list(iter_filings_rows(payload))
    assert len(rows) == 1
    assert rows[0]["items"] == ""


# ---------------------------------------------------------------------------
# build_event_doc
# ---------------------------------------------------------------------------

_NOW_ISO = "2026-04-17T00:00:00+00:00"
_APPLE_CIK = "0000320193"
_APPLE_QID = "Q312"


def _make_row(**overrides) -> dict:
    base = {
        "accessionNumber": "0001193125-24-198765",
        "filingDate": "2024-07-15",
        "form": "8-K",
        "primaryDocument": "d12345.htm",
        "items": "5.02",
    }
    base.update(overrides)
    return base


def test_build_event_doc_non_matching_returns_none():
    """8-K with item 8.01 only → no subtype → returns None."""
    row = _make_row(items="8.01")
    assert build_event_doc(_APPLE_CIK, _APPLE_QID, row, _NOW_ISO) is None


def test_build_event_doc_non_8k_form_returns_none():
    """10-K form is not a material event → returns None."""
    row = _make_row(form="10-K", items="5.02")
    assert build_event_doc(_APPLE_CIK, _APPLE_QID, row, _NOW_ISO) is None


def test_build_event_doc_amendment_sets_is_amendment_true():
    """8-K/A sets is_amendment=True."""
    row = _make_row(form="8-K/A", items="1.03")
    doc = build_event_doc(_APPLE_CIK, _APPLE_QID, row, _NOW_ISO)
    assert doc is not None
    assert doc["is_amendment"] is True


def test_build_event_doc_plain_8k_is_amendment_false():
    """Plain 8-K sets is_amendment=False."""
    row = _make_row(form="8-K", items="5.02")
    doc = build_event_doc(_APPLE_CIK, _APPLE_QID, row, _NOW_ISO)
    assert doc is not None
    assert doc["is_amendment"] is False


def test_build_event_doc_key_is_dashless_accession():
    """_key is the accession number with dashes stripped."""
    row = _make_row(accessionNumber="0001193125-24-198765")
    doc = build_event_doc(_APPLE_CIK, _APPLE_QID, row, _NOW_ISO)
    assert doc is not None
    assert doc["_key"] == "000119312524198765"


def test_build_event_doc_subtype_exec_departure():
    """item 5.02 → exec_departure subtype."""
    row = _make_row(items="5.02,8.01")
    doc = build_event_doc(_APPLE_CIK, _APPLE_QID, row, _NOW_ISO)
    assert doc is not None
    assert doc["subtype"] == "exec_departure"


def test_build_event_doc_all_items_sorted():
    """items field in doc contains ALL parsed items, sorted."""
    row = _make_row(items="8.01,1.03,5.02")
    doc = build_event_doc(_APPLE_CIK, _APPLE_QID, row, _NOW_ISO)
    assert doc is not None
    assert doc["items"] == ["1.03", "5.02", "8.01"]


def test_build_event_doc_fields_populated():
    """Verify all expected fields are present and correctly populated."""
    row = _make_row()
    doc = build_event_doc(_APPLE_CIK, _APPLE_QID, row, _NOW_ISO)
    assert doc is not None
    assert doc["type"] == "material_event"
    assert doc["filing_date"] == "2024-07-15"
    assert doc["accession"] == "0001193125-24-198765"
    assert doc["org_cik"] == _APPLE_CIK
    assert doc["org_qid"] == _APPLE_QID
    assert doc["last_synced"] == _NOW_ISO


# ---------------------------------------------------------------------------
# build_filing_url
# ---------------------------------------------------------------------------


def test_build_filing_url_apple_example():
    """Canonical Apple-style URL is produced correctly."""
    url = build_filing_url("0000320193", "0001193125-24-198765", "d12345.htm")
    assert url == (
        "https://www.sec.gov/Archives/edgar/data/320193"
        "/000119312524198765/d12345.htm"
    )


def test_build_filing_url_strips_leading_zeros_from_cik():
    """CIK integer part has no leading zeros."""
    url = build_filing_url("0000000123", "0001193125-24-000001", "filing.htm")
    assert "/edgar/data/123/" in url


def test_build_filing_url_dashes_removed_from_accession():
    """Accession number in URL has all dashes removed."""
    url = build_filing_url("0000320193", "0001193125-24-198765", "doc.htm")
    assert "-" not in url.split("/edgar/data/")[1]


# ---------------------------------------------------------------------------
# build_involvement_doc
# ---------------------------------------------------------------------------


def test_build_involvement_doc_structure():
    """Verify involvement edge fields."""
    doc = build_involvement_doc("000119312524198765", "Q312", _NOW_ISO)
    assert doc["_key"] == "000119312524198765_Q312"
    assert doc["_from"] == "events/000119312524198765"
    assert doc["_to"] == "organizations/Q312"
    assert doc["role"] == "filer"
    assert doc["last_synced"] == _NOW_ISO
