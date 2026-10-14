"""Featured / trending people endpoint."""

from fastapi import APIRouter, Query as Q, Request

from app.database import get_arango_client, get_db
from app.rate_limit import limiter
from app.services.trending import get_trending

router = APIRouter(prefix="/api", tags=["featured"])


def _get_db():
    client = get_arango_client()
    return get_db(client)


@router.get("/featured")
@limiter.limit("60/minute")
async def get_featured(
    request: Request,
    lang: str = Q(default="it", max_length=5),
    limit: int = Q(default=6, ge=1, le=20),
):
    """Return trending / notable people for the given language."""
    db = _get_db()
    people = get_trending(lang, limit, db)
    return {"people": people}
