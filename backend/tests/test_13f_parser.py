"""Unit tests for scripts/sec/parse_13f.py (§11.4.a).

Run in the backend container:
    docker compose exec backend pytest tests/test_13f_parser.py -v
"""

from pathlib import Path

import pytest

from scripts.sec.parse_13f import (
    Holding,
    aggregate_by_cusip,
    iter_top_by_value,
    parse_13f_xml,
)

FIXTURES = Path(__file__).parent / "fixtures" / "13f"


# ── Minimal-document behaviors ────────────────────────────────────────────────

def test_empty_document_returns_empty_list():
    xml = b"""<?xml version="1.0"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
</informationTable>"""
    assert parse_13f_xml(xml) == []


def test_single_row_parsed():
    xml = b"""<?xml version="1.0"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <infoTable>
    <nameOfIssuer>APPLE INC</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>037833100</cusip>
    <value>1000000</value>
    <shrsOrPrnAmt>
      <sshPrnamt>5000</sshPrnamt>
      <sshPrnamtType>SH</sshPrnamtType>
    </shrsOrPrnAmt>
  </infoTable>
</informationTable>"""
    out = parse_13f_xml(xml)
    assert len(out) == 1
    h = out[0]
    assert h.cusip == "037833100"
    assert h.name_of_issuer == "APPLE INC"
    assert h.value_usd == 1_000_000
    assert h.shares == 5_000
    assert h.shares_type == "SH"
    assert h.title_of_class == "COM"


def test_missing_cusip_row_skipped():
    xml = b"""<?xml version="1.0"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <infoTable>
    <nameOfIssuer>JUST NAME NO CUSIP</nameOfIssuer>
    <value>100</value>
    <shrsOrPrnAmt><sshPrnamt>1</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
  </infoTable>
  <infoTable>
    <nameOfIssuer>GOOD ONE</nameOfIssuer>
    <cusip>123456789</cusip>
    <value>100</value>
    <shrsOrPrnAmt><sshPrnamt>1</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
  </infoTable>
</informationTable>"""
    out = parse_13f_xml(xml)
    assert len(out) == 1
    assert out[0].cusip == "123456789"


def test_cusip_uppercased():
    xml = b"""<?xml version="1.0"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <infoTable>
    <nameOfIssuer>X</nameOfIssuer>
    <cusip>abc123def</cusip>
    <value>1</value>
    <shrsOrPrnAmt><sshPrnamt>1</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
  </infoTable>
</informationTable>"""
    assert parse_13f_xml(xml)[0].cusip == "ABC123DEF"


def test_value_is_thousands_flag():
    xml = b"""<?xml version="1.0"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <infoTable>
    <nameOfIssuer>X</nameOfIssuer>
    <cusip>111111111</cusip>
    <value>500</value>
    <shrsOrPrnAmt><sshPrnamt>1</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
  </infoTable>
</informationTable>"""
    assert parse_13f_xml(xml)[0].value_usd == 500
    assert parse_13f_xml(xml, value_is_thousands=True)[0].value_usd == 500_000


def test_namespaceless_document():
    """Some filers omit the default namespace entirely."""
    xml = b"""<?xml version="1.0"?>
<informationTable>
  <infoTable>
    <nameOfIssuer>NONS INC</nameOfIssuer>
    <cusip>000000001</cusip>
    <value>1</value>
    <shrsOrPrnAmt><sshPrnamt>1</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
  </infoTable>
</informationTable>"""
    out = parse_13f_xml(xml)
    assert len(out) == 1
    assert out[0].cusip == "000000001"


