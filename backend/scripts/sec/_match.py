"""Shared name-matching helpers for SEC ingestion scripts.

Public API
----------
    tier1_match(sec_name, candidates) -> tuple[str|None, str|None, str|None]
    last_name_match(last_name, *, candidates) -> list[dict]

``tier1_match`` is the exact-same logic extracted from ``fetch_form4_bulk.py``
(diacritic stripping + nickname tiers). Behaviour is strictly preserved.

``last_name_match`` is new (§11.2.d): scans a candidate list for entries whose
parsed last name matches the supplied token.  Used to resolve iXBRL PeoName
values like "Mr. Cook" (last-name-only) to QIDs.

Neither function imports from ``fetch_form4_bulk`` — zero circular-import risk.
"""

from __future__ import annotations

import re
import unicodedata

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Titles / suffixes to strip during name normalisation
_TITLE_RE = re.compile(
    r"\b(mr|mrs|ms|dr|jr|sr|ii|iii|iv|v|esq|prof|rev)\b\.?",
    re.IGNORECASE,
)

# Name-order suffix tokens dropped during structural parsing
_SUFFIX_TOKENS = {
    "JR", "SR", "I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X",
    "MR", "MRS", "MS", "DR", "PROF", "REV", "ESQ",
}

# First-name nickname → canonical equivalence. Used by tier-c (nickname-scoped)
# match in tier1_match. Constrained additions only — must be unambiguous in
# common business-context usage.
_NICKNAMES: dict[str, set[str]] = {
    "TIMOTHY": {"TIM", "TIMMY"},
    "WILLIAM": {"BILL", "BILLY", "WILL", "WILLY"},
    "ROBERT": {"BOB", "BOBBY", "ROB", "ROBBIE"},
    "RICHARD": {"DICK", "RICK", "RICHIE", "RICKY"},
    "JAMES": {"JIM", "JIMMY", "JAMIE"},
    "JOHN": {"JACK", "JOHNNY"},
    "JOSEPH": {"JOE", "JOEY"},
    "MICHAEL": {"MIKE", "MIKEY", "MICK"},
    "DAVID": {"DAVE", "DAVEY"},
    "DANIEL": {"DAN", "DANNY"},
    "THOMAS": {"TOM", "TOMMY"},
    "CHRISTOPHER": {"CHRIS"},
    "MATTHEW": {"MATT"},
    "ANTHONY": {"TONY"},
    "ANDREW": {"ANDY", "DREW"},
    "EDWARD": {"ED", "EDDIE", "TED", "TEDDY"},
    "CHARLES": {"CHARLIE", "CHUCK"},
    "BENJAMIN": {"BEN", "BENNY"},
    "ALEXANDER": {"ALEX", "XANDER"},
    "NICHOLAS": {"NICK", "NICKY"},
    "FRANCIS": {"FRANK", "FRANKIE"},
    "STEPHEN": {"STEVE"},
    "STEVEN": {"STEVE"},
    "ELIZABETH": {"LIZ", "BETH", "BETSY", "LIZZIE"},
    "KATHERINE": {"KATE", "KATHY", "KATIE"},
    "CATHERINE": {"CATE", "CATHY", "KATIE"},
    "MARGARET": {"MAGGIE", "PEGGY", "MEG"},
    "PATRICIA": {"PAT", "PATTY", "TRICIA"},
    "JENNIFER": {"JEN", "JENNY"},
    "REBECCA": {"BECKY", "BECCA"},
    "SUSAN": {"SUE", "SUSIE"},
    "DEBORAH": {"DEB", "DEBBIE"},
    "BARBARA": {"BARB"},
}

