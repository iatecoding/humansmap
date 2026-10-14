"""Unit tests for backend/scripts/sec/widen_cik_map.py.

Covers:
- form.idx parser (fixed-width format, header skip, form-type filtering)
- name normaliser (reused from resolve_cusip; tested here for the SEC manager context)
- quarter-selection logic (last_n_completed_quarters)

No network or DB access required.

Run inside the Docker container:
    docker compose exec backend pytest tests/test_widen_cik_map.py -v
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.sec.widen_cik_map import (
    extract_13f_ciks,
    last_n_completed_quarters,
    parse_form_idx,
)
from scripts.sec.resolve_cusip import normalize_issuer_name

# ---------------------------------------------------------------------------
# Fixtures — hand-written form.idx sample
# ---------------------------------------------------------------------------

# Real form.idx structure: fixed-width, header ends with a separator of dashes,
# then data rows.  Column layout:
#   0:12  form type
#   12:74 company name
#   74:86 CIK (right-justified)
#   86:98 date filed
#   98+   filename

_SAMPLE_IDX = b"""\
Form Type   Company Name                                            CIK         Date Filed  Filename
---------   -------------------------------------------------------  ----------  ----------  --------
13F-HR      VANGUARD GROUP INC                                       102909      2025-02-14  edgar/data/102909/000010290925000001.txt
13F-HR      BLACKROCK INC                                            1086364     2025-02-13  edgar/data/1086364/000108636425000002.txt
13F-HR/A    FIDELITY MANAGEMENT & RESEARCH CO                       315066      2025-02-15  edgar/data/315066/000031506625000003.txt
DEF 14A     APPLE INC                                                320193      2025-02-20  edgar/data/320193/000032019325000004.txt
10-K        MICROSOFT CORP                                           789019      2025-01-29  edgar/data/789019/000078901925000005.txt
"""


def test_parse_form_idx_yields_correct_tuples():
    """Parser should skip header and return all data rows as (form, name, cik) tuples."""
    rows = list(parse_form_idx(_SAMPLE_IDX))
    # 5 data rows total
    assert len(rows) == 5

    forms = [r[0] for r in rows]
    assert "13F-HR" in forms
    assert "13F-HR/A" in forms
    assert "DEF 14A" in forms
    assert "10-K" in forms


def test_parse_form_idx_returns_stripped_cik():
    """CIK values should be returned as stripped digit strings (not zero-padded)."""
    rows = list(parse_form_idx(_SAMPLE_IDX))
    cik_by_form: dict[str, list[str]] = {}
    for form, _name, cik in rows:
        cik_by_form.setdefault(form, []).append(cik)

    # Vanguard CIK is 102909
    assert "102909" in cik_by_form.get("13F-HR", [])
    # BlackRock CIK is 1086364
    assert "1086364" in cik_by_form.get("13F-HR", [])


def test_parse_form_idx_empty_body():
    """Completely empty body should yield nothing."""
    assert list(parse_form_idx(b"")) == []


def test_parse_form_idx_header_only():
    """A file with a header but no data rows should yield nothing."""
    header_only = b"Form Type   Company Name   CIK   Date Filed   Filename\n-----------\n"
    assert list(parse_form_idx(header_only)) == []


def test_extract_13f_ciks_filters_only_13f():
    """Only 13F-HR and 13F-HR/A form types should be included."""
    result = extract_13f_ciks(_SAMPLE_IDX)
    # DEF 14A and 10-K should NOT be present
    apple_cik = "0000320193"
    microsoft_cik = "0000789019"
    assert apple_cik not in result
    assert microsoft_cik not in result


def test_extract_13f_ciks_includes_amendments():
    """13F-HR/A (amendments) should be included."""
    result = extract_13f_ciks(_SAMPLE_IDX)
    fidelity_cik = "0000315066"
    assert fidelity_cik in result


def test_extract_13f_ciks_zero_pads_cik():
    """CIKs should be zero-padded to 10 digits."""
    result = extract_13f_ciks(_SAMPLE_IDX)
    # 102909 → 0000102909
    assert "0000102909" in result
    # 1086364 → 0001086364
    assert "0001086364" in result


def test_extract_13f_ciks_company_name_preserved():
    """Company names should be stored as-is from the index."""
    result = extract_13f_ciks(_SAMPLE_IDX)
    vanguard_cik = "0000102909"
    assert result[vanguard_cik] == "VANGUARD GROUP INC"


def test_extract_13f_ciks_dedup_last_wins():
    """When same CIK appears twice, last row wins (most recent quarter order)."""
    duplicate_idx = b"""\
