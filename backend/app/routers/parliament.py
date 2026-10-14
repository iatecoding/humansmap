"""Parliament hemicycle data endpoint."""

import logging
from fastapi import APIRouter, Depends, HTTPException, Request
from app.database import get_arango_client, get_db, PERSONS, ORGANIZATIONS, AFFILIATIONS
from app.services.cache import get_cached, set_cached
from app.rate_limit import limiter
from app.validators import valid_qid
from app.config import settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/parliament", tags=["parliament"])


def _get_db():
    client = get_arango_client()
    return get_db(client)


@router.get("/hemicycle/{qid}")
@limiter.limit("60/minute")
async def parliament_hemicycle(
    request: Request,
    qid: str = Depends(valid_qid),
):
    """Return all members of a parliamentary position with their party affiliations.

    Deduplicates by person QID (a member can have multiple position_held edges for
    different terms). Resolves multi-party edge cases by picking the party affiliation
    with the latest start_date (most recent party membership wins).

    After rebuilding the backend container, verify with:
        curl http://localhost/api/parliament/hemicycle/Q77685926 | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d['members']))"
    Expected: 658
        curl http://localhost/api/parliament/hemicycle/Q126063447 | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d['members']))"
    Expected: 650
    """
    cache_key = f"parliament_hemicycle:{qid}"
    cached = get_cached(cache_key)
    if cached is not None:
        return cached

    db = _get_db()
    org_id = f"{ORGANIZATIONS}/{qid}"
    if not db.collection(ORGANIZATIONS).has(qid):
        raise HTTPException(404, f"Organization {qid} not found")

    aql = f"""
        LET parliament = DOCUMENT(@org_id)
        LET raw = (
            FOR e IN {AFFILIATIONS}
                FILTER e._to == @org_id
                FILTER e.type == 'position_held'
                LET person = DOCUMENT(e._from)
                FILTER person != null AND IS_SAME_COLLECTION("{PERSONS}", e._from)
                LET parties = (
                    FOR p IN {AFFILIATIONS}
                        FILTER p._from == e._from
                        FILTER p.wikidata_property == 'P102'
                        LET party_org = DOCUMENT(p._to)
                        FILTER party_org != null
                            AND IS_SAME_COLLECTION("{ORGANIZATIONS}", p._to)
                            AND party_org.type == 'political_party'
                        SORT p.start_date == null ? '0' : p.start_date DESC
                        RETURN {{
                            qid: party_org._key,
                            name: party_org.name_en
                        }}
                )
                RETURN {{
                    qid: person._key,
                    name: person.name_en,
                    image: person.image_cached_path,
                    party_qid: LENGTH(parties) > 0 ? parties[0].qid : null,
                    party_name: LENGTH(parties) > 0 ? parties[0].name : null
                }}
        )
        LET members = (
            FOR m IN raw
                COLLECT qid = m.qid INTO group
                RETURN group[0].m
        )
        RETURN {{
            parliament: {{qid: @qid, name: parliament.name_en}},
            members: members
        }}
    """
    cursor = db.aql.execute(aql, bind_vars={"org_id": org_id, "qid": qid})
    rows = list(cursor)
    data = rows[0] if rows else {"parliament": {"qid": qid, "name": ""}, "members": []}

    # Cache for 5 minutes (parliament membership changes slowly)
    set_cached(cache_key, data, 300)
    return data
