"""Unit tests for backend/scripts/sec/fetch_submissions.py helper functions.

Covers pure logic only — no network or DB access required.

Run inside the Docker container:
    docker compose exec backend pytest tests/test_fetch_submissions.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow import without the app on PYTHONPATH
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.sec.fetch_submissions import build_update_doc, extract_metadata

# ---------------------------------------------------------------------------
# Sample payload (Apple-like, trimmed for brevity)
# ---------------------------------------------------------------------------

SAMPLE = {
    "cik": "0000320193",
    "name": "Apple Inc.",
    "entityType": "operating",
    "sic": "3571",
    "sicDescription": "Electronic Computers",
    "stateOfIncorporation": "CA",
    "stateOfIncorporationDescription": "CA",
    "ein": "942404110",
    "lei": None,
    "category": "Large accelerated filer",
    "exchanges": ["Nasdaq"],
    "tickers": ["AAPL"],
    "fiscalYearEnd": "0926",
    "formerNames": [
        {
            "name": "APPLE INC",
            "from": "2007-01-10T05:00:00.000Z",
            "to": "2019-08-05T04:00:00.000Z",
        }
    ],
    "insiderTransactionForIssuerExists": 1,
    "addresses": {"mailing": {"city": "CUPERTINO"}},
    # filings.recent is deliberately present here to verify it is ignored
    "filings": {"recent": ["huge", "list", "of", "entries"]},
}


# ---------------------------------------------------------------------------
# Test 1: full Apple-like payload produces all expected fields
# ---------------------------------------------------------------------------


def test_extract_metadata_full_payload():
    md = extract_metadata(SAMPLE)

    assert md["sic"] == "3571"
    assert md["sic_description"] == "Electronic Computers"
    assert md["fiscal_year_end"] == "0926"
    assert md["state_of_incorporation"] == "CA"
    assert md["state_of_incorporation_description"] == "CA"
    assert md["ein"] == "942404110"
    assert md["entity_type"] == "operating"
    assert md["category"] == "Large accelerated filer"
    assert md["exchanges"] == ["Nasdaq"]
    assert md["tickers"] == ["AAPL"]
    assert md["insider_transaction_for_issuer_exists"] == 1
    assert md["addresses"] == {"mailing": {"city": "CUPERTINO"}}

    # former_names transformation
    assert len(md["former_names"]) == 1
    fn = md["former_names"][0]
    assert fn["name"] == "APPLE INC"
    assert fn["from"] == "2007-01-10T05:00:00.000Z"
    assert fn["to"] == "2019-08-05T04:00:00.000Z"


# ---------------------------------------------------------------------------
# Test 2: missing optional field → key absent from result (not None)
# ---------------------------------------------------------------------------


def test_extract_metadata_missing_sic_key_absent():
    payload = dict(SAMPLE)
    del payload["sic"]

    md = extract_metadata(payload)

    assert "sic" not in md


# ---------------------------------------------------------------------------
# Test 3: formerNames empty list → former_names key is []
# ---------------------------------------------------------------------------


def test_extract_metadata_empty_former_names():
    payload = dict(SAMPLE)
    payload["formerNames"] = []

    md = extract_metadata(payload)

    assert "former_names" in md
    assert md["former_names"] == []


# ---------------------------------------------------------------------------
# Test 4: filings key is never present in output regardless of input
# ---------------------------------------------------------------------------


def test_extract_metadata_strips_filings():
    md = extract_metadata(SAMPLE)

    assert "filings" not in md


def test_extract_metadata_strips_filings_even_when_nested():
    """Even if someone passes a payload with deeply-nested filings, it must not leak."""
    payload = {
        "sic": "3571",
        "filings": {
            "recent": list(range(5000)),
            "files": [{"name": "some-file.json"}],
        },
    }
    md = extract_metadata(payload)

    assert "filings" not in md
    assert md["sic"] == "3571"


# ---------------------------------------------------------------------------
# Test 5: build_update_doc always sets last_synced to a string
# ---------------------------------------------------------------------------


def test_build_update_doc_last_synced_is_string():
    doc = build_update_doc({"sic": "3571"})

    assert isinstance(doc["last_synced"], str)
    assert len(doc["last_synced"]) > 0


def test_build_update_doc_preserves_metadata_fields():
    md = {"sic": "3571", "tickers": ["AAPL"]}
    doc = build_update_doc(md)

    assert doc["sic"] == "3571"
    assert doc["tickers"] == ["AAPL"]


def test_build_update_doc_does_not_mutate_input():
    md = {"sic": "3571"}
    build_update_doc(md)

    assert "last_synced" not in md


# ---------------------------------------------------------------------------
# Additional edge cases
# ---------------------------------------------------------------------------


def test_extract_metadata_lei_none_omitted():
    """lei: None should be omitted since we only include non-None values."""
    md = extract_metadata(SAMPLE)

    # SAMPLE has "lei": None — must be absent from output
    assert "lei" not in md


def test_extract_metadata_lei_present_when_not_none():
    payload = dict(SAMPLE)
    payload["lei"] = "549300XXXXXXXXXX"

    md = extract_metadata(payload)

    assert md["lei"] == "549300XXXXXXXXXX"


def test_extract_metadata_former_names_absent_key_absent():
    """If formerNames key is entirely absent, former_names must be absent too."""
    payload = {k: v for k, v in SAMPLE.items() if k != "formerNames"}

    md = extract_metadata(payload)

    assert "former_names" not in md


def test_extract_metadata_multiple_former_names():
    payload = dict(SAMPLE)
    payload["formerNames"] = [
        {"name": "APPLE COMPUTER INC", "from": "1977-01-01T00:00:00.000Z", "to": "2007-01-09T00:00:00.000Z"},
        {"name": "APPLE INC", "from": "2007-01-10T05:00:00.000Z", "to": "2019-08-05T04:00:00.000Z"},
    ]

    md = extract_metadata(payload)

    assert len(md["former_names"]) == 2
    assert md["former_names"][0]["name"] == "APPLE COMPUTER INC"
    assert md["former_names"][1]["name"] == "APPLE INC"
