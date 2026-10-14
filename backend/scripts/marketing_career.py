#!/usr/bin/env python3
"""Marketing career miner — read-only, emits the "career adjacency network" card.

The insight: occupations are linked when the *same people* held both. Nodes are
occupations, node size is how many people held that occupation, and an edge's
weight is the number of people who held both. The graph is a map of which
careers travel together — and which pairs (scientist x philosopher,
actor x singer, architect x engineer) bridge otherwise separate worlds.

It never writes to ArangoDB. The heavy aggregation is **one bounded pass** over
``occupations[]``:

  * ``wikidata_statements_count`` has a persistent index, so the statement floor
    (``--min-statements``, default 50) is what bounds the scan on the live DB.
    ``--scan-limit`` (default 400000) is a hard ceiling; at the default floor it
    does not bite (about 156k people qualify).
  * For every person with two or more occupations, ``occupations[]`` is UNNESTed
    against itself **once** and every unordered pair ``a <= b`` is counted. The
    diagonal (``a == b``) is an occupation's people count; the off-diagonal
    (``a < b``) is the co-occurrence count. Only the diagonal and pairs with
    ``--min-pair`` shared people are returned, so the tens of millions of
    unnested rows never leave the database.
  * A light companion count (same statement floor, no unnest) returns how many
    people were considered.

Node sizes and edge weights therefore come from the same bounded pass and the
same population, so they are directly comparable.

Display clustering is by broad field (hand-curated, like the gender card's
curated field list); the edge set is entirely data-driven. Occupation recording
is uneven and multi-valued in Wikidata, so common occupations dominate — see the
card caveat.

Outputs (both written by default):

  career.json   machine-readable payload: nodes, edges, fields, bridges, scale
  career.html   brand-styled card sheet (1600x1000) to screenshot to career.png

Run it manually inside the backend container:

    docker compose exec -T backend python - --out-dir /tmp/career \\
        < backend/scripts/marketing_career.py
    docker compose exec -T backend cat /tmp/career/career.json \\
        > docs/marketing/career/career.json
    docker compose exec -T backend cat /tmp/career/career.html \\
        > docs/marketing/career/career.html

Flags:
    --out-dir PATH       output directory (default: docs/marketing/career)
    --min-statements N   Wikidata-statement floor, the notability bound (default 50)
    --min-pair N         shared-people floor for an edge (default 20)
    --scan-limit N       max people fed into the aggregation (default 400000)
    --max-nodes N        cap on occupations kept by people count (default 130)
    --node-floor N       min people for an occupation to be drawn (default 100)
    --from-json PATH     re-render the HTML from an existing career.json
    --no-html            emit only the JSON payload
"""

from __future__ import annotations

import argparse
import json
import math
import random
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

DEFAULT_MIN_STATEMENTS = 50
DEFAULT_MIN_PAIR = 20
DEFAULT_SCAN_LIMIT = 400_000
DEFAULT_MAX_NODES = 130
DEFAULT_NODE_FLOOR = 100
DEFAULT_MAX_EDGES = 6000

# Edges below this many shared people are drawn as faint grey structure rather
# than as orange signal. Above it, orange opacity/width ramp with the count.
STRONG_EDGE = 400
DRAW_EDGE_MIN = 100  # weaker edges stay in the JSON but are not drawn

# ---------------------------------------------------------------------------
# Queries — bounded by the statement floor (persistent index) and scan limit.
# ---------------------------------------------------------------------------

BASE = """
FOR p IN persons
  FILTER p.wikidata_statements_count >= @min_statements
    AND LENGTH(p.occupations) >= 2
  LIMIT @scan_limit
"""

CELLS_AQL = BASE + """
  FOR a IN p.occupations
    FOR b IN p.occupations
      FILTER a <= b
      COLLECT oa = a, ob = b WITH COUNT INTO n
      FILTER oa == ob OR n >= @min_pair
      RETURN {a: oa, b: ob, count: n}
"""

# Same bounded population, no unnest — used only for the sample line.
PEOPLE_AQL = """
RETURN LENGTH(
""" + BASE + """
    RETURN 1
)
"""

# ---------------------------------------------------------------------------
# Display fields — the clusters the card draws. Hand-curated (the edge data is
# not); every drawn occupation belongs to exactly one field. Order sets the
# left-to-right shelf order so related fields sit next to each other.
# ---------------------------------------------------------------------------

