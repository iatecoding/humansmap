"""Leaderboard endpoints: persons ranked by wikidata notability, orgs by member count."""

import os
from urllib.parse import quote

from fastapi import APIRouter, Query, Request

from app.config import settings
from app.database import (APP_METADATA, AFFILIATIONS, ORGANIZATIONS, PERSONS,
                          get_arango_client, get_db)
from app.rate_limit import limiter
from app.services.cache import get_cached, set_cached
from app.routers.trivia_common import TRIVIA_CATEGORIES

router = APIRouter(prefix="/api/leaderboard", tags=["leaderboard"])

EXCLUDED_AFF_TYPES = ["insider_of", "holds_shares_of"]
LEADERBOARD_CACHE_VERSION = "v4"
LEADERBOARD_PAGE_SIZE = 500
LEADERBOARD_CACHE_TTL = 604800

# Org type groupings — position excluded from ALL by default
ORG_CATEGORIES: dict[str, list[str]] = {
    "politics":     ["political_party", "government_agency", "international_organization"],
    "media":        ["newspaper", "magazine", "television_station", "broadcaster", "radio_station", "news_agency", "publisher"],
    "business":     ["company", "bank"],
    "culture":      ["museum", "art_gallery", "foundation", "nonprofit", "organization"],
    "sport":        ["sports_club", "sports_facility", "stadium"],
    "education":    ["university", "secondary_school", "primary_school", "educational_institution"],
    "positions":    ["position"],
    "occupations":  [],
}

# All types included when no category is selected (position excluded)
_ALL_EXCEPT_POSITION = [t for types in ORG_CATEGORIES.values() for t in types if t != "position"]


def _get_db():
    client = get_arango_client()
    return get_db(client)


def _page_cache_key(kind: str, dimensions: list[str], offset: int) -> str:
    """Build a canonical key shared by every requested limit at an offset."""
    encoded = [quote(str(value), safe="") for value in dimensions]
    return ":".join([
        "leaderboard", LEADERBOARD_CACHE_VERSION, kind, *encoded, str(offset)
    ])


def _page_response(page: dict, *, limit: int, offset: int, metadata: dict) -> dict:
    all_items = page.get("items", [])
    return {
        "items": all_items[:limit],
        **metadata,
        "limit": limit,
        "offset": offset,
        "has_more": bool(page.get("has_more") or len(all_items) > limit),
    }


def _org_counts_ready(db) -> bool:
    """Use materialized counts only after the refresh completion marker exists."""
    try:
        marker = db.collection(APP_METADATA).get("leaderboard_org_counts")
        return bool(
            marker and marker.get("status") == "ready" and marker.get("version") == 1
        )
    except Exception:
        return False


PERSON_SORT_OPTIONS = {"degree", "birth_date"}
PERSON_SORT_DEFAULT = "degree"

# Person sort clauses paired with the extra filter each needs to stay
# index-backed.  Only sorts the query planner can serve from an index are
# exposed:
#   * degree     -> persistent index on wikidata_statements_count
#   * birth_date -> sparse index on birth_date, which the planner only uses for
#                   SORT when the query also filters out nulls, so it carries
#                   `FILTER p.birth_date != null`.
# Sorting on nationalities[0] cannot use the nationalities[*] array index and
# full-scans the ~8M-row persons collection, so it is deliberately not offered.
_PERSON_SORT_SQL = {
    "degree": ("", "SORT p.wikidata_statements_count DESC"),
    "birth_date": ("FILTER p.birth_date != null", "SORT p.birth_date DESC"),
}


def _person_sort_sql(sort_key: str) -> tuple[str, str]:
    """Return the (extra filter, sort clause) pair for an exposed person sort."""
    return _PERSON_SORT_SQL.get(sort_key, _PERSON_SORT_SQL[PERSON_SORT_DEFAULT])


