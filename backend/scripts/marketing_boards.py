#!/usr/bin/env python3
"""Marketing boards miner — read-only, emits the "interlocking directorates" card.

The insight: organisations are linked when the *same people* sit on their
boards. Wikidata-only (no SEC): only ``board_member``, ``chairperson`` and
``chief_executive`` edges are used as the primary set; ``owner`` can be added
with ``--include-owner`` (ownership is a different relationship, and in this
graph it mostly adds single-family holding clusters, so it is off by default).

It never writes to ArangoDB. Everything is computed from one indexed pull on
the ``affiliations`` type index (~32k leadership edges), then aggregated in
Python — the graph is small and cheap to compute fully.

Outputs (both written by default):

  boards.json   machine-readable payload: nodes, edges, clusters, probe stats
  boards.html   brand-styled card sheet (1600x1000) to screenshot to boards.png

Run it manually inside the backend container:

    docker compose exec -T backend python - --out-dir /tmp/boards \\
        < backend/scripts/marketing_boards.py
    docker compose exec -T backend cat /tmp/boards/boards.json \\
        > docs/marketing/boards/boards.json
    docker compose exec -T backend cat /tmp/boards/boards.html \\
        > docs/marketing/boards/boards.html

Flags:
    --out-dir PATH        output directory (default: docs/marketing/boards)
    --min-shared N        people two orgs must share to make an edge (default 2)
    --min-cluster-size N  smallest cluster drawn in the network (default 3)
    --max-clusters N      cap on drawn clusters (default 30)
    --include-owner       add ``owner`` edges to the leadership set
    --no-html             emit only the JSON payload
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import sys
from collections import OrderedDict, defaultdict
from datetime import date
from pathlib import Path

# When run as a file inside the container this resolves to /app; when piped
# over stdin (``python -``) __file__ is "<stdin>" and /app is cwd, so fall back.
try:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
except (NameError, OSError):  # pragma: no cover - stdin invocation
    sys.path.insert(0, str(Path.cwd()))

from app.database import get_arango_client, get_db  # noqa: E402

CORE_TYPES = ["board_member", "chairperson", "chief_executive"]
OWNER_TYPE = "owner"

ROLE_LABEL = {
    "board_member": "board",
    "chairperson": "chair",
    "chief_executive": "CEO",
    "owner": "owner",
}

# Plain plural labels for the coverage note (never expose the raw field names).
ROLE_PLURAL = {
    "board_member": "board members",
    "chairperson": "chairs",
    "chief_executive": "chief executives",
    "owner": "owners",
}

# Long official names that would collide in a dense network. Display-only.
SHORT_NAME = {
    "President and Fellows of Harvard College": "Harvard",
    "Center for Strategic and International Studies": "CSIS",
    "Peterson Institute for International Economics": "Peterson Inst.",
    "Carnegie Endowment for International Peace": "Carnegie",
    "The Coca-Cola Company": "Coca-Cola",
    "Alphabet Inc.": "Alphabet",
    "Johnson & Johnson": "J&J",
    "General Dynamics": "General Dynamics",
    "Council on Foreign Relations": "CFR",
    "A. P. Møller and Chastine Mc-Kinney Møller Foundation": "A.P. Møller Fdn.",
    "Элион (компания)": "Elion (RU)",
    "Scientific Research Institute of Precision Mechanical Engineering": "Sci. Res. Inst. Precision Mech. Eng.",
    "Nordstern Lebensversicherungs-AG": "Nordstern Lebensvers.",
    "Kongsberg Våpenfabrikk": "Kongsberg Våpenfabrikk",
    "United Communication Hldg II": "United Comm. Hldg II",
    "Univerzální správa majetku": "Univerzální správa",
    "Aktieselskabet af 1.8.2007": "Aktieselskabet 1.8.2007",
    "Aktieselskabet af 21. november 2001": "Aktieselskabet 21.11.2001",
    "Nadace EP Corporate Group": "Nadace EP",
    "Energetický a průmyslový holding": "Energetický holding",
}

STOP_TOKENS = {
    "the", "of", "and", "for", "a", "an", "group", "holding", "holdings",
    "company", "corporation", "corp", "inc", "ltd", "limited", "plc", "as",
    "ab", "a/s", "aps", "gmbh", "sa", "nv", "ag", "spa", "srl", "sro",
    "investment", "investments", "capital", "management", "fund", "funds",
    "investiční", "společnost", "správa", "majetku", "fond", "fondy",
}

# Hand-written cluster labels where the automatic heuristic reads poorly.
CLUSTER_LABEL_OVERRIDES = {
    "Czech News Center": "Czech Media \u00b7 EP",
    "Krakovska RE 25": "WGCZ group",
    "EP Infrastructure": "EP Group",
    "Aktieselskabet af 1.8.2007": "Bestseller \u00b7 Heartland",
    "Pankrác": "ČKD Polovodiče",
    "Výnosný investiční fond": "PPF",
    "Charles Koch Institute": "Koch network",
    "A.P. Møller Holding": "A.P. Møller \u00b7 Maersk",
}

# ---------------------------------------------------------------------------
# Rendering geometry
# ---------------------------------------------------------------------------

CARD_W = 1600
CARD_H = 1000
NET_W = 1520
NET_H = 525
NET_Y = 186
NODE_R_MIN = 7.0
NODE_R_MAX = 22.0


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


def _rows(db, aql: str, bind: dict | None = None) -> list:
    try:
        return list(db.aql.execute(aql, bind_vars=bind or {}))
    except Exception as exc:  # noqa: BLE001 - a marketing job must not crash
        print(f"[warn] query failed: {exc}", file=sys.stderr)
        return []


def _type_count(db, etype: str) -> int:
    rows = _rows(
        db,
        "RETURN LENGTH(FOR e IN affiliations FILTER e.type == @t LIMIT 1000000 RETURN 1)",
        {"t": etype},
    )
    return int(rows[0]) if rows else 0


def _fetch_edges(db, types: list[str]) -> list[dict]:
    return _rows(
        db,
        "FOR e IN affiliations FILTER e.type IN @types "
        "RETURN {f: e._from, t: e._to, ty: e.type}",
        {"types": types},
    )


def _documents(db, ids: list[str]) -> dict[str, dict]:
    """Batched DOCUMENT lookups. ``ids`` are Arango document ids."""
    out: dict[str, dict] = {}
    ids = list(ids)
    for i in range(0, len(ids), 4000):
        chunk = ids[i : i + 4000]
        for r in _rows(
            db,
            "FOR d IN DOCUMENT(@ids) "
            "RETURN {id: d._id, name: d.name_en, qid: d.wikidata_id, "
            "type: d.type, country: d.country, country_id: d.country_id}",
            {"ids": chunk},
        ):
            out[r["id"]] = r
    return out


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def collect(db, types: list[str], min_shared: int, min_cluster_size: int,
            max_clusters: int) -> dict:
    edges = _fetch_edges(db, types)

    # person -> set(org); (person, org) -> set(edge types)
    porgs: dict[str, set] = defaultdict(set)
    ptypes: dict[tuple, set] = defaultdict(set)
    for e in edges:
        porgs[e["f"]].add(e["t"])
        ptypes[(e["f"], e["t"])].add(e["ty"])
    people_total = len(porgs)
    orgs_total = len({o for s in porgs.values() for o in s})

    # Every org pair that shares at least one person, with the distinct people.
    pair_people: dict[tuple, set] = defaultdict(set)
    for p, orgs in porgs.items():
        ordered = sorted(orgs)
        for i in range(len(ordered)):
            for j in range(i + 1, len(ordered)):
                pair_people[(ordered[i], ordered[j])].add(p)

    pairs = {k: v for k, v in pair_people.items() if len(v) >= min_shared}

    # Connected components over the pair graph (union-find).
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for a, b in pairs:
        union(a, b)
    comp_members: dict[str, list[str]] = defaultdict(list)
    for node in list(parent):
        comp_members[find(node)].append(node)

    # Rank components by member count, then edge weight.
    comps = list(comp_members.values())
    comp_edge_weight = {}
    for (a, b), v in pairs.items():
        r = find(a)
        comp_edge_weight[r] = comp_edge_weight.get(r, 0) + len(v)
    comps.sort(key=lambda m: (len(m), comp_edge_weight.get(find(m[0]), 0)), reverse=True)

    drawn = [c for c in comps if len(c) >= min_cluster_size][:max_clusters]
    drawn_set = {m for c in drawn for m in c}
    giant = drawn[0] if drawn else []

    # --- names for everybody we might mention ------------------------------
    involved_orgs = {m for c in comps for m in c}
    pair_people_ids = {p for v in pairs.values() for p in v}
    org_docs = _documents(db, list(involved_orgs))
    person_docs = _documents(db, list(pair_people_ids))

    def org_name(oid: str) -> str:
        return (org_docs.get(oid) or {}).get("name") or oid

    def person_name(pid: str) -> str:
        return (person_docs.get(pid) or {}).get("name") or pid

    # --- node metrics -------------------------------------------------------
    def shared_people_of(org: str) -> set:
        s: set = set()
        for (a, b), v in pairs.items():
            if a == org:
                s |= v
            elif b == org:
                s |= v
        return s

    nodes = []
    for oid in involved_orgs:
        doc = org_docs.get(oid) or {}
        members = [p for p in porgs if oid in porgs[p]]
        nodes.append(
            OrderedDict(
                qid=doc.get("qid") or oid,
                id=oid,
                name=doc.get("name") or oid,
                short=SHORT_NAME.get(doc.get("name") or "", doc.get("name") or oid),
                type=doc.get("type"),
                country=doc.get("country"),
                country_id=doc.get("country_id"),
                leaders=len(members),
                shared_people=len(shared_people_of(oid)),
                degree=sum(1 for (a, b) in pairs if a == oid or b == oid),
                in_network=oid in drawn_set,
            )
        )
    nodes.sort(key=lambda n: (-n["shared_people"], -n["leaders"], n["name"]))

    # --- edges --------------------------------------------------------------
    edge_rows = []
    for (a, b), v in sorted(pairs.items(), key=lambda kv: -len(kv[1])):
        people = []
        for p in sorted(v, key=lambda x: person_name(x).lower()):
            people.append(
                OrderedDict(
                    qid=(person_docs.get(p) or {}).get("qid") or p,
                    name=person_name(p),
                    a_roles=sorted(ROLE_LABEL.get(t, t) for t in ptypes[(p, a)]),
                    b_roles=sorted(ROLE_LABEL.get(t, t) for t in ptypes[(p, b)]),
                )
            )
        edge_rows.append(
            OrderedDict(
                source=(org_docs.get(a) or {}).get("qid") or a,
                target=(org_docs.get(b) or {}).get("qid") or b,
                source_id=a,
                target_id=b,
                source_name=org_name(a),
                target_name=org_name(b),
                shared=len(v),
                in_network=a in drawn_set and b in drawn_set,
                same_lineage=_same_lineage(org_name(a), org_name(b)),
                people=people,
            )
        )

    # --- clusters -----------------------------------------------------------
    clusters = []
    for idx, members in enumerate(drawn):
        comp_edges = [e for e in edge_rows if e["source_id"] in set(members)
                      and e["target_id"] in set(members)]
        if idx == 0:
            label = "The boardroom network"
        else:
            label = _cluster_label([org_name(m) for m in members],
                                   [n for n in nodes if n["id"] in members])
        clusters.append(
            OrderedDict(
                id=f"c{idx+1}",
                label=label,
                size=len(members),
                edges=len(comp_edges),
                giant=(idx == 0),
                orgs=[(org_docs.get(m) or {}).get("qid") or m for m in members],
                org_ids=members,
            )
        )
    cluster_of = {}
    for c in clusters:
        for m in c["org_ids"]:
            cluster_of[m] = c["id"]
    for n in nodes:
        n["cluster"] = cluster_of.get(n["id"])

    # --- probe stats --------------------------------------------------------
    dist = defaultdict(int)
    for v in pairs.values():
        dist[len(v)] += 1
    all_dist = defaultdict(int)
    for v in pair_people.values():
        all_dist[len(v)] += 1

    return OrderedDict(
        generated=date.today().isoformat(),
        concept="interlocking_directorates",
        edge_types=types,
        min_shared=min_shared,
        scale=OrderedDict(
            leadership_edges=len(edges),
            people_total=people_total,
            orgs_total=orgs_total,
            people_on_2plus_orgs=sum(1 for s in porgs.values() if len(s) >= 2),
            interlocking_pairs=len(pairs),
            network_clusters=len(drawn),
            network_nodes=len(drawn_set),
            network_edges=sum(1 for e in edge_rows if e["in_network"]),
            giant_cluster_size=len(giant),
        ),
        type_counts={t: _type_count(db, t) for t in types},
        probe=OrderedDict(
            pairs_by_shared={str(k): dist[k] for k in sorted(dist, reverse=True)},
            pairs_by_shared_incl_single={str(k): all_dist[k] for k in sorted(all_dist, reverse=True)},
            component_sizes=sorted((len(c) for c in comps), reverse=True),
        ),
        nodes=nodes,
        edges=edge_rows,
        clusters=clusters,
        top_pairs=[
            OrderedDict(
                a=e["source_name"], aq=e["source"],
                b=e["target_name"], bq=e["target"],
                shared=e["shared"], same_lineage=e["same_lineage"],
                people=[p["name"] for p in e["people"]],
            )
            for e in edge_rows[:20]
        ],
    )


def _normalize(name: str) -> str:
    s = name.lower()
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"[\W_]+", " ", s)
    words = [w for w in s.split() if w not in STOP_TOKENS]
    return " ".join(words)


def _acronym(name: str) -> str:
    words = [w for w in re.findall(r"[^\W_]+", name)]
    return "".join(w[0] for w in words).lower()


def _same_lineage(a: str, b: str) -> bool:
    """Heuristic flag for corporate successors / renames / parent-subsidiary."""
    na, nb = _normalize(a), _normalize(b)
    if not na or not nb:
        return False
    if na == nb or na.startswith(nb) or nb.startswith(na):
        return True
    wa, wb = na.split(), nb.split()
    if wa and wb and wa[0] == wb[0] and len(wa[0]) >= 5:
        return True
    if nb.replace(" ", "") == _acronym(a) or na.replace(" ", "") == _acronym(b):
        return True
    return False


def _cluster_label(names: list[str], node_rows: list[dict]) -> str:
    for n in names:
        if n in CLUSTER_LABEL_OVERRIDES:
            return CLUSTER_LABEL_OVERRIDES[n]
    token_lists = [
        [w for w in re.findall(r"[^\W_]+", n) if w.lower() not in STOP_TOKENS and len(w) >= 2]
        for n in names
    ]
    token_lists = [t for t in token_lists if t]
    if token_lists:
        # common leading tokens
        prefix: list[str] = []
        for i in range(min(len(t) for t in token_lists)):
            tok = token_lists[0][i].lower()
            if all(t[i].lower() == tok for t in token_lists):
                prefix.append(token_lists[0][i])
            else:
                break
        if prefix and len(prefix[0]) >= 3:
            return " ".join(prefix[:2])
        # most frequent token across names
        freq: dict[str, int] = defaultdict(int)
        for t in token_lists:
            for w in set(x.lower() for x in t):
                freq[w] += 1
        best = max(freq.items(), key=lambda kv: (kv[1], len(kv[0])))
        if best[1] >= max(2, math.ceil(0.6 * len(names))) and len(best[0]) >= 3:
            # recover original casing
            for t in token_lists:
                for w in t:
                    if w.lower() == best[0]:
                        return w
    if node_rows:
        top = max(node_rows, key=lambda n: (n["shared_people"], n["leaders"]))
        return top["short"]
    return names[0] if names else "Cluster"


# ---------------------------------------------------------------------------
# Layout: Fruchterman-Reingold per component, then pack component blobs
# ---------------------------------------------------------------------------


def _fr(n: int, edge_list: list[tuple], iters: int = 480, seed: int = 7) -> list:
    if n <= 1:
        return [[0.0, 0.0]]
    rnd = random.Random(seed)
    pos = []
    for i in range(n):
        a = 2 * math.pi * i / n + rnd.uniform(-0.08, 0.08)
        pos.append([math.cos(a), math.sin(a)])
    k = 1.0 / math.sqrt(n)
    temp = 0.30
    for _ in range(iters):
        disp = [[0.0, 0.0] for _ in range(n)]
        for i in range(n):
            xi, yi = pos[i]
            for j in range(i + 1, n):
                dx = xi - pos[j][0]
                dy = yi - pos[j][1]
                d2 = dx * dx + dy * dy
                if d2 < 1e-9:
                    dx, dy, d2 = 1e-3, 1e-3, 1e-6
                d = math.sqrt(d2)
                f = k * k / d
                ux, uy = dx / d, dy / d
                disp[i][0] += ux * f
                disp[i][1] += uy * f
                disp[j][0] -= ux * f
                disp[j][1] -= uy * f
        for (i, j, w) in edge_list:
            dx = pos[i][0] - pos[j][0]
            dy = pos[i][1] - pos[j][1]
            d = math.hypot(dx, dy) or 1e-3
            f = d * d / k * (0.55 + 0.45 * (w - 1))
            ux, uy = dx / d, dy / d
            disp[i][0] -= ux * f
            disp[i][1] -= uy * f
            disp[j][0] += ux * f
            disp[j][1] += uy * f
        for i in range(n):
            dd = math.hypot(disp[i][0], disp[i][1]) or 1e-3
            step = min(dd, temp)
            pos[i][0] += disp[i][0] / dd * step
            pos[i][1] += disp[i][1] / dd * step
        temp = max(0.006, temp * 0.985)
    cx = sum(p[0] for p in pos) / n
    cy = sum(p[1] for p in pos) / n
    return [[p[0] - cx, p[1] - cy] for p in pos]


def _node_radius(shared: int, smin: int, smax: int) -> float:
    if smax <= smin:
        return NODE_R_MIN + 4
    t = (shared - smin) / (smax - smin)
    return NODE_R_MIN + (NODE_R_MAX - NODE_R_MIN) * math.sqrt(max(0.0, t))


def _expand(pos: list, power: float = 0.68) -> list:
    """Push clustered centre nodes outward (r -> r**power, r<1 expands)."""
    out = []
    for x, y in pos:
        r = math.hypot(x, y)
        if r < 1e-9:
            out.append([x, y])
            continue
        nr = r ** power
        out.append([x / r * nr, y / r * nr])
    return out


def build_layout(payload: dict) -> dict:
    """Return {org_id: (x, y, r)} plus cluster centroids, in NET_W x NET_H."""
    nodes = {n["id"]: n for n in payload["nodes"] if n["in_network"]}
    edges = [e for e in payload["edges"] if e["in_network"]]
    clusters = payload["clusters"]

    shared_vals = [n["shared_people"] for n in nodes.values()]
    smin, smax = min(shared_vals or [2]), max(shared_vals or [2])

    def radius(oid: str) -> float:
        return _node_radius(nodes[oid]["shared_people"], smin, smax)

    layout: dict[str, tuple] = {}
    centroids: dict[str, tuple] = {}

    def local_layout(cluster: dict, ci: int, power: float) -> tuple:
        members = [m for m in cluster["org_ids"] if m in nodes]
        index = {m: i for i, m in enumerate(members)}
        elist = []
        for e in edges:
            if e["source_id"] in index and e["target_id"] in index:
                elist.append((index[e["source_id"]], index[e["target_id"]], e["shared"]))
        pos = _expand(_fr(len(members), elist, seed=7 + ci), power)
        extent = max((math.hypot(x, y) for x, y in pos), default=1.0) or 1.0
        return members, index, pos, extent

    # --- the giant component, centred -------------------------------------
    giant = clusters[0]
    members, index, pos, extent = local_layout(giant, 0, 0.66)
    gcx, gcy = NET_W * 0.5, NET_H * 0.5
    tgt = 150.0
    scale = tgt / extent
    for m in members:
        x, y = pos[index[m]]
        layout[m] = (x * scale + gcx, y * scale + gcy, radius(m))
    centroids[giant["id"]] = (gcx, gcy)

    # --- satellites: ellipse around the giant -----------------------------
    satellites = sorted(
        [c for c in clusters if not c["giant"] and any(m in nodes for m in c["org_ids"])],
        key=lambda c: c["size"], reverse=True,
    )
    n_sat = max(1, len(satellites))
    rx, ry = NET_W * 0.40, NET_H * 0.40
    for si, cluster in enumerate(satellites):
        members, index, pos, extent = local_layout(cluster, si + 1, 0.58)
        max_r = max((radius(m) for m in members), default=0)
        node_tgt = 15.0 * math.sqrt(len(members)) + max_r
        scale = node_tgt / extent
        ang = -math.pi / 2 + (si + 0.35) * 2.0 * math.pi / n_sat
        cx = gcx + rx * math.cos(ang)
        cy = gcy + ry * math.sin(ang)
        cx = min(max(cx, node_tgt + 8), NET_W - node_tgt - 8)
        cy = min(max(cy, node_tgt + 8), NET_H - node_tgt - 8)
        for m in members:
            x0, y0 = pos[index[m]]
            layout[m] = (x0 * scale + cx, y0 * scale + cy, radius(m))
        centroids[cluster["id"]] = (cx, cy)

    return {"nodes": layout, "centroids": centroids, "smin": smin, "smax": smax}


# ---------------------------------------------------------------------------
# SVG scene
# ---------------------------------------------------------------------------


def _esc(value) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _n(value) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "0"


def _pretty(value: str) -> str:
    return str(value).replace("_", " ")


def _people_line(edge: dict, limit: int = 3) -> str:
    names = [p["name"] for p in edge["people"]]
    shown = names[:limit]
    line = " · ".join(shown)
    if len(names) > limit:
        line += f" +{len(names) - limit}"
    return line


def build_scene(payload: dict, lay: dict) -> str:
    layout = lay["nodes"]
    nodes = {n["id"]: n for n in payload["nodes"] if n["in_network"]}
    edges = [e for e in payload["edges"] if e["in_network"]]
    clusters = payload["clusters"]
    cluster_of = {m: c["id"] for c in clusters for m in c["org_ids"]}
    giant_id = clusters[0]["id"] if clusters else None

    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {NET_W} {NET_H}" '
        f'width="{NET_W}" height="{NET_H}" role="img" '
        f'aria-label="Network of organisations linked by shared board members">'
    )
    parts.append("<defs>")
    parts.append(
        f'<pattern id="dots" width="22" height="22" patternUnits="userSpaceOnUse">'
        f'<circle cx="1.4" cy="1.4" r="1.1" fill="rgba(28,21,16,.06)"/></pattern>'
    )
    parts.append("</defs>")
    parts.append(f'<rect x="0" y="0" width="{NET_W}" height="{NET_H}" fill="url(#dots)"/>')

    # --- edges --------------------------------------------------------------
    def edge_style(e: dict, is_giant: bool) -> tuple[str, float]:
        """Edges are the data colour (orange family). The giant cluster's links
        read a touch stronger than the satellites'."""
        w = e["shared"]
        width = 1.1 + (w - 2) * 1.55
        if is_giant:
            alpha = min(0.62, 0.28 + 0.10 * w)
            return f"rgba(191,107,40,{alpha:.2f})", width
        alpha = min(0.48, 0.18 + 0.07 * w)
        return f"rgba(191,107,40,{alpha:.2f})", width * 0.9

    def curve(e: dict) -> str:
        x1, y1, _ = layout[e["source_id"]]
        x2, y2, _ = layout[e["target_id"]]
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        dx, dy = x2 - x1, y2 - y1
        length = math.hypot(dx, dy) or 1.0
        bend = min(16.0, length * 0.14)
        ox, oy = -dy / length * bend, dx / length * bend
        return f"M{x1:.1f},{y1:.1f} Q{mx + ox:.1f},{my + oy:.1f} {x2:.1f},{y2:.1f}"

    # satellites first so the giant cluster reads on top
    for e in sorted(edges, key=lambda e: cluster_of.get(e["source_id"]) == giant_id):
        is_giant = cluster_of.get(e["source_id"]) == giant_id
        color, width = edge_style(e, is_giant)
        parts.append(
            f'<path d="{curve(e)}" fill="none" stroke="{color}" '
            f'stroke-width="{width:.2f}" stroke-linecap="round"><title>'
            f'{_esc(e["source_name"])} \u2192 {_esc(e["target_name"])}: '
            f'{e["shared"]} shared</title></path>'
        )

    # --- annotation callouts for the strongest bridges ----------------------
    # Cluster captions are placed first so the callouts (and then the node
    # labels) can steer clear of them instead of painting over them.
    cluster_labels = place_cluster_labels(payload, lay)
    ann = plan_annotations(payload, lay, [cl["box"] for cl in cluster_labels])

    # --- nodes --------------------------------------------------------------
    satellite_ids = [m for m in layout if cluster_of.get(m) != giant_id]
    for oid in sorted(satellite_ids, key=lambda m: layout[m][2]):
        x, y, r = layout[oid]
        parts.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}" fill="#bf6b28" '
            f'stroke="#f9f8f4" stroke-width="1.6"/>'
        )
    for oid in sorted(layout, key=lambda m: layout[m][2]):
        if cluster_of.get(oid) == giant_id:
            x, y, r = layout[oid]
            parts.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}" fill="#bf6b28" '
                f'stroke="#f9f8f4" stroke-width="1.8"/>'
            )

    # --- labels (placed to avoid overlaps) ---------------------------------
    labels = place_labels(
        payload, lay,
        [A["box"] for A in ann] + [cl["box"] for cl in cluster_labels],
    )
    for oid, L in labels.items():
        if L["leader"]:
            dx, dy = L["x"] - L["nx"], L["y"] - L["ny"]
            d = math.hypot(dx, dy) or 1.0
            sx = L["nx"] + dx / d * (L["r"] + 1.0)
            sy = L["ny"] + dy / d * (L["r"] + 1.0)
            parts.append(
                f'<path d="M{sx:.1f},{sy:.1f} L{L["x"]:.1f},{L["y"] - 3:.1f}" '
                f'stroke="rgba(28,21,16,.28)" stroke-width="0.8" fill="none"/>'
            )
        cls = "nlabel giant" if cluster_of.get(oid) == giant_id else "nlabel"
        parts.append(
            f'<text x="{L["x"]:.1f}" y="{L["y"]:.1f}" text-anchor="{L["anchor"]}" '
            f'class="{cls}">{_esc(L["text"])}</text>'
        )

    # --- cluster labels (drawn on top) -------------------------------------
    for cl in cluster_labels:
        parts.append(
            f'<text x="{cl["x"]:.1f}" y="{cl["y"]:.1f}" text-anchor="middle" '
            f'class="cluster-label{" giant" if cl["giant"] else ""}">{_esc(cl["text"])}</text>'
        )

    # --- bridge annotations -------------------------------------------------
    for A in ann:
        parts.append(
            f'<path d="M{A["mx"]:.1f},{A["my"]:.1f} L{A["x"]:.1f},{A["y"] - 3:.1f}" '
            f'stroke="rgba(156,74,18,.6)" stroke-width="1" stroke-dasharray="3 3" fill="none"/>'
        )
        parts.append(f'<circle cx="{A["mx"]:.1f}" cy="{A["my"]:.1f}" r="2.8" fill="#9c4a12"/>')
        x0, y0, x1, y1 = A["box"]
        parts.append(
            f'<rect x="{x0 - 3:.1f}" y="{y0 - 1:.1f}" width="{x1 - x0 + 6:.1f}" '
            f'height="{y1 - y0 + 2:.1f}" rx="3" fill="rgba(249,248,244,.86)"/>'
        )
        parts.append(
            f'<text x="{A["x"]:.1f}" y="{A["y"]:.1f}" text-anchor="{A["anchor"]}" '
            f'class="bridge-label">{_esc(A["text"])}</text>'
        )

    parts.append("</svg>")
    return "".join(parts)