FIELD_SPEC = [
    ("letters", "Letters & ideas", [
        "writer", "poet", "journalist", "essayist", "historian", "philosopher",
        "publisher", "teacher", "university_teacher", "sociologist",
        "political_scientist", "economist", "clergyman", "activist",
        "consultant", "podcaster",
    ]),
    ("screen", "Screen & stage", [
        "actor", "film_actor", "television_actor", "voice_actor", "screenwriter",
        "film_director", "film_producer", "theatre_director", "model",
        "television_presenter", "fashion_designer",
    ]),
    ("music", "Music", [
        "composer", "musician", "singer", "pianist", "conductor", "guitarist",
        "record_producer", "keyboardist",
    ]),
    ("art", "Visual art & design", [
        "painter", "photographer", "sculptor", "printmaker", "architect",
    ]),
    ("science", "Science & medicine", [
        "mathematician", "physicist", "chemist", "botanist", "zoologist",
        "biologist", "scientist", "engineer", "psychiatrist",
    ]),
    ("power", "Power & business", [
        "politician", "lawyer", "diplomat", "military_person", "military_officer",
        "businessperson", "judge", "entrepreneur", "banker", "civil_servant",
        "philanthropist", "trade_unionist", "public_official", "financier",
        "executive", "investor", "lobbyist", "intelligence_officer", "astronaut",
    ]),
    ("sport", "Sport", [
        "football_player", "football_manager", "basketball_player",
        "american_football_player", "boxer", "baseball_player", "tennis_player",
    ]),
]

FIELD_ORDER = [f[0] for f in FIELD_SPEC]

# Which bridges the card annotates (3-5). Chosen from the computed cross-field
# bridge list for a mix of scale and surprise; if one is below the floor the
# renderer silently omits it.
FEATURED_BRIDGES = [
    ("actor", "singer"),                 # the biggest cross-world tie
    ("mathematician", "philosopher"),    # the concept's scientist x philosopher
    ("architect", "engineer"),           # art x science, tight for its size
    ("banker", "economist"),             # power x letters, strongest lift
    ("printmaker", "publisher"),         # visual art x letters, tight for its size
]

# ---------------------------------------------------------------------------
# Rendering geometry
# ---------------------------------------------------------------------------

CARD_W = 1600
CARD_H = 1000
NET_W = 1520
NET_H = 500
NODE_R_MIN = 5.0
NODE_R_MAX = 22.0
LABEL_TOP_N = 20  # biggest hubs get a text label


def _rows(db, aql: str, bind: dict) -> list:
    try:
        return list(db.aql.execute(aql, bind_vars=bind))
    except Exception as exc:  # noqa: BLE001 - a marketing job must not crash
        print(f"[warn] query failed: {exc}", file=sys.stderr)
        return []


# ---------------------------------------------------------------------------
# Collection (one heavy bounded pass + two light companions)
# ---------------------------------------------------------------------------


def collect(min_statements: int, min_pair: int, scan_limit: int,
            max_nodes: int, max_edges: int) -> dict:
    db = get_db(get_arango_client(request_timeout=900))
    common = {"min_statements": min_statements, "scan_limit": scan_limit}

    cells = _rows(db, CELLS_AQL, {**common, "min_pair": min_pair})
    people = _rows(db, PEOPLE_AQL, common)

    # The single self-UNNEST returns the diagonal (a == b, an occupation's
    # people count) and the off-diagonal (a < b, a co-occurrence count).
    nodes = {r["a"]: int(r["count"]) for r in cells if r["a"] == r["b"]}
    edges = [OrderedDict(a=r["a"], b=r["b"], count=int(r["count"]))
             for r in cells if r["a"] != r["b"]]

    node_rows = [OrderedDict(occupation=o, people=p)
                 for o, p in sorted(nodes.items(), key=lambda kv: -kv[1])]
    node_rows = node_rows[:max_nodes]
    kept = {n["occupation"] for n in node_rows}
    edges = [e for e in edges if e["a"] in kept and e["b"] in kept]
    edges.sort(key=lambda e: -e["count"])
    edges = edges[:max_edges]

    people_considered = int(people[0]) if people else 0
    occupation_records = sum(int(n["people"]) for n in node_rows)

    return OrderedDict(
        generated=date.today().isoformat(),
        concept="career_adjacency",
        params=OrderedDict(
            min_statements=min_statements,
            min_pair=min_pair,
            scan_limit=scan_limit,
            max_nodes=max_nodes,
            max_edges=max_edges,
            node_floor=DEFAULT_NODE_FLOOR,
            draw_edge_min=DRAW_EDGE_MIN,
            strong_edge=STRONG_EDGE,
        ),
        scale=OrderedDict(
            people_considered=people_considered,
            occupation_records=occupation_records,
            distinct_occupations=len(nodes),
        ),
        nodes=node_rows,
        edges=edges,
    )


# ---------------------------------------------------------------------------
# Analysis: fields, degrees, radii, bridges
# ---------------------------------------------------------------------------

FIELD_OF = {occ: fid for fid, _label, members in FIELD_SPEC for occ in members}
FIELD_LABEL = {fid: label for fid, label, _m in FIELD_SPEC}


def _pretty(occ: str) -> str:
    return (occ or "").replace("_", " ").strip()


def _label(occ: str) -> str:
    s = _pretty(occ)
    return s[:1].upper() + s[1:] if s else s


def _n(value) -> str:
    try:
        return f"{int(round(float(value))):,}"
    except (TypeError, ValueError):
        return "0"


