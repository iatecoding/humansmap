"""Unit tests for iter_def14a_filings in fetch_def14a.py.

Tests cover:
- Only "DEF 14A" and "DEF 14A/A" rows pass the form filter (10-K, 8-K rejected).
- One Filing is yielded per calendar year (same-year dedup).
- DEF 14A/A amendment is preferred over DEF 14A when both present in a year.
- Most-recent filing date wins when multiple filings share the same form.
- since_year cutoff skips filings whose year < since_year.
- Rows with missing accession or primaryDocument are silently dropped.
- The since_year boundary is inclusive (year == since_year is kept).
"""
from __future__ import annotations

import pytest

from scripts.sec.fetch_def14a import Filing, iter_def14a_filings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_submissions(*rows: dict) -> dict:
    """Build a minimal submissions JSON from a list of row dicts.

    Each row dict must have: form, filingDate, accessionNumber, primaryDocument.
    """
    fields = ["accessionNumber", "filingDate", "form", "primaryDocument"]
    # Column-oriented parallel arrays
    recent: dict[str, list] = {f: [r.get(f, "") for r in rows] for f in fields}
    return {"filings": {"recent": recent}}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mixed_submissions():
    """Submissions JSON with a realistic mixture of form types and years."""
    return _make_submissions(
        # 2021 – plain DEF 14A
        {
            "form": "DEF 14A",
            "filingDate": "2021-04-15",
            "accessionNumber": "0001193125-21-001000",
            "primaryDocument": "proxy2021.htm",
        },
        # 2022 – plain DEF 14A first …
        {
            "form": "DEF 14A",
            "filingDate": "2022-03-10",
            "accessionNumber": "0001193125-22-001000",
            "primaryDocument": "proxy2022.htm",
        },
        # … then an amendment in 2022 (should win)
        {
            "form": "DEF 14A/A",
            "filingDate": "2022-04-05",
            "accessionNumber": "0001193125-22-002000",
            "primaryDocument": "proxy2022a.htm",
        },
        # 2023 – amendment comes FIRST by date, plain DEF 14A later (plain wins by date)
        {
            "form": "DEF 14A/A",
            "filingDate": "2023-02-01",
            "accessionNumber": "0001193125-23-001000",
            "primaryDocument": "proxy2023a_early.htm",
        },
        {
            "form": "DEF 14A",
            "filingDate": "2023-04-20",
            "accessionNumber": "0001193125-23-002000",
            "primaryDocument": "proxy2023.htm",
        },
        # 2024 – single plain DEF 14A
        {
            "form": "DEF 14A",
            "filingDate": "2024-03-25",
            "accessionNumber": "0001193125-24-001000",
            "primaryDocument": "proxy2024.htm",
        },
        # Noise – should be filtered out entirely
        {
            "form": "10-K",
            "filingDate": "2022-01-28",
            "accessionNumber": "0001193125-22-000001",
            "primaryDocument": "tenk2022.htm",
        },
        {
            "form": "8-K",
            "filingDate": "2023-07-10",
            "accessionNumber": "0001193125-23-000099",
            "primaryDocument": "8k.htm",
        },
    )


# ---------------------------------------------------------------------------
# Tests — form filter
# ---------------------------------------------------------------------------


def test_only_def14a_forms_pass(mixed_submissions):
    """10-K and 8-K rows must not appear in the output."""
    filings = list(iter_def14a_filings(mixed_submissions, since_year=None))
    for f in filings:
        assert f.form in {"DEF 14A", "DEF 14A/A"}, (
            f"Unexpected form in output: {f.form}"
        )


def test_all_def14a_years_returned(mixed_submissions):
    """All four DEF 14A years (2021-2024) should be represented."""
    filings = list(iter_def14a_filings(mixed_submissions, since_year=None))
    years = {f.filing_date[:4] for f in filings}
    assert years == {"2021", "2022", "2023", "2024"}


# ---------------------------------------------------------------------------
# Tests — per-year dedup
# ---------------------------------------------------------------------------


def test_one_filing_per_year(mixed_submissions):
    """Exactly one Filing per calendar year must be yielded."""
    filings = list(iter_def14a_filings(mixed_submissions, since_year=None))
    dates = [f.filing_date[:4] for f in filings]
    assert len(dates) == len(set(dates)), "Duplicate years in output"


