"""
DEF 14A per-layout extractors.

Exposes one function per layout plus a dispatcher:

    extract_table(html)  — Apple / Microsoft: "Director Since" column tables
    extract_mini(html)   — Alphabet: per-director mini-tables with inline year
    extract_prose(html)  — Berkshire: ALL-CAPS bold-span narrative style
    extract_span(html)   — Tesla: letter-spacing spans in Director Bios section

    extract_rows(html, layout) — dispatcher; layout=="unknown" returns []

Each extractor returns list[DirectorRow].  All rows default role="director".
NEO classification (officer_of) is deferred to §11.2.c via iXBRL ecd:PeoName.

Quality filters applied by every extractor:
- Drop strings < 3 whitespace-separated tokens that don't look name-like
- Drop pure-number or pure-role tokens
- Deduplicate (name, role) within a single extraction run
- Cap output at 50 rows (proxies typically list 10-15 directors)
"""

from __future__ import annotations

import logging
import re
import textwrap
from typing import Literal, TypedDict

from lxml import html as lxml_html

logger = logging.getLogger(__name__)


_XML_DECL_RE = re.compile(r"^\s*<\?xml[^>]*\?>\s*", re.IGNORECASE)


def _lxml_parse(html: str):
    # lxml refuses str inputs that carry an encoding declaration; strip it.
    return lxml_html.fromstring(_XML_DECL_RE.sub("", html, count=1))

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class DirectorRow(TypedDict):
    name: str                   # "First Last" or "First M. Last", cleaned
    role: Literal["director", "officer"]
    start_date_hint: str | None  # "YYYY" or "YYYY-MM-DD" if found; else None
    raw_blob: str               # ≤300 char raw source snippet for audit


class PeoNameRow(TypedDict):
    raw_name: str         # as tagged: "Mr. Cook", "Ms. Barra", "Timothy D. Cook", etc.
    context_element: str  # "ecd:PeoName" (verbatim)
    raw_blob: str         # ≤300 char snippet surrounding the tag for audit


# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

_MAX_ROWS = 50

# Tokens to drop from candidate name strings
_ROLE_TOKENS = frozenset(
    w.lower() for w in [
        "Chairman", "Chair", "CEO", "CFO", "COO", "CTO", "CIO", "President",
        "Director", "Officer", "Executive", "Independent", "Lead", "Nominee",
        "Board", "Member", "Vice", "Senior", "General", "Counsel",
        "Secretary", "Treasurer", "Managing", "Principal", "The",
    ]
)

_NOISE_PHRASES = frozenset([
    "director since", "age", "independent", "committee", "audit",
    "compensation", "nominating", "governance", "risk", "other",
    "public", "boards", "career highlights", "term of office",
    "committee membership", "executive officers", "frequently requested",
    "corporate governance", "additional information",
])


def _title_case(text: str) -> str:
    """Convert ALL-CAPS name to Title Case, preserving initials like 'E.'."""
    tokens = text.split()
    result = []
    for tok in tokens:
        if re.match(r'^[A-Z]\.$', tok):
            result.append(tok)
        else:
            result.append(tok.capitalize())
    return " ".join(result)


def _looks_like_name(s: str) -> bool:
    """Return True if the string has ≥2 tokens each of which is alphabetic
    (allowing '.' and '-' within tokens). Rejects pure numbers, single words,
    and known noise phrases.
    """
    s = s.strip()
    if not s or s.lower() in _NOISE_PHRASES:
        return False
    tokens = s.split()
    if len(tokens) < 2:
        return False
    # Accept tokens: purely alphabetic, or with embedded . or -
    valid_tokens = [
        t for t in tokens
        if re.match(r"^[^\W\d_](?:[^\W\d_]|[.\-'])*$", t, re.UNICODE)
        and t.lower() not in _ROLE_TOKENS
    ]
    return len(valid_tokens) >= 2