def plan_annotations(payload: dict, lay: dict, extra_boxes: list | None = None) -> list:
    """Pick the strongest bridges and a collision-free spot for their labels."""
    layout = lay["nodes"]
    edges = [e for e in payload["edges"] if e["in_network"]]
    clusters = payload["clusters"]
    cluster_of = {m: c["id"] for c in clusters for m in c["org_ids"]}
    circles = [(x, y, r) for (x, y, r) in layout.values()]
    leaders = {n["id"]: n["leaders"] for n in payload["nodes"]}
    occupied: list = list(extra_boxes or [])

    selected, seen = [], set()
    for e in sorted(
        edges,
        key=lambda e: (-e["shared"],
                       -(leaders.get(e["source_id"], 0) + leaders.get(e["target_id"], 0))),
    ):
        key = frozenset(p["name"] for p in e["people"])
        if key in seen:
            continue
        seen.add(key)
        selected.append(e)
        if len(selected) >= 2:
            break

    out = []
    for i, e in enumerate(selected):
        x1, y1, _ = layout[e["source_id"]]
        x2, y2, _ = layout[e["target_id"]]
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        text = _people_line(e, limit=2)
        ref = lay["centroids"].get(cluster_of.get(e["source_id"]), (NET_W / 2, NET_H / 2))
        dx, dy = mx - ref[0], my - ref[1]
        d = math.hypot(dx, dy) or 1.0
        ux, uy = dx / d, dy / d
        placed = None
        for dist in (52.0, 76.0, 102.0, 132.0, 166.0):
            lx = min(max(mx + ux * dist, 60), NET_W - 60)
            ly = min(max(my + uy * dist, 16), NET_H - 8)
            box = _text_box(text, lx, ly, "middle")
            if box[0] < 4 or box[2] > NET_W - 4 or box[1] < 6 or box[3] > NET_H - 2:
                continue
            if any(_box_hits_circle(box, px, py, pr) for (px, py, pr) in circles):
                continue
            if any(_boxes_overlap(box, ob) for ob in occupied):
                continue
            if any(_boxes_overlap(box, o["box"]) for o in out):
                continue
            placed = (lx, ly, box)
            break
        if placed is None:
            lx = min(max(mx, 80), NET_W - 80)
            ly = min(max(my - 40, 16), NET_H - 8)
            placed = (lx, ly, _text_box(text, lx, ly, "middle"))
        lx, ly, box = placed
        out.append({"text": text, "x": lx, "y": ly, "anchor": "middle",
                    "box": box, "mx": mx, "my": my})
    return out


