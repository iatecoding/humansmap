"""Organizations endpoint: detail lookup for the Dossier panel."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.database import get_arango_client, get_db, ORGANIZATIONS, AFFILIATIONS, EVENTS, PERSONS, ORG_STRUCTURE
from app.config import settings
from app.rate_limit import limiter
from app.services.cache import get_cached, set_cached
from app.validators import valid_qid

router = APIRouter(prefix="/api/organizations", tags=["organizations"])


def _get_db():
    client = get_arango_client()
    return get_db(client)


@router.get("/{qid}")
@limiter.limit("120/minute")
async def get_organization(request: Request, qid: str = Depends(valid_qid)):
    cache_key = f"organization:{qid}"
    cached = get_cached(cache_key)
    if cached is not None:
        return cached

    db = _get_db()
    doc = db.collection(ORGANIZATIONS).get(qid)
    if not doc:
        raise HTTPException(404, f"Organization {qid} not found")
    doc["id"] = doc["_key"]
    set_cached(cache_key, doc, settings.redis_default_ttl)
    return doc


@router.get("/{qid}/filings")
@limiter.limit("120/minute")
async def get_organization_filings(
    request: Request,
    qid: str = Depends(valid_qid),
    events_offset: int = Query(0, ge=0),
    events_limit: int = Query(50, ge=1, le=100),
):
    cache_key = f"dossier_filings:{qid}:{events_offset}:{events_limit}"
    cached = get_cached(cache_key)
    if cached is not None:
        return cached

    db = _get_db()
    org_doc = db.collection(ORGANIZATIONS).get(qid)
    if not org_doc:
        raise HTTPException(404, f"Organization {qid} not found")

    sec_meta = org_doc.get("sec_metadata") or {}
    unmatched_reporters = (org_doc.get("sec_unmatched_reporters") or [])[:200]
    unmatched_directors = (org_doc.get("sec_unmatched_directors") or [])[:200]

    # Build filing_url for unmatched reporters
    org_cik_raw = sec_meta.get("cik", "")
    try:
        org_cik_int = int(org_cik_raw) if org_cik_raw else None
    except (ValueError, TypeError):
        org_cik_int = None

    def build_unmatched_url(reporter: dict) -> str:
        cik = reporter.get("cik") or org_cik_raw
        form_type = reporter.get("form_type", "4")
        if not cik:
            return ""
        return f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type={form_type}"

    def build_insider_url(accession: str) -> str:
        if not accession or org_cik_int is None:
            return ""
        acc_nodash = accession.replace("-", "")
        return f"https://www.sec.gov/Archives/edgar/data/{org_cik_int}/{acc_nodash}/"

    unmatched_out = []
    for r in unmatched_reporters:
        unmatched_out.append({
            "reporter_name": r.get("reporter_name", ""),
            "filing_date": r.get("filing_date", ""),
            "form_type": r.get("form_type", ""),
            "accession": r.get("accession", ""),
            "cik": r.get("cik", ""),
            "filing_url": build_unmatched_url(r),
        })

    # ── Affiliations: insider_of / officer_of / director_of ──────────────────
    aff_aql = f"""
    FOR e IN {AFFILIATIONS}
      FILTER e._to == @to_id
         AND e.type IN ["insider_of", "officer_of", "director_of"]
      RETURN e
    """
    aff_cursor = db.aql.execute(aff_aql, bind_vars={"to_id": f"organizations/{qid}"})
    affiliations_list = list(aff_cursor)

    # Group by person QID
    from collections import defaultdict
    person_edges: dict[str, dict] = defaultdict(lambda: {"insider": [], "roles": []})
    for e in affiliations_list:
        person_qid = e["_from"].split("/")[-1]
        if e["type"] == "insider_of":
            person_edges[person_qid]["insider"].append(e)
        else:
            person_edges[person_qid]["roles"].append(e)

    # Fetch person docs for matched persons
    person_keys = list(person_edges.keys())
    matched_out = []
    if person_keys:
        persons_aql = f"""
        FOR p IN {PERSONS}
          FILTER p._key IN @keys
          RETURN {{_key: p._key, label_en: p.name_en, has_image: p.has_image}}
        """
        p_cursor = db.aql.execute(persons_aql, bind_vars={"keys": person_keys})
        person_docs = {p["_key"]: p for p in p_cursor}

        for person_qid, edge_data in person_edges.items():
            p_doc = person_docs.get(person_qid, {})

            # Roles
            roles_out = []
            for re in edge_data["roles"]:
                roles_out.append({
                    "type": re.get("type"),
                    "role": re.get("role"),
                    "start_date": re.get("start_date"),
                    "last_seen": re.get("last_seen"),
                    "source_accession": re.get("source_accession"),
                })

            # Transaction summary from insider edges
            txns = edge_data["insider"]
            txn_dates = [t.get("transaction_date") for t in txns if t.get("transaction_date")]
            directions: dict[str, int] = defaultdict(int)
            for t in txns:
                d = t.get("direction")
                if d:
                    directions[d] += 1

            summary = {
                "count": len(txns),
                "first_date": min(txn_dates) if txn_dates else None,
                "last_date": max(txn_dates) if txn_dates else None,
                "directions": dict(directions),
            }

            # Recent transactions: top 10 desc by date
            recent = sorted(txns, key=lambda t: t.get("transaction_date") or "", reverse=True)[:10]
            recent_out = []
            for t in recent:
                recent_out.append({
                    "transaction_date": t.get("transaction_date"),
                    "shares": t.get("shares"),
                    "price_usd": t.get("price_usd"),
                    "direction": t.get("direction"),
                    "form_type": t.get("form_type"),
                    "accession": t.get("accession"),
                    "filing_url": build_insider_url(t.get("accession", "")),
                })

            # insiders.matched is strictly for persons with ≥1 insider transaction.
            # Non-transacting directors (DEF 14A-only) go into directors.matched below.
            if edge_data["insider"]:
                matched_out.append({
                    "person_qid": person_qid,
                    "name": p_doc.get("label_en") or person_qid,
                    "has_image": bool(p_doc.get("has_image")),
                    "roles": roles_out,
                    "transaction_summary": summary,
                    "recent_transactions": recent_out,
                })

    # ── Directors / officers (role edges, transaction-independent) ────────────
    directors_matched = []
    for person_qid, edge_data in person_edges.items():
        if not edge_data["roles"]:
            continue
        p_doc = person_docs.get(person_qid, {}) if person_keys else {}
        role_entries = []
        for re in edge_data["roles"]:
            role_entries.append({
                "type": re.get("type"),
                "role": re.get("role"),
                "start_date": re.get("start_date"),
                "end_date": re.get("end_date"),
                "last_seen": re.get("last_seen"),
                "source": re.get("source"),
                "match_method": re.get("match_method"),
            })
        directors_matched.append({
            "person_qid": person_qid,
            "name": p_doc.get("label_en") or person_qid,
            "has_image": bool(p_doc.get("has_image")),
            "roles": role_entries,
        })
    directors_matched.sort(key=lambda d: d["name"].lower())

    # ── Material events ───────────────────────────────────────────────────────
    # Count total
    count_aql = f"""
    FOR e IN {EVENTS}
      FILTER e.org_qid == @qid AND e.type == "material_event"
      COLLECT WITH COUNT INTO total
      RETURN total
    """
    count_cursor = db.aql.execute(count_aql, bind_vars={"qid": qid})
    events_total = next(iter(count_cursor), 0)

    # by_subtype aggregation
    subtype_aql = f"""
    FOR e IN {EVENTS}
      FILTER e.org_qid == @qid AND e.type == "material_event"
      COLLECT subtype = e.subtype WITH COUNT INTO n
      RETURN {{subtype: subtype, count: n}}
    """
    subtype_cursor = db.aql.execute(subtype_aql, bind_vars={"qid": qid})
    by_subtype = {row["subtype"]: row["count"] for row in subtype_cursor}

    # Paginated items
    items_aql = f"""
    FOR e IN {EVENTS}
      FILTER e.org_qid == @qid AND e.type == "material_event"
      SORT e.filing_date DESC
      LIMIT @offset, @limit
      RETURN {{
        key: e._key,
        subtype: e.subtype,
        filing_date: e.filing_date,
        items: e.items,
        accession: e.accession,
        filing_url: e.filing_url,
        is_amendment: e.is_amendment
      }}
    """
    items_cursor = db.aql.execute(
        items_aql,
        bind_vars={"qid": qid, "offset": events_offset, "limit": events_limit},
    )
    events_items = list(items_cursor)
    for event in events_items:
        # Keep the public response stable when a lightweight test/mock or an
        # older collection returns Arango's `_key` instead of the API's `key`.
        if "key" not in event and "_key" in event:
            event["key"] = event["_key"]

    # ── Institutional holders (13F holds_shares_of edges pointing TO this org) ─
    holders_aql = f"""
    FOR e IN {ORG_STRUCTURE}
      FILTER e._to == @to_id AND e.type == "holds_shares_of"
      COLLECT manager_id = e._from INTO grp
      LET best = (
        FOR x IN grp[*].e
          SORT x.period_of_report DESC
          LIMIT 1
          RETURN x
      )[0]
      SORT best.value_usd DESC
      LIMIT 20
      LET mgr = DOCUMENT(manager_id)
      RETURN {{
        manager_qid: LAST(SPLIT(manager_id, "/")),
        manager_name: mgr.name_en || mgr.name_it || LAST(SPLIT(manager_id, "/")),
        manager_has_image: mgr.has_image || false,
        value_usd: best.value_usd,
        shares: best.shares,
        period_of_report: best.period_of_report
      }}
    """
    holders_cursor = db.aql.execute(holders_aql, bind_vars={"to_id": f"organizations/{qid}"})
    institutional_holders = list(holders_cursor)

    # Totals
    total_insider_txns = sum(len(e["insider"]) for e in person_edges.values())

    result = {
        "qid": qid,
        "sec_metadata": sec_meta,
        "insiders": {
            "matched": matched_out,
            "unmatched": unmatched_out,
            "totals": {
                "matched_persons": len(matched_out),
                "matched_transactions": total_insider_txns,
                "unmatched": len(unmatched_out),
            },
        },
        "directors": {
            "matched": directors_matched,
            "unmatched": unmatched_directors,
            "totals": {
                "matched": len(directors_matched),
                "unmatched": len(unmatched_directors),
            },
        },
        "material_events": {
            "by_subtype": by_subtype,
            "items": events_items,
            "page": {
                "limit": events_limit,
                "offset": events_offset,
                "total": events_total,
            },
        },
        "institutional_holders": institutional_holders,
    }

    set_cached(cache_key, result, settings.redis_default_ttl)
    return result
