"""Dynamic edge type registry — returns all distinct edge types in the DB."""

from fastapi import APIRouter

from app.database import get_arango_client, get_db, RELATIONSHIPS, AFFILIATIONS, INVOLVEMENTS, ORG_STRUCTURE
from app.services.cache import get_cached, set_cached

router = APIRouter(prefix="/api/edge-types", tags=["edge-types"])


def _get_db():
    client = get_arango_client()
    return get_db(client)


@router.get("")
async def edge_types():
    """List all distinct edge types across all edge collections."""
    cached = get_cached("edge_types")
    if cached is not None:
        return cached

    db = _get_db()

    aql = f"""
        LET all_types = UNION(
            (FOR e IN {RELATIONSHIPS} RETURN DISTINCT {{type: e.type, collection: "{RELATIONSHIPS}"}}),
            (FOR e IN {AFFILIATIONS} RETURN DISTINCT {{type: e.type, collection: "{AFFILIATIONS}"}}),
            (FOR e IN {INVOLVEMENTS} RETURN DISTINCT {{type: e.type, collection: "{INVOLVEMENTS}"}}),
            (FOR e IN {ORG_STRUCTURE} RETURN DISTINCT {{type: e.type, collection: "{ORG_STRUCTURE}"}})
        )
        FOR t IN all_types
            COLLECT type = t.type, collection = t.collection
            RETURN {{type, collection}}
    """
    cursor = db.aql.execute(aql)
    items = list(cursor)
    result = {"edge_types": items, "count": len(items)}
    set_cached("edge_types", result, 86400)  # 24h — edge types rarely change
    return result