def _text_box(text: str, x: float, y: float, anchor: str, cluster: bool = False) -> tuple:
    # Reserve a little more room than the glyphs need: the label font grew, and
    # the de-collision below must never under-estimate the box.
    w = (len(text) * 11.0 if cluster else len(text) * 6.6) + 2.0
    h = 16.0
    if anchor == "start":
        x0, x1 = x, x + w
    elif anchor == "end":
        x0, x1 = x - w, x
    else:
        x0, x1 = x - w / 2, x + w / 2
    return (x0, y - 12.0, x1, y + 4.0)


def _boxes_overlap(a: tuple, b: tuple, pad: float = 4.0) -> bool:
    return not (a[2] + pad <= b[0] or b[2] + pad <= a[0]
                or a[3] + pad <= b[1] or b[3] + pad <= a[1])


def _box_hits_circle(box: tuple, cx: float, cy: float, r: float) -> bool:
    x0, y0, x1, y1 = box
    nx = min(max(cx, x0), x1)
    ny = min(max(cy, y0), y1)
    return math.hypot(cx - nx, cy - ny) < r + 1.5


def place_cluster_labels(payload: dict, lay: dict) -> list:
    """Lift each cluster caption above the nodes it happens to sit on."""
    layout = lay["nodes"]
    clusters = payload["clusters"]
    circles = [(x, y, r) for (x, y, r) in layout.values()]
    out: list = []
    for c in clusters:
        members = [m for m in c["org_ids"] if m in layout]
        if not members:
            continue
        cx = lay["centroids"][c["id"]][0]
        top_y = min(layout[m][1] for m in members)
        text = c["label"].upper()
        y = max(15.0, top_y - 16)
        box = _text_box(text, cx, y, "middle", cluster=True)
        # lift the cluster caption above any node it happens to sit on
        for _ in range(8):
            if not any(_box_hits_circle(box, px, py, pr) for (px, py, pr) in circles):
                break
            y -= 13.0
            if y < 15.0:
                y = 15.0
                break
            box = _text_box(text, cx, y, "middle", cluster=True)
        out.append({"text": text, "x": cx, "y": y, "giant": c["giant"], "box": box})
    return out


