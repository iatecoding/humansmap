"""Unit tests for backend/scripts/sec/fetch_ownership.py helper functions.

Covers pure logic only — no network or DB access required.

Run inside the Docker container:
    docker compose exec backend pytest tests/test_fetch_ownership.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow import without the full app on PYTHONPATH
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.sec.fetch_ownership import (
    build_edge_key,
    build_filing_url,
    classify_13_form,
    parse_stake_pct,
    parse_subject_cik,
)

# ---------------------------------------------------------------------------
# classify_13_form
# ---------------------------------------------------------------------------


def test_classify_sc13d_plain():
    """SC 13D → ('SC 13D', False)."""
    assert classify_13_form("SC 13D") == ("SC 13D", False)


def test_classify_sc13d_amendment():
    """SC 13D/A → ('SC 13D', True)."""
    assert classify_13_form("SC 13D/A") == ("SC 13D", True)


def test_classify_sc13g_plain():
    """SC 13G → ('SC 13G', False)."""
    assert classify_13_form("SC 13G") == ("SC 13G", False)


def test_classify_sc13g_amendment():
    """SC 13G/A → ('SC 13G', True)."""
    assert classify_13_form("SC 13G/A") == ("SC 13G", True)


def test_classify_8k_returns_none():
    """8-K → None (not a 13D/G form)."""
    assert classify_13_form("8-K") is None


def test_classify_empty_returns_none():
    """Empty string → None."""
    assert classify_13_form("") is None


def test_classify_10k_returns_none():
    """10-K → None."""
    assert classify_13_form("10-K") is None


def test_classify_sc13e_returns_none():
    """SC 13E-3 (a different form type) → None."""
    assert classify_13_form("SC 13E-3") is None


# ---------------------------------------------------------------------------
# parse_stake_pct — XML path
# ---------------------------------------------------------------------------


def test_parse_stake_xml_lowercase_tag():
    """<percentOfClass>7.5</percentOfClass> → 7.5 (lowercase tag)."""
    xml = "<?xml version='1.0'?><root><percentOfClass>7.5</percentOfClass></root>"
    result = parse_stake_pct(xml, "text/xml")
    assert result == 7.5


def test_parse_stake_xml_mixed_case_tag():
    """<PercentOfClass>12.3</PercentOfClass> → 12.3 (mixed case)."""
    xml = "<?xml version='1.0'?><root><PercentOfClass>12.3</PercentOfClass></root>"
    result = parse_stake_pct(xml, "text/xml")
    assert result == 12.3


def test_parse_stake_xml_with_percent_suffix():
    """Value with trailing % is still parsed correctly."""
    xml = "<?xml version='1.0'?><root><percentOfClass>9.8%</percentOfClass></root>"
    result = parse_stake_pct(xml, "text/xml")
    assert result == 9.8


# ---------------------------------------------------------------------------
# parse_stake_pct — HTML/regex path
# ---------------------------------------------------------------------------


def test_parse_stake_html_table_row():
    """<tr><td>Percent of class</td><td>12.3%</td></tr> → 12.3."""
    html = "<tr><td>Percent of class</td><td>12.3%</td></tr>"
    result = parse_stake_pct(html, "text/html")
    assert result == 12.3


def test_parse_stake_html_row_11_format():
    """'Percent of Class represented by amount in Row (11): 4.9%' → 4.9."""
    text = "Percent of Class represented by amount in Row (11): 4.9%"
    result = parse_stake_pct(text, "text/html")
    assert result == 4.9


def test_parse_stake_html_colon_format():
    """'Percent of Class: 7.5%' style → 7.5."""
    text = "Percent of Class: 7.5%"
    result = parse_stake_pct(text, "text/html")
    assert result == 7.5


def test_parse_stake_no_stake_returns_none():
    """Text with no stake percentage → None."""
    result = parse_stake_pct("This filing contains no ownership percentage.", "text/html")
    assert result is None


def test_parse_stake_empty_string_returns_none():
    """Empty document → None."""
    result = parse_stake_pct("", "text/html")
    assert result is None


def test_parse_stake_decimal_value():
    """Decimal stake value is parsed as float."""
    text = "Percent of class represented 15.75%"
    result = parse_stake_pct(text, "text/html")
    assert result == 15.75


# ---------------------------------------------------------------------------
# parse_subject_cik
# ---------------------------------------------------------------------------


def test_parse_subject_cik_xml_style():
    """<SUBJECT-COMPANY><CIK>0000320193</CIK> → '0000320193'."""
    text = "<SUBJECT-COMPANY><CIK>0000320193</CIK></SUBJECT-COMPANY>"
    assert parse_subject_cik(text) == "0000320193"


def test_parse_subject_cik_xml_style_zero_padded():
    """Short CIK inside XML tag is zero-padded to 10 digits."""
    text = "<SUBJECT-COMPANY><CIK>320193</CIK></SUBJECT-COMPANY>"
    assert parse_subject_cik(text) == "0000320193"


def test_parse_subject_cik_sgml_header_style():
    """SGML header 'SUBJECT COMPANY: ... CENTRAL INDEX KEY: 0000320193' → '0000320193'."""
    text = (
        "SUBJECT COMPANY:\n"
        "\tCOMPANY CONFORMED NAME:  APPLE INC\n"
        "\tCENTRAL INDEX KEY:  0000320193\n"
        "\tSTANDARD INDUSTRIAL CLASSIFICATION: 3571\n"
    )
    assert parse_subject_cik(text) == "0000320193"


def test_parse_subject_cik_sgml_header_without_leading_zeros():
    """SGML header with unpadded CIK is still zero-padded to 10 digits."""
    text = (
        "SUBJECT COMPANY:\n"
        "  CENTRAL INDEX KEY: 320193\n"
    )
    assert parse_subject_cik(text) == "0000320193"


def test_parse_subject_cik_inline_cik_equals():
    """'CIK=0000320193' inline pattern → '0000320193'."""
    text = "Some filing header CIK=0000320193 more text"
    assert parse_subject_cik(text) == "0000320193"


def test_parse_subject_cik_inline_cik_colon():
    """'CIK: 320193' returns zero-padded result."""
    text = "CIK: 320193"
    assert parse_subject_cik(text) == "0000320193"


def test_parse_subject_cik_short_cik_zero_padded():
    """Short CIK number is always returned zero-padded to 10 digits."""
    text = "<SUBJECT-COMPANY><CIK>1234</CIK></SUBJECT-COMPANY>"
    result = parse_subject_cik(text)
    assert result == "0000001234"
    assert len(result) == 10


def test_parse_subject_cik_no_cik_returns_none():
    """Document with no CIK → None."""
    assert parse_subject_cik("No CIK information here.") is None


def test_parse_subject_cik_empty_string_returns_none():
    """Empty document → None."""
    assert parse_subject_cik("") is None


# ---------------------------------------------------------------------------
# build_edge_key
# ---------------------------------------------------------------------------


def test_build_edge_key_basic():
    """Standard inputs produce the expected underscore-separated key."""
    assert build_edge_key("000119312524123456", "Q312", "Q380") == (
        "000119312524123456_Q312_Q380"
    )


def test_build_edge_key_accession_format():
    """The accession_nodash portion (no dashes) is passed through as-is."""
    key = build_edge_key("000119312524123456", "Q312", "Q380")
    # Should not contain dashes in the accession portion
    assert "-" not in key.split("_")[0]


def test_build_edge_key_different_qids():
    """Different QID pairs produce different keys for the same accession."""
    key1 = build_edge_key("000119312524123456", "Q312", "Q380")
    key2 = build_edge_key("000119312524123456", "Q312", "Q381")
    assert key1 != key2


# ---------------------------------------------------------------------------
# build_filing_url
# ---------------------------------------------------------------------------


def test_build_filing_url_basic():
    """Canonical EDGAR URL is built correctly."""
    url = build_filing_url("0000320193", "0001193125-24-198765", "sc13d.xml")
    assert url == (
        "https://www.sec.gov/Archives/edgar/data/320193"
        "/000119312524198765/sc13d.xml"
    )


def test_build_filing_url_strips_cik_leading_zeros():
    """CIK integer part in the URL has no leading zeros."""
    url = build_filing_url("0000000123", "0001193125-24-000001", "filing.htm")
    assert "/edgar/data/123/" in url


def test_build_filing_url_dashes_removed_from_accession():
    """Accession number path segment has all dashes removed."""
    url = build_filing_url("0000320193", "0001193125-24-198765", "doc.htm")
    # Everything after /edgar/data/320193/ should have no dashes until the filename
    path_after_cik = url.split("/edgar/data/320193/")[1]
    accession_segment = path_after_cik.split("/")[0]
    assert "-" not in accession_segment
