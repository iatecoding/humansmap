"""Graph endpoints: neighborhood, shortest path, cluster."""

import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.database import get_arango_client, get_db, PERSONS, ORGANIZATIONS, EVENTS, GRAPH_NAME, RELATIONSHIPS, AFFILIATIONS, INVOLVEMENTS, ORG_STRUCTURE
from app.config import settings
from app.services.cache import get_cached, set_cached
from app.rate_limit import limiter
from app.validators import valid_qid

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/graph", tags=["graph"])

_NODE_TYPES = frozenset({"person", "organization", "event"})


def _parse_node_types(value: str) -> tuple[set[str], str]:
    """Validate and canonicalize the traversal-shaping node_types parameter."""
    parsed = {item.strip() for item in value.split(",") if item.strip()}
    invalid = parsed - _NODE_TYPES
    if not parsed or invalid:
        detail = (
            f"Invalid node_types: {', '.join(sorted(invalid))}"
            if invalid
            else "node_types cannot be empty"
        )
        raise HTTPException(422, detail)
    return parsed, ",".join(sorted(parsed))


def _neighborhood_cache_key(qid: str, depth: int, limit: int, node_types: str) -> str:
    """Include every parameter that changes a cacheable neighborhood response."""
    return f"neighborhood:v2:{qid}:d{depth}:l{limit}:types={node_types}"


def _get_db():
    client = get_arango_client()
    return get_db(client)


def _resolve_node(db, qid: str) -> str:
    """Find which collection a QID belongs to. Returns full document ID."""
    for col_name in [PERSONS, ORGANIZATIONS, EVENTS]:
        if db.collection(col_name).has(qid):
            return f"{col_name}/{qid}"
    return None


