"""Resolve 13F CUSIP + nameOfIssuer → Organization QID via local name matching.

§11.4.b — zero external HTTP dependencies. We match the `nameOfIssuer` string
that ships in every 13F row against the `organizations` collection (name_en +
aliases_en), normalised to strip corporate suffixes and share-class tokens.

The CUSIP itself is not resolvable from local data (no Wikidata property for
CUSIP), so we use it only as a cache key: once a CUSIP has matched (or failed)
via its issuer name, the decision is memoised in `/data/sec/cusip_to_qid.json`
for future quarters.

Tradeoff vs. an external resolver (OpenFIGI etc.):
    - Simpler: no auth, no throttle, no network.
    - Noisier: "ALPHABET INC CL A" vs "Alphabet Inc." needs normalisation;
      share-class splits (Google A/C) collapse to the same Wikidata entity.
    - Ambiguous hits (e.g. two orgs with name "APEX") resolve to None —
      bias toward unmatched over wrong edge.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from pathlib import Path

logger = logging.getLogger("resolve_cusip")

# Tokens routinely trailing issuer names that add no semantic information.
# Matched as whole tokens after punctuation stripping and uppercasing.
_CORP_SUFFIXES = frozenset({
    "INC", "CORP", "CORPORATION", "CO", "COMPANY", "LTD", "LIMITED",
    "LLC", "LP", "LLP", "PLC", "SA", "NV", "AG", "SE", "AB", "OYJ",
    "HOLDINGS", "HOLDING", "GROUP", "GRP", "TRUST", "FUND",
    "THE", "NEW",
})

# Share-class / instrument tokens. Strip "CL A", "CLASS B", "COM", "ADR", "ORD".
_CLASS_SUFFIXES = frozenset({
    "CL", "CLASS", "COM", "COMMON", "ADR", "ADS", "ORD", "SHS",
    "A", "B", "C", "D",  # only stripped when trailing a CL/CLASS token
})

# Single-letter share-class tokens (A, B, C, D) are only stripped if the
# preceding token was CL/CLASS. Otherwise they stay (e.g. "VISA A" is
# legitimate, but "VISA CL A" should collapse to "VISA").


def _strip_diacritics(s: str) -> str:
    nfd = unicodedata.normalize("NFD", s)
    return "".join(c for c in nfd if not unicodedata.combining(c))


def normalize_issuer_name(raw: str) -> str:
    """Normalise an issuer name for matching.

    - Uppercase, strip diacritics
    - Replace punctuation with spaces
    - Drop trailing corporate suffixes (INC, CORP, LTD, …) repeatedly
    - Drop trailing share-class tokens (COM, CL A, CLASS B, ADR, …)
    - Collapse whitespace

    >>> normalize_issuer_name("Apple Inc.")
    'APPLE'
    >>> normalize_issuer_name("ALPHABET INC CL A")
    'ALPHABET'
    >>> normalize_issuer_name("Berkshire Hathaway Inc. Class B")
    'BERKSHIRE HATHAWAY'
    >>> normalize_issuer_name("MICROSOFT CORP COM")
    'MICROSOFT'
    >>> normalize_issuer_name("")
    ''
    """
    if not raw:
        return ""
    s = _strip_diacritics(raw).upper()
    s = re.sub(r"[^A-Z0-9 ]+", " ", s)
    tokens = [t for t in s.split() if t]

    # Strip share-class tail: peel CL/CLASS [A|B|…] or COM/ADR/ADS/ORD/SHS
    while tokens:
        last = tokens[-1]
        if last in {"COM", "COMMON", "ADR", "ADS", "ORD", "SHS"}:
            tokens.pop()
            continue
        # "CL A", "CLASS B": drop pair
        if len(tokens) >= 2 and tokens[-2] in {"CL", "CLASS"} and len(tokens[-1]) == 1:
            tokens.pop(); tokens.pop()
            continue
        # Lone "CL" / "CLASS" at the tail
        if last in {"CL", "CLASS"}:
            tokens.pop()
            continue
        break

    # Strip trailing corporate suffixes repeatedly (e.g. "… HOLDINGS INC")
    while tokens and tokens[-1] in _CORP_SUFFIXES:
        tokens.pop()
    # Punctuation-separated suffixes such as "S.A." become two tokens after
    # punctuation normalization. Treat the common forms as one suffix.
    while len(tokens) >= 2 and tuple(tokens[-2:]) in {
        ("S", "A"), ("N", "V"), ("S", "E"), ("A", "G"),
    }:
        tokens[-2:] = []

    # Strip leading "THE"
    while tokens and tokens[0] == "THE":
        tokens.pop(0)

    return " ".join(tokens)


def _resolve_via_db(name_of_issuer: str, db) -> str | None:
    """Match a normalised issuer name against organizations.name_en / aliases_en.

    Returns the QID on a unique hit, else None. Ambiguous multi-hit resolves to
    None — we prefer unmatched over wrong edge.
    """
    target = normalize_issuer_name(name_of_issuer)
    if not target:
        return None

    # AQL: normalise org name_en and aliases_en the same way, filter on equality.
    # We push normalisation into AQL (UPPER + REGEX_REPLACE + token strip is not
    # trivial in AQL), so instead we fetch candidates by a coarse prefix filter
    # on the first token and refine in Python. First token is a good index-ish
    # selector because corporate suffixes are always trailing.
    first_token = target.split(" ", 1)[0]
    aql = """
    FOR o IN organizations
      FILTER CONTAINS(UPPER(o.name_en), @first)
          OR LENGTH(
               FOR a IN (o.aliases_en != null ? o.aliases_en : [])
                 FILTER CONTAINS(UPPER(a), @first)
                 LIMIT 1
                 RETURN 1
             ) > 0
      LIMIT 500
      RETURN {qid: o._key, name_en: o.name_en, aliases_en: o.aliases_en}
    """
    try:
        cursor = db.aql.execute(aql, bind_vars={"first": first_token})
        candidates = list(cursor)
    except Exception as exc:
        logger.warning("AQL failed for issuer %r: %s", name_of_issuer, exc)
        return None

    hits: list[str] = []
    for c in candidates:
        names = [c.get("name_en") or ""] + list(c.get("aliases_en") or [])
        for n in names:
            if normalize_issuer_name(n) == target:
                hits.append(c["qid"])
                break

    # Unique hit only
    unique = set(hits)
    if len(unique) == 1:
        return next(iter(unique))
    return None


class CusipResolver:
    """CUSIP→QID resolver with persistent JSON cache.

    Cache file stores {cusip: qid_or_null}. Negative entries are cached too, so
    a missing issuer doesn't re-scan the org collection every quarter. Call
    ``save()`` to flush; safe to call repeatedly.
    """

    def __init__(self, db, cache_path: str | Path = "/data/sec/cusip_to_qid.json"):
        self.db = db
        self.cache_path = Path(cache_path)
        self._cache: dict[str, str | None] = {}
        self._dirty = False
        self._load()

    def _load(self) -> None:
        if self.cache_path.exists():
            try:
                self._cache = json.loads(self.cache_path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("could not load %s: %s — starting fresh", self.cache_path, exc)
                self._cache = {}

    def save(self) -> None:
        if not self._dirty:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._cache, sort_keys=True, indent=0))
        tmp.replace(self.cache_path)
        self._dirty = False

    def resolve(self, cusip: str, name_of_issuer: str) -> str | None:
        cusip = (cusip or "").strip().upper()
        if not cusip:
            return None
        if cusip in self._cache:
            return self._cache[cusip]
        qid = _resolve_via_db(name_of_issuer, self.db)
        self._cache[cusip] = qid
        self._dirty = True
        return qid

    def stats(self) -> dict[str, int]:
        total = len(self._cache)
        matched = sum(1 for v in self._cache.values() if v)
        return {"total": total, "matched": matched, "unmatched": total - matched}