def place_labels(payload: dict, lay: dict, extra_boxes: list | None = None) -> dict:
    """Greedy node-label placement: outward preference, then de-collision."""
    layout = lay["nodes"]
    nodes = [n for n in payload["nodes"] if n["in_network"] and n["id"] in layout]
    clusters = payload["clusters"]
    cluster_of = {m: c["id"] for c in clusters for m in c["org_ids"]}
    giant_id = clusters[0]["id"] if clusters else None
    circles = [(x, y, r) for (x, y, r) in layout.values()]
    occupied: list = list(extra_boxes or [])

    labels: dict = {}
    ordered = sorted(
        nodes,
        key=lambda n: (cluster_of.get(n["id"]) != giant_id, -n["shared_people"], n["name"]),
    )
    for n in ordered:
        oid = n["id"]
        x, y, r = layout[oid]
        ref = lay["centroids"].get(cluster_of.get(oid), (NET_W / 2, NET_H / 2))
        if cluster_of.get(oid) == giant_id:
            base = math.atan2(y - ref[1], x - ref[0])
        else:
            # satellites: point labels toward the centre, never off-canvas
            base = 0.0 if x < NET_W / 2 else math.pi
        text = n["short"]
        best = None
        for R in (r + 5.0, r + 12.0, r + 20.0, r + 30.0, r + 42.0, r + 56.0,
                  r + 72.0, r + 90.0):
            for da in (0.0, 0.3, -0.3, 0.62, -0.62, 1.0, -1.0,
                       1.45, -1.45, 1.95, -1.95, 2.6, -2.6, math.pi):
                a = base + da
                ux, uy = math.cos(a), math.sin(a)
                lx, ly = x + ux * R, y + uy * R + 3.2
                if abs(ux) < 0.28:
                    anchor = "middle"
                elif ux > 0:
                    anchor = "start"
                else:
                    anchor = "end"
                box = _text_box(text, lx, ly, anchor)
                if box[0] < 4 or box[2] > NET_W - 4 or box[1] < 6 or box[3] > NET_H - 2:
                    continue
                if any(_boxes_overlap(box, ob) for ob in occupied):
                    continue
                if any(_box_hits_circle(box, px, py, pr)
                       for (px, py, pr) in circles
                       if not (abs(px - x) < 0.5 and abs(py - y) < 0.5)):
                    continue
                best = (lx, ly, anchor, box)
                break
            if best:
                break
        if not best:
            lx = min(max(x + math.cos(base) * (r + 5), 8), NET_W - 8)
            ly = min(max(y + math.sin(base) * (r + 5) + 3.2, 12), NET_H - 4)
            best = (lx, ly, "middle", _text_box(text, lx, ly, "middle"))
        lx, ly, anchor, box = best
        occupied.append(box)
        labels[oid] = {
            "text": text, "x": lx, "y": ly, "anchor": anchor,
            "leader": math.hypot(lx - x, ly - y) > r + 15,
            "nx": x, "ny": y, "r": r,
        }
    return labels


