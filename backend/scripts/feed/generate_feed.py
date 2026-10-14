"""generate_feed.py — Build the HumansMap connections-feed card pool.

Reads ArangoDB (READ-ONLY) and writes Redis (v2 layout):
    feed:cardmap:v2       — Redis HASH: id → compact JSON card body.
    feed:order:all:v2     — JSON array of all card IDs in ranked order.
    feed:order:cat:<c>:v2 — JSON array of IDs for each category.
    feed:meta:v2          — {"count", "generated_at", "by_type", "by_category", "categories"}.

Card types produced:
    relationship  — two notable people connected by a romantic/familial/
                    professional edge, cross-domain pairs preferred.
    dynasty       — surname-coherent family (Bach, Grimm, Kennedys…), no
                    royal blobs.
    organization  — org + its most notable affiliates, relation-aware
                    (founded / led / alumni / leadership / players / members).

Usage (inside the backend container):
    python -m scripts.feed.generate_feed
    python -m scripts.feed.generate_feed --dry-run /tmp/feed_cards.json
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import logging
import math
import sys
from datetime import UTC, datetime
from typing import Any

from app.database import get_arango_client, get_db
from app.domain.feed_contract import FEED_CONTENT_VERSION
from app.domain.organization_roles import (
    FEED_ORGANIZATION_ROLE_LABELS,
    FEED_ORGANIZATION_ROLE_PRIORITY,
    feed_role_label,
)
from app.services.cache import get_redis
from scripts.feed.domains import (
    CATEGORY_ORDER,
    DOMAIN_LABELS,
    categories_for_domains,
    categories_for_org,
    domain_label,
    domain_of,
    is_cross_domain,
    pretty_occupation,
)
from scripts.feed.generate_card_samples import (
    UnionFind,
    _assign_depths,
    _year,
    format_dates,
    format_lifespan,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Redis keys (v2 layout — memory-scalable). The feed is regenerated
# explicitly, not by Redis expiry: a TTL can leave a healthy long-running API
# with no feed for every visitor after seven days.
FEED_CARDMAP_KEY: str = "feed:cardmap:v2"
FEED_ORDER_ALL_KEY: str = "feed:order:all:v2"
FEED_ORDER_CAT_PREFIX: str = "feed:order:cat:"
FEED_ORDER_NAT_PREFIX: str = "feed:order:nat:"
FEED_ORDER_TYPE_PREFIX: str = "feed:order:type:"
FEED_META_KEY: str = "feed:meta:v2"

# Curated set of ISO-3166 alpha-2 codes we build per-nationality boost-lists for.
# Covers Europe + North America. Codes are uppercase strings.
# Source: ISO 3166-1 alpha-2; scope: EU/EEA member states + major EU neighbours
# + UK + NAFTA/USMCA countries.  Sorted for readability; order has no effect.
SUPPORTED_NATIONALITIES: frozenset[str] = frozenset({
    # North America
    "US", "CA", "MX",
    # British Isles
    "GB", "IE",
    # Western Europe
    "FR", "DE", "IT", "ES", "PT", "NL", "BE", "LU", "CH", "AT",
    # Nordic / Baltic
    "SE", "NO", "DK", "FI", "IS",
    "EE", "LV", "LT",
    # Central / Eastern Europe
    "PL", "CZ", "SK", "HU", "RO", "BG",
    # Southern Europe / Balkans
    "GR", "HR", "SI", "RS",
    # Others with notable Wikidata presence
    "UA",
})

# Country NAME → ISO-3166 alpha-2, for the single org flag (org.country is a
# full name like "Germany"). Org cards show ONE flag — the org's own country —
# rather than the union of its people's nationalities (which is confusing, e.g.
# a German university with alumni from many countries). Unmapped → no flag.
COUNTRY_NAME_TO_ISO2: dict[str, str] = {
    "United States": "US", "United States of America": "US", "USA": "US",
    "Canada": "CA", "Mexico": "MX",
    "United Kingdom": "GB", "Great Britain": "GB", "England": "GB",
    "Scotland": "GB", "Wales": "GB", "Northern Ireland": "GB", "Ireland": "IE",
    "France": "FR", "Germany": "DE", "Italy": "IT", "Spain": "ES",
    "Portugal": "PT", "Netherlands": "NL", "Belgium": "BE", "Luxembourg": "LU",
    "Switzerland": "CH", "Austria": "AT",
    "Sweden": "SE", "Norway": "NO", "Denmark": "DK", "Finland": "FI",
    "Iceland": "IS", "Estonia": "EE", "Latvia": "LV", "Lithuania": "LT",
    "Poland": "PL", "Czech Republic": "CZ", "Czechia": "CZ", "Slovakia": "SK",
    "Hungary": "HU", "Romania": "RO", "Bulgaria": "BG",
    "Greece": "GR", "Croatia": "HR", "Slovenia": "SI", "Serbia": "RS",
    "Ukraine": "UA", "Russia": "RU", "Turkey": "TR",
    "Australia": "AU", "New Zealand": "NZ", "Japan": "JP", "China": "CN",
    "India": "IN", "Brazil": "BR", "Argentina": "AR", "Israel": "IL",
    "South Africa": "ZA",
}


def _org_country_iso(country_name: str | None) -> str | None:
    """Map an org's country name to an ISO-2 code for its single flag, or None."""
    if not country_name:
        return None
    return COUNTRY_NAME_TO_ISO2.get(country_name.strip())


# Statement-count thresholds
REL_MIN_STATEMENTS: int = 100        # lowered from 120 → more relationship cards
ORG_MIN_STATEMENTS_DEFAULT: int = 120
ORG_MIN_STATEMENTS_THIN: int = 80   # for sports/founder where pools are thinner
DYN_MIN_STATEMENTS: int = 50         # 120→100→70→50: floor admits more family members
                                     # so more families reach 3; ~2000 quality candidates
                                     # at 50 (surname filter still guards against blobs)

# Card-count targets (caps; actual count limited by available data)
REL_TARGET: int = 600
ORG_TARGET: int = 900  # raised from 700 to accommodate alumni + leadership cards
DYN_TARGET: int = 1000  # raised 400→500→1000 — dynasties are a favourite (top-scored kept)

# Dynasty constraints (§3.2) — relaxed for larger pool
DYN_MAX_COMPONENT_SIZE: int = 20        # raised from 15 → catch more mid-size families
DYN_COHERENT_TRIM_SIZE: int = 15        # raised from 12 → keep more members before trim
DYN_MIN_MODAL_FRACTION: float = 0.40   # lowered from 0.50 → more families qualify
DYN_MIN_MEMBERS: int = 3               # minimum surviving notable members
DYN_MAX_DISPLAYED: int = 6             # shown in card; rest shown as "+N more"

# Mega-hub threshold for member_of / sports / board relations
ORG_MEGA_HUB_THRESHOLD: int = 60

# Max people shown per org card
ORG_MAX_PEOPLE: int = 5

# Max cards produced per org (raised from 2 → 3 for more org coverage)
ORG_MAX_CARDS_PER_ORG: int = 3

# Relation types that get children appended (precise parentage only)
COUPLE_RELATION_TYPES: frozenset[str] = frozenset({"married", "together"})

# Maximum children shown per relationship card (oldest→newest); overflow → summary entry
REL_MAX_CHILDREN: int = 4

# Maximum OTHER (previous/concurrent) romantic relationships listed per partner
# on a relationship card, ordered earliest→latest. Keeps the card payload small.
REL_MAX_OTHER: int = 3

# Edge `type` values (NOT feed labels) that count as romantic/spousal couple
# relations — used to scope the per-partner "other relationships" timeline.
# These are the spouse/partner edge types; parent/child/sibling/collaborator are
# intentionally excluded so the timeline stays about partners.
COUPLE_EDGE_TYPES: frozenset[str] = frozenset({"spouse", "unmarried_partner"})

# Relationship types → feed relation labels
RELATION_LABEL_MAP: dict[str, str] = {
    "spouse":                  "married",
    "unmarried_partner":       "together",
    "professional_collaborator": "collaborators",
    "relative":                "relatives",
}

# Relation weight for scoring (§3.1)
RELATION_SCORE_WEIGHT: dict[str, float] = {
    "married":       1.0,
    "together":      1.0,
    "relatives":     0.7,
    "collaborators": 0.6,
}

# Affiliation type → (headline, relation key, use_thin_threshold, min_people)
# founder/CEO/board/etc. are NOT here — they all fold into the single
# relation-aware "leadership" card with per-person role labels (the role
# label under the name conveys founder/director/etc., so a dedicated
# "Founded by" card is redundant). Sport players come from _build_sport_cards.
AFFILIATION_MAP: dict[str, tuple[str, str, bool, int]] = {
    "member_of":          ("Members",         "members",     False, 2),
    "educated_at":        ("Notable alumni",  "alumni",      False, 4),  # §A3 — default stmts threshold (120) for better scoring
}