# Reverse index: any spelling (nickname OR canonical) → set of canonical forms
# it could refer to.  Built once at import.
_NAME_TO_CANONICALS: dict[str, set[str]] = {}
for _canonical, _nicks in _NICKNAMES.items():
    _NAME_TO_CANONICALS.setdefault(_canonical, set()).add(_canonical)
    for _n in _nicks:
        _NAME_TO_CANONICALS.setdefault(_n, set()).add(_canonical)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _strip_diacritics(s: str) -> str:
    """Strip combining diacritical marks (NFD decomposition).

    >>> _strip_diacritics("Oliván")
    'Olivan'
    >>> _strip_diacritics("Renée")
    'Renee'
    >>> _strip_diacritics("ASCII")
    'ASCII'
    """
    decomposed = unicodedata.normalize("NFD", s)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def name_canonicals(name: str) -> set[str]:
    """Return canonical first-name forms compatible with ``name``.

    Always includes the input itself (uppercased). For known nicknames or
    canonicals, also includes registered equivalents.

    >>> sorted(name_canonicals("TIM"))
    ['TIM', 'TIMOTHY']
    >>> name_canonicals("TIM") & name_canonicals("TIMOTHY")
    {'TIMOTHY'}
    >>> name_canonicals("")
    set()
    """
    if not name:
        return set()
    u = name.upper()
    return _NAME_TO_CANONICALS.get(u, set()) | {u}


def parse_sec_name(raw: str) -> tuple[str, str]:
    """Parse an SEC RPTOWNERNAME into (lastname, firstname) uppercase tokens.

    SEC convention: last-name first, then first+middle. Comma optional. Suffixes
    (JR/SR/II/III/IV) may trail. Corporate reporters return ("", "").

    >>> parse_sec_name("COOK TIMOTHY D")
    ('COOK', 'TIMOTHY')
    >>> parse_sec_name("COOK, TIMOTHY D.")
    ('COOK', 'TIMOTHY')
    >>> parse_sec_name("BUFFETT WARREN E JR")
    ('BUFFETT', 'WARREN')
    >>> parse_sec_name("OLIVÁN JAVIER")
    ('OLIVAN', 'JAVIER')
    >>> parse_sec_name("")
    ('', '')
    """
    s = unicodedata.normalize("NFKC", raw or "")
    s = _strip_diacritics(s)
    s = _TITLE_RE.sub(" ", s)
    s = s.replace(",", " ").replace(".", " ")
    tokens = [t for t in s.split() if t and t.upper() not in _SUFFIX_TOKENS]
    # Keep one-letter first-name initials (e.g. SEC's "COOK T") while still
    # dropping empty tokens and the explicit suffix list above.
    tokens = [t for t in tokens if len(t) >= 1]
    if len(tokens) < 2:
        return ("", "")
    return (tokens[0].upper(), tokens[1].upper())


def parse_wd_name(raw: str) -> tuple[str, str]:
    """Parse a Wikidata "First Middle Last" label into (lastname, firstname).

    Last token = lastname; first non-initial token = firstname.

    >>> parse_wd_name("Tim Cook")
    ('COOK', 'TIM')
    >>> parse_wd_name("Warren E. Buffett")
    ('BUFFETT', 'WARREN')
    >>> parse_wd_name("Javier Oliván")
    ('OLIVAN', 'JAVIER')
    >>> parse_wd_name("Musk")
    ('', '')
    """
    s = unicodedata.normalize("NFKC", raw or "")
    s = _strip_diacritics(s)
    s = _TITLE_RE.sub(" ", s)
    s = s.replace(",", " ").replace(".", " ")
    tokens = [t for t in s.split() if t and t.upper() not in _SUFFIX_TOKENS]
    tokens = [t for t in tokens if len(t) >= 1]
    if len(tokens) < 2:
        return ("", "")
    return (tokens[-1].upper(), tokens[0].upper())


# ---------------------------------------------------------------------------
# Tier-1 name matching
# ---------------------------------------------------------------------------