# ---------------------------------------------------------------------------
# Arc atlas — the shared family look for the network cards.
# Nodes sit on a baseline in labelled blocks; every node carries its name.
# Links are elliptical arcs above the baseline, width and opacity = volume.
# ---------------------------------------------------------------------------

ARC_GROUP_COLORS = [
    "#5b8c5a",  # green
    "#b5586b",  # berry
    "#7a6bab",  # violet
    "#4f8a8b",  # teal
    "#a9832f",  # ochre
    "#8a5a3c",  # brown
    "#6d7f9c",  # slate
    "#a35d8a",  # plum
    "#57764f",  # deep green
    "#9c5a4a",  # terracotta
    "#6f8fa8",  # dusty blue
    "#8c7d4a",  # olive
]


def _axesc(v) -> str:
    return (str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _axn(v) -> str:
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return str(v)


def _shelf(items, char_w, min_gap=12.0):
    """items: [(x, width)] -> list of row indices (greedy, left to right)."""
    rows: list = []
    out = []
    for (x, w) in items:
        l, r = x - w / 2, x + w / 2
        row = None
        for ri, occ in enumerate(rows):
            if all(r < ol - min_gap or l > orr + min_gap for (ol, orr) in occ):
                row = ri
                break
        if row is None:
            rows.append([])
            row = len(rows) - 1
        rows[row].append((l, r))
        out.append(row)
    return out


def arc_atlas(nodes, edges, groups, *, width=1512, height=500,
              palette=None, top_links=6, label_size=10.5, max_chars=30,
              radius_range=(2.6, 12.0), arc_widths=(0.7, 4.2),
              arc_alpha=(0.06, 0.52), block_order=None,
              block_label_size=10.2, title="") -> str:
    """nodes:[{id,label,group,value,block?,block_label?}] edges:[{a,b,w}] groups:[(gid,label)]."""
    palette = palette or ARC_GROUP_COLORS
    gorder = {g[0]: i for i, g in enumerate(groups)}
    glabel = {g[0]: g[1] for g in groups}
    gcolor = {g[0]: palette[i % len(palette)] for i, g in enumerate(groups)}
    bo = {b: i for i, b in enumerate(block_order)} if block_order else {}

    def blk(x):
        return x.get("block", x["group"])

    def blabel(x):
        return x.get("block_label", glabel.get(blk(x), blk(x)))

    present = [g for g in groups if any(n["group"] == g[0] for n in nodes)]
    ns = sorted(nodes, key=lambda x: (bo.get(blk(x), 999), gorder.get(blk(x), gorder.get(x["group"], 99)),
                                      gorder.get(x["group"], 99), -x["value"], x["label"]))
    n = len(ns)
    if n == 0:
        return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}"></svg>'
    ids = {x["id"] for x in ns}
    ed = [e for e in edges if e["a"] in ids and e["b"] in ids and e["a"] != e["b"]]
    char_w = 0.545 * label_size

    # --- blocks & x positions ---------------------------------------------
    pad_l = pad_r = 26.0
    gap = 22.0
    blocks = []
    for x in ns:
        if not blocks or blocks[-1][0] != blk(x):
            blocks.append((blk(x), []))
        blocks[-1][1].append(x)
    avail = width - pad_l - pad_r - gap * max(0, len(blocks) - 1)
    slot = avail / n
    xs: dict = {}
    x = pad_l
    for _, members in blocks:
        for node in members:
            xs[node["id"]] = x + slot / 2
            x += slot
        x += gap

    # --- block-label shelf (staggered rows) --------------------------------
    bl_info = []
    bl_widths = []
    for bid, members in blocks:
        xl = xs[members[0]["id"]] - slot / 2
        xr = xs[members[-1]["id"]] + slot / 2
        label = str(blabel(members[0]))
        bw = len(label) * (block_label_size * 0.60 + 1.2) + 6
        bl_info.append((bid, members, xl, xr, label, (xl + xr) / 2, bw))
        bl_widths.append(((xl + xr) / 2, bw))
    bl_rows = _shelf(bl_widths, char_w)
    max_row = max(bl_rows) if bl_rows else 0

    # --- vertical budget ---------------------------------------------------
    trunc = [_axesc(x["label"])[:max_chars] for x in ns]
    lmax = max(len(t) for t in trunc) * char_w
    base_y = height - (lmax + 46.0 + max_row * 14.0)
    label_start = base_y + 16.0
    group_rule_y = label_start + lmax + 11.0
    group_label_y = group_rule_y + 13.0
    arc_max_h = max(40.0, base_y - 18.0)

    vmax = max((x["value"] for x in ns), default=1) or 1
    rmin, rmax = radius_range
    wmax = max((e["w"] for e in ed), default=1) or 1
    wmin_a, wmax_a = arc_widths
    amin, amax = arc_alpha

    P = []
    P.append(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
             f'width="{width}" height="{height}" role="img" aria-label="{_axesc(title)}">')
    P.append("<defs>")
    P.append(f'<pattern id="axdots" width="22" height="22" patternUnits="userSpaceOnUse">'
             f'<circle cx="1.4" cy="1.4" r="1.1" fill="rgba(28,21,16,.055)"/></pattern>')
    P.append("</defs>")
    P.append(f'<rect x="0" y="0" width="{width}" height="{height}" fill="url(#axdots)"/>')

    # --- block bands, rules, labels ---------------------------------------
    for (bid, members, xl, xr, label, cx, bw), row in zip(bl_info, bl_rows):
        gset = {m["group"] for m in members}
        col = gcolor[next(iter(gset))] if len(gset) == 1 else "#8a8278"
        P.append(f'<rect x="{xl:.1f}" y="{label_start - 8:.1f}" width="{max(1.0, xr - xl):.1f}" '
                 f'height="{group_rule_y - label_start + 8:.1f}" fill="{col}" '
                 f'opacity="{0.07 if len(gset) == 1 else 0.05:.2f}"/>')
        P.append(f'<path d="M{xl:.1f},{group_rule_y:.1f} L{xr:.1f},{group_rule_y:.1f}" '
                 f'stroke="{col}" stroke-width="2.2" opacity="0.8"/>')
        P.append(f'<text x="{cx:.1f}" y="{group_label_y + row * 14.0:.1f}" text-anchor="middle" '
                 f'font-family="DM Sans, sans-serif" font-size="{block_label_size}" '
                 f'font-weight="700" letter-spacing="1.1" '
                 f'fill="{col if len(gset) == 1 else "#6f675d"}">{_axesc(label.upper())}</text>')

    # --- arcs --------------------------------------------------------------
    for e in sorted(ed, key=lambda e: e["w"]):
        x1, x2 = xs[e["a"]], xs[e["b"]]
        if x1 > x2:
            x1, x2 = x2, x1
        dx = x2 - x1
        if dx <= 0.1:
            continue
        rx = dx / 2.0
        ry = min(dx / 2.0, arc_max_h)
        t = math.log1p(e["w"]) / math.log1p(wmax)
        alpha = amin + (amax - amin) * t
        swid = wmin_a + (wmax_a - wmin_a) * t
        P.append(f'<path d="M{x1:.1f},{base_y:.1f} A{rx:.1f},{ry:.1f} 0 0 1 {x2:.1f},{base_y:.1f}" '
                 f'fill="none" stroke="rgba(191,107,40,{alpha:.3f})" stroke-width="{swid:.2f}" '
                 f'stroke-linecap="round"/>')

    # --- top-link callouts on a clean top shelf ---------------------------
    lo = {x["id"]: x for x in ns}
    ngroup = {x["id"]: x["group"] for x in ns}
    cross = [e for e in ed if ngroup[e["a"]] != ngroup[e["b"]]]
    pool = cross if len(cross) >= top_links else ed
    annotated = []
    for e in sorted(pool, key=lambda e: -e["w"])[:top_links]:
        x1, x2 = xs[e["a"]], xs[e["b"]]
        if x1 > x2:
            x1, x2 = x2, x1
        rx = (x2 - x1) / 2.0
        ry = min(rx, arc_max_h)
        mx = (x1 + x2) / 2.0
        text = f'{lo[e["a"]]["label"]} \u00d7 {lo[e["b"]]["label"]} \u00b7 {_axn(e["w"])}'
        annotated.append((mx, base_y - ry - 5.0, text, e["w"]))
    annotated.sort(key=lambda a: a[0])
    rows: list = []
    for (mx, apex, text, w) in annotated:
        tw = len(text) * char_w + 10
        row = 0
        while row < len(rows):
            if all(mx - tw / 2 > r + 12 or mx + tw / 2 < l - 12 for (l, r) in rows[row]):
                break
            row += 1
        if row == len(rows):
            rows.append([])
        rows[row].append((mx - tw / 2, mx + tw / 2))
        ly = 16.0 + row * 17.0
        lx = min(max(mx, tw / 2 + 4), width - tw / 2 - 4)
        P.append(f'<line x1="{lx:.1f}" y1="{ly + 4:.1f}" x2="{mx:.1f}" y2="{apex:.1f}" '
                 f'stroke="rgba(191,107,40,.45)" stroke-width="0.9" stroke-dasharray="2 3"/>')
        P.append(f'<rect x="{lx - tw / 2:.1f}" y="{ly - 10.5:.1f}" width="{tw:.1f}" '
                 f'height="14.0" rx="3" fill="rgba(249,248,244,.92)" '
                 f'stroke="rgba(191,107,40,.28)" stroke-width="0.8"/>')
        P.append(f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="middle" '
                 f'font-family="DM Sans, sans-serif" font-size="9.6" font-weight="700" '
                 f'fill="#bf6b28">{_axesc(text)}</text>')

    # --- baseline, nodes, labels ------------------------------------------
    P.append(f'<line x1="{pad_l - 8:.1f}" y1="{base_y:.1f}" x2="{width - pad_r + 8:.1f}" '
             f'y2="{base_y:.1f}" stroke="rgba(28,21,16,.22)" stroke-width="1"/>')
    for node in ns:
        x = xs[node["id"]]
        r = rmin + (rmax - rmin) * math.sqrt(node["value"] / vmax)
        col = gcolor.get(node["group"], "#8a8278")
        P.append(f'<line x1="{x:.1f}" y1="{base_y:.1f}" x2="{x:.1f}" y2="{label_start:.1f}" '
                 f'stroke="rgba(28,21,16,.28)" stroke-width="0.8"/>')
        P.append(f'<circle cx="{x:.1f}" cy="{base_y:.1f}" r="{r:.1f}" fill="{col}" '
                 f'stroke="#f9f8f4" stroke-width="1.6"/>')
        t = _axesc(node["label"][:max_chars])
        P.append(f'<text x="{x:.1f}" y="{label_start + 5:.1f}" '
                 f'transform="rotate(90 {x:.1f} {label_start + 5:.1f})" '
                 f'font-family="DM Sans, sans-serif" font-size="{label_size}" '
                 f'font-weight="500" fill="#1c1510" stroke="#f9f8f4" stroke-width="2.6" '
                 f'paint-order="stroke" stroke-linejoin="round">{t}</text>')

    P.append("</svg>")
    return "".join(P)