# Leadership affiliation types merged into a single card per org (§A4).
# Maps raw affiliation type → display role label. founder + CEO included here
# so they show with a role label when known.
# The org "people" card shows the most-notable people connected to an org
# (high statement-count) regardless of role — leaders, but also notable
# employees / shareholders / insiders. A role label is attached when the
# affiliation type maps to one; otherwise the person shows with no label.
# value None → known affiliation but no editorial role label to show.
ORG_ROLE_LABELS = FEED_ORGANIZATION_ROLE_LABELS
# Label priority for choosing one role when a person has several (lower = better).
# Persons with only unlabelled affiliations (employee/insider) get no label.
ORG_ROLE_PRIORITY = list(FEED_ORGANIZATION_ROLE_PRIORITY)
# Max people shown on an organization card.
ORG_PEOPLE_CAP: int = 5
# Low floor for the org "people" pass — we rank by notability and cap, so a low
# floor just lets sparse orgs fill up (a single 120+ affiliate would otherwise
# leave a lone-portrait card). Matches the sport-players floor.
ORG_PEOPLE_MIN_STATEMENTS: int = 30

# Sport: org types + ranking floor for the top-N sports-club players pass.
SPORT_ORG_TYPES: list[str] = ["sports_club", "sports_facility", "stadium"]
SPORT_TOP_ORGS: int = 150           # top N sports orgs by affiliate count
SPORT_PLAYER_MIN_STATEMENTS: int = 50  # low floor — we want the most-known players
SPORT_MIN_PLAYERS: int = 3          # need at least this many to make a card

# Org types eligible for alumni cards (§A3)
ALUMNI_ORG_TYPES: frozenset[str] = frozenset({
    "university", "college", "educational_institution",
})

# Relation weight for org scoring
ORG_RELATION_WEIGHT: dict[str, float] = {
    "founded_by":   1.0,
    "led_by":       0.95,
    "organization": 0.9,  # the people-connected-to-an-org card (was "leadership")
    "alumni":       0.85,  # §A3 — boosted so university alumni cards rank above members
    "board":         0.7,
    "players":      0.5,
    "members":      0.4,
}