Form Type   Company Name                                            CIK         Date Filed  Filename
---------   -------------------------------------------------------  ----------  ----------  --------
13F-HR      OLD NAME LLC                                             102909      2025-01-15  edgar/data/102909/001.txt
13F-HR      NEW NAME INC                                             102909      2025-02-15  edgar/data/102909/002.txt
"""
    result = extract_13f_ciks(duplicate_idx)
    assert result["0000102909"] == "NEW NAME INC"


# ---------------------------------------------------------------------------
# Name normaliser (reused from resolve_cusip)
# ---------------------------------------------------------------------------

def test_normalize_vanguard_group_inc():
    """'VANGUARD GROUP INC' should normalise to 'VANGUARD'."""
    # resolve_cusip strips INC, GROUP, HOLDINGS etc.
    result = normalize_issuer_name("VANGUARD GROUP INC")
    assert result == "VANGUARD"


def test_normalize_vanguard_group_inc_dotted():
    """'Vanguard Group, Inc.' should normalise to the same token as the SEC form."""
    result = normalize_issuer_name("Vanguard Group, Inc.")
    assert result == "VANGUARD"


def test_normalize_both_vanguard_forms_equal():
    """Both representations of Vanguard normalise to the same string."""
    a = normalize_issuer_name("VANGUARD GROUP INC")
    b = normalize_issuer_name("Vanguard Group, Inc.")
    assert a == b


def test_normalize_blackrock():
    assert normalize_issuer_name("BLACKROCK INC") == normalize_issuer_name("BlackRock, Inc.")


def test_normalize_fidelity():
    """Trailing LLC should be stripped."""
    assert normalize_issuer_name("Fidelity Management LLC") == normalize_issuer_name("Fidelity Management")


def test_normalize_empty_string():
    assert normalize_issuer_name("") == ""


def test_normalize_punctuation_stripped():
    """Punctuation (commas, periods) is replaced with spaces then collapsed."""
    result = normalize_issuer_name("Some, Corp.")
    # SOME + CORP stripped → SOME
    assert result == "SOME"


# ---------------------------------------------------------------------------
# Quarter-selection logic
# ---------------------------------------------------------------------------

_TODAY_APRIL_2026 = datetime(2026, 4, 18, tzinfo=timezone.utc)


def test_last_4_quarters_april_2026():
    """Given 2026-04-18, last 4 completed quarters = 2025Q2, 2025Q3, 2025Q4, 2026Q1."""
    result = last_n_completed_quarters(4, _TODAY_APRIL_2026)
    assert result == [(2025, 2), (2025, 3), (2025, 4), (2026, 1)]


def test_last_1_quarter_april_2026():
    """Most recent completed quarter as of 2026-04-18 is 2026Q1."""
    result = last_n_completed_quarters(1, _TODAY_APRIL_2026)
    assert result == [(2026, 1)]


def test_last_2_quarters_april_2026():
    result = last_n_completed_quarters(2, _TODAY_APRIL_2026)
    assert result == [(2025, 4), (2026, 1)]


def test_last_4_quarters_exact_quarter_boundary():
    """On the first day of Q2 (2026-04-01), Q1 is completed, Q2 has just started."""
    today = datetime(2026, 4, 1, tzinfo=timezone.utc)
    result = last_n_completed_quarters(4, today)
    assert result == [(2025, 2), (2025, 3), (2025, 4), (2026, 1)]


def test_last_4_quarters_mid_q1():
    """In mid-Q1 (2026-02-15), Q4 of 2025 is the most recent completed quarter."""
    today = datetime(2026, 2, 15, tzinfo=timezone.utc)
    result = last_n_completed_quarters(4, today)
    assert result == [(2025, 1), (2025, 2), (2025, 3), (2025, 4)]


def test_last_4_quarters_end_of_year():
    """On 2025-12-31, Q3 2025 is completed but Q4 is still in progress."""
    today = datetime(2025, 12, 31, tzinfo=timezone.utc)
    result = last_n_completed_quarters(4, today)
    assert result == [(2024, 4), (2025, 1), (2025, 2), (2025, 3)]


def test_quarters_ordered_oldest_first():
    """Result should always be oldest-first."""
    result = last_n_completed_quarters(6, _TODAY_APRIL_2026)
    years_qs = [(y, q) for y, q in result]
    for i in range(len(years_qs) - 1):
        y1, q1 = years_qs[i]
        y2, q2 = years_qs[i + 1]
        assert (y1, q1) < (y2, q2), "Quarters not in ascending order"