def analyze(payload: dict) -> dict:
    """Derive fields, degrees, node radii, the drawn subset and bridges."""
    node_floor = int(payload["params"].get("node_floor", DEFAULT_NODE_FLOOR))
    min_pair = int(payload["params"].get("min_pair", DEFAULT_MIN_PAIR))
    total_people = int(payload["scale"].get("people_considered") or 0)

    all_people = {n["occupation"]: int(n["people"]) for n in payload["nodes"]}
    drawn = {o: p for o, p in all_people.items()
             if p >= node_floor and o in FIELD_OF}
    if not drawn:
        raise SystemExit("no occupations above --node-floor; lower the floor")
    excluded = [OrderedDict(occupation=o, people=p)
                for o, p in sorted(all_people.items(), key=lambda kv: -kv[1])
                if o not in drawn]

    # degree / weighted degree over the full edge set, then keep edges with both
    # endpoints drawn.
    edges = [e for e in payload["edges"]
             if e["a"] in drawn and e["b"] in drawn and e["count"] >= min_pair]
    degree = defaultdict(int)
    weight = defaultdict(int)
    for e in edges:
        degree[e["a"]] += 1
        degree[e["b"]] += 1
        weight[e["a"]] += e["count"]
        weight[e["b"]] += e["count"]

    ids = sorted(drawn, key=lambda o: (-drawn[o], o))
    pmin = min(drawn.values())
    pmax = max(drawn.values())

    def radius(people: int) -> float:
        if pmax <= pmin:
            return NODE_R_MIN + 6
        t = (people - pmin) / (pmax - pmin)
        return NODE_R_MIN + (NODE_R_MAX - NODE_R_MIN) * math.sqrt(max(0.0, t))

    nodes = OrderedDict()
    for o in ids:
        nodes[o] = OrderedDict(
            occupation=o,
            label=_label(o),
            people=drawn[o],
            field=FIELD_OF[o],
            field_label=FIELD_LABEL[FIELD_OF[o]],
            degree=degree.get(o, 0),
            weight=weight.get(o, 0),
            radius=round(radius(drawn[o]), 2),
        )

    # Bridge candidacy: edges that connect two different fields, ranked by a
    # blend of scale (shared people) and surprise (lift vs. both node sizes).
    def lift(e: dict) -> float:
        denom = drawn.get(e["a"], 0) * drawn.get(e["b"], 0)
        return (e["count"] * total_people / denom) if denom else 0.0

    cross = [e for e in edges if FIELD_OF[e["a"]] != FIELD_OF[e["b"]]]
    for e in cross:
        e["lift"] = lift(e)
        e["score"] = math.sqrt(max(0.0, e["lift"]) * e["count"])
    # "Bridges" are cross-field links ranked by lift (how much more often the
    # pair occurs than the two occupation sizes alone would predict), with a
    # volume floor so one-off oddities do not top the list.
    bridges = sorted([e for e in cross if e["count"] >= 150],
                     key=lambda e: -e["lift"])

    fields = []
    for fid, label, _m in FIELD_SPEC:
        members = [o for o in ids if FIELD_OF[o] == fid]
        if not members:
            continue
        fields.append(OrderedDict(
            id=fid,
            label=label,
            members=members,
            total_people=sum(drawn[o] for o in members),
            nodes=len(members),
        ))
    fields.sort(key=lambda f: FIELD_ORDER.index(f["id"]))

    payload["fields"] = fields
    payload["nodes"] = list(nodes.values())
    payload["edges"] = edges
    payload["excluded_nodes"] = excluded
    payload["bridges"] = [
        OrderedDict(a=e["a"], b=e["b"], a_label=_label(e["a"]),
                    b_label=_label(e["b"]), count=e["count"],
                    lift=round(e["lift"], 2), score=round(e["score"], 1),
                    a_field=FIELD_OF[e["a"]], b_field=FIELD_OF[e["b"]])
        for e in bridges[:40]
    ]
    payload["top_pairs"] = [
        OrderedDict(a=e["a"], b=e["b"], count=e["count"])
        for e in sorted(edges, key=lambda e: -e["count"])[:20]
    ]
    payload["scale"]["nodes_drawn"] = len(nodes)
    payload["scale"]["edges_drawn"] = len(edges)
    payload["scale"]["fields_drawn"] = len(fields)
    payload["scale"]["edges_strong"] = sum(1 for e in edges if e["count"] >= STRONG_EDGE)
    payload["scale"]["max_people"] = pmax
    payload["scale"]["min_people"] = pmin

    # Featured bridges, resolved to drawn edges.
    featured = []
    lookup = {frozenset((e["a"], e["b"])): e for e in edges}
    for a, b in FEATURED_BRIDGES:
        e = lookup.get(frozenset((a, b)))
        if e:
            featured.append(OrderedDict(
                a=a, b=b, a_label=_label(a), b_label=_label(b),
                count=e["count"], lift=round(lift(e), 2),
                a_field=FIELD_OF.get(a), b_field=FIELD_OF.get(b)))
    payload["featured_bridges"] = featured
    return payload


# ---------------------------------------------------------------------------
# Layout: Fruchterman-Reingold inside each field, then shelf-pack the fields
# ---------------------------------------------------------------------------


