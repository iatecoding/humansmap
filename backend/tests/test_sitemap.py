"""Unit tests for sitemap helpers — pure-function tests, no database or network.

Run inside the Docker container:
    docker compose exec backend sh -c "pip install -r requirements-dev.txt && pytest"

These tests only cover the pure helpers (_parse_lastmod, _url_block, _person_loc).
The route handlers (_get_notable_persons, sitemap_index, sitemap_page) require
a live ArangoDB connection and are not exercised here.
"""

import asyncio
from datetime import datetime, timezone

import pytest

from app.routers.sitemap import _parse_lastmod, _url_block, organization_sitemap_page
from app.utils.slug import slugify


# ---------------------------------------------------------------------------
# _parse_lastmod — timestamp field → YYYY-MM-DD or None
# ---------------------------------------------------------------------------


def test_invalid_organization_sitemap_page_is_rejected():
    with pytest.raises(Exception) as exc:
        asyncio.run(organization_sitemap_page(0))
    assert exc.value.status_code == 404


class TestParseLastmod:
    def test_full_iso_string_with_utc_offset(self):
        """Full ISO 8601 timestamp with timezone → date only."""
        assert _parse_lastmod("2026-04-12T14:23:00+00:00") == "2026-04-12"

    def test_full_iso_string_with_z_suffix(self):
        """ISO 8601 with Z suffix → date only."""
        assert _parse_lastmod("2025-11-30T08:00:00Z") == "2025-11-30"

    def test_full_iso_string_no_tz(self):
        """ISO 8601 without timezone component → date only."""
        assert _parse_lastmod("2024-06-15T00:00:00") == "2024-06-15"

    def test_date_only_string(self):
        """Date-only input is returned unchanged."""
        assert _parse_lastmod("2023-01-01") == "2023-01-01"

    def test_datetime_object(self):
        """datetime instance (defensive path) → date only."""
        dt = datetime(2026, 3, 7, 9, 30, tzinfo=timezone.utc)
        assert _parse_lastmod(dt) == "2026-03-07"

    def test_none_returns_none(self):
        """None input → None (field absent in DB)."""
        assert _parse_lastmod(None) is None

    def test_empty_string_returns_none(self):
        """Empty string → None."""
        assert _parse_lastmod("") is None

    def test_whitespace_only_returns_none(self):
        """Whitespace-only string → None."""
        assert _parse_lastmod("   ") is None

    def test_malformed_string_returns_none(self):
        """Unparseable string → None (must not raise)."""
        assert _parse_lastmod("not-a-date") is None

    def test_partial_date_too_short_returns_none(self):
        """String shorter than 10 chars with wrong separators → None."""
        assert _parse_lastmod("2026-4-1") is None

    def test_zero_returns_none(self):
        """Falsy integer → None."""
        assert _parse_lastmod(0) is None


# ---------------------------------------------------------------------------
# _url_block — renders a <url> XML block
# ---------------------------------------------------------------------------


class TestUrlBlock:
    def test_with_lastmod_no_priority(self):
        """Standard person URL with a real lastmod date."""
        result = _url_block("https://humansmap.com/person/Q123", "2026-04-12")
        assert "<loc>https://humansmap.com/person/Q123</loc>" in result
        assert "<lastmod>2026-04-12</lastmod>" in result
        assert "<priority>" not in result
        assert result.startswith("  <url>")
        assert result.endswith("  </url>")

    def test_with_lastmod_none_omits_tag(self):
        """When lastmod is None, no <lastmod> tag is emitted."""
        result = _url_block("https://humansmap.com/person/Q456", None)
        assert "<loc>https://humansmap.com/person/Q456</loc>" in result
        assert "<lastmod>" not in result

    def test_with_priority(self):
        """Homepage entry includes priority."""
        result = _url_block("https://humansmap.com/", "2026-04-12", priority="1.0")
        assert "<loc>https://humansmap.com/</loc>" in result
        assert "<lastmod>2026-04-12</lastmod>" in result
        assert "<priority>1.0</priority>" in result

    def test_with_priority_and_no_lastmod(self):
        """Priority without lastmod — edge case."""
        result = _url_block("https://humansmap.com/", None, priority="1.0")
        assert "<lastmod>" not in result
        assert "<priority>1.0</priority>" in result

    def test_valid_xml_structure(self):
        """Output is a well-formed <url>...</url> block."""
        result = _url_block("https://humansmap.com/person/Q999", "2026-01-15")
        lines = result.splitlines()
        assert lines[0] == "  <url>"
        assert lines[-1] == "  </url>"
        # All inner lines are indented with 4 spaces
        for line in lines[1:-1]:
            assert line.startswith("    "), f"Inner line not indented: {line!r}"


# ---------------------------------------------------------------------------
# Integration: simulate sitemap page rendering with mock person list
# ---------------------------------------------------------------------------