def _clean_name(raw: str) -> str:
    """Strip HTML entities, extra whitespace, and trailing punctuation."""
    # Remove quoted nicknames embedded in a formal name, e.g. R. Martin
    # “Marty” Chávez. The formal name remains intact for matching.
    s = re.sub(r'[\u201c"].*?[\u201d"]', " ", raw)
    # Remove common HTML entities
    s = re.sub(r"&#\d+;|&[a-zA-Z]+;", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # Drop trailing commas or periods that aren't initials
    s = re.sub(r"[,;]+$", "", s).strip()
    return s


def _short_blob(text: str) -> str:
    """Truncate to 300 chars for raw_blob."""
    return textwrap.shorten(text, width=300, placeholder="…")


def _dedup(rows: list[DirectorRow]) -> list[DirectorRow]:
    seen: set[str] = set()
    out: list[DirectorRow] = []
    for r in rows:
        key = r["name"].lower()
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _cap_and_log(rows: list[DirectorRow], layout: str) -> list[DirectorRow]:
    if len(rows) > _MAX_ROWS:
        logger.warning(
            "extract_%s: %d rows — likely noise; truncating to %d",
            layout, len(rows), _MAX_ROWS,
        )
        return rows[:_MAX_ROWS]
    return rows


# ---------------------------------------------------------------------------
# extract_table — Apple / Microsoft
# ---------------------------------------------------------------------------

# Matches the "Director Since" column header in Apple (one-liner bold) or MSFT
# (split across two <p> tags).
_TABLE_DS_ONELINER = re.compile(r"<b>\s*Director\s+Since\s*</b>", re.IGNORECASE)
_TABLE_DS_SPLIT = re.compile(
    r"Director\s*</p>\s*<p[^>]*>\s*Since", re.IGNORECASE | re.DOTALL
)
_YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")


def _find_director_since_col(header_cells: list) -> int:
    """Return the index of the 'Director Since' column in a header row, or -1."""
    for i, cell in enumerate(header_cells):
        cell_text = (cell.text_content() or "").strip()
        if re.search(r"Director\s+Since", cell_text, re.IGNORECASE):
            return i
    return -1


def extract_table(html: str) -> list[DirectorRow]:
    """Extract directors from tables with a 'Director Since' column header.

    Handles Apple (one-liner <b>Director Since</b>) and Microsoft (split bold
    <p>Director</p><p>Since</p> pattern).  For each data row the name comes
    from the first cell and the year from the 'Director Since' column.
    """
    doc = _lxml_parse(html)
    rows_out: list[DirectorRow] = []

    for table in doc.iter("table"):
        trs = list(table.iter("tr"))
        if not trs:
            continue

        # Identify the header row — first <tr> that contains the Director Since signal.
        header_idx = -1
        ds_col = -1
        for idx, tr in enumerate(trs[:5]):  # headers are near the top
            cells = list(tr.iter("td", "th"))
            col = _find_director_since_col(cells)
            if col >= 0:
                header_idx = idx
                ds_col = col
                break

        if ds_col < 0:
            continue

        # Iterate data rows (everything after the header)
        for tr in trs[header_idx + 1:]:
            cells = list(tr.iter("td", "th"))
            if len(cells) <= ds_col:
                continue

            # Name: prefer the first bold text element. Microsoft sometimes
            # puts the role/company descriptor in the same cell after it.
            first_cell = cells[0]
            raw_name = first_cell.text_content()
            for elem in first_cell.iter():
                tag = str(elem.tag).lower() if elem.tag is not None else ""
                style = elem.get("style", "") if hasattr(elem, "get") else ""
                if tag in {"b", "strong"} or re.search(
                    r"font-weight\s*:\s*bold", style, re.IGNORECASE
                ):
                    candidate = _clean_name(elem.text_content())
                    if _looks_like_name(candidate):
                        raw_name = candidate
                        break

            name = _clean_name(raw_name)
            if not _looks_like_name(name):
                continue

            # Director Since: year from the ds_col cell
            year_cell = cells[ds_col]
            year_text = (year_cell.text_content() or "").strip()
            year_match = _YEAR_RE.search(year_text)
            start_date_hint = year_match.group(1) if year_match else None

            raw_blob = _short_blob(
                first_cell.text_content() + " | " + year_text
            )

            rows_out.append(DirectorRow(
                name=name,
                role="director",
                start_date_hint=start_date_hint,
                raw_blob=raw_blob,
            ))

    rows_out = _dedup(rows_out)
    return _cap_and_log(rows_out, "table")


# ---------------------------------------------------------------------------
# extract_mini — Alphabet
# ---------------------------------------------------------------------------

_MINI_PHRASE_RE = re.compile(r"Director\s+since\s+(\d{4})", re.IGNORECASE)


def extract_mini(html: str) -> list[DirectorRow]:
    """Extract directors from Alphabet-style per-director mini-tables.

    Each mini-table contains "Director since YYYY" inline, and the director's
    name appears as the first bold element inside that same table.
    """
    doc = _lxml_parse(html)
    rows_out: list[DirectorRow] = []

    for table in doc.iter("table"):
        table_text = table.text_content() or ""
        m = _MINI_PHRASE_RE.search(table_text)
        if not m:
            continue

        year = m.group(1)

        # Name: the first bold element in the table (typically the director name)
        bold_elems = list(table.iter("b", "strong"))
        name = None
        for elem in bold_elems:
            candidate = _clean_name(elem.text_content())
            if _looks_like_name(candidate):
                name = candidate
                break

        if name is None:
            continue

        raw_blob = _short_blob(table_text)
        rows_out.append(DirectorRow(
            name=name,
            role="director",
            start_date_hint=year,
            raw_blob=raw_blob,
        ))

    rows_out = _dedup(rows_out)
    return _cap_and_log(rows_out, "mini")


# ---------------------------------------------------------------------------
# extract_prose — Berkshire
# ---------------------------------------------------------------------------

# Bold spans: <span style="font-weight:bold">ALL-CAPS NAME</span>
# The Berkshire fixture uses bare style="font-weight:bold" (no extra properties).
# Match ALL-CAPS content that looks like a person name (2+ uppercase words).
_PROSE_BOLD_CAPS = re.compile(
    r"<span[^>]*font-weight\s*:\s*bold[^>]*>"
    r"([A-Z]{2,}(?:[.\s]+[A-Z][A-Z0-9.]*)+)"
    r"</span>",
    re.IGNORECASE,
)

# Exclude known non-name all-caps phrases
_PROSE_NOISE = re.compile(
    r"^(RESOLVED|WHEREAS|NOTE|IMPORTANT|WARNING|NOTICE|THE\s+BOARD|"
    r"DIRECTOR\s+SINCE|ANNUAL\s+MEETING|BOARD\s+OF)\b",
    re.IGNORECASE,
)


def extract_prose(html: str) -> list[DirectorRow]:
    """Extract directors from Berkshire-style ALL-CAPS bold-span prose.

    Names appear as <span style="font-weight:bold">WARREN E. BUFFETT</span>.
    Converts to Title Case.  start_date_hint is always None (no year in prose).
    """
    rows_out: list[DirectorRow] = []

    for m in _PROSE_BOLD_CAPS.finditer(html):
        raw = m.group(1).strip()

        # Drop known noise phrases
        if _PROSE_NOISE.match(raw):
            continue

        name = _title_case(raw)
        if not _looks_like_name(name):
            continue

        # raw_blob: snippet around the span
        start = max(0, m.start() - 20)
        raw_blob = _short_blob(html[start: m.end() + 100])

        rows_out.append(DirectorRow(
            name=name,
            role="director",
            start_date_hint=None,
            raw_blob=raw_blob,
        ))

    rows_out = _dedup(rows_out)
    return _cap_and_log(rows_out, "prose")


# ---------------------------------------------------------------------------
# extract_span — Tesla
# ---------------------------------------------------------------------------

# Tesla Director Bios section uses letter-spacing:0.05pt spans for director names.
# Elon Musk:   font-weight:bold;letter-spacing:0.05pt  (different from other directors)
# Other names: letter-spacing:0.05pt  (no extra bold in same span)
# Both patterns confirmed against fixture (8 directors total).
# Match any span whose style contains letter-spacing:0.05pt (possibly with other props).
_SPAN_DIRECTOR_NAME = re.compile(
    r'<span style="[^"]*letter-spacing:0\.05pt[^"]*">'
    r'([A-Z][A-Z\s]+)'
    r'</span>',
)

# Anchor: only extract from "Director Bios" section to avoid noise elsewhere
_DIRECTOR_BIOS_ANCHOR = re.compile(r'Director\s+Bios', re.IGNORECASE)

# Noise check: drop short non-name ALL-CAPS strings
_SPAN_NOISE = re.compile(
    r'^(TESLA\s+PROPOSALS?|SHAREHOLDER\s+PROPOSALS?|TABLE\s+OF\s+CONTENTS?|'
    r'PROXY\s+STATEMENT|FOR\s+EACH|AGAINST|FOR|NONE)\b',
    re.IGNORECASE,
)


def extract_span(html: str) -> list[DirectorRow]:
    """Extract directors from Tesla-style Director Bios section.

    Director names appear as ALL-CAPS in spans with letter-spacing:0.05pt
    within the 'Director Bios' section.  Elon Musk additionally carries
    font-weight:bold in the same span attribute string; other directors do not.
    Both styles are matched by the same pattern.  Converts to Title Case.
    start_date_hint is None (iXBRL year harvest deferred to §11.2.c).
    """
    rows_out: list[DirectorRow] = []

    # Restrict search to Director Bios section to avoid noise elsewhere
    anchor = _DIRECTOR_BIOS_ANCHOR.search(html)
    if anchor:
        search_start = anchor.start()
    else:
        # Fall back to full document if anchor not found
        search_start = 0

    for m in _SPAN_DIRECTOR_NAME.finditer(html, search_start):
        raw = m.group(1).strip()

        if _SPAN_NOISE.match(raw):
            continue

        name = _title_case(raw)
        if not _looks_like_name(name):
            continue

        start = max(0, m.start() - 20)
        raw_blob = _short_blob(html[start: m.end() + 100])

        rows_out.append(DirectorRow(
            name=name,
            role="director",
            start_date_hint=None,
            raw_blob=raw_blob,
        ))

    rows_out = _dedup(rows_out)
    return _cap_and_log(rows_out, "span")


# ---------------------------------------------------------------------------
# extract_peonames — iXBRL ecd:PeoName
# ---------------------------------------------------------------------------

# iXBRL PeoName tags come in two forms:
#   <ix:nonNumeric name="ecd:PeoName" ...>Mr. Cook</ix:nonNumeric>
#   <span ix:name="ecd:PeoName" ...>Tim Cook</span>
# or deeply nested: the innermost text node is the visible name.
# We use a two-pass regex approach:
#   1. Find every tag that has name="ecd:PeoName" (or ix:name="ecd:PeoName")
#   2. Grab the text content up to the first closing tag.
# This is safe because iXBRL inline tag syntax is regular (no recursive nesting
# within the same tag opening → first close tag is the matching one).

_PEONAME_TAG_RE = re.compile(
    r'<(?:ix:nonNumeric|[a-zA-Z][a-zA-Z0-9:]*)\b[^>]*\bix:name\s*=\s*["\']ecd:PeoName["\'][^>]*>'
    r'|'
    r'<(?:ix:nonNumeric|[a-zA-Z][a-zA-Z0-9:]*)\b[^>]*\bname\s*=\s*["\']ecd:PeoName["\'][^>]*>',
    re.IGNORECASE,
)

# Strip inner HTML tags to get visible text
_STRIP_TAGS_RE = re.compile(r"<[^>]+>")

_MAX_PEONAMES = 20


def extract_peonames(html: str) -> list[PeoNameRow]:
    """Extract iXBRL ecd:PeoName tagged references.

    Tags can appear as:
      <ix:nonNumeric name="ecd:PeoName" ...>Mr. Cook</ix:nonNumeric>
      <span ix:name="ecd:PeoName" ...>Tim Cook</span>
    or similar. Matches on the `name` attribute value exactly equal to
    'ecd:PeoName'. Dedupes identical raw_name values. Caps at 20 results.

    Handles deeply nested structures (multiple ecd:PeoName tags wrapping the
    same text across different reporting periods) by skipping forward past
    any consecutive opening tags to locate the visible text before the first
    closing tag.
    """
    if not html:
        return []

    rows_out: list[PeoNameRow] = []
    seen_names: set[str] = set()

    for m in _PEONAME_TAG_RE.finditer(html):
        tag_end = m.end()
        # Scan forward past any consecutive opening tags to reach the text node.
        # iXBRL nests multiple ecd:PeoName contexts for multi-year tables so
        # the visible name may appear hundreds of chars after the first opening tag.
        # We search up to 2000 chars to cover the deepest nesting seen in practice.
        window = html[tag_end: tag_end + 2000]
        # Find the first closing tag — the text immediately before it is the name.
        close_match = re.search(r"</[^>]+>", window)
        if close_match:
            raw_inner = window[: close_match.start()]
        else:
            raw_inner = window[:300]

        # Strip any nested opening tags and normalise whitespace
        raw_name = _STRIP_TAGS_RE.sub("", raw_inner)
        raw_name = re.sub(r"\s+", " ", raw_name).strip()
        # Decode common HTML entities
        raw_name = re.sub(r"&#\d+;|&[a-zA-Z]+;", " ", raw_name).strip()

        if not raw_name:
            continue

        # Dedupe on normalised lowercase
        key = raw_name.lower()
        if key in seen_names:
            continue
        seen_names.add(key)

        # Build a ≤300 char audit snippet (20 chars before the tag, through the close)
        snippet_start = max(0, m.start() - 20)
        snippet_end = min(len(html), tag_end + (close_match.end() if close_match else 300))
        raw_blob = _short_blob(html[snippet_start:snippet_end])

        rows_out.append(PeoNameRow(
            raw_name=raw_name,
            context_element="ecd:PeoName",
            raw_blob=raw_blob,
        ))

        if len(rows_out) >= _MAX_PEONAMES:
            break

    return rows_out


# ---------------------------------------------------------------------------
# Public dispatcher
# ---------------------------------------------------------------------------


def extract_rows(html: str, layout: str) -> list[DirectorRow]:
    """Dispatch to the per-layout extractor.

    Args:
        html:   Raw HTML string of a DEF 14A filing.
        layout: One of "table", "mini", "prose", "span", "unknown".
                "unknown" always returns [].

    Returns:
        List of DirectorRow dicts.
    """
    if layout == "table":
        return extract_table(html)
    if layout == "mini":
        return extract_mini(html)
    if layout == "prose":
        return extract_prose(html)
    if layout == "span":
        return extract_span(html)
    # "unknown" or any unrecognised value
    return []