def _fr(n: int, edge_list: list, iters: int = 420, seed: int = 11) -> list:
    if n <= 1:
        return [[0.0, 0.0]]
    rnd = random.Random(seed)
    pos = []
    for i in range(n):
        a = 2 * math.pi * i / n + rnd.uniform(-0.06, 0.06)
        pos.append([math.cos(a), math.sin(a)])
    k = 1.0 / math.sqrt(n)
    temp = 0.28
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
            strength = min(3.0, math.log10(max(10, w))) / 3.0
            f = d * d / k * (0.35 + 0.65 * strength)
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


def _expand(pos: list, power: float = 0.62) -> list:
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
    nodes = {n["occupation"]: n for n in payload["nodes"]}
    edges = payload["edges"]
    fields = payload["fields"]

    # Grid of field blobs: 4 across, 2 down for seven fields. Each field's
    # force layout is scaled into its own cell, which keeps the clusters
    # separated while the inter-field edges carry the bridges.
    cols = 4 if len(fields) > 4 else max(1, len(fields))
    rows_n = math.ceil(len(fields) / cols)
    cell_w = NET_W / cols
    cell_h = NET_H / rows_n
    gap = 24.0
    fit = min(cell_w, cell_h) / 2.0 - gap

    layout: dict[str, tuple] = {}
    centroids: dict[str, tuple] = {}
    row_counts = [min(cols, len(fields) - r * cols) for r in range(rows_n)]
    for fi, f in enumerate(fields):
        members = [m for m in f["members"] if m in nodes]
        if not members:
            continue
        index = {m: i for i, m in enumerate(members)}
        elist = []
        for e in edges:
            if e["a"] in index and e["b"] in index:
                elist.append((index[e["a"]], index[e["b"]], e["count"]))
        pos = _expand(_fr(len(members), elist, seed=13 + fi), 0.60)
        extent = max((math.hypot(x, y) for x, y in pos), default=1.0) or 1.0
        max_r = max((nodes[m]["radius"] for m in members), default=6.0)
        target = min(fit, max_r + 19.0 * math.sqrt(len(members)))
        scale = target / extent
        r_i, c_i = divmod(fi, cols)
        x_off = (cols - row_counts[r_i]) * cell_w / 2.0
        cx = x_off + (c_i + 0.5) * cell_w
        cy = (r_i + 0.5) * cell_h
        centroids[f["id"]] = (cx, cy)
        for m in members:
            x, y = pos[index[m]]
            layout[m] = (cx + x * scale, cy + y * scale, nodes[m]["radius"])

    # Keep every node centre inside the canvas.
    for m, (x, y2, r) in layout.items():
        layout[m] = (min(max(x, r + 2), NET_W - r - 2),
                     min(max(y2, r + 2), NET_H - r - 2), r)

    return {"nodes": layout, "centroids": centroids}


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


def _text_box(text: str, x: float, y: float, anchor: str,
              size: float = 5.4, height: float = 13.0) -> tuple:
    w = len(text) * size + 3.0
    if anchor == "start":
        x0, x1 = x, x + w
    elif anchor == "end":
        x0, x1 = x - w, x
    else:
        x0, x1 = x - w / 2, x + w / 2
    return (x0, y - height + 3.0, x1, y + 3.0)


def _boxes_overlap(a: tuple, b: tuple, pad: float = 2.4) -> bool:
    return not (a[2] + pad <= b[0] or b[2] + pad <= a[0]
                or a[3] + pad <= b[1] or b[3] + pad <= a[1])


def _box_hits_circle(box: tuple, cx: float, cy: float, r: float) -> bool:
    x0, y0, x1, y1 = box
    nx = min(max(cx, x0), x1)
    ny = min(max(cy, y0), y1)
    return math.hypot(cx - nx, cy - ny) < r + 1.5