@router.get("/degree")
@limiter.limit("60/minute")
def degree_leaderboard(
    request: Request,
    nationality: str | None = None,
    status: str | None = Query("living", pattern="^(living|deceased|all)$"),
    category: str | None = None,
    sort_by: str | None = Query("degree", pattern="^(degree|birth_date)$"),
    limit: int = Query(60, ge=1, le=500),
    offset: int = Query(0, ge=0, le=5000),
):
    """Top persons ranked by wikidata_statements_count (notability proxy).

    `status`: 'living' (default), 'deceased', or 'all'.
    `category`: grouped key from TRIVIA_CATEGORIES or individual occupation string.
    `sort_by`: 'degree' (default) or 'birth_date' (only persons with a known
        birth date). A nationality sort is deliberately not offered: it cannot
        use the nationalities[*] index and full-scans the collection.
    `offset`: skip this many results (for deep pagination beyond the 500-item buffer).
    `has_more` is derived from one extra row beyond the canonical cache page.
    Canonical 500-row pages are cached in Redis for 7 days and sliced per request.
    """
    nat_key = nationality or "all"
    status_key = status or "all"
    cat_key = category if category else "all"
    sort_key = sort_by or PERSON_SORT_DEFAULT
    cache_key = _page_cache_key(
        "persons", [nat_key, status_key, cat_key, sort_key], offset
    )
    cached = get_cached(cache_key)
    if cached is not None:
        return _page_response(
            cached, limit=limit, offset=offset,
            metadata={"nationality": nationality, "status": status,
                      "category": category, "sort_by": sort_key},
        )

    db = _get_db()

    if category and category in TRIVIA_CATEGORIES:
        cat_filter = "FILTER p.primary_occupation IN @cat_occs"
    elif category:
        cat_filter = "FILTER p.primary_occupation == @cat_occ"
    else:
        cat_filter = ""

    living_filter = ""
    if status == "living":
        living_filter = "FILTER p.death_date == null"
    elif status == "deceased":
        living_filter = "FILTER p.death_date != null"

    nat_filter = "FILTER @nat IN p.nationalities" if nationality else ""

    sort_filter, sort_clause = _person_sort_sql(sort_key)

    aql = f"""
        FOR p IN {PERSONS}
            FILTER p.wikidata_statements_count != null
            {nat_filter}
            {living_filter}
            {cat_filter}
            {sort_filter}
            {sort_clause}
            LIMIT @offset, @fetch_limit
            RETURN {{
                qid: p._key,
                name_en: p.name_en,
                name_it: p.name_it,
                degree: p.wikidata_statements_count,
                occupations: p.occupations,
                nationalities: p.nationalities,
                birth_date: p.birth_date,
                has_image: p.image_cached_path != null
            }}
    """
    bind_vars: dict = {"fetch_limit": LEADERBOARD_PAGE_SIZE + 1, "offset": offset}
    if nationality:
        bind_vars["nat"] = nationality
    if category and category in TRIVIA_CATEGORIES:
        bind_vars["cat_occs"] = TRIVIA_CATEGORIES[category]
    elif category:
        bind_vars["cat_occ"] = category
    cursor = db.aql.execute(aql, bind_vars=bind_vars)
    items = list(cursor)
    page = {
        "items": items[:LEADERBOARD_PAGE_SIZE],
        "has_more": len(items) > LEADERBOARD_PAGE_SIZE,
    }
    set_cached(cache_key, page, LEADERBOARD_CACHE_TTL)
    return _page_response(
        page, limit=limit, offset=offset,
        metadata={"nationality": nationality, "status": status,
                  "category": category, "sort_by": sort_key},
    )


ORG_SORT_OPTIONS = {"members", "founded"}
ORG_SORT_DEFAULT = "members"


def _org_sort_key(category: str | None, sort_by: str | None) -> str:
    """Occupation lists are aggregates with a fixed ranking, so ignore sort."""
    if category == "occupations":
        return ORG_SORT_DEFAULT
    return sort_by or ORG_SORT_DEFAULT