@router.get("/neighborhood/{qid}")
@limiter.limit("120/minute")
async def neighborhood(
    request: Request,
    qid: str = Depends(valid_qid),
    depth: int = Query(None, ge=1, le=3),
    limit: int = Query(None, ge=1, le=1000),
    edge_type: str | None = None,
    node_types: str = Query("person,organization,event"),
    target_qid: str | None = None,
):
    """Subgraph: any node + N degrees of connections.

    node_types: comma-separated list of entity types to include (person, organization, event).
    Controls which edge collections are traversed — reduces data returned.
    """
    d = min(depth or settings.default_traversal_depth, settings.max_traversal_depth)
    node_limit = limit or settings.neighborhood_node_limit
    type_set, normalized_node_types = _parse_node_types(node_types)

    # Cache-aside: filtered/targeted requests have extra response semantics.
    cacheable = not edge_type and not target_qid
    if cacheable:
        cache_key = _neighborhood_cache_key(qid, d, node_limit, normalized_node_types)
        cached = get_cached(cache_key)
        if cached is not None:
            return cached

    db = _get_db()

    start_node = _resolve_node(db, qid)
    if not start_node:
        raise HTTPException(404, f"Node {qid} not found")

    _VERTEX_FIELDS = (
        '"_key", "_id", "name_en", "name_it", '
        '"description_en", "description_it", "image_cached_path", '
        '"occupations", "birth_date", "death_date", "gender", '
        '"birth_place", "death_place", "cause_of_death", '
        '"type", "founded", "dissolved", "date", "wikidata_statements_count"'
    )
    _EDGE_FIELDS = '"_from", "_to", "type", "edge_collection", "role", "wikidata_property"'

    is_org_root = start_node.startswith(f"{ORGANIZATIONS}/")

    # Org-root path: query affiliations directly sorted by notability so the
    # most important members always appear (BFS returns an arbitrary subset).
    if is_org_root and not edge_type:
        aql = f"""
            LET aff_results = (
                FOR e IN {AFFILIATIONS}
                    FILTER @includePersons AND (e._to == @start OR e._from == @start)
                    LET other = e._from == @start ? e._to : e._from
                    LET doc = DOCUMENT(other)
                    FILTER doc != null
                    SORT doc.wikidata_statements_count DESC
                    LIMIT @nodeLimit
                    RETURN {{vertex: doc, edge: e}}
            )
            LET os_results = (
                FOR e IN {ORG_STRUCTURE}
                    FILTER @includeOrganizations AND (e._to == @start OR e._from == @start)
                    LET other = e._from == @start ? e._to : e._from
                    LET doc = DOCUMENT(other)
                    FILTER doc != null
                    RETURN {{vertex: doc, edge: e}}
            )
            LET inv_results = (
                FOR e IN {INVOLVEMENTS}
                    FILTER @includeEvents AND (e._to == @start OR e._from == @start)
                    LET other = e._from == @start ? e._to : e._from
                    LET doc = DOCUMENT(other)
                    FILTER doc != null
                    SORT doc.wikidata_statements_count DESC
                    LIMIT @nodeLimit
                    RETURN {{vertex: doc, edge: e}}
            )
            LET all_results = UNION(aff_results, os_results, inv_results)
            LET vertices = UNION_DISTINCT(
                (FOR r IN all_results RETURN KEEP(r.vertex, {_VERTEX_FIELDS})),
                [KEEP(DOCUMENT(@start), {_VERTEX_FIELDS})]
            )
            LET edges = (FOR r IN all_results
                RETURN KEEP(r.edge, {_EDGE_FIELDS}))
            RETURN {{vertices: vertices, edges: edges}}
        """
        cursor = db.aql.execute(aql, bind_vars={
            "start": start_node,
            "nodeLimit": node_limit,
            "includePersons": "person" in type_set,
            "includeOrganizations": "organization" in type_set,
            "includeEvents": "event" in type_set,
        })
    else:
        prune_clause = ""
        allowed_collections = []
        if "person" in type_set:
            allowed_collections.append(PERSONS)
        if "organization" in type_set:
            allowed_collections.append(ORGANIZATIONS)
        if "event" in type_set:
            allowed_collections.append(EVENTS)
        bind = {
            "start": start_node,
            "depth": d,
            "nodeLimit": node_limit,
            "allowedCollections": allowed_collections,
        }

        if edge_type:
            prune_clause = "PRUNE e.type != @edgeType FILTER e.type == @edgeType"
            bind["edgeType"] = edge_type

        # Build the traversal edge collections from the requested node types.
        # The root is always returned even when its own type is excluded.
        collections = []
        if "person" in type_set:
            collections.append(RELATIONSHIPS)
        if "organization" in type_set:
            collections.append(AFFILIATIONS)
            collections.append(ORG_STRUCTURE)
        if "event" in type_set:
            collections.append(INVOLVEMENTS)
        edge_cols = ", ".join(collections)

        aql = f"""
            LET results = (
                FOR v, e, p IN 1..@depth ANY @start
                    {edge_cols}
                    OPTIONS {{bfs: true}}
                    {prune_clause}
                    LIMIT @nodeLimit
                    RETURN {{vertex: v, edge: e}}
            )
            LET vertices = UNION_DISTINCT(
                (FOR r IN results
                    FILTER r.vertex != null
                    FILTER r.vertex._id == @start OR PARSE_IDENTIFIER(r.vertex._id).collection IN @allowedCollections
                    RETURN KEEP(r.vertex, {_VERTEX_FIELDS})),
                [KEEP(DOCUMENT(@start), {_VERTEX_FIELDS})]
            )
            LET vertex_ids = vertices[*]._id
            LET edges = (FOR r IN results
                FILTER r.edge != null
                FILTER r.edge._from IN vertex_ids AND r.edge._to IN vertex_ids
                RETURN KEEP(r.edge, {_EDGE_FIELDS}))
            RETURN {{vertices: vertices, edges: edges}}
        """
        cursor = db.aql.execute(aql, bind_vars=bind)

    result = list(cursor)
    if not result:
        return {"vertices": [], "edges": []}

    data = result[0]

    # Backfill org→org edges between orgs already in the result. BFS with a
    # node_limit saturates on a well-connected root (family trees, etc.) and
    # org_structure edges at hop 2 rarely survive. This second query finds
    # ownership edges whose both endpoints are already visible and injects
    # them — O(orgs_in_view), bounded and cheap.
    # Skipped for org-root path (already includes org_structure) and when edge_type filter is active.
    if not is_org_root and not edge_type and "organization" in type_set and data["vertices"]:
        org_ids = [
            v["_id"] for v in data["vertices"]
            if v and v.get("_id", "").startswith("organizations/")
        ]
        if len(org_ids) >= 2:
            backfill_aql = f"""
                FOR e IN {ORG_STRUCTURE}
                    FILTER e._from IN @orgIds AND e._to IN @orgIds
                    RETURN KEEP(e, "_from", "_to", "type", "edge_collection", "role", "wikidata_property")
            """
            extra = list(db.aql.execute(backfill_aql, bind_vars={"orgIds": org_ids}))
            seen = {(e["_from"], e["_to"], e.get("type")) for e in data["edges"]}
            for e in extra:
                key = (e["_from"], e["_to"], e.get("type"))
                if key not in seen:
                    data["edges"].append(e)
                    seen.add(key)

    # Sort vertices by notability (wikidata_statements_count DESC).
    # Nodes without the field (orgs, events) sort last.
    data["vertices"].sort(
        key=lambda v: v.get("wikidata_statements_count", 0) if v else 0,
        reverse=True,
    )

    # Game mode target check: if target_qid is provided, verify it's
    # visible. If target is a direct neighbor but got truncated by the
    # LIMIT, inject it into the response.
    if target_qid and not any(
        v and (v.get("_key") == target_qid or v.get("_id", "").endswith(f"/{target_qid}"))
        for v in data["vertices"]
    ):
        target_node = _resolve_node(db, target_qid)
        if target_node:
            check_aql = f"""
                FOR e IN UNION(
                    (FOR e IN {RELATIONSHIPS} FILTER (e._from == @start AND e._to == @target) OR (e._to == @start AND e._from == @target) LIMIT 1 RETURN e),
                    (FOR e IN {AFFILIATIONS} FILTER (e._from == @start AND e._to == @target) OR (e._to == @start AND e._from == @target) LIMIT 1 RETURN e),
                    (FOR e IN {ORG_STRUCTURE} FILTER (e._from == @start AND e._to == @target) OR (e._to == @start AND e._from == @target) LIMIT 1 RETURN e)
                )
                LIMIT 1
                RETURN e
            """
            edge_cursor = db.aql.execute(check_aql, bind_vars={"start": start_node, "target": target_node})
            edge_results = list(edge_cursor)
            if edge_results:
                target_doc = db.aql.execute(
                    "RETURN KEEP(DOCUMENT(@id), '_key', '_id', 'name_en', 'name_it', "
                    "'description_en', 'description_it', 'image_cached_path', 'occupations', "
                    "'birth_date', 'death_date', 'gender', "
                    "'birth_place', 'death_place', 'cause_of_death', "
                    "'type', 'founded', 'dissolved', 'date', "
                    "'wikidata_statements_count')",
                    bind_vars={"id": target_node},
                )
                target_vertex = list(target_doc)[0]
                if target_vertex:
                    target_vertex["_is_target"] = True
                    data["vertices"].append(target_vertex)
                    data["edges"].append({
                        "_from": edge_results[0]["_from"],
                        "_to": edge_results[0]["_to"],
                        "type": edge_results[0].get("type"),
                        "edge_collection": edge_results[0].get("edge_collection"),
                    })

    if cacheable:
        set_cached(cache_key, data, settings.redis_default_ttl)

    return data