def test_amendment_preferred_when_same_year(mixed_submissions):
    """2022: DEF 14A/A (2022-04-05) must be chosen over DEF 14A (2022-03-10)."""
    filings = {f.filing_date[:4]: f for f in iter_def14a_filings(mixed_submissions, since_year=None)}
    f2022 = filings["2022"]
    assert f2022.form == "DEF 14A/A", f"Expected amendment for 2022, got {f2022.form}"
    assert f2022.accession == "0001193125-22-002000"


def test_latest_date_wins_when_no_amendment(mixed_submissions):
    """2023: The later filing date wins even though no amendment is present for it."""
    filings = {f.filing_date[:4]: f for f in iter_def14a_filings(mixed_submissions, since_year=None)}
    f2023 = filings["2023"]
    # 2023-04-20 (DEF 14A) is later than 2023-02-01 (DEF 14A/A)
    assert f2023.filing_date == "2023-04-20"
    assert f2023.form == "DEF 14A"


# ---------------------------------------------------------------------------
# Tests — since_year cutoff
# ---------------------------------------------------------------------------


def test_since_year_excludes_earlier(mixed_submissions):
    """Filings before since_year must be absent."""
    filings = list(iter_def14a_filings(mixed_submissions, since_year=2023))
    years = {f.filing_date[:4] for f in filings}
    assert "2021" not in years
    assert "2022" not in years


def test_since_year_is_inclusive(mixed_submissions):
    """The since_year boundary year must be included in the output."""
    filings = list(iter_def14a_filings(mixed_submissions, since_year=2022))
    years = {f.filing_date[:4] for f in filings}
    assert "2022" in years


def test_since_year_none_returns_all(mixed_submissions):
    """since_year=None must not filter any filing."""
    all_filings = list(iter_def14a_filings(mixed_submissions, since_year=None))
    assert len(all_filings) == 4  # 2021, 2022, 2023, 2024


def test_since_year_future_returns_empty(mixed_submissions):
    """A far-future since_year must yield nothing."""
    filings = list(iter_def14a_filings(mixed_submissions, since_year=2099))
    assert filings == []


# ---------------------------------------------------------------------------
# Tests — edge cases
# ---------------------------------------------------------------------------


def test_missing_primary_doc_skipped():
    """Rows with an empty primaryDocument must be silently dropped."""
    subs = _make_submissions(
        {
            "form": "DEF 14A",
            "filingDate": "2022-04-01",
            "accessionNumber": "0001193125-22-999",
            "primaryDocument": "",  # empty — must be dropped
        },
        {
            "form": "DEF 14A",
            "filingDate": "2022-05-01",
            "accessionNumber": "0001193125-22-888",
            "primaryDocument": "proxy.htm",
        },
    )
    filings = list(iter_def14a_filings(subs, since_year=None))
    # Only the second row (with a valid doc) should survive
    assert len(filings) == 1
    assert filings[0].accession == "0001193125-22-888"


def test_missing_accession_skipped():
    """Rows with an empty accessionNumber must be silently dropped."""
    subs = _make_submissions(
        {
            "form": "DEF 14A",
            "filingDate": "2023-03-01",
            "accessionNumber": "",  # empty — must be dropped
            "primaryDocument": "proxy.htm",
        },
    )
    filings = list(iter_def14a_filings(subs, since_year=None))
    assert filings == []


def test_empty_submissions_returns_empty():
    """An empty submissions object must yield no filings."""
    filings = list(iter_def14a_filings({}, since_year=None))
    assert filings == []


def test_no_def14a_in_submissions_returns_empty():
    """A submissions object with only 10-K and 8-K must yield nothing."""
    subs = _make_submissions(
        {"form": "10-K", "filingDate": "2022-02-01", "accessionNumber": "acc1", "primaryDocument": "doc.htm"},
        {"form": "8-K", "filingDate": "2022-06-01", "accessionNumber": "acc2", "primaryDocument": "doc.htm"},
    )
    filings = list(iter_def14a_filings(subs, since_year=None))
    assert filings == []


def test_filing_dataclass_fields():
    """The yielded Filing objects must expose accession, filing_date, primary_document, form."""
    subs = _make_submissions(
        {
            "form": "DEF 14A",
            "filingDate": "2024-03-15",
            "accessionNumber": "0001234567-24-000001",
            "primaryDocument": "proxy.htm",
        },
    )
    filings = list(iter_def14a_filings(subs, since_year=None))
    assert len(filings) == 1
    f = filings[0]
    assert f.accession == "0001234567-24-000001"
    assert f.filing_date == "2024-03-15"
    assert f.primary_document == "proxy.htm"
    assert f.form == "DEF 14A"
