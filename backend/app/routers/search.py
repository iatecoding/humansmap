"""Search endpoint using ArangoSearch for bounded indexed full-text search."""

from fastapi import APIRouter, Query as Q, Request

from app.database import get_arango_client, get_db, SEARCH_VIEW
from app.rate_limit import limiter
from app.services.cache import get_cached, set_cached

router = APIRouter(prefix="/api/search", tags=["search"])

# Ranking every document matched by a broad prefix made cold searches take tens
# of seconds on the global graph. Keep enough top BM25 hits for exact-name
# reranking without allowing result ordering work to scale with the corpus.
_CANDIDATE_FLOOR = 50
_CANDIDATE_MULTIPLIER = 5


def _get_db():
    client = get_arango_client()
    return get_db(client)


@router.get("")
@limiter.limit("60/minute")
async def search(
    request: Request,
    q: str = Q(..., min_length=2, max_length=200),
    limit: int = Q(20, ge=1, le=100),
):
    """Full-text search on person and organization names via ArangoSearch.

    Ranking: exact multi-word match (5x) > accent-normalized match (3x) > prefix.
    Accent normalization supports names with diacritics.
    """
    normalized_q = q.strip()
    if len(normalized_q) < 2:
        # Pydantic validates raw length; enforce the same contract after trimming.
        from fastapi import HTTPException

        raise HTTPException(status_code=422, detail="Search query must contain at least 2 characters")

    cache_key = f"search:v4:{normalized_q}:{limit}"
    cached = get_cached(cache_key)
    if cached is not None:
        return cached

    db = _get_db()
    candidate_limit = max(_CANDIDATE_FLOOR, limit * _CANDIDATE_MULTIPLIER)

    aql = f"""
        LET candidates = (
            FOR d IN {SEARCH_VIEW}
                SEARCH
                    ANALYZER(BOOST(TOKENS(@q, "text_en") ALL IN d.name_en, 5), "text_en") OR
                    ANALYZER(BOOST(TOKENS(@q, "text_it") ALL IN d.name_it, 5), "text_it") OR
                    ANALYZER(BOOST(TOKENS(@q, "text_norm") ALL IN d.name_en, 3), "text_norm") OR
                    ANALYZER(STARTS_WITH(d.name_en, TOKENS(@q, "text_en")), "text_en") OR
                    ANALYZER(STARTS_WITH(d.name_it, TOKENS(@q, "text_it")), "text_it") OR
                    ANALYZER(STARTS_WITH(d.name_en, TOKENS(@q, "text_norm")), "text_norm")
                LET relevance = BM25(d)
                SORT relevance DESC, d.wikidata_statements_count DESC
                LIMIT @candidate_limit
                RETURN {{ d, relevance }}
        )
        FOR candidate IN candidates
            LET d = candidate.d
            LET exact_name = (
                LOWER(TRIM(NOT_NULL(d.name_en, ""))) == LOWER(@q) OR
                LOWER(TRIM(NOT_NULL(d.name_it, ""))) == LOWER(@q)
            )
            LET exact_notability = exact_name ? TO_NUMBER(NOT_NULL(d.wikidata_statements_count, 0)) : 0
            SORT exact_name DESC, exact_notability DESC, candidate.relevance DESC,
                 d.wikidata_statements_count DESC
            LIMIT @limit
            RETURN MERGE(d, {{
                id: d._key,
                _collection: PARSE_IDENTIFIER(d._id).collection
            }})
    """
    cursor = db.aql.execute(
        aql,
        bind_vars={
            "q": normalized_q,
            "limit": limit,
            "candidate_limit": candidate_limit,
        },
    )
    items = list(cursor)
    result = {"items": items, "count": len(items), "query": normalized_q}
    set_cached(cache_key, result, 3600)  # 1h - search results change slowly
    return result