@router.get("/path/{qid1}/{qid2}")
@limiter.limit("30/minute")
async def shortest_path(request: Request, qid1: str, qid2: str):
    """Shortest path between two nodes (persons, organizations, or events)."""
    for val, label in [(qid1, "qid1"), (qid2, "qid2")]:
        if not re.fullmatch(r"Q[0-9]+", val):
            raise HTTPException(422, f"Invalid QID format for {label}: {val}")

    # Cache key: sort QIDs lexicographically so A->B and B->A share cache
    qid_lo, qid_hi = sorted([qid1, qid2])
    cache_key = f"sp:{qid_lo}:{qid_hi}"
    cached = get_cached(cache_key)
    if cached is not None:
        return cached

    db = _get_db()
    start1 = _resolve_node(db, qid1)
    start2 = _resolve_node(db, qid2)
    if not start1:
        raise HTTPException(404, f"Node {qid1} not found")
    if not start2:
        raise HTTPException(404, f"Node {qid2} not found")

    aql = """
        FOR v, e IN ANY SHORTEST_PATH
            @start TO @end
            GRAPH @graphName
            RETURN {vertex: v, edge: e}
    """
    cursor = db.aql.execute(aql, bind_vars={
        "start": start1,
        "end": start2,
        "graphName": GRAPH_NAME,
    })
    results = list(cursor)
    if not results:
        no_path = {"path": [], "found": False}
        set_cached(cache_key, no_path, settings.redis_path_ttl)
        return no_path

    vertices = [r["vertex"] for r in results if r["vertex"]]
    edges = [r["edge"] for r in results if r["edge"]]
    result = {"vertices": vertices, "edges": edges, "found": True, "length": len(edges)}
    set_cached(cache_key, result, settings.redis_path_ttl)
    return result


@router.get("/cluster/{edge_type}")
@limiter.limit("60/minute")
async def cluster(
    request: Request,
    edge_type: str,
    limit: int = Query(200, ge=1, le=1000),
):
    """Subgraph of all connections of a given edge type."""
    db = _get_db()

    aql = f"""
        LET matched = (
            FOR e IN UNION(
                (FOR e IN {RELATIONSHIPS} FILTER e.type == @edgeType LIMIT @limit RETURN e),
                (FOR e IN {AFFILIATIONS} FILTER e.type == @edgeType LIMIT @limit RETURN e),
                (FOR e IN {INVOLVEMENTS} FILTER e.type == @edgeType LIMIT @limit RETURN e),
                (FOR e IN {ORG_STRUCTURE} FILTER e.type == @edgeType LIMIT @limit RETURN e)
            )
            LIMIT @limit
            RETURN e
        )
        LET vertex_ids = UNION_DISTINCT(
            (FOR e IN matched RETURN e._from),
            (FOR e IN matched RETURN e._to)
        )
        LET vertices = (FOR vid IN vertex_ids RETURN DOCUMENT(vid))
        RETURN {{vertices: vertices, edges: matched}}
    """
    cursor = db.aql.execute(aql, bind_vars={"edgeType": edge_type, "limit": limit})
    result = list(cursor)
    if not result:
        return {"vertices": [], "edges": []}
    return result[0]