def build_scene(payload: dict, lay: dict) -> str:
    layout = lay["nodes"]
    nodes = {n["occupation"]: n for n in payload["nodes"]}
    edges = [e for e in payload["edges"] if e["a"] in layout and e["b"] in layout]
    draw = [e for e in edges if e["count"] >= DRAW_EDGE_MIN]

    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {NET_W} {NET_H}" '
        f'width="{NET_W}" height="{NET_H}" role="img" '
        f'aria-label="Network of occupations linked by the people who held more '
        f'than one">'
    )
    parts.append("<defs>")
    parts.append(
        f'<pattern id="dots" width="22" height="22" patternUnits="userSpaceOnUse">'
        f'<circle cx="1.4" cy="1.4" r="1.1" fill="rgba(28,21,16,.06)"/></pattern>'
    )
    parts.append("</defs>")
    parts.append(f'<rect x="0" y="0" width="{NET_W}" height="{NET_H}" fill="url(#dots)"/>')

    # --- edges: grey for weak structure, orange for the strong signal --------
    def curve(e: dict) -> str:
        x1, y1, _ = layout[e["a"]]
        x2, y2, _ = layout[e["b"]]
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        dx, dy = x2 - x1, y2 - y1
        length = math.hypot(dx, dy) or 1.0
        bend = min(22.0, length * 0.12)
        ox, oy = -dy / length * bend, dx / length * bend
        return f"M{x1:.1f},{y1:.1f} Q{mx + ox:.1f},{my + oy:.1f} {x2:.1f},{y2:.1f}"

    for e in sorted(draw, key=lambda e: e["count"]):
        if e["count"] >= STRONG_EDGE:
            alpha = min(0.78, 0.30 + 0.09 * math.log10(e["count"] / STRONG_EDGE + 1) * 6)
            width = min(4.2, 1.1 + 1.5 * math.log10(e["count"] / 100 + 1))
            stroke = f"rgba(191,107,40,{alpha:.2f})"
        else:
            stroke = "rgba(138,130,120,0.16)"
            width = 0.9
        parts.append(
            f'<path d="{curve(e)}" fill="none" stroke="{stroke}" '
            f'stroke-width="{width:.2f}" stroke-linecap="round"><title>'
            f'{_esc(_label(e["a"]))} \u00d7 {_esc(_label(e["b"]))}: '
            f'{_n(e["count"])} people held both</title></path>'
        )

    # --- nodes --------------------------------------------------------------
    circles = [(x, y, r) for (x, y, r) in layout.values()]
    for o in sorted(layout, key=lambda m: layout[m][2]):
        x, y, r = layout[o]
        parts.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}" fill="#8a8278" '
            f'stroke="#f9f8f4" stroke-width="1.5"/>'
        )

    # --- labels: the biggest hubs, then every featured-bridge endpoint -------
    featured = [frozenset((b["a"], b["b"])) for b in payload.get("featured_bridges", [])]
    must_label = {o for o in layout
                  if any(o in p for p in featured)}
    ranked = sorted(nodes.values(), key=lambda n: -n["people"])
    label_set = {n["occupation"] for n in ranked[:LABEL_TOP_N]
                 if n["occupation"] in layout}
    label_set |= (must_label & set(layout))

    occupied: list = []
    labels = []
    for o in sorted(label_set, key=lambda m: -nodes[m]["people"]):
        x, y, r = layout[o]
        text = nodes[o]["label"]
        best = None
        for R in (r + 6.0, r + 13.0, r + 21.0, r + 30.0, r + 41.0):
            for da in (0.0, 0.45, -0.45, 0.9, -0.9, 1.4, -1.4,
                       2.0, -2.0, 2.7, -2.7, math.pi):
                a = -math.pi / 2 + da
                ux, uy = math.cos(a), math.sin(a)
                lx, ly = x + ux * R, y + uy * R + 3.0
                if abs(ux) < 0.3:
                    anchor = "middle"
                elif ux > 0:
                    anchor = "start"
                else:
                    anchor = "end"
                box = _text_box(text, lx, ly, anchor)
                if box[0] < 3 or box[2] > NET_W - 3 or box[1] < 4 or box[3] > NET_H - 1:
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
            lx = min(max(x, 26), NET_W - 26)
            ly = min(max(y - r - 6, 12), NET_H - 4)
            best = (lx, ly, "middle", _text_box(text, lx, ly, "middle"))
        lx, ly, anchor, box = best
        occupied.append(box)
        labels.append({"text": text, "x": lx, "y": ly, "anchor": anchor})

    for L in labels:
        parts.append(
            f'<text x="{L["x"]:.1f}" y="{L["y"]:.1f}" text-anchor="{L["anchor"]}" '
            f'class="nlabel">{_esc(L["text"])}</text>'
        )

    # --- field labels -------------------------------------------------------
    for f in payload["fields"]:
        members = [m for m in f["members"] if m in layout]
        if not members:
            continue
        cx = lay["centroids"][f["id"]][0]
        top_y = min(layout[m][1] for m in members)
        y = max(14.0, top_y - 15.0)
        box = _text_box(f["label"].upper(), cx, y, "middle", size=6.4, height=14)
        for _ in range(8):
            if (not any(_box_hits_circle(box, px, py, pr) for (px, py, pr) in circles)
                    and not any(_boxes_overlap(box, ob) for ob in occupied)):
                break
            y -= 12.0
            if y < 14.0:
                y = 14.0
                break
            box = _text_box(f["label"].upper(), cx, y, "middle", size=6.4, height=14)
        occupied.append(box)
        parts.append(
            f'<text x="{cx:.1f}" y="{y:.1f}" text-anchor="middle" '
            f'class="cluster-label">{_esc(f["label"].upper())}</text>'
        )

    # --- featured bridge callouts ------------------------------------------
    ann = _plan_annotations(payload, lay, occupied)
    for A in ann:
        parts.append(
            f'<path d="M{A["mx"]:.1f},{A["my"]:.1f} L{A["x"]:.1f},{A["y"] - 3:.1f}" '
            f'stroke="rgba(191,107,40,.55)" stroke-width="1" '
            f'stroke-dasharray="3 3" fill="none"/>'
        )
        parts.append(f'<circle cx="{A["mx"]:.1f}" cy="{A["my"]:.1f}" r="2.8" fill="#bf6b28"/>')
        x0, y0, x1, y1 = A["box"]
        parts.append(
            f'<rect x="{x0 - 3:.1f}" y="{y0 - 1:.1f}" width="{x1 - x0 + 6:.1f}" '
            f'height="{y1 - y0 + 2:.1f}" rx="3" fill="rgba(249,248,244,.88)"/>'
        )
        parts.append(
            f'<text x="{A["x"]:.1f}" y="{A["y"]:.1f}" text-anchor="{A["anchor"]}" '
            f'class="bridge-label">{_esc(A["text"])}</text>'
        )

    # --- node size key ------------------------------------------------------
    parts.append("</svg>")
    return "".join(parts)


