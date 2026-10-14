"""Pre-warm leaderboard Redis caches by hitting every filter combination.

Run inside the backend container after a deploy or cache flush:
    python -m scripts.warm_leaderboard_cache

Runs the same AQL as the live endpoints directly against ArangoDB and
populates Redis.  Every exposed (filter x sort) page is warmed — including the
birth_date sort — so a cold cache never lets a first visitor trigger a heavy
scan.  Already-warm keys are skipped, so re-running is cheap.
"""

import time
import sys

from app.database import get_arango_client, get_db, PERSONS
from app.routers.leaderboard import (
    LEADERBOARD_PAGE_SIZE,
    ORG_CATEGORIES,
    ORG_SORT_DEFAULT,
    ORG_SORT_OPTIONS,
    PERSON_OCCUPATIONS,
    PERSON_SORT_OPTIONS,
    _ALL_EXCEPT_POSITION,
    _org_counts_ready,
    _org_sort_key,
    _page_cache_key,
    _person_sort_sql,
)
from app.routers.trivia_common import TRIVIA_CATEGORIES
from app.services.cache import get_cached, set_cached


def _get_db():
    client = get_arango_client()
    return get_db(client)


SCOPE_NATIONALITIES = [
    "AD", "AL", "AM", "AT", "AZ", "BA", "BE", "BG", "BY", "CH", "CY", "CZ",
    "DE", "DK", "EE", "ES", "FI", "FO", "FR", "GB", "GE", "GR", "HR", "HU",
    "IE", "IS", "IT", "LI", "LT", "LU", "LV", "MC", "MD", "ME", "MK", "MT",
    "NL", "NO", "PL", "PT", "RO", "RS", "RU", "SE", "SI", "SK", "SM", "TR",
    "UA", "VA", "XK", "US", "CA",
]

# Exposed sorts, in a stable order.  Every combination below is pre-warmed so a
# first visitor never pays for a cold page.
PERSON_SORTS = sorted(PERSON_SORT_OPTIONS)
ORG_SORTS = sorted(ORG_SORT_OPTIONS)


def _warm_person_page(db, *, nat, status, cat, sort_key, offset):
    """Warm one canonical person page. Returns 1 when written, else None."""
    cache_key = _page_cache_key(
        "persons", [nat or "all", status or "all", cat or "all", sort_key], offset
    )
    if get_cached(cache_key) is not None:
        return None

    if cat and cat in TRIVIA_CATEGORIES:
        cat_filter = "FILTER p.primary_occupation IN @cat_occs"
    elif cat:
        cat_filter = "FILTER p.primary_occupation == @cat_occ"
    else:
        cat_filter = ""

    living_filter = ""
    if status == "living":
        living_filter = "FILTER p.death_date == null"
    elif status == "deceased":
        living_filter = "FILTER p.death_date != null"

    nat_filter = "FILTER @nat IN p.nationalities" if nat else ""
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
    bind_vars: dict = {"offset": offset, "fetch_limit": LEADERBOARD_PAGE_SIZE + 1}
    if nat:
        bind_vars["nat"] = nat
    if cat and cat in TRIVIA_CATEGORIES:
        bind_vars["cat_occs"] = TRIVIA_CATEGORIES[cat]
    elif cat:
        bind_vars["cat_occ"] = cat

    items = list(db.aql.execute(aql, bind_vars=bind_vars))
    page = {
        "items": items[:LEADERBOARD_PAGE_SIZE],
        "has_more": len(items) > LEADERBOARD_PAGE_SIZE,
    }
    set_cached(cache_key, page, 604800)
    return 1


def warm_persons(db):
    """Warm person leaderboard for every exposed filter combo and sort."""
    statuses = ["living", "deceased", "all"]
    categories = list(TRIVIA_CATEGORIES.keys()) + PERSON_OCCUPATIONS + [None]
    offset = 0
    count = 0

    for status in statuses:
        for cat in categories:
            for sort_key in PERSON_SORTS:
                count += _warm_person_page(
                    db, nat=None, status=status, cat=cat,
                    sort_key=sort_key, offset=offset,
                ) or 0

    # Default filters (living, no category) for every scope nationality.
    for nat in SCOPE_NATIONALITIES:
        for sort_key in PERSON_SORTS:
            count += _warm_person_page(
                db, nat=nat, status="living", cat=None,
                sort_key=sort_key, offset=offset,
            ) or 0

    return count