# ---------------------------------------------------------------------------
# Card HTML
# ---------------------------------------------------------------------------


HTML_HEAD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>HumansMap interlocking directorates</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;800&family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:#f9f8f4; --text:#1c1510; --accent:#1b4d8e; --person:#bf6b28;
    --person-deep:#9c4a12;
    --muted:#8a8278; --surface:#ece8de; --line:rgba(28,21,16,.10);
    --display:'Playfair Display', Georgia, serif; --body:'DM Sans', system-ui, sans-serif;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:#d9d4c9; font-family:var(--body); padding:32px; }
  .card { width:1600px; height:1000px; background:var(--bg); color:var(--text);
    padding:24px 44px 12px; display:flex; flex-direction:column; position:relative;
    overflow:hidden; }
  .card header { display:flex; justify-content:space-between; align-items:baseline; }
  .eyebrow { font:700 15px/1 var(--body); letter-spacing:.2em; text-transform:uppercase;
    color:var(--accent); }
  .brand { font:700 27px/1 var(--display); color:var(--accent); }
  h1 { font:800 42px/1 var(--display); margin:13px 0 0; letter-spacing:-.012em;
    max-width:40ch; }
  h1 em { font-style:normal; color:var(--person); }
  .deck { font:400 16px/1.45 var(--body); color:var(--muted); margin:6px 0 0;
    max-width:1500px; }
  .deck b { color:var(--text); font-weight:700; }
  .net { margin-top:2px; position:relative; }
  .net svg { display:block; width:1512px; height:470px; overflow:visible; }
  .band { margin-top:auto; border-top:2px solid var(--text); padding-top:7px;
    display:grid; grid-template-columns:430px 1fr; gap:32px; }
  .plabel { font:700 11.5px/1 var(--body); letter-spacing:.17em; text-transform:uppercase;
    color:var(--muted); margin-bottom:8px; }
  .read { display:flex; flex-direction:column; gap:6px; font:400 13.2px/1.35 var(--body);
    color:var(--muted); }
  .read .row { display:flex; align-items:center; gap:9px; }
  .swatch { flex:0 0 auto; }
  .blockkey { display:grid; grid-template-columns:1fr 1fr; gap:4px 14px; margin-top:9px;
    font:400 12px/1.3 var(--body); color:var(--muted); }
  .bk { display:flex; align-items:center; gap:6px; }
  .bk i { width:10px; height:10px; border-radius:3px; flex:0 0 auto; }
  .stats { display:grid; grid-template-columns:1fr 1fr; gap:8px 20px; margin-top:11px; }
  .stat .v { font:800 25px/1 var(--display); }
  .stat .k { font:500 11.2px/1.3 var(--body); color:var(--muted); text-transform:uppercase;
    letter-spacing:.07em; margin-top:3px; }
  ul.bridges { list-style:none; margin:0; padding:0; columns:2; column-gap:30px; }
  ul.bridges li { break-inside:avoid; padding:3px 0; border-bottom:1px solid var(--line); }
  .bhead { display:flex; justify-content:space-between; gap:10px; align-items:baseline; }
  .bname { font:600 14px/1.25 var(--body); }
  .bname b { color:var(--text); }
  .bcount { font:800 15px/1 var(--display); color:var(--person); white-space:nowrap; }
  .bppl { font:400 12.2px/1.3 var(--body); color:var(--muted); margin-top:2px; }
  .caveat { font:400 12.2px/1.4 var(--body); color:var(--muted); margin-top:10px;
    padding-top:8px; border-top:1px solid var(--line); }
  .caveat b { color:var(--text); }
  .card footer { margin-top:6px; display:flex; justify-content:space-between;
    align-items:baseline; font:400 13px/1.4 var(--body); color:var(--muted); }