def _plan_annotations(payload: dict, lay: dict, occupied: list) -> list:
    layout = lay["nodes"]
    circles = [(x, y, r) for (x, y, r) in layout.values()]
    out = []
    for fb in payload.get("featured_bridges", []):
        a, b = fb["a"], fb["b"]
        if a not in layout or b not in layout:
            continue
        x1, y1, _ = layout[a]
        x2, y2, _ = layout[b]
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        text = f'{_label(a)} \u00d7 {_label(b)} \u00b7 {_n(fb["count"])}'
        dx, dy = mx - NET_W / 2, my - NET_H / 2
        d = math.hypot(dx, dy) or 1.0
        ux, uy = dx / d, dy / d
        dirs = [(ux, uy), (-uy, ux), (uy, -ux), (-ux, -uy)]
        best = None
        for dist in (34.0, 50.0, 70.0, 94.0, 122.0, 154.0):
            for (ox, oy) in dirs:
                lx = min(max(mx + ox * dist, 78), NET_W - 78)
                ly = min(max(my + oy * dist, 14), NET_H - 4)
                anchor = "middle"
                box = _text_box(text, lx, ly, anchor, size=6.2, height=14)
                if box[0] < 3 or box[2] > NET_W - 3 or box[1] < 4 or box[3] > NET_H - 1:
                    continue
                if any(_boxes_overlap(box, ob) for ob in occupied):
                    continue
                if any(_box_hits_circle(box, px, py, pr) for (px, py, pr) in circles):
                    continue
                best = (lx, ly, anchor, box)
                break
            if best:
                break
        if best is None:
            lx = min(max(mx + ux * 120, 78), NET_W - 78)
            ly = min(max(my + uy * 120, 14), NET_H - 4)
            best = (lx, ly, "middle", _text_box(text, lx, ly, "middle", size=6.2, height=14))
        lx, ly, anchor, box = best
        occupied.append(box)
        out.append({"text": text, "x": lx, "y": ly, "anchor": anchor,
                    "box": box, "mx": mx, "my": my})
    return out


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
<title>HumansMap career adjacency network</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;800&family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:#f9f8f4; --text:#1c1510;
    --chrome:#1b4d8e;          /* chrome only: wordmark, eyebrow, headline emphasis */
    --data:#bf6b28;            /* data only: co-occurrence (edges, bridge figures) */
    --structure:#8a8278;       /* structure: node fill, grids, weak edges, labels */
    --surface:#ece8de; --line:rgba(28,21,16,.10);
    --display:'Playfair Display', Georgia, serif; --body:'DM Sans', system-ui, sans-serif;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:#d9d4c9; font-family:var(--body); padding:32px; }
  .card { width:1600px; height:1000px; background:var(--bg); color:var(--text);
    padding:26px 44px 14px; display:flex; flex-direction:column; position:relative;
    overflow:hidden; }
  .card header { display:flex; justify-content:space-between; align-items:baseline; }
  .eyebrow { font:700 15px/1 var(--body); letter-spacing:.2em; text-transform:uppercase;
    color:var(--chrome); }
  .brand { font:700 27px/1 var(--display); color:var(--chrome); }
  h1 { font:800 36px/1.06 var(--display); margin:9px 0 0; letter-spacing:-.012em;
    max-width:46ch; }
  h1 em { font-style:normal; color:var(--chrome); }
  .deck { font:400 14px/1.4 var(--body); color:var(--structure); margin:6px 0 0;
    max-width:1510px; }
  .deck b { color:var(--text); font-weight:700; }
  .net { margin-top:2px; position:relative; }
  .net svg { display:block; width:1512px; height:470px; overflow:visible; }
  .band { margin-top:auto; border-top:2px solid var(--text); padding-top:9px;
    display:grid; grid-template-columns:448px 1fr; gap:34px; }
  .plabel { font:700 11px/1 var(--body); letter-spacing:.18em; text-transform:uppercase;
    color:var(--structure); margin-bottom:9px; }
  .read { display:flex; flex-direction:column; gap:7px; font:400 12.4px/1.34 var(--body);
    color:var(--structure); }
  .read .row { display:flex; align-items:center; gap:9px; }
  .swatch { flex:0 0 auto; }
  .blockkey { display:grid; grid-template-columns:1fr 1fr; gap:4px 14px; margin-top:9px;
    font:400 11.6px/1.3 var(--body); color:var(--structure); }
  .bk { display:flex; align-items:center; gap:6px; }
  .bk i { width:10px; height:10px; border-radius:3px; flex:0 0 auto; }
  .stats { display:grid; grid-template-columns:1fr 1fr; gap:8px 20px; margin-top:11px; }
  .stat .v { font:800 22px/1 var(--display); }
  .stat .k { font:500 10.3px/1.3 var(--body); color:var(--structure);
    text-transform:uppercase; letter-spacing:.07em; margin-top:3px; }
  ul.bridges { list-style:none; margin:0; padding:0; columns:2; column-gap:30px; }
  ul.bridges li { break-inside:avoid; padding:4px 0; border-bottom:1px solid var(--line); }
  .bhead { display:flex; justify-content:space-between; gap:10px; align-items:baseline; }
  .bname { font:600 12.8px/1.25 var(--body); }
  .bname b { color:var(--text); }
  .bcount { font:800 15px/1 var(--display); color:var(--data); white-space:nowrap; }
  .bmeta { font:400 10.4px/1.3 var(--body); color:var(--structure); margin-top:2px; }
  .caveat { font:400 11.2px/1.4 var(--body); color:var(--structure); margin-top:10px;
    padding-top:8px; border-top:1px solid var(--line); }
  .caveat b { color:var(--text); }
  .card footer { margin-top:6px; display:flex; justify-content:space-between;
    align-items:baseline; font:400 12.5px/1.4 var(--body); color:var(--structure); }