def _warm_org_page(db, cat, sort_key, offset):
    """Warm one canonical org page. Returns 1 when written, else None."""
    cat_key = cat or "all"
    cache_key = _page_cache_key("orgs", [cat_key, sort_key], offset)
    if get_cached(cache_key) is not None:
        return None

    if cat == "occupations":
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
                    has_image: false,
                    member_count: cnt
                }}
        """
        bind_vars = {"fetch_limit": LEADERBOARD_PAGE_SIZE + 1, "offset": offset}
    else:
        from app.database import AFFILIATIONS, ORGANIZATIONS

        if cat and cat in ORG_CATEGORIES:
            types = ORG_CATEGORIES[cat]
        else:
            types = _ALL_EXCEPT_POSITION

        if _org_counts_ready(db):
            org_sort = {
                "members": "SORT org.affiliation_count DESC",
                "founded": "SORT org.founded DESC, org.affiliation_count DESC",
            }.get(sort_key, "SORT org.affiliation_count DESC")
            aql = f"""
                FOR org IN {ORGANIZATIONS}
                    FILTER org.affiliation_count > 0
                    FILTER org.type IN @types
                    {org_sort}
                    LIMIT @offset, @fetch_limit
                    RETURN {{
                        qid: org._key,
                        name_en: org.name_en,
                        name_it: org.name_it,
                        type: org.type,
                        founded: org.founded,
                        has_image: org.image_cached_path != null,
                        member_count: org.affiliation_count
                    }}
            """
            bind_vars = {"fetch_limit": LEADERBOARD_PAGE_SIZE + 1, "types": types, "offset": offset}
        else:
            from app.routers.leaderboard import EXCLUDED_AFF_TYPES

            org_sort = {
                "members": "SORT member_count DESC",
                "founded": "SORT org.founded DESC, member_count DESC",
            }.get(sort_key, "SORT member_count DESC")
            aql = f"""
                FOR e IN {AFFILIATIONS}
                    FILTER STARTS_WITH(e._to, "{ORGANIZATIONS}/")
                    FILTER e.type NOT IN @excluded
                    COLLECT org_id = e._to WITH COUNT INTO member_count
                    LET org = DOCUMENT(org_id)
                    FILTER org != null
                    FILTER org.type IN @types
                    {org_sort}
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
            bind_vars = {"fetch_limit": LEADERBOARD_PAGE_SIZE + 1, "types": types,
                         "excluded": EXCLUDED_AFF_TYPES, "offset": offset}

    items = list(db.aql.execute(aql, bind_vars=bind_vars))
    page = {
        "items": items[:LEADERBOARD_PAGE_SIZE],
        "has_more": len(items) > LEADERBOARD_PAGE_SIZE,
    }
    set_cached(cache_key, page, 604800)
    return 1


def warm_orgs(db):
    """Warm org leaderboard for each category and exposed sort."""
    offset = 0
    count = 0

    for cat in list(ORG_CATEGORIES.keys()) + [None]:
        # Occupation aggregates are always ranked by member count.
        sorts = [ORG_SORT_DEFAULT] if cat == "occupations" else ORG_SORTS
        for sort_key in sorts:
            count += _warm_org_page(
                db, cat, _org_sort_key(cat, sort_key), offset
            ) or 0

    return count


def warm_occupation_graphs(db):
    """Warm occupation graph endpoints (top 20 occupations from leaderboard)."""
    from app.services.cache import get_cached, set_cached

    # Use the new cache key format that warm_orgs writes (limit=500, offset=0)
    cat_key = "occupations"
    cache_key = _page_cache_key("orgs", [cat_key, "members"], 0)
    cached = get_cached(cache_key)
    if not cached:
        return 0

    count = 0
    for item in cached.get("items", [])[:30]:
        occ = item["qid"]
        for limit in [20, 200, 1000]:
            occ_cache_key = f"occupation_graph:{occ}:{limit}"
            if get_cached(occ_cache_key) is not None:
                continue

            aql = f"""
                FOR p IN {PERSONS}
                    FILTER p.primary_occupation == @occ
                    FILTER p.wikidata_statements_count != null
                    SORT p.wikidata_statements_count DESC
                    LIMIT @limit
                    RETURN KEEP(p, "_key", "_id", "name_en", "name_it",
                        "description_en", "description_it", "image_cached_path",
                        "occupations", "birth_date", "death_date", "gender",
                        "birth_place", "death_place", "cause_of_death",
                        "type", "wikidata_statements_count")
            """
            cursor = db.aql.execute(aql, bind_vars={"occ": occ, "limit": limit})
            persons = list(cursor)

            virtual_org = {
                "_key": f"occ_{occ}",
                "_id": f"organizations/occ_{occ}",
                "name_en": occ.replace("_", " ").title(),
                "name_it": occ.replace("_", " ").title(),
                "type": "occupation",
                "wikidata_statements_count": 0,
            }
            edges = [
                {"_from": p["_id"], "_to": virtual_org["_id"], "type": "member_of", "edge_collection": "affiliations"}
                for p in persons
            ]
            vertices = [virtual_org] + persons
            result = {"vertices": vertices, "edges": edges}
            set_cached(occ_cache_key, result, 604800)
            count += 1

    return count


def main():
    t0 = time.time()
    db = _get_db()

    print("Warming person leaderboard caches...")
    n1 = warm_persons(db)
    print(f"  {n1} person queries cached")

    print("Warming org leaderboard caches...")
    n2 = warm_orgs(db)
    print(f"  {n2} org queries cached")

    print("Warming occupation graph caches...")
    n3 = warm_occupation_graphs(db)
    print(f"  {n3} occupation graph queries cached")

    elapsed = time.time() - t0
    total = n1 + n2 + n3
    print(f"Done: {total} caches warmed in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
