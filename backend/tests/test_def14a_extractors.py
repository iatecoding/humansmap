"""
Unit tests for DEF 14A per-layout extractors (§11.2.b).

Run inside the backend container (lxml available after 11.1.a rebuild):
    docker compose exec backend pytest backend/tests/test_def14a_extractors.py -v

Tests call extract_rows(html, classify_layout(html)) for each of the five
canonical fixtures and assert the expected structural/semantic properties.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# These imports work in-container after the 11.1.a lxml rebuild.
from scripts.sec.def14a_layouts import classify_layout
from scripts.sec.def14a_extractors import DirectorRow, extract_rows

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "def14a"


def _load(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Shared structural assertions (applied to every fixture result)
# ---------------------------------------------------------------------------

_NAME_TOKEN_RE = re.compile(r"^[^\W\d_](?:[^\W\d_]|[.\-'])*$", re.UNICODE)


def _assert_structural(rows: list[DirectorRow], fixture_label: str) -> None:
    """Assert shared structural invariants on any extraction result."""
    assert rows, f"{fixture_label}: expected non-empty result"

    for row in rows:
        # Every row must have the required keys
        assert "name" in row, f"{fixture_label}: missing 'name' key"
        assert "role" in row, f"{fixture_label}: missing 'role' key"
        assert "start_date_hint" in row, f"{fixture_label}: missing 'start_date_hint'"
        assert "raw_blob" in row, f"{fixture_label}: missing 'raw_blob'"

        name = row["name"]
        tokens = name.split()

        # Must have ≥2 tokens
        assert len(tokens) >= 2, (
            f"{fixture_label}: name {name!r} has fewer than 2 tokens"
        )

        # Each token must be alphabetic (allowing '.' and '-' within)
        for tok in tokens:
            assert _NAME_TOKEN_RE.match(tok), (
                f"{fixture_label}: name token {tok!r} in {name!r} contains "
                f"invalid characters"
            )

        # Role must be "director" (NEOs deferred to 11.2.c)
        assert row["role"] == "director", (
            f"{fixture_label}: expected role='director', got {row['role']!r}"
        )

        # raw_blob must be a non-empty string ≤ 300 chars
        assert isinstance(row["raw_blob"], str) and row["raw_blob"], (
            f"{fixture_label}: raw_blob missing or empty"
        )
        assert len(row["raw_blob"]) <= 300, (
            f"{fixture_label}: raw_blob exceeds 300 chars"
        )

    # No duplicate names within a single extraction
    names_lower = [r["name"].lower() for r in rows]
    assert len(names_lower) == len(set(names_lower)), (
        f"{fixture_label}: duplicate names found: {names_lower}"
    )


# ---------------------------------------------------------------------------
# Per-fixture tests
# ---------------------------------------------------------------------------


class TestAppleTable:
    def setup_method(self):
        html = _load("apple_def14a.htm")
        assert classify_layout(html) == "table", "Apple should classify as 'table'"
        self.rows = extract_rows(html, "table")

    def test_structural(self):
        _assert_structural(self.rows, "apple")

    def test_cook_present(self):
        names = [r["name"] for r in self.rows]
        assert any("Cook" in n for n in names), (
            f"Tim Cook not found in Apple directors: {names}"
        )

    def test_start_date_hint_populated(self):
        hints = [r["start_date_hint"] for r in self.rows]
        assert any(
            h is not None and re.fullmatch(r"\d{4}", h) for h in hints
        ), f"No 4-digit start_date_hint found in Apple rows: {hints}"


class TestMicrosoftTable:
    def setup_method(self):
        html = _load("microsoft_def14a.htm")
        assert classify_layout(html) == "table", "Microsoft should classify as 'table'"
        self.rows = extract_rows(html, "table")

    def test_structural(self):
        _assert_structural(self.rows, "microsoft")

    def test_nadella_present(self):
        names = [r["name"] for r in self.rows]
        assert any("Nadella" in n for n in names), (
            f"Satya Nadella not found in Microsoft directors: {names}"
        )

    def test_start_date_hint_populated(self):
        hints = [r["start_date_hint"] for r in self.rows]
        assert any(
            h is not None and re.fullmatch(r"\d{4}", h) for h in hints
        ), f"No 4-digit start_date_hint found in Microsoft rows: {hints}"


class TestAlphabetMini:
    def setup_method(self):
        html = _load("alphabet_def14a.htm")
        assert classify_layout(html) == "mini", "Alphabet should classify as 'mini'"
        self.rows = extract_rows(html, "mini")

    def test_structural(self):
        _assert_structural(self.rows, "alphabet")

    def test_pichai_or_page_present(self):
        names = [r["name"] for r in self.rows]
        found = any("Pichai" in n or "Page" in n for n in names)
        assert found, (
            f"Neither Pichai nor Page found in Alphabet directors: {names}"
        )

    def test_start_date_hint_populated(self):
        hints = [r["start_date_hint"] for r in self.rows]
        assert any(
            h is not None and re.fullmatch(r"\d{4}", h) for h in hints
        ), f"No 4-digit start_date_hint found in Alphabet rows: {hints}"


class TestBerkshireProse:
    def setup_method(self):
        html = _load("berkshire_def14a.htm")
        assert classify_layout(html) == "prose", "Berkshire should classify as 'prose'"
        self.rows = extract_rows(html, "prose")

    def test_structural(self):
        _assert_structural(self.rows, "berkshire")

    def test_buffett_present(self):
        names = [r["name"] for r in self.rows]
        assert any("Buffett" in n for n in names), (
            f"Buffett not found in Berkshire directors: {names}"
        )

    def test_no_start_date_hints(self):
        """Prose extractor never emits years — all start_date_hint must be None."""
        hints = [r["start_date_hint"] for r in self.rows]
        assert all(h is None for h in hints), (
            f"Unexpected start_date_hint in Berkshire prose rows: {hints}"
        )


class TestTeslaSpan:
    def setup_method(self):
        html = _load("tesla_def14a.htm")
        assert classify_layout(html) == "span", "Tesla should classify as 'span'"
        self.rows = extract_rows(html, "span")

    def test_structural(self):
        _assert_structural(self.rows, "tesla")

    def test_musk_present(self):
        names = [r["name"] for r in self.rows]
        assert any("Musk" in n for n in names), (
            f"Elon Musk not found in Tesla directors: {names}"
        )


# ---------------------------------------------------------------------------
# Unknown layout → empty list
# ---------------------------------------------------------------------------


def test_extract_rows_unknown_returns_empty():
    assert extract_rows("<html/>", "unknown") == []


def test_extract_rows_unknown_plain_html():
    assert extract_rows("<html><body><p>Hello world</p></body></html>", "unknown") == []