@router.get("/occupation/{occupation}")
@limiter.limit("30/minute")
async def occupation_graph(
    request: Request,
    occupation: str,
    limit: int = Query(1000, ge=1, le=1000),
):
    """Virtual neighborhood: synthetic occupation node + top persons by notability."""
    cache_key = f"occupation_graph:{occupation}:{limit}"
    cached = get_cached(cache_key)
    if cached is not None:
        return cached

    db = _get_db()
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
    cursor = db.aql.execute(aql, bind_vars={"occ": occupation, "limit": limit})
    persons = list(cursor)

    virtual_org = {
        "_key": f"occ_{occupation}",
        "_id": f"organizations/occ_{occupation}",
        "name_en": occupation.replace("_", " ").title(),
        "name_it": occupation.replace("_", " ").title(),
        "type": "occupation",
        "wikidata_statements_count": 0,
    }

    edges = [
        {
            "_from": p["_id"],
            "_to": virtual_org["_id"],
            "type": "member_of",
            "edge_collection": "affiliations",
        }
        for p in persons
    ]

    vertices = [virtual_org] + persons
    result = {"vertices": vertices, "edges": edges}
    set_cached(cache_key, result, 604800)
    return result


@router.get("/org-members/{qid}")
@limiter.limit("60/minute")
async def org_members(
    request: Request,
    qid: str = Depends(valid_qid),
    leader_limit: int = Query(20, ge=1, le=100),
    member_limit: int = Query(30, ge=1, le=200),
):
    """Top affiliated persons for an organization, sorted by notability.

    Unlike the generic neighborhood BFS (which returns an arbitrary subset
    capped at node_limit), this endpoint queries affiliations directly and
    joins person docs sorted by wikidata_statements_count DESC — so the
    most notable leaders and members always appear.
    """
    cache_key = f"org_members:{qid}:{leader_limit}:{member_limit}"
    cached = get_cached(cache_key)
    if cached is not None:
        return cached

    db = _get_db()
    org_id = f"{ORGANIZATIONS}/{qid}"
    if not db.collection(ORGANIZATIONS).has(qid):
        raise HTTPException(404, f"Organization {qid} not found")

    leader_types = ["chief_executive", "chairperson", "founder", "board_member", "position_held", "owner"]
    member_types = ["member_of", "employee", "employer"]

    aql = f"""
        LET leaders = (
            FOR e IN {AFFILIATIONS}
                FILTER e._to == @org_id AND e.type IN @leader_types
                LET p = DOCUMENT(e._from)
                FILTER p != null AND IS_SAME_COLLECTION("{PERSONS}", e._from)
                SORT p.wikidata_statements_count DESC
                LIMIT @leader_limit
                RETURN {{person: KEEP(p, "_key", "_id", "name_en", "name_it",
                    "description_en", "description_it", "image_cached_path",
                    "occupations", "birth_date", "death_date", "gender",
                    "type", "wikidata_statements_count"),
                    edge: KEEP(e, "_from", "_to", "type", "role")}}
        )
        LET members = (
            FOR e IN {AFFILIATIONS}
                FILTER e._to == @org_id AND e.type IN @member_types
                LET p = DOCUMENT(e._from)
                FILTER p != null AND IS_SAME_COLLECTION("{PERSONS}", e._from)
                SORT p.wikidata_statements_count DESC
                LIMIT @member_limit
                RETURN {{person: KEEP(p, "_key", "_id", "name_en", "name_it",
                    "description_en", "description_it", "image_cached_path",
                    "occupations", "birth_date", "death_date", "gender",
                    "type", "wikidata_statements_count"),
                    edge: KEEP(e, "_from", "_to", "type", "role")}}
        )
        RETURN {{leaders: leaders, members: members}}
    """
    cursor = db.aql.execute(aql, bind_vars={
        "org_id": org_id,
        "leader_types": leader_types,
        "member_types": member_types,
        "leader_limit": leader_limit,
        "member_limit": member_limit,
    })
    rows = list(cursor)
    data = rows[0] if rows else {"leaders": [], "members": []}
    set_cached(cache_key, data, settings.redis_default_ttl)
    return data