</style></head>
<body>"""


def render_html(payload: dict) -> str:
    if "bridges" not in payload:
        payload = analyze(payload)
    scene = arc_atlas(
        [{"id": n["occupation"], "label": n["label"], "group": n["field"],
          "value": n["people"]} for n in payload["nodes"]],
        [{"a": e["a"], "b": e["b"], "w": e["count"]} for e in payload["edges"]],
        [(f["id"], f["label"]) for f in payload["fields"]],
        width=1512, height=470, label_size=10.3, max_chars=22, top_links=6,
        title="Occupations linked by people who held more than one",
    )

    scale = payload["scale"]
    params = payload["params"]
    fields = payload["fields"]
    featured = payload["featured_bridges"]
    bridges = payload["bridges"]

    biggest = max(featured, key=lambda b: b["count"]) if featured else None
    concept = None
    for b in featured:
        if {b["a"], b["b"]} == {"mathematician", "philosopher"}:
            concept = b
    if biggest:
        headline = (
            f'Jobs cluster by field, but <em>'
            f'{_esc(_label(biggest["a"]).lower())} and '
            f'{_esc(_label(biggest["b"]).lower())}</em> is the biggest crossover '
            f'({_n(biggest["count"])} people did both).'
        )
    else:
        headline = "Which jobs do the same people hold?"

    deck = (
        f"How the world&rsquo;s jobs overlap. Every dot is a job recorded for the "
        f"<b>{_n(scale['people_considered'])}</b> people with "
        f"<b>&ge;{_n(params['min_statements'])}</b> statements and two or more "
        f"occupations. Two jobs are linked when the same person held both; an arc&rsquo;s "
        f"<b>thickness is how many people</b> held that pair. Jobs are grouped into "
        f"<b>{_n(scale['fields_drawn'])}</b> broad fields, and the strongest cross-field "
        f"links are named at the top. Drawn: the <b>{_n(scale['nodes_drawn'])}</b> jobs "
        f"with {_n(params['node_floor'])}+ people and their <b>{_n(scale['edges_drawn'])}</b> "
        f"shared pairs."
    )

    def bridge_li(b: dict) -> str:
        return (
            f'<li><div class="bhead"><span class="bname">'
            f'<b>{_esc(b["a_label"])}</b> \u00d7 {_esc(b["b_label"])}</span>'
            f'<span class="bcount">{_n(b["count"])}</span></div>'
            f'<div class="bmeta">{_esc(FIELD_LABEL.get(b["a_field"], b["a_field"]))} \u2194 '
            f'{_esc(FIELD_LABEL.get(b["b_field"], b["b_field"]))} \u00b7 {b["lift"]:.1f}\u00d7 chance</div></li>'
        )

    shown = [b for b in bridges if b["count"] >= 100][:5]

    caveat = (
        "<b>Read with care.</b> Wikidata records occupations unevenly, "
        "multi-valued and with a coarse vocabulary; common jobs (writer, "
        "university teacher, actor, politician, painter) dominate the middle. "
        "Edge weight counts <em>documented</em> people, not probabilities: this is "
        "<b>correlation, not causation</b>. Fields are a hand-curated reading "
        "frame; the edges are the data."
    )

    blockkey = "".join(
        f'<span class="bk"><i style="background:{ARC_GROUP_COLORS[i % len(ARC_GROUP_COLORS)]}">'
        f'</i>{_esc(f["label"])}</span>'
        for i, f in enumerate(fields)
    )
    legend = (
        '<div class="read">'
        '<div class="row"><svg class="swatch" width="44" height="20">'
        '<circle cx="7" cy="10" r="3" fill="#8a8278"/>'
        '<circle cx="20" cy="10" r="5" fill="#8a8278"/>'
        '<circle cx="35" cy="10" r="7" fill="#8a8278"/></svg>'
        '<span>every dot is a job; <b>size</b> = people who held it</span></div>'
        '<div class="row"><svg class="swatch" width="42" height="16">'
        f'<path d="M2,11 L40,11" stroke="rgba(191,107,40,.7)" stroke-width="3.2"/>'
        f'<path d="M2,4 L40,4" stroke="rgba(191,107,40,.35)" stroke-width="1.2"/></svg>'
        '<span>every arc joins two jobs held by the same people; '
        '<b>thicker = more</b></span></div>'
        '</div>'
        '<div class="plabel" style="margin:11px 0 5px">Colour = broad field</div>'
        f'<div class="blockkey">{blockkey}</div>'
    )

    stats = (
        f'<div class="stat"><div class="v">{_n(scale["people_considered"])}</div>'
        '<div class="k">people scanned</div></div>'
        f'<div class="stat"><div class="v">{_n(scale["occupation_records"])}</div>'
        '<div class="k">occupation records</div></div>'
        f'<div class="stat"><div class="v">{_n(scale["edges_drawn"])}</div>'
        '<div class="k">co-occurring pairs</div></div>'
        f'<div class="stat"><div class="v">{_n(scale["fields_drawn"])}</div>'
        '<div class="k">fields</div></div>'
    )

    concept_line = ""
    if concept and concept is not biggest:
        concept_line = (
            f'<div class="bmeta" style="margin-top:7px">Also a large crossover: '
            f'<b>{_esc(concept["a_label"])}</b> \u00d7 <b>{_esc(concept["b_label"])}</b>: '
            f'{_n(concept["count"])} people.</div>'
        )

    html = f"""<section class="card">
  <header>
    <span class="eyebrow">How jobs overlap</span>
    <span class="brand">Humans&nbsp;Map</span>
  </header>
  <h1>{headline}</h1>
  <p class="deck">{deck}</p>
  <div class="net">{scene}</div>
  <div class="band">
    <div>
      <div class="plabel">How to read it</div>
      {legend}
      <div class="stats">{stats}</div>
    </div>
    <div>
      <div class="plabel">Unexpected bridges between fields</div>
      <ul class="bridges">{''.join(bridge_li(b) for b in shown)}</ul>
      {concept_line}
      <div class="caveat">{caveat}</div>
    </div>
  </div>
  <footer>
    <span>Data: Wikidata (CC BY-SA) &middot; humansmap.com</span>
    <span>{_n(scale['nodes_drawn'])} occupations &middot; {_n(scale['edges_drawn'])} linked pairs &middot; &ge;{_n(params['min_pair'])} people per pair</span>
  </footer>
