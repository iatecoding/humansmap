"""
DEF 14A layout classifier.

classify_layout(html) returns one of five layout types based on structural
signals in the proxy statement HTML:

  "table"   — consolidated director table with a "Director Since" column header
  "mini"    — grid of small per-director tables each stating "Director since YYYY"
  "prose"   — narrative bio section with ALL-CAPS bold director names (Berkshire style)
  "span"    — letter-spacing spans dominate layout (Tesla/image-grid style)
  "unknown" — none of the above matched

Heuristic ordering: table → mini → prose → span → unknown.
First match wins. Uses stdlib re only; lxml is NOT imported here (available
in-container after rebuild, but classifier only needs regex).
"""

import re
from typing import Literal

Layout = Literal["table", "mini", "prose", "span", "unknown"]

# ── table ─────────────────────────────────────────────────────────────────────
# Apple: <b>Director Since</b> inside a <td> cell.
# MSFT:  "Director" and "Since" in consecutive bold <p> tags inside a <td>.
_TABLE_BOLD_ONELINER = re.compile(
    r"<b>\s*Director\s+Since\s*</b>",
    re.IGNORECASE,
)
_TABLE_BOLD_SPLITLINE = re.compile(
    r"Director\s*</p>\s*<p[^>]*>\s*Since",
    re.IGNORECASE | re.DOTALL,
)


def _is_table(html: str) -> bool:
    """True when HTML has a bold 'Director Since' column header."""
    return bool(_TABLE_BOLD_ONELINER.search(html)) or bool(
        _TABLE_BOLD_SPLITLINE.search(html)
    )


# ── mini ──────────────────────────────────────────────────────────────────────
# Alphabet: small per-person tables each containing "Director since YYYY".
# Signal: ≥3 distinct <table> segments that contain the inline phrase.
_MINI_PHRASE = re.compile(r"Director\s+since\s+\d{4}", re.IGNORECASE)
_TABLE_SPLIT = re.compile(r"<table\b", re.IGNORECASE)
_MINI_THRESHOLD = 3


def _is_mini(html: str) -> bool:
    """True when ≥3 separate table blocks each contain 'Director since YYYY'."""
    segments = _TABLE_SPLIT.split(html)
    hits = sum(1 for seg in segments if _MINI_PHRASE.search(seg))
    return hits >= _MINI_THRESHOLD


# ── prose ─────────────────────────────────────────────────────────────────────
# Berkshire: director bios as paragraphs; each director's name is rendered in a
# bold <span> as ALL-CAPS (e.g. <span style="font-weight:bold">WARREN E. BUFFETT</span>).
# Signal: ≥3 such bold-span ALL-CAPS names (to avoid false positives from Tesla
# which has only 1 such occurrence).
_PROSE_BOLD_SPAN = re.compile(
    r"(?i:<span[^>]*font-weight\s*:\s*bold[^>]*>)"
    r"([A-Z]{2,}(?:\s+[A-Z]\.?)?\s+[A-Z]{2,})"
    r"(?i:</span>)",
)
_PROSE_THRESHOLD = 3


def _is_prose(html: str) -> bool:
    """True when ≥3 bold-span ALL-CAPS director names appear in the HTML."""
    return len(_PROSE_BOLD_SPAN.findall(html)) >= _PROSE_THRESHOLD


# ── span ──────────────────────────────────────────────────────────────────────
# Tesla: image-grid layout generated from a typesetting tool; CSS letter-spacing
# is applied to virtually every text run.
# Signal: large number (≥50) of <span style="...letter-spacing..."> elements.
_SPAN_LS = re.compile(
    r"<span[^>]*style=[^>]*letter-spacing",
    re.IGNORECASE,
)
_SPAN_THRESHOLD = 50


def _is_span(html: str) -> bool:
    """True when the document is saturated with letter-spacing spans (≥50)."""
    return len(_SPAN_LS.findall(html)) >= _SPAN_THRESHOLD


# ── public API ────────────────────────────────────────────────────────────────

def classify_layout(html: str) -> Layout:
    """
    Classify the layout of a DEF 14A HTML document.

    Evaluation order: table → mini → prose → span → unknown.
    The first matching heuristic wins.

    Args:
        html: Raw HTML string of a DEF 14A filing.

    Returns:
        A Layout literal string.
    """
    if not html or not html.strip():
        return "unknown"
    if _is_table(html):
        return "table"
    if _is_mini(html):
        return "mini"
    if _is_prose(html):
        return "prose"
    if _is_span(html):
        return "span"
    return "unknown"
