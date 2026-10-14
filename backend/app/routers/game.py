"""Game mode endpoints: challenge selection."""

import random

from fastapi import APIRouter, HTTPException, Query as Q, Request

from app.database import get_arango_client, get_db, GAME_CHALLENGES
from app.rate_limit import limiter
from app.routers.trivia_common import SCOPE_NATIONALITIES
from app.services.cache import get_cached, set_cached

router = APIRouter(prefix="/api/game", tags=["game"])


def _get_db():
    client = get_arango_client()
    return get_db(client)


@router.get("/nationalities")
@limiter.limit("60/minute")
def get_nationalities(request: Request):
    """Return the fixed filter universe without scanning the global persons collection.

    This endpoint is also used by the leaderboard filter bar.  The previous
    cold-cache query grouped nationalities across all 7.9M persons, which
    could take a minute and block the same Uvicorn worker as leaderboard/feed
    requests.  The UI already restricts this to the product's fixed scope;
    returning that scope keeps a cache miss bounded and deterministic.
    """
    cached = get_cached("game:nationalities:v2")
    if cached is not None:
        return cached

    result = {"nationalities": sorted(SCOPE_NATIONALITIES - {"XK"})}
    set_cached("game:nationalities:v2", result, 86400)  # 24h
    return result


@router.get("/challenge")
@limiter.limit("60/minute")
async def get_challenge(request: Request, nationality: str | None = Q(None)):
    """Return a random active challenge, optionally filtered by nationality."""
    db = _get_db()

    if nationality:
        aql = """
           FOR c IN @@col
               FILTER c.active == true
               LET sp = DOCUMENT(CONCAT('persons/', c.start_qid))
               LET tp = DOCUMENT(CONCAT('persons/', c.target_qid))
               FILTER @nat IN (sp.nationalities != null ? sp.nationalities : [])
                  AND @nat IN (tp.nationalities != null ? tp.nationalities : [])
               SORT RAND()
               LIMIT 1
               RETURN {
                 id: c._key,
                 start: {qid: c.start_qid, name: c.start_name},
                 target: {qid: c.target_qid, name: c.target_name},
                 shortest_path_length: c.shortest_path_length
               }"""
        bind_vars = {"@col": GAME_CHALLENGES, "nat": nationality}
    else:
        aql = """
           FOR c IN @@col
               FILTER c.active == true
               SORT RAND()
               LIMIT 1
               RETURN {
                 id: c._key,
                 start: {qid: c.start_qid, name: c.start_name},
                 target: {qid: c.target_qid, name: c.target_name},
                 shortest_path_length: c.shortest_path_length
               }"""
        bind_vars = {"@col": GAME_CHALLENGES}

    cursor = db.aql.execute(aql, bind_vars=bind_vars)
    challenge = list(cursor)
    if not challenge:
        if nationality:
            # Fall back to any challenge if no pair found for this nationality
            fallback = db.aql.execute(
                """FOR c IN @@col FILTER c.active == true SORT RAND() LIMIT 1
                   RETURN {id: c._key, start: {qid: c.start_qid, name: c.start_name},
                           target: {qid: c.target_qid, name: c.target_name},
                           shortest_path_length: c.shortest_path_length}""",
                bind_vars={"@col": GAME_CHALLENGES},
            )
            challenge = list(fallback)
            if challenge:
                challenge[0]["nationality_fallback"] = True
                return challenge[0]
        raise HTTPException(503, "No challenges available. Run generate_challenges first.")
    return challenge[0]


@router.get("/challenge/{challenge_id}/reveal")
@limiter.limit("60/minute")
async def reveal_challenge(request: Request, challenge_id: str):
    """Return the full shortest path for a completed/abandoned game."""
    db = _get_db()
    col = db.collection(GAME_CHALLENGES)

    doc = col.get(challenge_id)
    if not doc:
        raise HTTPException(404, "Challenge not found")

    return {
        "id": doc["_key"],
        "start": {"qid": doc["start_qid"], "name": doc["start_name"]},
        "target": {"qid": doc["target_qid"], "name": doc["target_name"]},
        "shortest_path_length": doc["shortest_path_length"],
        "shortest_path": doc["shortest_path"],
    }