</section>"""

    return HTML_HEAD + html + "\n</body></html>\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default="docs/marketing/career")
    parser.add_argument("--min-statements", type=int, default=DEFAULT_MIN_STATEMENTS)
    parser.add_argument("--min-pair", type=int, default=DEFAULT_MIN_PAIR)
    parser.add_argument("--scan-limit", type=int, default=DEFAULT_SCAN_LIMIT)
    parser.add_argument("--max-nodes", type=int, default=DEFAULT_MAX_NODES)
    parser.add_argument("--max-edges", type=int, default=DEFAULT_MAX_EDGES)
    parser.add_argument("--node-floor", type=int, default=DEFAULT_NODE_FLOOR)
    parser.add_argument("--from-json", default=None)
    parser.add_argument("--no-html", action="store_true")
    args = parser.parse_args()

    if args.from_json:
        payload = json.loads(Path(args.from_json).read_text())
        payload.setdefault("params", {})["node_floor"] = args.node_floor
        for key in ("bridges", "featured_bridges", "top_pairs", "fields",
                    "excluded_nodes"):
            payload.pop(key, None)
    else:
        payload = collect(args.min_statements, args.min_pair, args.scan_limit,
                          args.max_nodes, args.max_edges)
        payload["params"]["node_floor"] = args.node_floor

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    analyze(payload)

    if not args.from_json:
        json_path = out_dir / "career.json"
        json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        print(f"wrote {json_path}")

    if not args.no_html:
        html_path = out_dir / "career.html"
        html_path.write_text(render_html(payload))
        print(f"wrote {html_path}")

    analyzed = payload
    summary = {
        "people_considered": analyzed["scale"]["people_considered"],
        "occupation_records": analyzed["scale"]["occupation_records"],
        "distinct_occupations": analyzed["scale"]["distinct_occupations"],
        "nodes_drawn": analyzed["scale"]["nodes_drawn"],
        "edges_drawn": analyzed["scale"]["edges_drawn"],
        "fields": [f["label"] for f in analyzed["fields"]],
        "hubs": [
            {"occupation": n["label"], "people": n["people"], "degree": n["degree"]}
            for n in analyzed["nodes"][:12]
        ],
        "bridges": [
            {"a": b["a_label"], "b": b["b_label"], "count": b["count"],
             "lift": b["lift"], "fields": f'{b["a_field"]} x {b["b_field"]}'}
            for b in analyzed["bridges"][:10]
        ],
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