@router.get("/orgs")
@limiter.limit("60/minute")
def orgs_leaderboard(
    request: Request,
    category: str | None = None,
    sort_by: str | None = Query("members", pattern="^(members|founded)$"),
    limit: int = Query(60, ge=1, le=500),
    offset: int = Query(0, ge=0, le=5000),
):
    """Top organizations by number of affiliated persons.

    Optional category filter maps to a set of org types.
    When category is 'occupations', returns occupations ranked by person count
    (the sort_by value is ignored so there is a single cache entry per page).
    'position' is excluded from the default (no category) view.
    insider_of / holds_shares_of edges are excluded from counts.
    `sort_by`: 'members' (default) or 'founded'.
    `offset`: skip this many results (for deep pagination beyond the 500-item buffer).
    `has_more` is derived from one extra row beyond the canonical cache page.
    Canonical 500-row pages are cached in Redis for 7 days and sliced per request.
    """
    cat_key = category or "all"
    sort_key = _org_sort_key(category, sort_by)
    cache_key = _page_cache_key("orgs", [cat_key, sort_key], offset)
    cached = get_cached(cache_key)
    if cached is not None:
        return _page_response(
            cached, limit=limit, offset=offset,
            metadata={"category": category, "sort_by": sort_key},
        )

    db = _get_db()

    if category == "occupations":
        aql = f"""
            FOR p IN {PERSONS}
                FILTER p.primary_occupation != null
                COLLECT occupation = p.primary_occupation WITH COUNT INTO cnt
                SORT cnt DESC
                LIMIT @offset, @fetch_limit
                RETURN {{
                    qid: occupation,
                    name_en: occupation,
                    name_it: occupation,
                    type: "occupation",
                    founded: null,
                    has_image: occupation IN @occ_with_images,
                    member_count: cnt
                }}
        """
        try:
            cache_files = os.listdir(settings.image_cache_dir)
        except FileNotFoundError:
            # An empty or not-yet-mounted cache is a normal cold-start state.
            cache_files = []
        occ_with_images = [
            f[:-4] for f in cache_files
            if not f.startswith("Q") and f.endswith(".jpg")
        ]
        cursor = db.aql.execute(
            aql, bind_vars={"fetch_limit": LEADERBOARD_PAGE_SIZE + 1,
                            "offset": offset, "occ_with_images": occ_with_images}
        )
    else:
        if category and category in ORG_CATEGORIES:
            type_filter = "FILTER org.type IN @types"
            types = ORG_CATEGORIES[category]
        else:
            type_filter = "FILTER org.type IN @types"
            types = _ALL_EXCEPT_POSITION

        org_sort_clause = {
            "members": "SORT member_count DESC",
            "founded": "SORT org.founded DESC, member_count DESC",
        }.get(sort_key, "SORT member_count DESC")

        counts_ready = _org_counts_ready(db)
        if counts_ready:
            aql = f"""
                FOR org IN {ORGANIZATIONS}
                    FILTER org.affiliation_count > 0
                    {type_filter}
                    LET member_count = org.affiliation_count
                    {org_sort_clause}
                    LIMIT @offset, @fetch_limit
                    RETURN {{
                        qid: org._key,
                        name_en: org.name_en,
                        name_it: org.name_it,
                        type: org.type,
                        founded: org.founded,
                        has_image: org.image_cached_path != null,
                        member_count: member_count
                    }}
            """
        else:
            # Correct fallback until the maintenance refresh completes.
            aql = f"""
            FOR e IN {AFFILIATIONS}
                FILTER STARTS_WITH(e._to, "{ORGANIZATIONS}/")
                FILTER e.type NOT IN @excluded
                COLLECT org_id = e._to WITH COUNT INTO member_count
                LET org = DOCUMENT(org_id)
                FILTER org != null
                {type_filter}
                {org_sort_clause}
                LIMIT @offset, @fetch_limit
                RETURN {{
                    qid: org._key,
                    name_en: org.name_en,
                    name_it: org.name_it,
                    type: org.type,
                    founded: org.founded,
                    has_image: org.image_cached_path != null,
                    member_count: member_count
                }}
        """
        bind_vars = {
            "fetch_limit": LEADERBOARD_PAGE_SIZE + 1,
            "types": types,
            "offset": offset,
        }
        if not counts_ready:
            bind_vars["excluded"] = EXCLUDED_AFF_TYPES
        cursor = db.aql.execute(aql, bind_vars=bind_vars)

    items = list(cursor)
    page = {
        "items": items[:LEADERBOARD_PAGE_SIZE],
        "has_more": len(items) > LEADERBOARD_PAGE_SIZE,
    }
    set_cached(cache_key, page, LEADERBOARD_CACHE_TTL)
    return _page_response(
        page, limit=limit, offset=offset,
        metadata={"category": category, "sort_by": sort_key},
    )


@router.get("/orgs/categories")
@limiter.limit("60/minute")
def org_categories(request: Request):
    """Return the available org category keys."""
    cached = get_cached("leaderboard:orgs:categories:v2")
    if cached is not None:
        return cached
    result = {"categories": list(ORG_CATEGORIES.keys())}
    set_cached("leaderboard:orgs:categories:v2", result, 604800)
    return result


PERSON_OCCUPATIONS: list[str] = [
    "politician", "lawyer", "entrepreneur", "businessperson", "journalist",
    "actor", "university_teacher", "writer", "economist", "engineer",
    "diplomat", "military_officer", "musician", "film_director", "architect",
    "physicist", "mathematician", "philosopher", "historian", "photographer",
    "composer", "singer", "painter", "sculptor", "banker",
    "football_player", "basketball_player",
]


@router.get("/persons/categories")
@limiter.limit("60/minute")
def person_categories(request: Request):
    """Return the available person category keys (grouped + individual occupations)."""
    cached = get_cached("leaderboard:persons:categories:v2")
    if cached is not None:
        return cached
    result = {
        "categories": list(TRIVIA_CATEGORIES.keys()),
        "occupations": PERSON_OCCUPATIONS,
    }
    set_cached("leaderboard:persons:categories:v2", result, 604800)
    return result
