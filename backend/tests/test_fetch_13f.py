"""Filing-iteration tests for scripts/sec/fetch_13f.py (§11.4.d).

HTTP + DB glue is not unit-tested here; smoke run (§11.6) covers end-to-end.
"""

from scripts.sec.fetch_13f import Filing, iter_13f_filings


def _submissions(rows: list[dict]) -> dict:
    """Build a submissions JSON shape from row dicts."""
    if not rows:
        return {"filings": {"recent": {}}}
    keys = set()
    for r in rows:
        keys.update(r.keys())
    col = {k: [r.get(k, "") for r in rows] for k in keys}
    return {"filings": {"recent": col}}


def _row(**kw) -> dict:
    base = {
        "accessionNumber": "0001-25-000001",
        "filingDate": "2025-02-14",
        "reportDate": "2024-12-31",
        "form": "13F-HR",
        "primaryDocument": "cover.txt",
    }
    base.update(kw)
    return base


def test_filters_non_13f_forms():
    subs = _submissions([
        _row(form="10-K"),
        _row(form="13F-HR"),
        _row(form="DEF 14A"),
    ])
    out = list(iter_13f_filings(subs, since_year=None, provider=None))
    assert len(out) == 1
    assert out[0].form == "13F-HR"


def test_amendment_flag_set():
    subs = _submissions([_row(form="13F-HR/A")])
    out = list(iter_13f_filings(subs, since_year=None, provider=None))
    assert out[0].is_amendment is True
    assert out[0].form == "13F-HR/A"


def test_since_year_cutoff():
    subs = _submissions([
        _row(filingDate="2019-05-12", reportDate="2019-03-31"),
        _row(filingDate="2023-02-14", reportDate="2022-12-31"),
        _row(filingDate="2025-02-14", reportDate="2024-12-31"),
    ])
    out = list(iter_13f_filings(subs, since_year=2023, provider=None))
    assert len(out) == 2
    assert all(f.filing_date[:4] >= "2023" for f in out)


def test_missing_period_skipped():
    subs = _submissions([
        _row(reportDate="", periodOfReport=""),
        _row(reportDate="2024-12-31"),
    ])
    out = list(iter_13f_filings(subs, since_year=None, provider=None))
    assert len(out) == 1


def test_missing_accession_skipped():
    subs = _submissions([
        _row(accessionNumber=""),
        _row(accessionNumber="0001-25-000002"),
    ])
    out = list(iter_13f_filings(subs, since_year=None, provider=None))
    assert len(out) == 1


def test_quarterly_filings_all_yielded():
    """Unlike DEF 14A, 13F is quarterly — no per-year dedup."""
    subs = _submissions([
        _row(filingDate="2024-05-14", reportDate="2024-03-31", accessionNumber="A"),
        _row(filingDate="2024-08-14", reportDate="2024-06-30", accessionNumber="B"),
        _row(filingDate="2024-11-14", reportDate="2024-09-30", accessionNumber="C"),
        _row(filingDate="2025-02-14", reportDate="2024-12-31", accessionNumber="D"),
    ])
    out = list(iter_13f_filings(subs, since_year=None, provider=None))
    assert len(out) == 4
    assert {f.period_of_report for f in out} == {
        "2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31",
    }


def test_period_fallback_field():
    """Fall back to periodOfReport if reportDate missing."""
    subs = _submissions([_row(reportDate="", periodOfReport="2024-12-31")])
    out = list(iter_13f_filings(subs, since_year=None, provider=None))
    assert out[0].period_of_report == "2024-12-31"
