"""Graph statistics endpoint."""

from fastapi import APIRouter

from app.database import (
    get_arango_client, get_db,
    PERSONS, ORGANIZATIONS, EVENTS,
    RELATIONSHIPS, AFFILIATIONS, INVOLVEMENTS,
    DOCUMENT_COLLECTIONS, EDGE_COLLECTIONS,
)
from app.services.cache import get_cached, set_cached

router = APIRouter(prefix="/api/stats", tags=["stats"])


def _get_db():
    client = get_arango_client()
    return get_db(client)


@router.get("")
async def stats():
    """Graph statistics: node/edge counts per collection."""
    cached = get_cached("stats")
    if cached is not None:
        return cached

    db = _get_db()
    result = {}
    for name in DOCUMENT_COLLECTIONS + EDGE_COLLECTIONS:
        result[name] = db.collection(name).count()
    result["total_nodes"] = sum(result[n] for n in DOCUMENT_COLLECTIONS)
    result["total_edges"] = sum(result[n] for n in EDGE_COLLECTIONS)
    set_cached("stats", result, 3600)  # 1h
    return result