def test_prefixed_namespace_document():
    """Some filers use an ns1: prefix rather than default xmlns."""
    xml = b"""<?xml version="1.0"?>
<ns1:informationTable xmlns:ns1="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <ns1:infoTable>
    <ns1:nameOfIssuer>PFX INC</ns1:nameOfIssuer>
    <ns1:cusip>123123123</ns1:cusip>
    <ns1:value>42</ns1:value>
    <ns1:shrsOrPrnAmt>
      <ns1:sshPrnamt>7</ns1:sshPrnamt>
      <ns1:sshPrnamtType>SH</ns1:sshPrnamtType>
    </ns1:shrsOrPrnAmt>
  </ns1:infoTable>
</ns1:informationTable>"""
    out = parse_13f_xml(xml)
    assert len(out) == 1
    assert out[0].cusip == "123123123"
    assert out[0].value_usd == 42


# ── aggregate_by_cusip ────────────────────────────────────────────────────────

def test_aggregate_by_cusip_sums_subadviser_splits():
    rows = [
        Holding("APPLE", "037833100", 100, 10, "SH", "COM"),
        Holding("APPLE", "037833100", 200, 20, "SH", "COM"),
        Holding("APPLE", "037833100", 300, 30, "SH", "COM"),
    ]
    out = aggregate_by_cusip(rows)
    assert len(out) == 1
    assert out["037833100"].value_usd == 600
    assert out["037833100"].shares == 60


def test_aggregate_preserves_distinct_cusips():
    rows = [
        Holding("A", "AAA", 100, 1, "SH", "COM"),
        Holding("B", "BBB", 200, 2, "SH", "COM"),
    ]
    out = aggregate_by_cusip(rows)
    assert set(out.keys()) == {"AAA", "BBB"}


# ── Real-fixture integration ──────────────────────────────────────────────────

@pytest.mark.parametrize(
    "fixture,min_rows,min_unique_cusips",
    [
        ("berkshire_infotable.xml", 100, 35),
        ("blackrock_infotable.xml", 40_000, 5_000),
        ("vanguard_infotable.xml", 15_000, 4_000),
        ("bridgewater_infotable.xml", 1_000, 1_000),
        ("renaissance_infotable.xml", 3_000, 3_000),
    ],
)
def test_real_fixture_parses(fixture, min_rows, min_unique_cusips):
    """Each sample must parse to at least the row/cusip counts from the feasibility probe."""
    path = FIXTURES / fixture
    if not path.exists():
        pytest.skip(f"fixture missing: {path}")

    rows = parse_13f_xml(path)
    assert len(rows) >= min_rows, f"{fixture}: got {len(rows)} rows, expected ≥{min_rows}"

    # Required fields must all be non-empty / positive for every row
    for h in rows:
        assert h.cusip and len(h.cusip) == 9
        assert h.name_of_issuer
        assert h.value_usd >= 0
        assert h.shares >= 0
        assert h.shares_type in {"SH", "PRN"}

    unique = len({h.cusip for h in rows})
    assert unique >= min_unique_cusips, f"{fixture}: got {unique} unique cusips, expected ≥{min_unique_cusips}"


def test_berkshire_top_holdings_sane():
    """Spot-check: Berkshire's top holding by USD value should be a known name."""
    path = FIXTURES / "berkshire_infotable.xml"
    if not path.exists():
        pytest.skip("berkshire fixture missing")
    rows = parse_13f_xml(path)
    agg = aggregate_by_cusip(rows)
    top = sorted(agg.values(), key=lambda h: h.value_usd, reverse=True)[:5]
    assert top, "expected at least one holding"
    # Berkshire's top position for years has been Apple (CUSIP 037833100)
    top_cusips = [h.cusip for h in top]
    top_names = [h.name_of_issuer.upper() for h in top]
    assert any("APPLE" in n for n in top_names), f"no Apple in top 5: {top_names}"


def test_iter_top_by_value_respects_n():
    rows = [
        Holding("A", "AAA", 100, 1, "SH", "COM"),
        Holding("B", "BBB", 200, 2, "SH", "COM"),
        Holding("C", "CCC", 50, 3, "SH", "COM"),
    ]
    top2 = list(iter_top_by_value(rows, 2))
    assert [h.cusip for h in top2] == ["BBB", "AAA"]