def tier1_match(
    sec_name: str,
    candidates: list[dict],
    *,
    name_format: str = "sec",
) -> tuple[str | None, str | None, str | None]:
    """Attempt Tier-1 scoped name match against the candidate pool.

    SEC RPTOWNERNAME is "LAST FIRST MIDDLE" (with optional comma); Wikidata
    labels are "First Middle Last". Structural parsing extracts a canonical
    (lastname, firstname) tuple from each side before comparison.

    Returns (person_qid, match_method, match_confidence) or (None, None, None).

    Priority:
      (a) (LAST, FIRST) exact match → method="wd_p108_scoped", confidence="exact"
      (b) Same LAST + same first-initial unique in pool → method="wd_p108_initial_scoped",
          confidence="initial"
      (c) Same LAST + nickname-compatible FIRST, unique in pool →
          method="wd_p108_nickname_scoped", confidence="nickname"
      Otherwise: (None, None, None) — caller handles unmatched case.

    >>> candidates = [{"person_qid": "Q312097", "person_name_en": "Tim Cook"}]
    >>> tier1_match("COOK TIMOTHY D", candidates)
    ('Q312097', 'wd_p108_scoped', 'exact')

    >>> candidates = [{"person_qid": "Q76", "person_name_en": "Barack Obama"}]
    >>> tier1_match("OBAMA B", candidates)
    ('Q76', 'wd_p108_initial_scoped', 'initial')

    >>> # Ambiguous initial → no match
    >>> candidates = [
    ...     {"person_qid": "Q1", "person_name_en": "John Smith"},
    ...     {"person_qid": "Q2", "person_name_en": "Jane Smith"},
    ... ]
    >>> tier1_match("SMITH J", candidates)
    (None, None, None)

    >>> tier1_match("BERKSHIRE HATHAWAY INC", candidates)
    (None, None, None)
    """
    # Form 4 passes LAST-FIRST (RPTOWNERNAME convention); DEF 14A extractors
    # and iXBRL PeoName values are FIRST-LAST. parse_wd_name handles the
    # natural order — same as Wikidata labels.
    parser = parse_sec_name if name_format == "sec" else parse_wd_name
    sec_last, sec_first = parser(sec_name)
    if not sec_last or not sec_first:
        return None, None, None

    def _parsed_names_for(c: dict) -> list[tuple[str, str]]:
        """Return all (wd_last, wd_first) tuples for a candidate."""
        variants: list[str] = c.get("name_variants") or [c.get("person_name_en", "") or ""]
        results = []
        for v in variants:
            wd_last, wd_first = parse_wd_name(v)
            if wd_last and wd_first:
                results.append((wd_last, wd_first))
        base_name = c.get("person_name_en", "") or ""
        if base_name and base_name not in variants:
            wd_last, wd_first = parse_wd_name(base_name)
            if wd_last and wd_first:
                results.append((wd_last, wd_first))
        return results

    # (a) Exact (last, first)
    for c in candidates:
        for wd_last, wd_first in _parsed_names_for(c):
            if wd_last == sec_last and wd_first == sec_first:
                return c["person_qid"], "wd_p108_scoped", "exact"

    # (b) Same last + same first-initial, unique in pool
    sec_fi = sec_first[0]
    initial_matches = []
    for c in candidates:
        matched = any(
            wd_last == sec_last and wd_first and wd_first[0] == sec_fi
            for wd_last, wd_first in _parsed_names_for(c)
        )
        if matched:
            initial_matches.append(c)
    if len(initial_matches) == 1:
        return initial_matches[0]["person_qid"], "wd_p108_initial_scoped", "initial"

    # (c) Same last + nickname-compatible first, unique in pool
    sec_canonicals = name_canonicals(sec_first)
    if sec_canonicals and sec_canonicals != {sec_first}:
        nickname_matches = []
        for c in candidates:
            matched = any(
                wd_last == sec_last
                and wd_first
                and (name_canonicals(wd_first) & sec_canonicals)
                for wd_last, wd_first in _parsed_names_for(c)
            )
            if matched:
                nickname_matches.append(c)
        if len(nickname_matches) == 1:
            return nickname_matches[0]["person_qid"], "wd_p108_nickname_scoped", "nickname"

    return None, None, None