# Org type → human-readable label
ORG_TYPE_LABELS: dict[str, str] = {
    "company":           "Company",
    "nonprofit":         "Nonprofit",
    "government_agency": "Government agency",
    "university":        "University",
    "secondary_school":  "School",
    "foundation":        "Foundation",
    "sports_club":       "Sports club",
    "publisher":         "Publisher",
    "organization":      "Organisation",
    "political_party":   "Political party",
    "band":              "Music group",
    "record_label":      "Record label",
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("generate_feed")


# ---------------------------------------------------------------------------
# Shared formatting helpers
# ---------------------------------------------------------------------------


def _card_id(*parts: str) -> str:
    """Generate a stable 12-character hex id from arbitrary string parts."""
    raw = ":".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def _countries_from_persons(*nat_lists: list[str] | None) -> list[str]:
    """Return deduplicated, sorted ISO-2 country codes from person nationality lists.

    Accepts zero or more nationality lists (one per person).  Only uppercase
    2-letter codes are kept; the result is sorted alphabetically for
    determinism and capped at 4 entries.

    Args:
        *nat_lists: Each positional arg is a list of nationality codes for one
                    person (or None if unavailable).

    Returns:
        Sorted list of up to 4 valid ISO-2 uppercase codes.
    """
    seen: set[str] = set()
    for nat_list in nat_lists:
        if not nat_list:
            continue
        for code in nat_list:
            if isinstance(code, str) and len(code) == 2:
                seen.add(code.upper())
    return sorted(seen)[:4]


def _modal_country(*nat_lists: list[str] | None) -> str | None:
    """Return the single most-common ISO-2 code across the given nationality
    lists, or None.  Used as the org card's one flag — a German university's
    alumni are mostly German, so the modal nationality ≈ the org's country.
    (The org docs' own `country` field is unreliable in the source data.)
    """
    counter: collections.Counter[str] = collections.Counter()
    for nat_list in nat_lists:
        if not nat_list:
            continue
        for code in nat_list:
            if isinstance(code, str) and len(code) == 2:
                counter[code.upper()] += 1
    return counter.most_common(1)[0][0] if counter else None


def _domain_label_for_person(
    primary_occupation: str | None,
    occupations: list[str] | None,
) -> str | None:
    """Return editorial domain label for a person, trying primary first."""
    if primary_occupation:
        lbl = domain_label(primary_occupation)
        if lbl:
            return lbl
    if occupations:
        lbl = domain_label(occupations)
        if lbl:
            return lbl
    return None


def _prettify_org_type(raw_type: str | None) -> str:
    """Map raw org type string to editorial label."""
    if not raw_type:
        return "Organisation"
    return ORG_TYPE_LABELS.get(raw_type.lower(), raw_type.replace("_", " ").title())


# ---------------------------------------------------------------------------
# Children helper (precise parentage — both parents must share the child)
# ---------------------------------------------------------------------------


def _fetch_shared_children(
    db: Any,
    a_full_id: str,
    b_full_id: str,
) -> list[dict]:
    """Return children that appear in BOTH parents' child-edge sets.

    Uses AQL set intersection to guarantee BOTH parents have a ``child`` edge
    to the same person — no heuristic or looser match.  Children need not be
    notable; we resolve them regardless of statement count.

    Returns a list (oldest → newest by birth year, capped at REL_MAX_CHILDREN)
    of dicts ``{"qid", "name", "life"}``.  If more than REL_MAX_CHILDREN shared
    children exist, a trailing overflow entry ``{"name": "+N more",
    "overflow": True}`` is appended.  Returns ``[]`` if no shared child exists.

    Args:
        db: ArangoDB database handle.
        a_full_id: ArangoDB document id of parent A, e.g. ``"persons/Q10520"``.
        b_full_id: ArangoDB document id of parent B, e.g. ``"persons/Q19810"``.
    """
    aql = """
    LET a_child_keys = (
        FOR e IN relationships
            FILTER e.type == 'child'
            FILTER e._from == @a_id
            RETURN PARSE_IDENTIFIER(e._to).key
    )
    LET b_child_keys = (
        FOR e IN relationships
            FILTER e.type == 'child'
            FILTER e._from == @b_id
            RETURN PARSE_IDENTIFIER(e._to).key
    )
    LET shared_keys = INTERSECTION(a_child_keys, b_child_keys)
    FOR k IN shared_keys
        LET p = DOCUMENT(CONCAT('persons/', k))
        FILTER p != null
        RETURN {
            qid:        p.wikidata_id,
            name:       p.name_en,
            birth_date: p.birth_date,
            death_date: p.death_date
        }
    """
    cursor = db.aql.execute(
        aql,
        bind_vars={"a_id": a_full_id, "b_id": b_full_id},
        ttl=60,
    )
    raw: list[dict] = list(cursor)

    if not raw:
        return []

    # Sort oldest → newest; treat missing birth year as 9999 (sort last)
    def _birth_sort_key(child: dict) -> int:
        yr = _year(child.get("birth_date"))
        return int(yr) if yr else 9999

    raw.sort(key=_birth_sort_key)

    overflow_count = max(0, len(raw) - REL_MAX_CHILDREN)
    displayed = raw[:REL_MAX_CHILDREN]

    result: list[dict] = [
        {
            "qid":  c["qid"],
            "name": c["name"] or "",
            "life": format_lifespan(c.get("birth_date"), c.get("death_date")),
        }
        for c in displayed
    ]

    if overflow_count > 0:
        result.append({"name": f"+{overflow_count} more", "overflow": True})

    return result


# ---------------------------------------------------------------------------
# §3.1 — Relationship card builder
# ---------------------------------------------------------------------------


def _fetch_relationship_edges(db: Any) -> list[dict]:
    """Fetch relationship edges where both persons meet REL_MIN_STATEMENTS (100).

    The ``_id`` fields for A and B are included so that the children helper can
    query child edges by ArangoDB document id without a secondary lookup.
    """
    aql = """
    FOR a IN persons
        FILTER a.wikidata_statements_count >= @min_stmts
        FILTER a.description_en != null AND LENGTH(a.occupations) > 0
        FOR b, e IN 1..1 OUTBOUND a relationships
            FILTER e.type IN @types
            FILTER b.wikidata_statements_count >= @min_stmts
            FILTER b.description_en != null AND LENGTH(b.occupations) > 0
        RETURN {
            type:       e.type,
            start_date: e.start_date,
            end_date:   e.end_date,
            a: {
                _id:           a._id,
                qid:           a.wikidata_id,
                name:          a.name_en,
                description:   a.description_en,
                occupations:   a.occupations,
                primary_occ:   a.primary_occupation,
                notable_works: a.notable_works,
                birth_date:    a.birth_date,
                death_date:    a.death_date,
                stmts:         a.wikidata_statements_count,
                nationalities: a.nationalities
            },
            b: {
                _id:           b._id,
                qid:           b.wikidata_id,
                name:          b.name_en,
                description:   b.description_en,
                occupations:   b.occupations,
                primary_occ:   b.primary_occupation,
                notable_works: b.notable_works,
                birth_date:    b.birth_date,
                death_date:    b.death_date,
                stmts:         b.wikidata_statements_count,
                nationalities: b.nationalities
            }
        }
    """
    cursor = db.aql.execute(
        aql,
        bind_vars={
            "types": list(RELATION_LABEL_MAP.keys()),
            "min_stmts": REL_MIN_STATEMENTS,
        },
        ttl=180,
    )
    return list(cursor)


def _score_relationship(edge: dict) -> float:
    """Score a relationship edge for editorial interest (§3.1).

    Components:
        cross_domain       0.35  — different editorial domains
        min_notability     0.25  — log-scaled min statements (+ bonus both≥250)
        relation_weight    0.20  — married/together > relatives > collaborators
        both_have_desc     0.15  — always true here (filtered upstream)
        fame_asymmetry     0.05  — penalise extreme fame imbalance
    """
    a, b = edge["a"], edge["b"]
    rel_label = RELATION_LABEL_MAP.get(edge["type"], "collaborators")

    # Cross-domain
    cross = is_cross_domain(a["occupations"], b["occupations"])
    cross_score = 0.35 if cross else 0.0

    # Notability (log-scaled)
    sa, sb = a["stmts"], b["stmts"]
    min_stmts = min(sa, sb)
    notability = min(1.0, math.log1p(min_stmts) / math.log1p(2000))
    both_high = 0.05 if (sa >= 250 and sb >= 250) else 0.0
    notability_score = 0.25 * notability + both_high

    # Relation weight
    rel_weight = RELATION_SCORE_WEIGHT.get(rel_label, 0.5)
    rel_score = 0.20 * rel_weight

    # Description presence (always 1.0 here since we filtered)
    desc_score = 0.15

    # Fame asymmetry penalty: ratio of min/max, lower = more asymmetric
    max_stmts = max(sa, sb, 1)
    asymmetry_ratio = min_stmts / max_stmts
    asym_score = 0.05 * asymmetry_ratio

    return cross_score + notability_score + rel_score + desc_score + asym_score


def _build_relationship_cards(db: Any) -> list[dict]:
    """Return scored, deduped, cross-domain relationship cards up to REL_TARGET.

    For couple cards (``married`` / ``together``) a ``children`` list is added
    when the pair share ≥1 child.  Parentage is precise: only children linked by
    ``child`` edges from BOTH parents are included (see ``_fetch_shared_children``).
    """
    logger.info("[relationship] Fetching edges…")
    raw_edges = _fetch_relationship_edges(db)
    logger.info("[relationship] Raw edges: %d", len(raw_edges))

    # Dedup symmetric pairs
    seen: set[tuple[str, str, str]] = set()
    deduped: list[dict] = []
    for edge in raw_edges:
        qa = edge["a"]["qid"]
        qb = edge["b"]["qid"]
        if not qa or not qb:
            continue
        key = (min(qa, qb), max(qa, qb), edge["type"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(edge)

    # Filter cross-domain only
    cross_domain = [
        e for e in deduped
        if is_cross_domain(e["a"]["occupations"], e["b"]["occupations"])
    ]
    logger.info(
        "[relationship] After dedup + cross-domain filter: %d", len(cross_domain)
    )

    # Score and sort
    scored = sorted(cross_domain, key=_score_relationship, reverse=True)
    top_edges = scored[:REL_TARGET]
    anchor_ids = list({
        (edge["a"] if (edge["a"].get("stmts") or 0) >= (edge["b"].get("stmts") or 0) else edge["b"])["_id"]
        for edge in top_edges
    })

    # Fetch couple relationships only for anchors that can appear in the final feed.
    # This avoids materializing the global child and partner maps in Python.
    # Shared children are resolved later with indexed per-card queries.
    # Pre-fetch couple (spouse/partner) edges for selected anchors so each
    # partner's OTHER relationships can be listed without per-card AQL.  Maps
    # person QID → list of {other_qid, other_name, relation_label, start_date,
    # end_date}.  Both directions are recorded so a person found on either end
    # of an edge gets the other end as an "other relationship".
    logger.info("[relationship] Pre-fetching couple edges for other-relations…")
    other_rel_aql = """
    FOR anchor_id IN @anchor_ids
        FOR other, e IN 1..1 ANY anchor_id relationships
            FILTER e.type IN @couple_types
            LET pa = DOCUMENT(e._from)
            LET pb = DOCUMENT(e._to)
        FILTER pa != null AND pb != null
        FILTER pa.wikidata_id != null AND pb.wikidata_id != null
        FILTER pa.name_en != null AND pb.name_en != null
        RETURN {
            type:       e.type,
            start_date: e.start_date,
            end_date:   e.end_date,
            a_qid:      pa.wikidata_id, a_name: pa.name_en,
            b_qid:      pb.wikidata_id, b_name: pb.name_en
        }
    """
    other_cursor = db.aql.execute(
        other_rel_aql,
        bind_vars={
            "anchor_ids": anchor_ids,
            "couple_types": list(COUPLE_EDGE_TYPES),
        },
        ttl=180,
    )
    _qid_to_others: dict[str, list[dict]] = {}
    for row in other_cursor:
        label = RELATION_LABEL_MAP.get(row["type"], row["type"])
        # Record edge from A's perspective (other = B) and B's perspective.
        _qid_to_others.setdefault(row["a_qid"], []).append({
            "other_qid":      row["b_qid"],
            "other_name":     row["b_name"],
            "relation_label": label,
            "start_date":     row.get("start_date"),
            "end_date":       row.get("end_date"),
        })
        _qid_to_others.setdefault(row["b_qid"], []).append({
            "other_qid":      row["a_qid"],
            "other_name":     row["a_name"],
            "relation_label": label,
            "start_date":     row.get("start_date"),
            "end_date":       row.get("end_date"),
        })

    # Fetch full profiles for every couple-relationship partner, so the most
    # recent relationship can be promoted into the card's MAIN couple (the big
    # portraits need description / works / occupations / nationalities — not
    # just the name+qid that _qid_to_others carries).
    _all_partner_qids: set[str] = set()
    for _rows in _qid_to_others.values():
        for _r in _rows:
            if _r.get("other_qid"):
                _all_partner_qids.add(_r["other_qid"])

    partner_profiles: dict[str, dict] = {}
    if _all_partner_qids:
        pp_aql = """
        FOR qid IN @qids
            LET p = DOCUMENT(CONCAT("persons/", qid))
            FILTER p != null
            RETURN {
                qid:           p.wikidata_id,
                _id:           p._id,
                name:          p.name_en,
                description:   p.description_en,
                occupations:   p.occupations,
                primary_occ:   p.primary_occupation,
                notable_works: p.notable_works,
                nationalities: p.nationalities,
                stmts:         p.wikidata_statements_count
            }
        """
        pp_cursor = db.aql.execute(
            pp_aql, bind_vars={"qids": list(_all_partner_qids)}, ttl=180,
        )
        for p in pp_cursor:
            if p.get("qid"):
                partner_profiles[p["qid"]] = p

    def _recency_key(start_date: Any, end_date: Any) -> int:
        """Higher = more recent.  End year if present, else start year, else -1
        (undated relationships sort as least recent)."""
        y = _year(end_date) or _year(start_date)
        return int(y) if y else -1

    # Build compact card dicts.
    #
    # Each card is anchored on the MORE NOTABLE person of the scored pair, and
    # the BIG/main couple is that anchor's MOST RECENT relationship (the partner
    # must have a usable profile — description present). Older relationships are
    # demoted to the other-relations list shown below the couple. Featured pairs
    # are de-duplicated so two scored edges that promote to the same recent
    # pairing don't both appear.
    cards: list[dict] = []
    children_added = 0
    featured_seen: set[frozenset] = set()

    def _person_block(prof: dict) -> dict:
        return {
            "qid":   prof["qid"],
            "name":  prof["name"],
            "desc":  prof.get("description"),
            "works": (prof.get("notable_works") or [])[:2],
        }

    def _usable_partner(prof: dict | None) -> bool:
        return bool(prof and prof.get("description"))

    for edge in top_edges:
        a, b = edge["a"], edge["b"]

        # Anchor = more notable; partner0 = the scored partner (always carries a
        # full profile with description, so it's a safe fallback main partner).
        if (a.get("stmts") or 0) >= (b.get("stmts") or 0):
            anchor, partner0 = a, b
        else:
            anchor, partner0 = b, a

        # Build the anchor's full relationship list: the scored edge plus every
        # other couple relationship from the prefetched map (deduped by partner).
        rels: list[dict] = [{
            "qid":      partner0["qid"],
            "name":     partner0["name"],
            "relation": RELATION_LABEL_MAP.get(edge["type"], edge["type"]),
            "start":    edge.get("start_date"),
            "end":      edge.get("end_date"),
            "profile":  partner0,  # full profile from the scored edge
        }]
        seen_partners: set[str] = {partner0["qid"]}
        for r in _qid_to_others.get(anchor["qid"], []):
            oq = r.get("other_qid")
            if not oq or oq == anchor["qid"] or oq in seen_partners:
                continue
            seen_partners.add(oq)
            rels.append({
                "qid":      oq,
                "name":     r["other_name"],
                "relation": r["relation_label"],
                "start":    r.get("start_date"),
                "end":      r.get("end_date"),
                "profile":  partner_profiles.get(oq),
            })

        # Main couple = anchor's most recent relationship with a usable partner.
        ranked = sorted(
            rels, key=lambda r: _recency_key(r["start"], r["end"]), reverse=True
        )
        main_rel = next((r for r in ranked if _usable_partner(r["profile"])), None)
        if main_rel is None:
            main_rel = rels[0]  # fall back to the scored edge (partner0 usable)
        main_partner = main_rel["profile"] or partner0

        # De-dup featured pairs across all cards.
        pair_key = frozenset((anchor["qid"], main_partner["qid"]))
        if pair_key in featured_seen:
            continue
        featured_seen.add(pair_key)

        # Main relationship date span.
        sy = _year(main_rel["start"])
        ey = _year(main_rel["end"])
        if sy and ey:
            dates: str | None = f"{sy}–{ey}"
        elif sy:
            dates = f"since {sy}"
        else:
            dates = None

        label_anchor  = _domain_label_for_person(anchor.get("primary_occ"), anchor.get("occupations"))
        label_partner = _domain_label_for_person(main_partner.get("primary_occ"), main_partner.get("occupations"))

        card: dict = {
            "id":         _card_id("rel", anchor["qid"], main_partner["qid"], main_rel["relation"]),
            "type":       "relationship",
            "score":      round(_score_relationship(edge), 4),
            "relation":   main_rel["relation"],
            "dates":      dates,
            "domains":    [label_anchor or "Unknown", label_partner or "Unknown"],
            "categories": categories_for_domains(
                anchor.get("occupations") or [],
                main_partner.get("occupations") or [],
            ),
            "countries": _countries_from_persons(
                anchor.get("nationalities"),
                main_partner.get("nationalities"),
            ),
            "a": _person_block(anchor),
            "b": _person_block(main_partner),
        }

        # Older relationships (everything except the featured one) → below list,
        # oldest→newest (undated last), capped. Attached to the anchor (a).
        below = [r for r in rels if r["qid"] != main_partner["qid"]]
        below.sort(key=lambda r: (
            _recency_key(r["start"], r["end"])
            if _recency_key(r["start"], r["end"]) >= 0 else (1 << 30)
        ))
        below_entries = [
            {
                "qid":      r["qid"],
                "name":     r["name"],
                "relation": r["relation"],
                "dates":    format_dates(r["start"], r["end"]),
            }
            for r in below[:REL_MAX_OTHER]
        ]
        if below_entries:
            card["a"]["other_relations"] = below_entries

        # Children shared by the featured couple (couple-type relations only).
        if main_rel["relation"] in COUPLE_RELATION_TYPES:
            anchor_db_id = anchor.get("_id") or f"persons/{anchor['qid']}"
            mp_db_id = main_partner.get("_id") or f"persons/{main_partner['qid']}"
            children = _fetch_shared_children(db, anchor_db_id, mp_db_id)
            if children:
                card["children"] = children
                children_added += 1

        cards.append(card)

    logger.info(
        "[relationship] Cards built: %d  (couple cards with children: %d)",
        len(cards), children_added,
    )
    return cards


# ---------------------------------------------------------------------------
# §3.2 — Dynasty card builder (surname-coherent)
# ---------------------------------------------------------------------------

JUNK_SURNAME_PREFIXES: tuple[str, ...] = (
    "of ", "de ", "von ", "van ", "del ", "della ", "degli ",
    "di ", "da ", "le ", "la ", "el ", "al ",
)


def _is_junk_surname(name: str) -> bool:
    """Return True if the surname is a particle-only or empty junk name."""
    if not name or not name.strip():
        return True
    lower = name.strip().lower()
    # Pure lowercase particle
    if lower == name.strip():
        return True
    # Starts with a known junk prefix
    for prefix in JUNK_SURNAME_PREFIXES:
        if lower.startswith(prefix):
            return True
    return False


def _modal_family_name(members: list[dict]) -> str | None:
    """Return the most common non-null family_name among members."""
    counts: collections.Counter[str] = collections.Counter(
        m["family_name"]
        for m in members
        if m.get("family_name") and m["family_name"].strip()
    )
    if not counts:
        return None
    return counts.most_common(1)[0][0]


def _fetch_family_edges(db: Any) -> list[dict]:
    """Fetch parent/child/sibling relationship edges for dynasty building."""
    family_types = ["parent", "child", "sibling", "father", "mother"]
    aql = """
    FOR a IN persons
        FILTER a.wikidata_statements_count >= @min_stmts
        FOR b, e IN 1..1 OUTBOUND a relationships
            FILTER e.type IN @types
            FILTER b.wikidata_statements_count >= @min_stmts
        RETURN {
            type:     e.type,
            from_qid: a.wikidata_id,
            to_qid:   b.wikidata_id
        }
    """
    cursor = db.aql.execute(
        aql,
        bind_vars={"types": family_types, "min_stmts": DYN_MIN_STATEMENTS},
        ttl=180,
    )
    return list(cursor)


def _fetch_persons_by_qids(db: Any, qids: list[str]) -> dict[str, dict]:
    """Fetch person detail for a list of QIDs. Returns {qid: person_dict}."""
    if not qids:
        return {}
    aql = """
    FOR qid IN @qids
        LET p = DOCUMENT(CONCAT("persons/", qid))
        FILTER p != null
        RETURN {
            qid:          p.wikidata_id,
            name:         p.name_en,
            description:  p.description_en,
            primary_occ:  p.primary_occupation,
            occupations:  p.occupations,
            birth_date:   p.birth_date,
            death_date:   p.death_date,
            stmts:        p.wikidata_statements_count,
            family_name:  p.family_name,
            nationalities: p.nationalities
        }
    """
    cursor = db.aql.execute(aql, bind_vars={"qids": qids}, ttl=120)
    return {row["qid"]: row for row in cursor}


def _score_dynasty(
    members: list[dict],
    depths: dict[str, int],
) -> float:
    """Score a dynasty component for editorial interest (§3.2).

    Components:
        notable_member_count  0.40
        max_statements        0.25  (log-scaled)
        domain_diversity      0.20  (distinct domains / len)
        generation_span       0.15  (distinct depth levels)
    """
    n = len(members)

    # Notable member count (log-scaled, cap at 12)
    count_score = 0.40 * min(1.0, math.log1p(n) / math.log1p(12))

    # Max statements
    max_stmts = max(m["stmts"] for m in members)
    stmts_score = 0.25 * min(1.0, math.log1p(max_stmts) / math.log1p(5000))

    # Domain diversity
    domains = {
        domain_of(m.get("occupations") or [])
        for m in members
        if domain_of(m.get("occupations") or [])
    }
    diversity_score = 0.20 * min(1.0, len(domains) / 4)

    # Generation span
    gen_depths = set(depths.get(m["qid"], 0) for m in members)
    span_score = 0.15 * min(1.0, len(gen_depths) / 4)

    return count_score + stmts_score + diversity_score + span_score


def _build_dynasty_cards(db: Any) -> list[dict]:
    """Return scored, surname-coherent dynasty cards up to DYN_TARGET."""
    logger.info("[dynasty] Fetching family edges…")
    raw_edges = _fetch_family_edges(db)
    logger.info("[dynasty] Raw family edges: %d", len(raw_edges))

    # Build union-find components
    uf = UnionFind()
    all_edges: list[tuple[str, str, str]] = []
    for e in raw_edges:
        fq, tq = e["from_qid"], e["to_qid"]
        if fq and tq:
            uf.union(fq, tq)
            all_edges.append((fq, tq, e["type"]))

    components = uf.components()
    logger.info("[dynasty] Raw components: %d", len(components))

    # Collect all QIDs and fetch person data
    all_qids = list({q for members in components.values() for q in members})
    logger.info("[dynasty] Fetching details for %d persons…", len(all_qids))
    persons = _fetch_persons_by_qids(db, all_qids)

    rejected_size = 0
    rejected_surname = 0
    rejected_min = 0
    candidates: list[dict] = []

    for _root, raw_members in components.items():
        # Resolve to person dicts
        known = [persons[q] for q in raw_members if q in persons]
        if len(known) < DYN_MIN_MEMBERS:
            rejected_min += 1
            continue

        comp_member_set = set(raw_members)
        comp_edges = [
            (f, t, et)
            for f, t, et in all_edges
            if f in comp_member_set and t in comp_member_set
        ]

        # §3.2 — Reject/trim by surname coherence
        modal_surname = _modal_family_name(known)

        if modal_surname is None or _is_junk_surname(modal_surname):
            rejected_surname += 1
            continue

        # Fraction of members with modal surname
        surname_members = [
            m for m in known if m.get("family_name") == modal_surname
        ]
        fraction = len(surname_members) / len(known)

        if fraction < DYN_MIN_MODAL_FRACTION:
            if len(known) > DYN_MAX_COMPONENT_SIZE:
                # Oversized blob — reject outright
                rejected_size += 1
                continue
            # Trim: keep only modal-surname members
            known = surname_members
            if len(known) < DYN_MIN_MEMBERS:
                rejected_min += 1
                continue
            # Recompute fraction after trim — should be 1.0 now
        elif len(known) > DYN_MAX_COMPONENT_SIZE:
            # Oversized but coherent: keep only modal-surname members, trim to ≤12
            known = surname_members
            if len(known) < DYN_MIN_MEMBERS:
                rejected_min += 1
                continue

        # Cap coherent families at COHERENT_TRIM_SIZE by notability
        if len(known) > DYN_COHERENT_TRIM_SIZE:
            known = sorted(known, key=lambda m: m["stmts"], reverse=True)[
                :DYN_COHERENT_TRIM_SIZE
            ]

        if len(known) < DYN_MIN_MEMBERS:
            rejected_min += 1
            continue

        # Assign generational depths
        member_qids = [m["qid"] for m in known]
        member_set = set(member_qids)
        filtered_edges = [
            (f, t, et)
            for f, t, et in comp_edges
            if f in member_set and t in member_set
        ]
        depths = _assign_depths(member_qids, filtered_edges)

        # Sort for display: top DYN_MAX_DISPLAYED by notability, then oldest→newest
        by_notability = sorted(known, key=lambda m: m["stmts"], reverse=True)
        displayed = by_notability[:DYN_MAX_DISPLAYED]
        displayed.sort(
            key=lambda m: int(_year(m.get("birth_date")) or "9999")
        )
        overflow = len(known) - len(displayed)

        # Count distinct generation depths for displayed
        gen_depths_set = {depths.get(m["qid"], 0) for m in displayed}
        gen_count = max(1, len(gen_depths_set))

        score = _score_dynasty(known, depths)

        member_dicts: list[dict] = []
        for m in displayed:
            member_desc = (
                m.get("description") or pretty_occupation(m.get("primary_occ")) or ""
            )
            member_dicts.append({
                "qid":   m["qid"],
                "name":  m["name"],
                "desc":  member_desc,
                "life":  format_lifespan(m.get("birth_date"), m.get("death_date")),
                "depth": depths.get(m["qid"], 0),
            })

        # §A5 — dynasty categories: union of members' occupations, capped to
        # the 2 most-represented categories (by member count).
        _cat_counts: dict[str, int] = {}
        for m in known:
            for cat in categories_for_domains(m.get("occupations") or []):
                _cat_counts[cat] = _cat_counts.get(cat, 0) + 1
        _dyn_cats = sorted(
            _cat_counts, key=lambda c: -_cat_counts[c]
        )[:2]
        # Preserve canonical ordering
        _dyn_cats_ordered = [c for c in CATEGORY_ORDER if c in _dyn_cats]

        # Dynasty countries: union of all members' nationalities (displayed members
        # and non-displayed alike — use the full `known` set for coverage).
        _dyn_countries = _countries_from_persons(
            *[m.get("nationalities") for m in known]
        )

        # Family field — the members' dominant editorial domain (e.g. "Film",
        # "Politics", "Music"), shown as a tag so the card reads as e.g. an
        # acting / political / banking dynasty at a glance.
        _field_counts: collections.Counter[str] = collections.Counter()
        for m in known:
            lbl = domain_label(m.get("occupations") or [])
            if lbl:
                _field_counts[lbl] += 1
        _dyn_field = _field_counts.most_common(1)[0][0] if _field_counts else None

        candidates.append({
            # Include sorted member QIDs so two distinct families sharing a
            # surname + size don't collide on the same id (would drop cards).
            "id":         _card_id(
                "dynasty", modal_surname,
                ",".join(sorted(m["qid"] for m in known if m.get("qid"))),
            ),
            "type":       "dynasty",
            "score":      round(score, 4),
            "title":      f"The {modal_surname} Family",
            "subtitle":   f"{len(known)} members · {gen_count} generation{'s' if gen_count != 1 else ''}",
            "field":      _dyn_field,
            "members":    member_dicts,
            "categories": _dyn_cats_ordered,
            "countries":  _dyn_countries,
            "_overflow":  overflow,
            "_modal_surname": modal_surname,
        })

    logger.info(
        "[dynasty] Rejected — min_size: %d, surname: %d, size: %d",
        rejected_min, rejected_surname, rejected_size,
    )
    logger.info("[dynasty] Candidate dynasties: %d", len(candidates))

    # Sort by score descending, cap at target
    candidates.sort(key=lambda c: c["score"], reverse=True)
    selected = candidates[:DYN_TARGET]

    # Add overflow hint to subtitle and clean internal keys
    cards: list[dict] = []
    for c in selected:
        overflow = c.pop("_overflow", 0)
        c.pop("_modal_surname", None)
        if overflow > 0:
            c["subtitle"] += f" · +{overflow} more"
        cards.append(c)

    logger.info("[dynasty] Cards built: %d", len(cards))
    return cards


# ---------------------------------------------------------------------------
# §3.3 — Organization card builder (relation-aware)
# ---------------------------------------------------------------------------


def _fetch_org_affiliations(
    db: Any, aff_types: str | list[str], min_stmts: int
) -> list[dict]:
    """Fetch affiliations of a specific type with qualifying persons.

    Returns rows: {org_id, person_qid, person_name, person_desc,
                   person_stmts, person_occupations}.
    Grouped by org later in the caller.
    """
    if isinstance(aff_types, str):
        aff_types = [aff_types]

    aql = """
    FOR p IN persons
        FILTER p.wikidata_statements_count >= @min_stmts
        FILTER p.name_en != null
        FOR o, e IN 1..1 OUTBOUND p affiliations
            FILTER e.type IN @aff_types
            FILTER STARTS_WITH(e._to, 'organizations/')
            FILTER o.name_en != null
        RETURN {
            affiliation_type:       e.type,
            org_id:                e._to,
            org_qid:               o.wikidata_id,
            org_name:              o.name_en,
            org_type:              o.type,
            org_founded:           o.founded,
            org_hq:                o.headquarters,
            org_country:           o.country,
            org_logo:              o.image_cached_path,
            org_stmts:             o.wikidata_statements_count,
            person_qid:            p.wikidata_id,
            person_name:           p.name_en,
            person_desc:           p.description_en,
            person_stmts:          p.wikidata_statements_count,
            person_occupations:    p.occupations,
            person_nationalities:  p.nationalities,
            role_start_date:       e.start_date,
            role_end_date:         e.end_date
        }
    """
    cursor = db.aql.execute(
        aql,
        bind_vars={"aff_types": aff_types, "min_stmts": min_stmts},
        ttl=180,
    )
    return list(cursor)


def _score_org_card(
    people: list[dict],
    org_stmts: int,
    total_affiliates: int,
    relation_key: str,
) -> float:
    """Score an org card for editorial interest (§3.3).

    Components:
        people_notability  0.40  — avg log-scaled stmts of top people
        org_notability     0.25  — log-scaled org statements
        specificity        0.20  — fewer total affiliates = higher (capped at 200)
        relation_weight    0.15  — founder/led_by > board > players > members
    """
    if not people:
        return 0.0

    # People notability
    avg_stmts = sum(p["person_stmts"] for p in people) / len(people)
    people_score = 0.40 * min(1.0, math.log1p(avg_stmts) / math.log1p(5000))

    # Org notability
    org_score = 0.25 * min(1.0, math.log1p(org_stmts) / math.log1p(10000))

    # Specificity (fewer total affiliates = more specific = better)
    specificity = 1.0 - min(1.0, total_affiliates / (ORG_MEGA_HUB_THRESHOLD * 2))
    spec_score = 0.20 * specificity

    # Relation weight
    rel_weight = ORG_RELATION_WEIGHT.get(relation_key, 0.4)
    rel_score = 0.15 * rel_weight

    return people_score + org_score + spec_score + rel_score


def _build_org_cards(db: Any) -> list[dict]:
    """Return scored, relation-aware organization cards up to ORG_TARGET.

    §A2 — founder/led_by allow min_people=1.
    §A3 — educated_at → alumni cards for universities (min 4 alumni).
    §A4 — chairperson/board_member/director_of/officer_of/chief_executive/owner
           merged into ONE "Leadership" card per org with per-person role labels.
    §A5 — every card tagged with categories.
    """
    org_card_candidates: dict[str, list[dict]] = collections.defaultdict(list)

    # -----------------------------------------------------------------------
    # Pass 1: AFFILIATION_MAP types (founder, led_by, players, members, alumni)
    # -----------------------------------------------------------------------
    for aff_type, (headline, relation_key, use_thin, min_people) in AFFILIATION_MAP.items():
        min_stmts = ORG_MIN_STATEMENTS_THIN if use_thin else ORG_MIN_STATEMENTS_DEFAULT
        logger.info(
            "[org] Fetching affiliations for type=%s (min_stmts=%d)…",
            aff_type, min_stmts,
        )
        rows = _fetch_org_affiliations(db, aff_type, min_stmts)
        logger.info("[org] Rows for %s: %d", aff_type, len(rows))

        if not rows:
            continue

        # Group by org
        by_org: dict[str, list[dict]] = collections.defaultdict(list)
        org_meta: dict[str, dict] = {}
        for row in rows:
            oid = row["org_id"]
            by_org[oid].append(row)
            if oid not in org_meta:
                org_meta[oid] = {
                    "org_qid":     row["org_qid"],
                    "org_name":    row["org_name"],
                    "org_type":    row["org_type"],
                    "org_founded": row["org_founded"],
                    "org_hq":      row["org_hq"],
                    "org_country": row["org_country"],
                    "org_logo":    row["org_logo"],
                    "org_stmts":   row["org_stmts"] or 0,
                }

        for oid, people_rows in by_org.items():
            # §A3 — alumni restricted to university-type orgs
            if relation_key == "alumni":
                org_type = (org_meta[oid]["org_type"] or "").lower()
                if org_type not in ALUMNI_ORG_TYPES:
                    continue

            # Dedup by person QID
            seen_pqids: set[str] = set()
            deduped_rows: list[dict] = []
            for pr in people_rows:
                pqid = pr["person_qid"]
                if pqid and pqid not in seen_pqids:
                    seen_pqids.add(pqid)
                    deduped_rows.append(pr)

            total_affiliates = len(deduped_rows)

            # §A2 — per-relation min_people check
            if total_affiliates < min_people:
                continue

            # Mega-hub cap: skip only for non-founder/non-led/non-alumni relations
            if relation_key not in ("founded_by", "led_by", "alumni") and \
                    total_affiliates > ORG_MEGA_HUB_THRESHOLD:
                continue

            # Rank by notability, take top ORG_MAX_PEOPLE
            people_sorted = sorted(
                deduped_rows,
                key=lambda r: r["person_stmts"],
                reverse=True,
            )[:ORG_MAX_PEOPLE]

            meta = org_meta[oid]
            score = _score_org_card(
                people_sorted,
                meta["org_stmts"],
                total_affiliates,
                relation_key,
            )

            founded_year = _year(meta["org_founded"])

            # §A5 — categories
            org_type_str = meta["org_type"] or ""
            people_occs = [r.get("person_occupations") or [] for r in people_sorted]
            card_cats = categories_for_org(org_type_str, people_occs)

            # Founder people get role label
            role_for_relation = "Founder" if relation_key == "founded_by" else None

            # Countries: union of shown people's nationalities
            org_card_countries = _countries_from_persons(
                *[p.get("person_nationalities") for p in people_sorted]
            )

            card: dict = {
                "id":         _card_id("org", oid, relation_key),
                "type":       "organization",
                "score":      round(score, 4),
                "org": {
                    "qid":      meta["org_qid"],
                    "name":     meta["org_name"],
                    "label":    _prettify_org_type(meta["org_type"]),
                    "founded":  founded_year,
                    "hq":       meta["org_hq"] or None,
                    "country":  _modal_country(*[p.get("person_nationalities") for p in people_sorted]),
                    "has_logo": meta["org_logo"] is not None,
                },
                "relation":   relation_key,
                "headline":   headline,
                "categories": card_cats,
                "countries":  org_card_countries,
                "people": [
                    {
                        "qid":  p["person_qid"],
                        "name": p["person_name"],
                        "desc": p["person_desc"] or "",
                        **({"role": role_for_relation} if role_for_relation else {}),
                    }
                    for p in people_sorted
                ],
            }
            org_card_candidates[oid].append(card)

    # -----------------------------------------------------------------------
    # Pass 2: Organization card — the most-notable people connected to an org
    # -----------------------------------------------------------------------
    # Not just leadership: include notable employees / shareholders / insiders.
    # People are ranked by notability (statement count); a role label is shown
    # when the affiliation type maps to one, otherwise the person has no label.
    logger.info("[org] Building organization (connected-people) cards…")
    org_people_by_org: dict[str, list[dict]] = collections.defaultdict(list)
    org_people_meta: dict[str, dict] = {}

    connected_rows = _fetch_org_affiliations(
        db, list(ORG_ROLE_LABELS), ORG_PEOPLE_MIN_STATEMENTS
    )
    connected_by_type: dict[str, list[dict]] = collections.defaultdict(list)
    for row in connected_rows:
        connected_by_type[row["affiliation_type"]].append(row)
    logger.info("[org] Connected rows across role types: %d", len(connected_rows))

    for aff_type in ORG_ROLE_LABELS:
        # Use a low floor and let the per-card notability ranking + cap pick the
        # best people, so sparse orgs (e.g. Palantir, where only one affiliate
        # clears 120 stmts) still surface 3-5 of their actual connected people
        # instead of a lone portrait.
        rows = connected_by_type.get(aff_type, [])
        logger.info("[org] Connected rows for type=%s: %d", aff_type, len(rows))
        for row in rows:
            oid = row["org_id"]
            row["_role"] = feed_role_label(aff_type, row.get("role_end_date"))
            org_people_by_org[oid].append(row)
            if oid not in org_people_meta:
                org_people_meta[oid] = {
                    "org_qid":     row["org_qid"],
                    "org_name":    row["org_name"],
                    "org_type":    row["org_type"],
                    "org_founded": row["org_founded"],
                    "org_hq":      row["org_hq"],
                    "org_country": row["org_country"],
                    "org_logo":    row["org_logo"],
                    "org_stmts":   row["org_stmts"] or 0,
                }

    _role_priority_map = {r: i for i, r in enumerate(ORG_ROLE_PRIORITY)}

    for oid, people_rows in org_people_by_org.items():
        # Dedup by QID, keeping the highest-priority *labelled* role per person
        # (a labelled role beats an unlabelled affiliation like employee).
        seen_pqids: dict[str, dict] = {}
        for pr in people_rows:
            pqid = pr["person_qid"]
            if not pqid:
                continue
            if pqid not in seen_pqids:
                seen_pqids[pqid] = pr
            else:
                existing_priority = _role_priority_map.get(
                    seen_pqids[pqid].get("_role") or "", 999
                )
                new_priority = _role_priority_map.get(pr.get("_role") or "", 999)
                if new_priority < existing_priority:
                    seen_pqids[pqid] = pr

        deduped_rows = list(seen_pqids.values())
        total_affiliates = len(deduped_rows)

        if total_affiliates < 1:
            continue
        # Skip mega-hub orgs (huge employee lists aren't a coherent card)
        if total_affiliates > ORG_MEGA_HUB_THRESHOLD:
            continue

        # Rank by notability (high statement count first); cap at ORG_PEOPLE_CAP
        people_sorted = sorted(
            deduped_rows, key=lambda r: -(r.get("person_stmts") or 0)
        )[:ORG_PEOPLE_CAP]

        meta = org_people_meta[oid]
        score = _score_org_card(
            people_sorted, meta["org_stmts"], total_affiliates, "organization",
        )

        founded_year = _year(meta["org_founded"])
        org_type_str = meta["org_type"] or ""
        people_occs = [r.get("person_occupations") or [] for r in people_sorted]
        card_cats = categories_for_org(org_type_str, people_occs)

        people_out: list[dict] = []
        for p in people_sorted:
            entry = {
                "qid":  p["person_qid"],
                "name": p["person_name"],
                "desc": p["person_desc"] or "",
            }
            if p.get("_role"):          # only attach a label when we have one
                entry["role"] = p["_role"]
            people_out.append(entry)

        # Countries: union of shown people's nationalities
        org2_countries = _countries_from_persons(
            *[p.get("person_nationalities") for p in people_sorted]
        )

        card: dict = {
            "id":         _card_id("org", oid, "organization"),
            "type":       "organization",
            "score":      round(score, 4),
            "org": {
                "qid":      meta["org_qid"],
                "name":     meta["org_name"],
                "label":    _prettify_org_type(meta["org_type"]),
                "founded":  founded_year,
                "hq":       meta["org_hq"] or None,
                "country":  _modal_country(*[p.get("person_nationalities") for p in people_sorted]),
                "has_logo": meta["org_logo"] is not None,
            },
            "relation":   "organization",
            "headline":   "Organization",
            "categories": card_cats,
            "countries":  org2_countries,
            "people":     people_out,
        }
        org_card_candidates[oid].append(card)

    # -----------------------------------------------------------------------
    # Flatten, cap per org, sort, truncate
    # -----------------------------------------------------------------------
    all_candidates: list[dict] = []
    for oid, cards in org_card_candidates.items():
        cards_sorted = sorted(cards, key=lambda c: c["score"], reverse=True)
        all_candidates.extend(cards_sorted[:ORG_MAX_CARDS_PER_ORG])

    all_candidates.sort(key=lambda c: c["score"], reverse=True)
    selected = all_candidates[:ORG_TARGET]

    # Relation-diversify the org stream so founders don't dominate consecutive
    # slots ("not only founder cards"). Round-robin across relation groups,
    # each kept in score order; small groups (alumni/leadership/players/members)
    # are interleaved with the large founder group so early org slots vary.
    by_relation: "collections.OrderedDict[str, list[dict]]" = collections.OrderedDict()
    for c in selected:
        by_relation.setdefault(c["relation"], []).append(c)
    groups = list(by_relation.values())
    idxs = [0] * len(groups)
    diversified: list[dict] = []
    while True:
        progressed = False
        for gi, g in enumerate(groups):
            if idxs[gi] < len(g):
                diversified.append(g[idxs[gi]])
                idxs[gi] += 1
                progressed = True
        if not progressed:
            break

    rel_counts = {rel: len(g) for rel, g in by_relation.items()}
    logger.info("[org] Cards built: %d  by_relation=%s", len(diversified), rel_counts)
    return diversified


# ---------------------------------------------------------------------------
# Sport "Notable players" cards — top sports orgs by affiliate count
# ---------------------------------------------------------------------------


def _build_sport_cards(db: Any) -> list[dict]:
    """Return "Notable players" cards for the top SPORT_TOP_ORGS sports orgs.

    Mirrors the org-leaderboard ranking (organizations ordered by affiliate
    count within the sport subcategory), then for each org takes its most
    notable players (highest wikidata_statements_count). Guarantees sport
    coverage in the feed regardless of the global org score cap.
    """
    logger.info("[sport] Fetching top %d sports orgs + players…", SPORT_TOP_ORGS)
    aql = """
    LET sport_orgs = (
        FOR e IN affiliations
            FILTER e.type == 'sports_team_member'
            FILTER STARTS_WITH(e._to, 'organizations/')
            COLLECT org_id = e._to WITH COUNT INTO mc
            LET org = DOCUMENT(org_id)
            FILTER org != null AND org.type IN @sport_types
            SORT mc DESC
            LIMIT @top_n
            RETURN org_id
    )
    FOR org_id IN sport_orgs
        LET org = DOCUMENT(org_id)
        LET players_raw = (
            FOR a IN affiliations
                FILTER a._to == org_id AND a.type == 'sports_team_member'
                LET p = DOCUMENT(a._from)
                FILTER p != null AND p.name_en != null
                   AND p.name_en NOT IN @junk_names
                   AND p.wikidata_statements_count >= @floor
                COLLECT pq = p.wikidata_id INTO g = {
                    name: p.name_en,
                    bio: p.description_en,
                    stmts: p.wikidata_statements_count,
                    occupations: p.occupations,
                    nationalities: p.nationalities
                }
                RETURN {
                    qid: pq, name: g[0].name, bio: g[0].bio,
                    stmts: g[0].stmts, occupations: g[0].occupations,
                    nationalities: g[0].nationalities
                }
        )
        LET players = (FOR pl IN players_raw SORT pl.stmts DESC LIMIT 5 RETURN pl)
        FILTER LENGTH(players) >= @min_players
        RETURN {
            org: {
                qid: org.wikidata_id, name: org.name_en, type: org.type,
                founded: org.founded, hq: org.headquarters, country: org.country,
                logo: org.image_cached_path, stmts: org.wikidata_statements_count
            },
            players: players
        }
    """
    rows = list(db.aql.execute(
        aql,
        bind_vars={
            "sport_types": SPORT_ORG_TYPES,
            "top_n": SPORT_TOP_ORGS,
            "floor": SPORT_PLAYER_MIN_STATEMENTS,
            "min_players": SPORT_MIN_PLAYERS,
            "junk_names": ["not stated", "unknown value", "no value", "anonymous"],
        },
        ttl=180,
    ))

    cards: list[dict] = []
    for row in rows:
        org = row["org"]
        players = row["players"]
        people = [
            {"qid": p["qid"], "name": p["name"], "desc": p.get("bio") or ""}
            for p in players
        ]
        people_occs = [p.get("occupations") or [] for p in players]
        card_cats = categories_for_org(org.get("type") or "", people_occs)
        if "sport" not in card_cats:
            card_cats = ["sport", *card_cats]

        score = _score_org_card(
            [{"person_stmts": p["stmts"]} for p in players],
            org.get("stmts") or 0,
            len(players),
            "players",
        )

        sport_countries = _countries_from_persons(
            *[p.get("nationalities") for p in players]
        )

        cards.append({
            "id":         _card_id("org", org["qid"] or org["name"], "players"),
            "type":       "organization",
            "score":      round(score, 4),
            "org": {
                "qid":      org["qid"],
                "name":     org["name"],
                "label":    _prettify_org_type(org.get("type")),
                "founded":  _year(org.get("founded")),
                "hq":       org.get("hq") or None,
                "country":  _modal_country(*[p.get("nationalities") for p in players]),
                "has_logo": org.get("logo") is not None,
            },
            "relation":   "players",
            "headline":   "Notable players",
            "categories": card_cats,
            "countries":  sport_countries,
            "people":     people,
        })

    cards.sort(key=lambda c: c["score"], reverse=True)
    logger.info("[sport] Cards built: %d", len(cards))
    return cards


# ---------------------------------------------------------------------------
# §3.4 — Interleave and write to Redis
# ---------------------------------------------------------------------------


def _interleave_cards(
    rel_cards: list[dict],
    dyn_cards: list[dict],
    org_cards: list[dict],
) -> list[dict]:
    """Even-spread merge of the three card archetypes.

    Each type is distributed *evenly across the full output* (by fractional
    position) rather than front-loaded in a fixed block.  Dynasties — the
    smallest pool — therefore stay present throughout the feed instead of
    clustering at the front and vanishing, giving all three archetypes
    balanced presence by default.  Cards keep their within-type order
    (relevance for relationship/dynasty; relation-diversified for org).

    Note: proportions still reflect pool sizes (org > relationship > dynasty),
    so this balances *presence*, not raw counts.  Growing the dynasty pool is
    the lever for truly equal counts.
    """
    interleaved: list[tuple[float, int, dict]] = []
    # tie-breaker rank keeps the merge stable and round-robins types that land
    # on the same fractional position (org=0, rel=1, dyn=2 → org slightly first)
    for type_rank, stream in enumerate((org_cards, rel_cards, dyn_cards)):
        n = len(stream)
        if n == 0:
            continue
        for i, card in enumerate(stream):
            pos = (i + 0.5) / n
            interleaved.append((pos, type_rank, card))

    interleaved.sort(key=lambda t: (t[0], t[1]))
    return [card for _pos, _rank, card in interleaved]


def _write_to_redis(
    cards: list[dict],
    by_type: dict[str, int],
) -> None:
    """Write cards to Redis v2 layout without expiry.

    Layout:
      feed:cardmap:v2            HASH  id → compact JSON body
      feed:order:all:v2          JSON array string of all IDs
      feed:order:cat:<cat>:v2    JSON array string of IDs per category
      feed:order:nat:<CC>:v2     JSON array string of IDs per nationality code
      feed:meta:v2               JSON object with stats + countries list
    """
    r = get_redis()
    generated_at = datetime.now(UTC).isoformat()

    # Build id-ordered list and per-category id lists
    all_ids: list[str] = [c["id"] for c in cards]

    # Category buckets: {category: [ids in score order]}
    cat_ids: dict[str, list[str]] = {cat: [] for cat in CATEGORY_ORDER}
    for card in cards:
        for cat in card.get("categories") or []:
            if cat in cat_ids:
                cat_ids[cat].append(card["id"])

    # By-category counts for meta
    by_category = {cat: len(ids) for cat, ids in cat_ids.items()}

    # Type buckets: {card type: [ids in ranked order]} — drives the "Tailor
    # your feed" archetype filter (relationship / dynasty / organization).
    type_ids: dict[str, list[str]] = {}
    for card in cards:
        tp = card.get("type")
        if tp:
            type_ids.setdefault(tp, []).append(card["id"])

    # Nationality buckets: only for SUPPORTED_NATIONALITIES codes.
    # Preserves the same ranked order as 'all' (interleaved by type, scored
    # within type) — filtering to IDs that include each country code.
    nat_ids: dict[str, list[str]] = {cc: [] for cc in SUPPORTED_NATIONALITIES}
    for card in cards:
        for cc in card.get("countries") or []:
            if cc in nat_ids:
                nat_ids[cc].append(card["id"])

    # Compute which supported codes actually have ≥1 card (for meta)
    populated_countries = sorted(cc for cc, ids in nat_ids.items() if ids)

    # Log by-country summary
    by_country = {cc: len(ids) for cc, ids in nat_ids.items() if ids}
    logger.info("By-country card counts: %s", dict(sorted(by_country.items())))
    logger.info(
        "Populated nationality codes (%d): %s",
        len(populated_countries), populated_countries,
    )

    # Meta object
    meta = {
        "count":           len(cards),
        "generated_at":    generated_at,
        "content_version": FEED_CONTENT_VERSION,
        "by_type":      by_type,
        "by_category":  by_category,
        "categories":   CATEGORY_ORDER,
        "countries":    populated_countries,
    }

    pipe = r.pipeline()

    # Drop the previous cardmap first — HSET only adds/updates fields, so
    # without this, card bodies from prior generations accumulate forever
    # (stale ids never referenced by the new order arrays). Delete → rewrite
    # keeps the hash bounded to exactly the current pool.
    pipe.delete(FEED_CARDMAP_KEY)

    # HASH: id → compact JSON
    # Split into chunks to avoid huge pipeline payloads
    chunk_size = 500
    for i in range(0, len(cards), chunk_size):
        chunk = cards[i : i + chunk_size]
        mapping = {
            c["id"]: json.dumps(c, ensure_ascii=False, separators=(",", ":"))
            for c in chunk
        }
        pipe.hset(FEED_CARDMAP_KEY, mapping=mapping)

    # Order arrays
    pipe.set(
        FEED_ORDER_ALL_KEY,
        json.dumps(all_ids, ensure_ascii=False, separators=(",", ":")),
    )
    for cat, ids in cat_ids.items():
        pipe.set(
            f"{FEED_ORDER_CAT_PREFIX}{cat}:v2",
            json.dumps(ids, ensure_ascii=False, separators=(",", ":")),
        )

    # Nationality order arrays (only write populated codes)
    for cc, ids in nat_ids.items():
        if ids:
            pipe.set(
                f"{FEED_ORDER_NAT_PREFIX}{cc}:v2",
                json.dumps(ids, ensure_ascii=False, separators=(",", ":")),
            )

    # Per-type order arrays (card archetype filter)
    for tp, ids in type_ids.items():
        pipe.set(
            f"{FEED_ORDER_TYPE_PREFIX}{tp}:v2",
            json.dumps(ids, ensure_ascii=False, separators=(",", ":")),
        )

    # Meta
    pipe.set(FEED_META_KEY, json.dumps(meta, ensure_ascii=False))

    pipe.execute()

    total_bytes = sum(
        len(json.dumps(c, ensure_ascii=False, separators=(",", ":")).encode())
        for c in cards
    )
    logger.info(
        "Redis v2: wrote %d card bodies to %s, order keys + meta (total body bytes: %d)",
        len(cards), FEED_CARDMAP_KEY, total_bytes,
    )
    logger.info("By category: %s", by_category)


def _write_to_file(cards: list[dict], path: str) -> None:
    """Write cards JSON to a local file (--dry-run mode)."""
    payload = json.dumps(cards, ensure_ascii=False, indent=2)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(payload)
    logger.info("Dry-run: wrote %d cards to %s (%d bytes)", len(cards), path, len(payload))


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run(dry_run_path: str | None = None) -> None:
    """Full pipeline: read Arango → score → interleave → write Redis v2/file."""
    logger.info("=== generate_feed v2 ===")

    # Global feed queries scan millions of candidate edges and can legitimately
    # exceed the interactive API client's 60-second request bound. This is an
    # offline maintenance command, so give ArangoDB the same bounded window as
    # the candidate materializer and validator.
    db = get_db(get_arango_client(request_timeout=1_800))

    rel_cards = _build_relationship_cards(db)
    dyn_cards = _build_dynasty_cards(db)
    org_cards = _build_org_cards(db)
    # Guaranteed sport coverage from the top sports orgs (appended so the global
    # org score cap can't crowd them out). Dedup ids in case an org also
    # produced a leadership card with the same id space (different relation key).
    sport_cards = _build_sport_cards(db)
    existing_ids = {c["id"] for c in org_cards}
    org_cards = org_cards + [c for c in sport_cards if c["id"] not in existing_ids]

    combined = _interleave_cards(rel_cards, dyn_cards, org_cards)

    by_type = {
        "relationship": len(rel_cards),
        "dynasty":      len(dyn_cards),
        "organization": len(org_cards),
    }

    total = len(combined)
    payload_bytes = sum(
        len(json.dumps(c, ensure_ascii=False, separators=(",", ":")).encode())
        for c in combined
    )

    # Category summary
    by_category: dict[str, int] = {}
    cards_with_cats = 0
    for c in combined:
        cats = c.get("categories") or []
        if cats:
            cards_with_cats += 1
        for cat in cats:
            by_category[cat] = by_category.get(cat, 0) + 1

    logger.info(
        "Summary: total=%d  relationship=%d  dynasty=%d  organization=%d  bytes=%d",
        total, len(rel_cards), len(dyn_cards), len(org_cards), payload_bytes,
    )
    logger.info("By category: %s", by_category)
    logger.info("Cards with categories: %d / %d", cards_with_cats, total)

    # Nationality summary (for dry-run QA and normal runs alike)
    by_country: dict[str, int] = {}
    cards_with_countries = 0
    for c in combined:
        countries = c.get("countries") or []
        if countries:
            cards_with_countries += 1
        for cc in countries:
            by_country[cc] = by_country.get(cc, 0) + 1
    logger.info("By-country Summary: %s", dict(sorted(by_country.items())))
    logger.info("Cards with countries: %d / %d", cards_with_countries, total)

    # Print a few samples of each type for QA
    _print_samples(rel_cards, dyn_cards, org_cards)

    if dry_run_path:
        _write_to_file(combined, dry_run_path)
    else:
        _write_to_redis(combined, by_type)

    logger.info("Done.")


def _print_samples(
    rel_cards: list[dict],
    dyn_cards: list[dict],
    org_cards: list[dict],
) -> None:
    """Print a handful of cards of each type for spot-checking."""
    print("\n=== RELATIONSHIP samples (top 5) ===")
    for c in rel_cards[:5]:
        print(
            f"  [{c['relation']}] {c['a']['name']} ({c['domains'][0]})"
            f"  ↔  {c['b']['name']} ({c['domains'][1]})"
            f"  score={c['score']}"
        )
        if c.get("dates"):
            print(f"    dates: {c['dates']}")
        print(f"    A: {c['a']['desc'][:80]}")
        print(f"    B: {c['b']['desc'][:80]}")
        if c.get("children"):
            child_names = [ch["name"] for ch in c["children"]]
            print(f"    children: {', '.join(child_names)}")

    print("\n=== RELATIONSHIP cards with children (up to 8 samples) ===")
    couple_cards_with_children = [
        c for c in rel_cards
        if c.get("children") and c["relation"] in ("married", "together")
    ]
    for c in couple_cards_with_children[:8]:
        child_names = [ch["name"] for ch in c["children"]]
        print(
            f"  [{c['relation']}] {c['a']['name']} ↔ {c['b']['name']}"
            f"  → {', '.join(child_names)}"
        )

    print("\n=== DYNASTY samples (top 5) ===")
    for c in dyn_cards[:5]:
        names = [m["name"] for m in c["members"]]
        print(
            f"  {c['title']}  —  {c['subtitle']}  score={c['score']}"
        )
        print(f"    Members: {', '.join(names)}")

    print("\n=== ORGANIZATION samples (top 5) ===")
    for c in org_cards[:5]:
        org = c["org"]
        people_names = [p["name"] for p in c["people"]]
        hq = f" · {org['hq']}" if org.get("hq") else ""
        founded = f" · founded {org['founded']}" if org.get("founded") else ""
        print(
            f"  [{c['headline']}] {org['name']} ({org['label']}{founded}{hq})"
            f"  score={c['score']}  cats={c.get('categories','')}"
        )
        print(f"    People: {', '.join(people_names)}")

    print("\n=== ORGANIZATION samples (up to 8) ===")
    organization_cards = [c for c in org_cards if c.get("relation") == "organization"]
    print(f"  Total organization cards: {len(organization_cards)}")
    for c in organization_cards[:8]:
        org = c["org"]
        people_info = [f"{p['name']} ({p.get('role','—')})" for p in c["people"]]
        print(f"  {org['name']}  score={c['score']}")
        print(f"    {', '.join(people_info)}")

    print("\n=== ALUMNI samples (up to 5) ===")
    alumni_cards = [c for c in org_cards if c.get("relation") == "alumni"]
    for c in alumni_cards[:5]:
        org = c["org"]
        people_names = [p["name"] for p in c["people"]]
        print(f"  {org['name']}  score={c['score']}  cats={c.get('categories','')}")
        print(f"    Alumni: {', '.join(people_names)}")

    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate HumansMap feed cards.")
    parser.add_argument(
        "--dry-run",
        metavar="PATH",
        help="Write JSON output to PATH instead of Redis.",
    )
    args = parser.parse_args()
    run(dry_run_path=args.dry_run)
