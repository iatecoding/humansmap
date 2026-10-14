"""Events endpoint: detail lookup for the Dossier panel."""

from fastapi import APIRouter, Depends, HTTPException, Request

from app.database import get_arango_client, get_db, EVENTS
from app.config import settings
from app.rate_limit import limiter
from app.services.cache import get_cached, set_cached
from app.validators import valid_event_id

router = APIRouter(prefix="/api/events", tags=["events"])


def _get_db():
    client = get_arango_client()
    return get_db(client)


@router.get("/{qid}")
@limiter.limit("120/minute")
async def get_event(request: Request, qid: str = Depends(valid_event_id)):
    cache_key = f"event:{qid}"
    cached = get_cached(cache_key)
    if cached is not None:
        return cached

    db = _get_db()
    doc = db.collection(EVENTS).get(qid)
    if not doc:
        raise HTTPException(404, f"Event {qid} not found")
    doc["id"] = doc["_key"]
    set_cached(cache_key, doc, settings.redis_default_ttl)
    return doc