</style></head>
<body>"""


def render_html(payload: dict) -> str:
    scale = payload["scale"]
    probe = payload["probe"]
    giant = payload["clusters"][0] if payload["clusters"] else {"size": 0, "edges": 0}
    p2 = int(probe["pairs_by_shared"].get("2", 0))
    p3plus = sum(v for k, v in probe["pairs_by_shared"].items() if int(k) >= 3)

    # --- arc atlas: blocks are the clusters, colour is the organisation type --
    net_nodes = [n for n in payload["nodes"] if n.get("in_network")]
    cshort = {c["id"]: c["label"].replace("The boardroom network", "Boardroom network")
              .replace(" \u00b7 ", " ") for c in payload["clusters"]}
    tmap = {"company": "Company", "organization": "Organisation (generic)",
            "nonprofit": "Foundation or nonprofit"}
    tnodes = [{"id": n["id"], "label": n["short"], "group": n["type"],
               "block": n["cluster"],
               "block_label": cshort.get(n["cluster"], n["cluster"]),
               "value": max(1, n["shared_people"])} for n in net_nodes]
    tgroups = [(k, tmap[k]) for k in ("company", "organization", "nonprofit")
               if any(n["group"] == k for n in tnodes)]
    scene = arc_atlas(
        tnodes,
        [{"a": e["source_id"], "b": e["target_id"], "w": e["shared"]}
         for e in payload["edges"] if e.get("in_network")],
        tgroups, width=1512, height=470, label_size=10.2, max_chars=20, top_links=6,
        arc_alpha=(0.10, 0.55), arc_widths=(0.8, 4.2),
        block_order=[c["id"] for c in payload["clusters"]],
        title="Organisations linked by people who sit on their boards",
    )
    type_key = "".join(
        f'<span class="bk"><i style="background:'
        f'{ARC_GROUP_COLORS[i % len(ARC_GROUP_COLORS)]}"></i>{_esc(lbl)}</span>'
        for i, (_k, lbl) in enumerate(tgroups)
    )

    # strongest bridges across genuinely separate organisations, one per
    # distinct set of shared people (family groups otherwise repeat).
    leaders = {n["id"]: n["leaders"] for n in payload["nodes"]}
    seen: set = set()
    bridges: list = []
    for e in sorted(
        payload["edges"],
        key=lambda e: (-e["shared"],
                       -(leaders.get(e["source_id"], 0) + leaders.get(e["target_id"], 0))),
    ):
        if e["same_lineage"]:
            continue
        key = frozenset(p["name"] for p in e["people"])
        if key in seen:
            continue
        seen.add(key)
        bridges.append(e)
        if len(bridges) >= 4:
            break

    def bridge_li(e: dict) -> str:
        names = e["people"]
        shown = " · ".join(p["name"] for p in names[:4])
        if len(names) > 4:
            shown += f" +{len(names) - 4}"
        return (
            f'<li><div class="bhead"><span class="bname">'
            f'<b>{_esc(SHORT_NAME.get(e["source_name"], e["source_name"]))}</b> '
            f'&harr; {_esc(SHORT_NAME.get(e["target_name"], e["target_name"]))}</span>'
            f'<span class="bcount">{e["shared"]}</span></div>'
            f'<div class="bppl">{_esc(shown)}</div></li>'
        )

    if p3plus:
        headline = (
            f"The largest boardroom interlock: a chain of <em>{giant['size']} organisations</em> "
            f"linked by shared directors."
        )
    else:
        headline = (
            f"<em>{scale['interlocking_pairs']}</em> pairs of organisations share "
            f"more than one person."
        )

    deck = (
        f"Who sits on whose board. Two organisations are linked when they share "
        f"<b>&ge;{payload['min_shared']} of the same people</b>, drawn from Wikidata&rsquo;s "
        f"<b>{_n(scale['leadership_edges'])}</b> board, chair and CEO records. Each dot "
        f"is an organisation, sized by how many of its people are shared; each arc joins "
        f"two organisations and its <b>thickness is how many people they share</b>. "
        f"Organisations are clustered into the <b>{giant['size']}-organisation "
        f"boardroom network</b> and the smaller corporate groups beside it."
    )

    type_parts = ", ".join(
        f"{_n(v)} {ROLE_PLURAL.get(k, _pretty(k))}"
        for k, v in sorted(payload["type_counts"].items(), key=lambda kv: -kv[1])
    )

    html = f"""<section class="card">
  <header>
    <span class="eyebrow">Shared board members</span>
    <span class="brand">Humans&nbsp;Map</span>
  </header>
  <h1>{headline}</h1>
  <p class="deck">{deck}</p>
  <div class="net">{scene}</div>
  <div class="band">
    <div>
      <div class="plabel">How to read it</div>
      <div class="read">
        <div class="row"><svg class="swatch" width="30" height="30"><circle cx="10" cy="15" r="4" fill="#8a8278" stroke="#f9f8f4" stroke-width="1.5"/><circle cx="22" cy="15" r="9" fill="#8a8278" stroke="#f9f8f4" stroke-width="1.5"/></svg>
          <span>every dot is an organisation; <b>size</b> = shared people</span></div>
        <div class="row"><svg class="swatch" width="30" height="14"><path d="M1,4 L29,4" stroke="rgba(191,107,40,.8)" stroke-width="4.5"/><path d="M1,10 L29,10" stroke="rgba(191,107,40,.28)" stroke-width="1.1"/></svg>
          <span>every arc joins two boards; <b>thicker = more</b> shared people</span></div>
      </div>
      <div class="plabel" style="margin:11px 0 5px">Colour = organisation type</div>
      <div class="blockkey">{type_key}</div>
      <div class="stats">
        <div class="stat"><div class="v">{_n(scale['interlocking_pairs'])}</div><div class="k">linked pairs</div></div>
        <div class="stat"><div class="v">{_n(scale['network_clusters'])}</div><div class="k">groups of 3+ organisations</div></div>
        <div class="stat"><div class="v">{_n(scale['people_on_2plus_orgs'])}</div><div class="k">people with &ge;2 board, chair or CEO roles</div></div>
        <div class="stat"><div class="v">{_n(scale['leadership_edges'])}</div><div class="k">board, chair &amp; CEO records</div></div>
      </div>
    </div>
    <div>
      <div class="plabel">Four strongest links between separate organisations</div>
      <ul class="bridges">{''.join(bridge_li(e) for e in bridges)}</ul>
      <div class="caveat">
        <b>This is Wikidata&rsquo;s board coverage, and it is uneven.</b> Every board
        seat, chair and chief-executive role recorded in the data was scanned
        ({type_parts}), but well-documented organisations are over-represented, and the
        biggest overlaps in the data are corporate successions and renames (for example
        Hewlett-Packard &harr; HP Inc., 13 people, and Kongsberg V&aring;penfabrikk &harr;
        Kongsberg Gruppen), which are flagged and excluded from this list. Counted by
        distinct people: {p2} pairs share 2, {p3plus} share 3 or more.
      </div>
    </div>
  </div>
  <footer>
    <span>Data: Wikidata (CC BY-SA) &middot; humansmap.com</span>
    <span>{_n(scale['network_nodes'])} organisations drawn &middot; {_n(scale['network_edges'])} drawn links</span>
  </footer>
