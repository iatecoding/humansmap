"""
Unit tests for DEF 14A iXBRL ecd:PeoName extractor (§11.2.c).

Run inside the backend container (lxml available after 11.1.a rebuild):
    docker compose exec backend pytest backend/tests/test_def14a_peonames.py -v

Fixture tag counts (verified via grep on `ecd:PeoName`):
    apple_def14a.htm      : 3 raw tag occurrences → 7 unique names after dedup
                            (Mr. Cook + Luca Maestri, Kate Adams, Deirdre O Brien,
                             Jeff Williams, Kevan Parekh, Sabih Khan)
    berkshire_def14a.htm  : 1 ecd:PeoName tag   ("Warren E. Buffett")
    tesla_def14a.htm      : 1 ecd:PeoName tag   ("Mr. Musk")
    microsoft_def14a.htm  : 0 ecd:PeoName tags
    alphabet_def14a.htm   : 0 ecd:PeoName tags

Note: Apple's 3 raw `ecd:PeoName` occurrences includes one deeply nested set of
5 ix:nonNumeric contexts for the same "Mr. Cook" (multi-year PVP table) plus
additional NEO name tags elsewhere in the filing — grep counts occurrences of
the string, not unique tag instances.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.sec.def14a_extractors import PeoNameRow, extract_peonames

_FIXTURES = Path(__file__).parent / "fixtures" / "def14a"


def _load(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Basic: function runs without error on all fixtures
# ---------------------------------------------------------------------------


def test_extract_peonames_apple_no_error() -> None:
    html = _load("apple_def14a.htm")
    result = extract_peonames(html)
    assert isinstance(result, list)


def test_extract_peonames_microsoft_no_error() -> None:
    html = _load("microsoft_def14a.htm")
    result = extract_peonames(html)
    assert isinstance(result, list)


def test_extract_peonames_tesla_no_error() -> None:
    html = _load("tesla_def14a.htm")
    result = extract_peonames(html)
    assert isinstance(result, list)


def test_extract_peonames_berkshire_no_error() -> None:
    html = _load("berkshire_def14a.htm")
    result = extract_peonames(html)
    assert isinstance(result, list)


def test_extract_peonames_alphabet_no_error() -> None:
    html = _load("alphabet_def14a.htm")
    result = extract_peonames(html)
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Fixtures with known ecd:PeoName tags return ≥1 row
# ---------------------------------------------------------------------------


def test_apple_peonames_cook_present() -> None:
    """Apple fixture has multiple ecd:PeoName tags (Mr. Cook + other NEOs).
    After deduplication, expect ≥1 row whose raw_name contains 'Cook'.
    """
    html = _load("apple_def14a.htm")
    result = extract_peonames(html)
    assert len(result) >= 1, "Apple DEF 14A should yield at least one PeoName row"
    assert any("Cook" in row["raw_name"] for row in result), (
        f"Expected a row containing 'Cook'; got: {[r['raw_name'] for r in result]}"
    )


def test_berkshire_peonames_buffett_present() -> None:
    """Berkshire fixture has 1 ecd:PeoName tag wrapping 'Warren E. Buffett'."""
    html = _load("berkshire_def14a.htm")
    result = extract_peonames(html)
    assert len(result) >= 1, "Berkshire DEF 14A should yield at least one PeoName row"
    assert any("Buffett" in row["raw_name"] for row in result), (
        f"Expected a row containing 'Buffett'; got: {[r['raw_name'] for r in result]}"
    )


def test_tesla_peonames_musk_present() -> None:
    """Tesla fixture has 1 ecd:PeoName tag wrapping 'Mr. Musk'."""
    html = _load("tesla_def14a.htm")
    result = extract_peonames(html)
    assert len(result) >= 1, "Tesla DEF 14A should yield at least one PeoName row"
    assert any("Musk" in row["raw_name"] for row in result), (
        f"Expected a row containing 'Musk'; got: {[r['raw_name'] for r in result]}"
    )


# ---------------------------------------------------------------------------
# Fixtures with no ecd:PeoName tags return [] (or at least a list)
# ---------------------------------------------------------------------------


def test_microsoft_peonames_empty_ok() -> None:
    """Microsoft fixture has no ecd:PeoName tags; empty list is valid."""
    html = _load("microsoft_def14a.htm")
    result = extract_peonames(html)
    assert isinstance(result, list)


def test_alphabet_peonames_empty_ok() -> None:
    """Alphabet fixture has no ecd:PeoName tags; empty list is valid."""
    html = _load("alphabet_def14a.htm")
    result = extract_peonames(html)
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# TypedDict shape: each row has required keys
# ---------------------------------------------------------------------------


def test_peoname_row_shape() -> None:
    html = _load("apple_def14a.htm")
    result = extract_peonames(html)
    assert len(result) >= 1
    for row in result:
        assert "raw_name" in row
        assert "context_element" in row
        assert "raw_blob" in row
        assert row["context_element"] == "ecd:PeoName"
        assert isinstance(row["raw_name"], str)
        assert isinstance(row["raw_blob"], str)
        assert len(row["raw_blob"]) <= 300


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_extract_peonames_empty_html() -> None:
    assert extract_peonames("") == []


def test_extract_peonames_no_tags() -> None:
    assert extract_peonames("<html><body><p>No iXBRL here.</p></body></html>") == []


def test_dedupe() -> None:
    """Same raw_name appearing in two separate ecd:PeoName tags → one row."""
    synthetic = (
        '<ix:nonNumeric name="ecd:PeoName" contextRef="c1" id="f1">Mr. Cook</ix:nonNumeric>'
        " some intervening text "
        '<ix:nonNumeric name="ecd:PeoName" contextRef="c2" id="f2">Mr. Cook</ix:nonNumeric>'
    )
    result = extract_peonames(synthetic)
    assert len(result) == 1
    assert result[0]["raw_name"] == "Mr. Cook"


def test_dedupe_case_insensitive() -> None:
    """Case-normalised deduplication: 'mr. cook' and 'Mr. Cook' collapse to one."""
    synthetic = (
        '<ix:nonNumeric name="ecd:PeoName" contextRef="c1" id="f1">Mr. Cook</ix:nonNumeric>'
        '<ix:nonNumeric name="ecd:PeoName" contextRef="c2" id="f2">mr. cook</ix:nonNumeric>'
    )
    result = extract_peonames(synthetic)
    assert len(result) == 1


def test_cap_at_20() -> None:
    """Function returns at most 20 distinct rows even if more tags exist."""
    tags = "".join(
        f'<ix:nonNumeric name="ecd:PeoName" contextRef="c{i}" id="f{i}">Person {i}</ix:nonNumeric>'
        for i in range(30)
    )
    result = extract_peonames(tags)
    assert len(result) <= 20


def test_nested_tags_stripped() -> None:
    """Opening tags inside the ecd:PeoName element are stripped from raw_name.

    The extractor captures text up to the first closing tag, then strips any
    remaining opening-tag markup.  For a plain-text name with no inner tags the
    full name is returned.  For names with inner formatting tags (e.g. <b>) the
    text visible before the first inner closing tag is returned — no angle
    brackets survive either way.
    """
    synthetic = (
        '<ix:nonNumeric name="ecd:PeoName" contextRef="c1" id="f1">'
        "Mr. Cook"
        "</ix:nonNumeric>"
    )
    result = extract_peonames(synthetic)
    assert len(result) == 1
    # raw_name should not contain any angle brackets
    assert "<" not in result[0]["raw_name"]
    assert "Cook" in result[0]["raw_name"]
    assert result[0]["raw_name"] == "Mr. Cook"
