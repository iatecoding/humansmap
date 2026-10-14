"""Persons endpoints: list, detail, connections."""

from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.database import get_arango_client, get_db, PERSONS, ORGANIZATIONS, RELATIONSHIPS, AFFILIATIONS, INVOLVEMENTS
from app.config import settings
from app.rate_limit import limiter
from app.services.cache import get_cached, set_cached
from app.validators import valid_qid

router = APIRouter(prefix="/api/persons", tags=["persons"])


def _get_db():
    client = get_arango_client()
    return get_db(client)


@router.get("")
@limiter.limit("60/minute")
async def list_persons(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(None, ge=1, le=200),
    occupation: str | None = None,
    sort: str = Query("wikidata_statements_count", pattern="^(name_en|name_it|birth_date|wikidata_statements_count)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
):
    """List persons with pagination and optional occupation filter."""
    ps = page_size or settings.persons_page_size
    offset = (page - 1) * ps
    db = _get_db()

    sort_dir = "ASC" if order == "asc" else "DESC"

    if occupation:
        # Filtered case: count separately with COLLECT (doesn't materialize all docs)
        count_aql = f"""
            FOR p IN {PERSONS}
                FILTER p.primary_occupation == @occ
                COLLECT WITH COUNT INTO total
                RETURN total
        """
        count_cursor = db.aql.execute(count_aql, bind_vars={"occ": occupation})
        total = next(count_cursor)

        items_aql = f"""
            FOR p IN {PERSONS}
                FILTER p.primary_occupation == @occ
                SORT p.{sort} {sort_dir}
                LIMIT @offset, @limit
                RETURN MERGE(p, {{id: p._key}})
        """
        items_cursor = db.aql.execute(items_aql, bind_vars={"occ": occupation, "offset": offset, "limit": ps})
        items = list(items_cursor)
    else:
        # Unfiltered: collection.count() is O(1), no full scan needed
        total = db.collection(PERSONS).count()

        items_aql = f"""
            FOR p IN {PERSONS}
                SORT p.{sort} {sort_dir}
                LIMIT @offset, @limit
                RETURN MERGE(p, {{id: p._key}})
        """
        items_cursor = db.aql.execute(items_aql, bind_vars={"offset": offset, "limit": ps})
        items = list(items_cursor)

    return {"items": items, "total": total, "page": page, "page_size": ps}


@router.get("/{qid}")
@limiter.limit("120/minute")
async def get_person(request: Request, qid: str = Depends(valid_qid)):
    """Get a single person by QID."""
    cache_key = f"person:{qid}"
    cached = get_cached(cache_key)
    if cached is not None:
        return cached

    db = _get_db()
    col = db.collection(PERSONS)
    doc = col.get(qid)
    if not doc:
        raise HTTPException(404, f"Person {qid} not found")
    doc["id"] = doc["_key"]
    set_cached(cache_key, doc, settings.redis_default_ttl)
    return doc


@router.get("/{qid}/connections")
@limiter.limit("120/minute")
async def get_connections(request: Request, qid: str = Depends(valid_qid), type: str | None = None):
    """All edges for a person across all edge collections in a single query."""
    db = _get_db()
    col = db.collection(PERSONS)
    if not col.get(qid):
        raise HTTPException(404, f"Person {qid} not found")

    person_id = f"{PERSONS}/{qid}"
    type_filter = "FILTER e.type == @type" if type else ""
    bind: dict = {"pid": person_id}
    if type:
        bind["type"] = type

    aql = f"""
        FOR e IN UNION(
            (FOR e IN {RELATIONSHIPS}
                FILTER e._from == @pid OR e._to == @pid
                {type_filter}
                RETURN MERGE(e, {{edge_collection: "{RELATIONSHIPS}"}})),
            (FOR e IN {AFFILIATIONS}
                FILTER e._from == @pid OR e._to == @pid
                {type_filter}
                RETURN MERGE(e, {{edge_collection: "{AFFILIATIONS}"}})),
            (FOR e IN {INVOLVEMENTS}
                FILTER e._from == @pid OR e._to == @pid
                {type_filter}
                RETURN MERGE(e, {{edge_collection: "{INVOLVEMENTS}"}}))
        )
        RETURN e
    """
    cursor = db.aql.execute(aql, bind_vars=bind)
    edges = list(cursor)

    return {"person": qid, "connections": edges, "count": len(edges)}


@router.get("/{qid}/filings")
@limiter.limit("120/minute")
async def get_person_filings(
    request: Request,
    qid: str = Depends(valid_qid),
    transactions_offset: int = Query(0, ge=0),
    transactions_limit: int = Query(50, ge=1, le=100),
):
    """SEC EDGAR filings data for a person: roles + insider transactions."""
    cache_key = f"person_filings:{qid}:{transactions_offset}:{transactions_limit}"
    cached = get_cached(cache_key)
    if cached is not None:
        return cached

    db = _get_db()
    person_doc = db.collection(PERSONS).get(qid)
    if not person_doc:
        raise HTTPException(404, f"Person {qid} not found")

    sec_cik = person_doc.get("sec_cik") or None

    # If person has no CIK, return empty-but-valid response
    if not sec_cik:
        result = {
            "qid": qid,
            "sec_cik": None,
            "roles": [],
            "transaction_summary": {
                "count": 0,
                "first_date": None,
                "last_date": None,
                "directions": {},
                "by_org": [],
            },
            "transactions": [],
            "page": {"limit": transactions_limit, "offset": transactions_offset, "total": 0},
        }
        set_cached(cache_key, result, settings.redis_default_ttl)
        return result

    person_id = f"persons/{qid}"

    # ── Affiliations: insider_of / officer_of / director_of / beneficial_owner_of ──
    aff_aql = f"""
    FOR e IN {AFFILIATIONS}
      FILTER e._from == @from_id
         AND e.type IN ["insider_of", "officer_of", "director_of", "beneficial_owner_of"]
      RETURN e
    """
    aff_cursor = db.aql.execute(aff_aql, bind_vars={"from_id": person_id})
    affiliations_list = list(aff_cursor)

    # Collect all org QIDs we'll need to look up
    org_qids = list({e["_to"].split("/")[-1] for e in affiliations_list})

    org_docs: dict = {}
    if org_qids:
        orgs_aql = f"""
        FOR o IN {ORGANIZATIONS}
          FILTER o._key IN @keys
          RETURN {{_key: o._key, label_en: o.name_en, has_image: o.has_image,
                  sec_metadata: o.sec_metadata}}
        """
        o_cursor = db.aql.execute(orgs_aql, bind_vars={"keys": org_qids})
        org_docs = {o["_key"]: o for o in o_cursor}

    def _org_cik_int(org_qid: str):
        o = org_docs.get(org_qid, {})
        raw = (o.get("sec_metadata") or {}).get("cik", "")
        try:
            return int(raw) if raw else None
        except (ValueError, TypeError):
            return None

    def build_filing_url(accession: str, org_qid: str) -> str:
        if not accession:
            return ""
        cik_int = _org_cik_int(org_qid)
        if cik_int is None:
            return ""
        acc_nodash = accession.replace("-", "")
        return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/"

    # ── Roles (non-insider edges) ─────────────────────────────────────────────
    roles_out = []
    insider_edges = []
    for e in affiliations_list:
        org_qid_e = e["_to"].split("/")[-1]
        o = org_docs.get(org_qid_e, {})
        if e["type"] == "insider_of":
            insider_edges.append((e, org_qid_e))
        else:
            roles_out.append({
                "org_qid": org_qid_e,
                "org_name": o.get("label_en") or o.get("name_en") or org_qid_e,
                "org_has_image": bool(o.get("has_image")),
                "type": e.get("type"),
                "role": e.get("role"),
                "start_date": e.get("start_date"),
                "last_seen": e.get("last_seen"),
                "source_accession": e.get("source_accession"),
            })

    # ── Transaction summary ───────────────────────────────────────────────────
    all_txn_dates = [e.get("transaction_date") for (e, _) in insider_edges if e.get("transaction_date")]
    directions_total: dict[str, int] = defaultdict(int)
    by_org_map: dict[str, dict] = defaultdict(lambda: {"count": 0, "last_date": None})

    for (e, org_qid_e) in insider_edges:
        d = e.get("direction")
        if d:
            directions_total[d] += 1
        td = e.get("transaction_date")
        rec = by_org_map[org_qid_e]
        rec["count"] += 1
        if td and (rec["last_date"] is None or td > rec["last_date"]):
            rec["last_date"] = td

    by_org_list = []
    for org_qid_e, rec in by_org_map.items():
        o = org_docs.get(org_qid_e, {})
        by_org_list.append({
            "org_qid": org_qid_e,
            "org_name": o.get("label_en") or o.get("name_en") or org_qid_e,
            "count": rec["count"],
            "last_date": rec["last_date"],
        })
    by_org_list.sort(key=lambda x: x["last_date"] or "", reverse=True)

    transaction_summary = {
        "count": len(insider_edges),
        "first_date": min(all_txn_dates) if all_txn_dates else None,
        "last_date": max(all_txn_dates) if all_txn_dates else None,
        "directions": dict(directions_total),
        "by_org": by_org_list,
    }

    # ── Paginated transactions ────────────────────────────────────────────────
    # Sort all insider edges descending by transaction_date, then paginate
    sorted_insider = sorted(
        insider_edges,
        key=lambda x: x[0].get("transaction_date") or "",
        reverse=True,
    )
    total_txns = len(sorted_insider)
    paged = sorted_insider[transactions_offset: transactions_offset + transactions_limit]

    transactions_out = []
    for (e, org_qid_e) in paged:
        o = org_docs.get(org_qid_e, {})
        transactions_out.append({
            "transaction_date": e.get("transaction_date"),
            "org_qid": org_qid_e,
            "org_name": o.get("label_en") or o.get("name_en") or org_qid_e,
            "org_has_image": bool(o.get("has_image")),
            "shares": e.get("shares"),
            "price_usd": e.get("price_usd"),
            "direction": e.get("direction"),
            "form_type": e.get("form_type"),
            "accession": e.get("accession"),
            "filing_url": build_filing_url(e.get("accession", ""), org_qid_e),
        })

    result = {
        "qid": qid,
        "sec_cik": sec_cik,
        "roles": roles_out,
        "transaction_summary": transaction_summary,
        "transactions": transactions_out,
        "page": {"limit": transactions_limit, "offset": transactions_offset, "total": total_txns},
    }

    set_cached(cache_key, result, settings.redis_default_ttl)
    return result