class TestSitemapRendering:
    """Simulate what sitemap_page does with a chunk of (qid, lastmod) pairs."""

    def _render_urls(self, pairs: list[tuple[str, str | None]]) -> str:
        """Replicate the rendering loop from sitemap_page."""
        return "\n".join(
            _url_block(f"https://humansmap.com/person/{qid}", lastmod)
            for qid, lastmod in pairs
        )

    def test_all_persons_have_lastmod(self):
        """All persons with timestamps → all URLs contain <lastmod>."""
        pairs = [
            ("Q1", "2026-04-12"),
            ("Q2", "2025-12-01"),
            ("Q3", "2024-07-04"),
        ]
        result = self._render_urls(pairs)
        assert result.count("<lastmod>") == 3
        assert "<lastmod>2026-04-12</lastmod>" in result
        assert "<lastmod>2025-12-01</lastmod>" in result
        assert "<lastmod>2024-07-04</lastmod>" in result

    def test_person_with_no_timestamp_omits_lastmod(self):
        """Person with None timestamp → no <lastmod> for that URL."""
        pairs = [("Q10", "2026-04-12"), ("Q11", None), ("Q12", "2023-03-01")]
        result = self._render_urls(pairs)
        assert result.count("<lastmod>") == 2
        # Q11's URL has no lastmod but its loc is present
        assert "<loc>https://humansmap.com/person/Q11</loc>" in result
        # Verify the two with lastmod are correct
        assert "<lastmod>2026-04-12</lastmod>" in result
        assert "<lastmod>2023-03-01</lastmod>" in result

    def test_all_persons_missing_timestamp(self):
        """All persons with None timestamp → no <lastmod> tags at all."""
        pairs = [("Q20", None), ("Q21", None)]
        result = self._render_urls(pairs)
        assert "<lastmod>" not in result
        # URLs are still present
        assert result.count("<loc>") == 2

    def test_empty_chunk(self):
        """Empty chunk → empty string (no urls, valid for edge cases)."""
        result = self._render_urls([])
        assert result == ""


# ---------------------------------------------------------------------------
# Slugged URL rendering — mirrors person_page.py canonical_path logic
# ---------------------------------------------------------------------------


def _person_loc(qid: str, name_en: str | None, name_it: str | None) -> str:
    """Replicate the _person_loc helper from sitemap_page.

    Must match person_page.py line 444:
        canonical_path = f"/person/{qid}-{canonical_slug}" if canonical_slug else f"/person/{qid}"
    """
    _SITE_URL = "https://humansmap.com"
    slug = slugify(name_en or name_it or "")
    path = f"/person/{qid}-{slug}" if slug else f"/person/{qid}"
    return f"{_SITE_URL}{path}"


class TestSluggedUrlRendering:
    """Verify that sitemap URLs match the canonical URL shape from person_page.py."""

    def test_person_with_name_en_uses_en_slug(self):
        """Person with name_en → slug derived from English name."""
        loc = _person_loc("Q42", "Douglas Adams", None)
        assert loc == "https://humansmap.com/person/Q42-douglas-adams"

    def test_person_with_only_name_it_uses_it_slug(self):
        """Person with name_it only → slug derived from Italian name."""
        loc = _person_loc("Q5686", None, "Galileo Galilei")
        assert loc == "https://humansmap.com/person/Q5686-galileo-galilei"

    def test_person_with_both_names_prefers_en(self):
        """When both name_en and name_it are present, name_en takes precedence."""
        loc = _person_loc("Q100", "Leonardo da Vinci", "Leonardo di ser Piero da Vinci")
        assert loc == "https://humansmap.com/person/Q100-leonardo-da-vinci"

    def test_person_with_both_names_empty_omits_slug(self):
        """When both names are absent/empty, URL has no trailing dash — just /person/{qid}."""
        loc = _person_loc("Q999", None, None)
        assert loc == "https://humansmap.com/person/Q999"
        assert not loc.endswith("-")

    def test_person_with_empty_string_names_omits_slug(self):
        """Explicit empty strings for both names → same as absent — no trailing dash."""
        loc = _person_loc("Q888", "", "")
        assert loc == "https://humansmap.com/person/Q888"
        assert not loc.endswith("-")

    def test_person_with_non_ascii_name_en(self):
        """Non-ASCII name_en is accent-folded by slugify."""
        loc = _person_loc("Q200", "Léon Blum", None)
        assert loc == "https://humansmap.com/person/Q200-leon-blum"

    def test_person_with_only_non_ascii_characters_omits_slug(self):
        """Name composed entirely of non-ASCII characters that slugify returns '' for."""
        # slugify("日本語") → ""
        loc = _person_loc("Q300", "日本語", None)
        assert loc == "https://humansmap.com/person/Q300"
        assert not loc.endswith("-")

    def test_url_block_with_slugged_loc(self):
        """Full <url> block with a slugged loc renders correctly."""
        loc = _person_loc("Q42", "Douglas Adams", None)
        block = _url_block(loc, "2026-04-12")
        assert "<loc>https://humansmap.com/person/Q42-douglas-adams</loc>" in block
        assert "<lastmod>2026-04-12</lastmod>" in block
        assert block.startswith("  <url>")
        assert block.endswith("  </url>")