</section>"""

    return HTML_HEAD + html + "\n</body></html>\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default="docs/marketing/boards")
    parser.add_argument("--min-shared", type=int, default=2)
    parser.add_argument("--min-cluster-size", type=int, default=3)
    parser.add_argument("--max-clusters", type=int, default=30)
    parser.add_argument("--include-owner", action="store_true")
    parser.add_argument("--from-json", default=None,
                        help="re-render the HTML from an existing boards.json")
    parser.add_argument("--no-html", action="store_true")
    args = parser.parse_args()

    types = list(CORE_TYPES)
    if args.include_owner:
        types.append(OWNER_TYPE)

    if args.from_json:
        payload = json.loads(Path(args.from_json).read_text())
    else:
        db = get_db(get_arango_client(request_timeout=600))
        payload = collect(db, types, args.min_shared, args.min_cluster_size,
                          args.max_clusters)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if not args.from_json:
        json_path = out_dir / "boards.json"
        json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        print(f"wrote {json_path}")

    if not args.no_html:
        html_path = out_dir / "boards.html"
        html_path.write_text(render_html(payload))
        print(f"wrote {html_path}")

    print(json.dumps({
        "edge_types": types,
        "scale": payload["scale"],
        "probe": payload["probe"],
        "top_pairs": [
            {"a": p["a"], "b": p["b"], "shared": p["shared"],
             "same_lineage": p["same_lineage"]}
            for p in payload["top_pairs"][:12]
        ],
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