def global_name_fallback(
    name: str,
    db,
    *,
    org_qid: str | None = None,
    org_country: str | None = None,
) -> tuple[str | None, str | None, str | None]:
    """Look up a "First Last" director name against the full persons collection.

    Safety-net matcher for DEF 14A pool-sparsity cases.  Fires only after
    tier1_match (scoped) misses.  Bias toward "unmatched" over wrong edge.

    Returns (person_qid, match_method, match_confidence) or (None, None, None).

    Strategy:
      1. Parse (last, first) via parse_wd_name.
      2. AQL: find all persons where last token of name_en == @last AND first
         token of name_en == @first  (also searches aliases_en entries).
      3. Disambiguate:
         - Exactly 1 hit → method="def14a_global", confidence="global_unique"
         - 2+ hits: narrow to BUSINESS_OCCUPATIONS set. If 1 hit →
           method="def14a_global_occupation", confidence="global_occupation"
         - Still 2+: if org_country provided, prefer hits whose nationalities
           contain org_country. If exactly 1 →
           method="def14a_global_country", confidence="global_country"
         - Otherwise → None (ambiguous; prefer unmatched over wrong edge).

    Schema observed on persons:
      name_en (str), aliases_en (list[str]|null), occupations (list[str]),
      nationalities (list[str] — ISO 3166-1 alpha-2 codes, e.g. "US").
    """
    # Business-context occupations for disambiguation filter
    _BUSINESS_OCCS = frozenset({
        "businessperson", "executive", "financier", "engineer",
        "politician", "lawyer", "economist",
    })

    wd_last, wd_first = parse_wd_name(name)
    if not wd_last or not wd_first:
        return None, None, None

    # AQL: match by last+first token of name_en, OR last+first token of any
    # aliases_en entry.  Use SPLIT/FIRST/LAST to avoid full-scan on tokens.
    # Schema note: name_en is "First Middle Last"; aliases_en is list or null.
    aql = """
    FOR p IN persons
      LET parts = SPLIT(p.name_en, " ")
      LET name_last  = UPPER(LAST(parts))
      LET name_first = UPPER(FIRST(parts))
      LET alias_match = (
        FOR a IN (p.aliases_en != null ? p.aliases_en : [])
          LET ap = SPLIT(a, " ")
          FILTER UPPER(LAST(ap)) == @last AND UPPER(FIRST(ap)) == @first
          LIMIT 1
          RETURN 1
      )
      FILTER (name_last == @last AND name_first == @first)
          OR LENGTH(alias_match) > 0
      RETURN {
        qid: p._key,
        occupations: p.occupations,
        nationalities: p.nationalities
      }
    """
    try:
        cursor = db.aql.execute(aql, bind_vars={"last": wd_last, "first": wd_first})
        hits = list(cursor)
    except Exception:
        return None, None, None

    if not hits:
        return None, None, None

    if len(hits) == 1:
        return hits[0]["qid"], "def14a_global", "global_unique"

    # Narrow by business occupation
    biz_hits = [
        h for h in hits
        if _BUSINESS_OCCS & set(h.get("occupations") or [])
    ]
    if len(biz_hits) == 1:
        return biz_hits[0]["qid"], "def14a_global_occupation", "global_occupation"

    if len(biz_hits) >= 2 and org_country:
        country_hits = [
            h for h in biz_hits
            if org_country in (h.get("nationalities") or [])
        ]
        if len(country_hits) == 1:
            return country_hits[0]["qid"], "def14a_global_country", "global_country"

    return None, None, None


def last_name_match(
    last_name_token: str,
    *,
    candidates: list[dict],
) -> list[dict]:
    """Return all candidates whose parsed last name matches ``last_name_token``.

    Used to resolve iXBRL PeoName values like "Mr. Cook" (where only the last
    name survives prefix-stripping) to a QID.  Caller is responsible for
    deciding what to do when multiple hits are returned.

    ``candidates`` should already be pre-filtered to the org of interest by the
    caller (pass ``person_candidates.get(org_qid, [])``).  If no scope is
    available, pass the full pool and accept that multiple-hits = unmatched.

    Diacritic normalisation is applied on both sides.

    >>> candidates = [
    ...     {"person_qid": "Q312097", "person_name_en": "Tim Cook"},
    ...     {"person_qid": "Q9685", "person_name_en": "Tim Burton"},
    ... ]
    >>> last_name_match("COOK", candidates=candidates)
    [{'person_qid': 'Q312097', 'person_name_en': 'Tim Cook'}]
    >>> last_name_match("UNKNOWN", candidates=candidates)
    []
    """
    target = _strip_diacritics(last_name_token.strip().upper())
    if not target:
        return []

    hits = []
    for c in candidates:
        variants: list[str] = c.get("name_variants") or [c.get("person_name_en", "") or ""]
        base = c.get("person_name_en", "") or ""
        if base and base not in variants:
            variants = list(variants) + [base]

        for v in variants:
            wd_last, wd_first = parse_wd_name(v)
            if wd_last and wd_last == target:
                hits.append(c)
                break  # one hit per candidate is enough

    return hits
