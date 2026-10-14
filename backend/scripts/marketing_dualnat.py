#!/usr/bin/env python3
"""Marketing dual-nationality network miner — read-only.

The concept: countries are linked when *the same people* hold citizenship of
both. Wikidata records a person's citizenships in ``persons.nationalities``
(ISO-3166-1 alpha-2 codes). Every person with two or more citizenships adds one
unordered pair of country edges; the edge weight is how many such people the
two countries share.

This is the basis for a genuinely original geopolitics graphic: the world as a
network of dual nationals rather than borders. The picture is the same radial
chord as the language bridge — countries sit on one ring, grouped into
continent arcs, ties inside a continent hug the rim, and ties that cross the
middle become gradient chords whose colour blends the two continents it joins.

It never writes to ArangoDB. One bounded READ-ONLY pass pulls every person with
``LENGTH(nationalities) >= 2`` and ``wikidata_statements_count >= 30`` (the
statement floor keeps the noise out and the scan bounded), then everything is
aggregated in Python. Outputs (both written by default):

  dualnat.json   machine-readable payload: nodes, edges, top lists, probe stats
  dualnat.html   brand-styled card sheet (1600x1000) to screenshot to dualnat.png

Run it manually inside the backend container:

    docker compose exec -T backend python - --out-dir /tmp/dualnat \\
        < backend/scripts/marketing_dualnat.py
    docker compose exec -T backend cat /tmp/dualnat/dualnat.json \\
        > docs/marketing/dualnat/dualnat.json
    docker compose exec -T backend cat /tmp/dualnat/dualnat.html \\
        > docs/marketing/dualnat/dualnat.html

Re-render the HTML from the committed payload without touching the DB:

    docker compose exec -T backend sh -c 'cat > /tmp/dualnat.json' \\
        < docs/marketing/dualnat/dualnat.json
    docker compose exec -T backend python - --from-json /tmp/dualnat.json \\
        --out-dir /tmp/dualnat < backend/scripts/marketing_dualnat.py
    docker compose exec -T backend cat /tmp/dualnat/dualnat.html \\
        > docs/marketing/dualnat/dualnat.html

Then render (from scripts/marketing/):

    source env.sh
    node render-insight-cards.js ../../docs/marketing/dualnat/dualnat.html \\
        /tmp/opencode/dualnat
    # inspect /tmp/opencode/dualnat/card-01.png, then copy it to
    # ../../docs/marketing/dualnat/dualnat.png

Flags:
    --out-dir PATH        output directory (default: docs/marketing/dualnat)
    --min-statements N    Wikidata statement floor (default: 30)
    --min-pair N          people two countries must share to draw an edge (default: 8)
    --max-persons N       hard safety cap on the single scan (default: 250000)
    --max-nodes N         countries kept by dual-national count (default: 60)
    --from-json PATH      re-render the HTML from an existing dualnat.json
    --no-html             emit only the JSON payload
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import OrderedDict, defaultdict
from datetime import date
from pathlib import Path

# When run as a file inside the container this resolves to /app; when piped
# over stdin (``python -``) __file__ is "<stdin>" and the container's cwd
# (/app) is already on sys.path, so fall back.
try:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
except (NameError, OSError):  # pragma: no cover - stdin invocation
    sys.path.insert(0, str(Path.cwd()))

from app.database import get_arango_client, get_db  # noqa: E402

# ISO 3166-1 alpha-2 → English names, mirroring
# ``backend/scripts/marketing_insights.py::COUNTRY_NAMES`` (the one place the
# repo keeps this map). Codes absent here fall back to the raw ISO code.
COUNTRY_NAMES: dict[str, str] = {
    "US": "United States", "FR": "France", "DE": "Germany", "GB": "United Kingdom",
    "IT": "Italy", "ES": "Spain", "JP": "Japan", "SE": "Sweden", "PL": "Poland",
    "CA": "Canada", "RU": "Russia", "AT": "Austria", "CH": "Switzerland",
    "HU": "Hungary", "BE": "Belgium", "NL": "Netherlands", "CZ": "Czechia",
    "NO": "Norway", "DK": "Denmark", "FI": "Finland", "IE": "Ireland",
    "PT": "Portugal", "GR": "Greece", "AU": "Australia", "BR": "Brazil",
    "IN": "India", "CN": "China", "KR": "South Korea", "MX": "Mexico",
    "AR": "Argentina", "ZA": "South Africa", "TR": "Turkey", "IL": "Israel",
    "UA": "Ukraine", "RO": "Romania", "BG": "Bulgaria", "HR": "Croatia",
    "RS": "Serbia", "SI": "Slovenia", "SK": "Slovakia", "LT": "Lithuania",
    "LV": "Latvia", "EE": "Estonia", "IS": "Iceland", "NZ": "New Zealand",
    "CL": "Chile", "CO": "Colombia", "PE": "Peru", "EG": "Egypt",
    "NG": "Nigeria", "KE": "Kenya", "MA": "Morocco", "IR": "Iran",
    "PK": "Pakistan", "BD": "Bangladesh", "ID": "Indonesia", "PH": "Philippines",
    "TH": "Thailand", "VN": "Vietnam", "SG": "Singapore", "TW": "Taiwan",
    "AD": "Andorra", "AG": "Antigua and Barbuda", "AL": "Albania", "AM": "Armenia",
    "AZ": "Azerbaijan", "BA": "Bosnia and Herzegovina", "BF": "Burkina Faso",
    "BJ": "Benin", "BO": "Bolivia", "BS": "Bahamas", "BY": "Belarus", "BZ": "Belize",
    "CD": "DR Congo", "CF": "Central African Republic", "CG": "Congo",
    "CI": "Ivory Coast", "CM": "Cameroon", "CR": "Costa Rica", "CU": "Cuba",
    "CV": "Cape Verde", "CY": "Cyprus", "DO": "Dominican Republic", "DZ": "Algeria",
    "EC": "Ecuador", "ET": "Ethiopia", "GA": "Gabon", "GE": "Georgia", "GH": "Ghana",
    "GN": "Guinea", "GT": "Guatemala", "GW": "Guinea-Bissau", "GY": "Guyana",
    "HN": "Honduras", "HT": "Haiti", "IQ": "Iraq", "JM": "Jamaica", "JO": "Jordan",
    "KG": "Kyrgyzstan", "KH": "Cambodia", "KP": "North Korea", "KZ": "Kazakhstan",
    "LB": "Lebanon", "LI": "Liechtenstein", "LR": "Liberia", "LU": "Luxembourg",
    "MC": "Monaco", "MD": "Moldova", "ME": "Montenegro", "MG": "Madagascar",
    "MK": "North Macedonia", "ML": "Mali", "MT": "Malta", "MU": "Mauritius",
    "MY": "Malaysia", "MZ": "Mozambique", "PA": "Panama", "PS": "Palestine",
    "PY": "Paraguay", "RW": "Rwanda", "SA": "Saudi Arabia", "SD": "Sudan",
    "SL": "Sierra Leone", "SN": "Senegal", "SO": "Somalia", "SS": "South Sudan",
    "SV": "El Salvador", "SY": "Syria", "TD": "Chad", "TG": "Togo",
    "TJ": "Tajikistan", "TM": "Turkmenistan", "TN": "Tunisia",
    "TT": "Trinidad and Tobago", "UG": "Uganda", "UY": "Uruguay",
    "UZ": "Uzbekistan", "VA": "Vatican City", "VE": "Venezuela", "WS": "Samoa",
    "ZM": "Zambia", "AE": "United Arab Emirates", "AO": "Angola",
}

# ---------------------------------------------------------------------------
# Countries grouped by continent / region. The payload carries no continent
# field, so every ISO-2 code that can appear is assigned here; anything absent
# falls into a neutral "Other" bucket. Groups are ordered west→east so the
# shelf order of the legend is stable; the actual ring order is data-driven.
# ---------------------------------------------------------------------------

REGION_SPEC = [
    ("americas", "Americas", "AMERICAS", "#2f6f9f"),
    ("europe", "Europe", "EUROPE", "#c15a86"),
    ("mena", "Middle East & North Africa", "M.EAST\u00b7N AFRICA", "#1f8f8a"),
    ("africa", "Sub-Saharan Africa", "SUB-SAHARAN AFRICA", "#7f8a2e"),
    ("asia", "South & East Asia", "S & E ASIA", "#9a3f9a"),
    ("oceania", "Oceania", "OCEANIA", "#5a5aa8"),
    ("other", "Other", "OTHER", "#6e6259"),
]
REGION_ORDER = [r[0] for r in REGION_SPEC]
REGION_LABEL = {r[0]: r[1] for r in REGION_SPEC}
REGION_SHORT = {r[0]: r[2] for r in REGION_SPEC}
REGION_COLOR = {r[0]: r[3] for r in REGION_SPEC}

REGION_MEMBERS: dict[str, set] = {
    "americas": set((
        "AG AR BB BO BR BS BZ CA CL CO CR CU DM DO EC GD GT GY HN HT JM KN LC "
        "MX NI PA PE PY SR SV TT US UY VE"
    ).split()),
    "europe": set((
        "AD AL AT BA BE BG BY CH CY CZ DE DK EE ES FI FR GB GR HR HU IE IS IT "
        "LI LT LU LV MC MD ME MK MT NL NO PL PT RO RS RU SE SI SK UA VA"
    ).split()),
    "mena": set("DZ EG IR IQ IL JO LB LY MA PS SA SY TN TR".split()),
    "africa": set((
        "AO BF BJ BW CD CF CG CI CM CV DJ ET GA GH GM GN GQ GW KE LR LS MG ML "
        "MR MU MW MZ NA NE NG RW SC SD SL SN SO SS ST SZ TD TG TZ UG ZA ZM ZW"
    ).split()),
    "asia": set((
        "AF AM AZ BD BN BT CN GE ID IN JP KG KH KP KR KZ LA LK MM MN MV MY NP "
        "PH PK SG TH TJ TL TM TW UZ VN"
    ).split()),
    "oceania": set("AU FJ NZ PG SB TO VU WS".split()),
}


def _region_of(code: str) -> str:
    for rid, codes in REGION_MEMBERS.items():
        if code in codes:
            return rid
    return "other"


# ---------------------------------------------------------------------------
# Rendering geometry (card units; the PNG renders at 2x)
# ---------------------------------------------------------------------------

CARD_W = 1600
CARD_H = 1000
NET_W = 1020          # chord-diagram panel, left of the reading key
NET_H = 740
NODE_R_MIN = 4.6
NODE_R_MAX = 22.0
MAX_ANNOT = 6

# Radial ring geometry, in card units of arc length.
DOT_GAP = 6.0         # arc room between neighbouring country dots
REGION_GAP = 20.0     # arc room between continent arcs
LABEL_SIZE = 9.6      # country label size
REGION_SIZE = 11.5    # continent label size
CHORD_BOW = 0.30      # how far chord control points are pulled to the centre

# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

# One bounded read-only pass. The statement floor + LIMIT cap keep it cheap on
# the live DB; every downstream number is derived from this result.
DUALNAT_AQL = """
FOR p IN persons
  FILTER LENGTH(p.nationalities) >= 2
     AND p.wikidata_statements_count >= @min_statements
  LET ns = UNIQUE(p.nationalities)
  FILTER LENGTH(ns) >= 2
  LIMIT @max_persons
  RETURN {n: p.name_en, q: p.wikidata_id, s: p.wikidata_statements_count, ns: ns}
"""


def _rows(db, aql: str, bind: dict | None = None) -> list:
    try:
        return list(db.aql.execute(aql, bind_vars=bind or {}))
    except Exception as exc:  # noqa: BLE001 - a marketing job must not crash
        print(f"[warn] query failed: {exc}", file=sys.stderr)
        return []


def _valid_codes(values) -> list[str]:
    out = []
    for code in values or []:
        if not isinstance(code, str):
            continue
        code = code.strip().upper()
        if len(code) == 2 and code.isalpha():
            out.append(code)
    return sorted(set(out))


# ---------------------------------------------------------------------------
# Collection (one bounded read-only DB pass)
# ---------------------------------------------------------------------------


def collect(db, min_statements: int, min_pair: int, max_persons: int) -> dict:
    scan = _rows(
        db,
        DUALNAT_AQL,
        {"min_statements": min_statements, "max_persons": max_persons},
    )

    node_people: dict[str, int] = defaultdict(int)
    pair_people: dict[tuple, list] = defaultdict(list)
    dual_nationals = 0
    multi = 0

    for row in scan:
        codes = _valid_codes(row.get("ns"))
        if len(codes) < 2:
            continue
        dual_nationals += 1
        if len(codes) > 2:
            multi += 1
        person = {
            "name": row.get("n") or row.get("q") or "Unknown",
            "qid": row.get("q") or None,
            "stmts": int(row.get("s") or 0),
        }
        for code in codes:
            node_people[code] += 1
        for i in range(len(codes)):
            for j in range(i + 1, len(codes)):
                pair_people[(codes[i], codes[j])].append(person)

    def person_sort(p: dict) -> tuple:
        return (-p["stmts"], p["name"].lower())

    all_pairs = sorted(pair_people.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    edges = []
    for (a, b), people in all_pairs:
        count = len(people)
        if count < min_pair:
            continue
        examples = [p["name"] for p in sorted(people, key=person_sort)[:4]]
        edges.append(OrderedDict(a=a, b=b, count=count, examples=examples))

    drawn_codes = sorted({c for e in edges for c in (e["a"], e["b"])})

    def node_row(code: str) -> OrderedDict:
        return OrderedDict(
            code=code,
            name=COUNTRY_NAMES.get(code, code),
            total_people=node_people.get(code, 0),
        )

    nodes = [node_row(c) for c in drawn_codes]

    top_pairs = [
        OrderedDict(
            a=a, b=b, count=len(people),
            examples=[p["name"] for p in sorted(people, key=person_sort)[:4]],
        )
        for (a, b), people in all_pairs[:40]
    ]
    top_nodes = [
        node_row(c)
        for c in sorted(node_people, key=lambda c: (-node_people[c], c))
    ]

    counts = [e["count"] for e in edges]
    return OrderedDict(
        generated=date.today().isoformat(),
        concept="dual_nationality_network",
        thresholds=OrderedDict(
            min_statements=min_statements,
            min_pair=min_pair,
            max_persons=max_persons,
        ),
        scale=OrderedDict(
            persons_scanned=len(scan),
            dual_nationals=dual_nationals,
            with_3plus_nationalities=multi,
            countries=len(node_people),
            all_pairs=len(all_pairs),
            qualifying_edges=len(edges),
            drawn_nodes=len(nodes),
            max_pair=counts[0] if counts else 0,
            min_drawn_pair=counts[-1] if counts else 0,
        ),
        nodes=nodes,
        edges=edges,
        top_pairs=top_pairs,
        top_nodes=top_nodes[:25],
    )


# ---------------------------------------------------------------------------
# Analysis: prune to the countries worth labelling, tag continents, size dots
# ---------------------------------------------------------------------------


def analyze(payload: dict, max_nodes: int) -> dict:
    """Keep the best-documented countries, tag continents, compute radii.

    The payload has 140 linked countries, which is far too dense for a labelled
    ring. Only the ``max_nodes`` countries with the most recorded dual nationals
    are drawn; every strongest bridge survives the cut, and the card says how
    many countries are shown. The full payload keeps the rest.
    """
    ranked = sorted(payload["nodes"],
                    key=lambda n: (-n["total_people"], n["code"]))
    kept = ranked[:max_nodes]
    kept_codes = {n["code"] for n in kept}
    edges = [e for e in payload["edges"]
             if e["a"] in kept_codes and e["b"] in kept_codes]

    degree: dict[str, int] = defaultdict(int)
    for e in edges:
        degree[e["a"]] += e["count"]
        degree[e["b"]] += e["count"]

    totals = [n["total_people"] for n in kept] or [1]
    tmin, tmax = min(totals), max(totals)

    def radius(total: int) -> float:
        if tmax <= tmin:
            return NODE_R_MIN + 6.0
        t = (total - tmin) / (tmax - tmin)
        return NODE_R_MIN + (NODE_R_MAX - NODE_R_MIN) * math.sqrt(max(0.0, t))

    nodes = []
    for n in kept:
        code = n["code"]
        region = _region_of(code)
        nodes.append(OrderedDict(
            code=code, name=n.get("name", COUNTRY_NAMES.get(code, code)),
            total_people=n["total_people"], degree=degree.get(code, 0),
            radius=round(radius(n["total_people"]), 2),
            region=region, region_label=REGION_LABEL.get(region, region),
        ))
    payload["nodes"] = nodes

    for e in edges:
        e["region_a"] = _region_of(e["a"])
        e["region_b"] = _region_of(e["b"])
        e["cross_region"] = e["region_a"] != e["region_b"]

    regions = []
    for rid, label, short, color in REGION_SPEC:
        members = [n["code"] for n in nodes if n["region"] == rid]
        if not members:
            continue
        regions.append(OrderedDict(
            id=rid, label=label, short=short, color=color, members=members,
            countries=len(members),
            people=sum(n["total_people"] for n in nodes if n["region"] == rid),
        ))
    payload["regions"] = regions

    by_count = sorted(edges, key=lambda e: (-e["count"], e["a"], e["b"]))
    payload["top_pairs"] = [
        OrderedDict(a=e["a"], b=e["b"], count=e["count"],
                    examples=e.get("examples", [])[:4])
        for e in by_count[:30]
    ]
    payload["edges"] = edges

    payload["scale"]["nodes_drawn"] = len(nodes)
    payload["scale"]["edges_drawn"] = len(edges)
    payload["scale"]["regions_drawn"] = len(regions)
    payload["scale"]["max_pair"] = by_count[0]["count"] if by_count else 0
    payload["scale"]["min_drawn_pair"] = by_count[-1]["count"] if by_count else 0
    payload["thresholds"]["max_nodes"] = max_nodes
    return payload


# ---------------------------------------------------------------------------
# Layout: countries grouped into continent arcs on one ring
# ---------------------------------------------------------------------------


def _esc(value) -> str:
    return (str(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _n(value) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "0"


def _display(code: str) -> str:
    return COUNTRY_NAMES.get(code, code)


def _ang_dist(a: float, b: float) -> float:
    d = abs(a - b) % (2 * math.pi)
    return min(d, 2 * math.pi - d)


def _ring_order(ids: list, weight: dict) -> list:
    """Order the continents around the ring so heavy bridges are short.

    A greedy chain (always step to the continent sharing the most dual
    nationals) is polished with circular 2-opt. Few continents, so this is
    cheap and exact enough; the objective is the weighted angular span of
    every bridge.
    """
    if len(ids) <= 2:
        return list(ids)
    remaining = set(ids)
    order = [ids[0]]
    remaining.discard(ids[0])
    while remaining:
        last = order[-1]
        nxt = max(remaining,
                  key=lambda x: (weight.get(tuple(sorted((last, x))), 0.0), x))
        order.append(nxt)
        remaining.discard(nxt)

    def cost(seq: list) -> float:
        n = len(seq)
        total = 0.0
        for i in range(n):
            for j in range(i + 1, n):
                w = weight.get(tuple(sorted((seq[i], seq[j]))), 0.0)
                if w:
                    total += w * min(j - i, n - (j - i))
        return total

    improved = True
    while improved:
        improved = False
        for i in range(1, len(order) - 1):
            for j in range(i + 1, len(order)):
                cand = order[:i] + order[i:j + 1][::-1] + order[j + 1:]
                if cost(cand) < cost(order) - 1e-9:
                    order = cand
                    improved = True
    return order


def _country_chain(members: list, edges: list, nodes: dict) -> list:
    """Chain a continent's countries so strongly linked ones sit side by side."""
    if len(members) <= 1:
        return list(members)
    w: dict = defaultdict(float)
    for e in edges:
        if e["a"] in members and e["b"] in members:
            w[frozenset((e["a"], e["b"]))] += e["count"]
    start = max(members, key=lambda m: (nodes[m]["total_people"], m))
    order = [start]
    remaining = set(members) - {start}
    while remaining:
        last = order[-1]
        nxt = max(remaining,
                  key=lambda m: (w.get(frozenset((last, m)), 0.0),
                                 nodes[m]["total_people"], m))
        order.append(nxt)
        remaining.discard(nxt)
    return order


def _pt(cx: float, cy: float, ang: float, r: float) -> tuple:
    return (cx + r * math.cos(ang), cy + r * math.sin(ang))


def _label_width(text: str, size: float) -> float:
    return len(text) * 0.56 * size


def _arc(cx: float, cy: float, r: float, a0: float, a1: float) -> str:
    x0, y0 = _pt(cx, cy, a0, r)
    x1, y1 = _pt(cx, cy, a1, r)
    delta = a1 - a0
    large = 1 if abs(delta) > math.pi else 0
    sweep = 1 if delta >= 0 else 0
    return (f"M{x0:.2f},{y0:.2f} A{r:.2f},{r:.2f} 0 {large} {sweep} "
            f"{x1:.2f},{y1:.2f}")


def build_layout(payload: dict) -> dict:
    """Place the countries on a ring, grouped into contiguous continent arcs."""
    nodes = {n["code"]: n for n in payload["nodes"]}
    edges = payload["edges"]
    regions = [r for r in payload["regions"]
               if any(m in nodes for m in r["members"])]
    cids = [r["id"] for r in regions]

    reg_weight: dict = defaultdict(float)
    for e in edges:
        if e["cross_region"]:
            reg_weight[tuple(sorted((e["region_a"], e["region_b"])))] += e["count"]
    reg_order = _ring_order(cids, reg_weight)

    chains = {r["id"]: _country_chain([m for m in r["members"] if m in nodes],
                                      edges, nodes)
              for r in regions}
    nreg = len(reg_order)

    def geometry(gap: float, reg_gap: float):
        sizes = {}
        for _cid, seq in chains.items():
            for code in seq:
                sizes[code] = 2.0 * nodes[code]["radius"] + gap
        total = sum(sizes.values()) + reg_gap * nreg
        r = total / (2.0 * math.pi)
        angles = {}
        spans = {}
        cursor = -math.pi / 2.0
        for cid in reg_order:
            start = cursor
            for code in chains[cid]:
                width = 2.0 * math.pi * sizes[code] / total
                angles[code] = cursor + width / 2.0
                cursor += width
            spans[cid] = (start, cursor)
            cursor += 2.0 * math.pi * reg_gap / total
        return r, angles, spans

    def cost(chains_try: dict) -> float:
        _r, angles, _spans = geometry(DOT_GAP, REGION_GAP)
        total = 0.0
        for e in edges:
            a = angles.get(e["a"])
            b = angles.get(e["b"])
            if a is not None and b is not None:
                total += e["count"] * _ang_dist(a, b)
        return total

    best = cost(chains)
    for _pass in range(6):
        improved = False
        for cid in reg_order:
            base = chains[cid]
            if len(base) <= 1:
                continue
            options = [base, base[::-1]]
            options += [base[k:] + base[:k] for k in range(1, len(base))]
            pick, pick_cost = base, best
            for opt in options:
                chains[cid] = opt
                c = cost(chains)
                if c < pick_cost - 1e-6:
                    pick, pick_cost = opt, c
            chains[cid] = pick
            if pick_cost < best - 1e-6:
                best = pick_cost
                improved = True
        if not improved:
            break

    # Fit the ring inside the panel; shrink the smallest gaps first.
    geom = None
    for gap, reg_gap in ((DOT_GAP, REGION_GAP), (5.0, 17.0), (4.0, 14.0),
                         (3.0, 11.0), (2.0, 8.0)):
        r, angles, spans = geometry(gap, reg_gap)
        r_reg = r
        for code in angles:
            end = (r + nodes[code]["radius"] + 3.0
                   + _label_width(nodes[code]["name"], LABEL_SIZE))
            r_reg = max(r_reg, end)
        r_reg += 15.0
        geom = (r, angles, spans, r_reg)
        if r_reg <= NET_H / 2.0 - 12.0:
            break
    r, angles, spans, r_reg = geom

    cx, cy = NET_W / 2.0, NET_H / 2.0
    lay_nodes = {}
    for code in nodes:
        x, y = _pt(cx, cy, angles[code], r)
        lay_nodes[code] = {"angle": angles[code], "x": x, "y": y,
                           "r": nodes[code]["radius"]}

    regions_out = []
    for cid in reg_order:
        reg = next(x for x in regions if x["id"] == cid)
        a0, a1 = spans[cid]
        arc_len = r_reg * (a1 - a0)
        short = REGION_SHORT.get(cid, reg["label"].upper())
        size = REGION_SIZE
        # 0.74 per glyph covers the tracked uppercase; keep the name inside the
        # arc so SVG textPath never clips its leading characters.
        width = len(short) * 0.74 * size
        if width > arc_len - 8.0:
            size = max(7.6, size * (arc_len - 8.0) / width)
        regions_out.append({
            "id": cid, "label": reg["label"], "short": short,
            "color": REGION_COLOR.get(cid, "#8a8278"),
            "span": (a0, a1), "mid": (a0 + a1) / 2.0,
            "size": round(size, 2),
            "members": [m for m in chains[cid] if m in lay_nodes],
        })

    return {"cx": cx, "cy": cy, "R": r, "R_fam": r_reg,
            "nodes": lay_nodes, "families": regions_out, "fam_order": reg_order}


def _cubic_point(p0: tuple, c1: tuple, c2: tuple, p1: tuple, t: float) -> tuple:
    mt = 1.0 - t
    x = (mt ** 3 * p0[0] + 3 * mt * mt * t * c1[0]
         + 3 * mt * t * t * c2[0] + t ** 3 * p1[0])
    y = (mt ** 3 * p0[1] + 3 * mt * mt * t * c1[1]
         + 3 * mt * t * t * c2[1] + t ** 3 * p1[1])
    return (x, y)


def _bridge_list(payload: dict) -> list:
    """The strongest bridges shown in the panel and highlighted on the ring."""
    return list(payload.get("top_pairs", []))[:MAX_ANNOT]


def build_scene(payload: dict, lay: dict) -> str:
    nodes = {n["code"]: n for n in payload["nodes"]}
    layout = lay["nodes"]
    edges = [e for e in payload["edges"] if e["a"] in layout and e["b"] in layout]
    cx, cy, R = lay["cx"], lay["cy"], lay["R"]
    r_reg = lay["R_fam"]

    counts = [e["count"] for e in edges] or [1]
    log_min = math.log10(max(1, min(counts)))
    log_max = math.log10(max(1, max(counts)))

    def strength(count: int) -> float:
        if log_max - log_min < 1e-6:
            return 1.0
        t = (math.log10(max(1, count)) - log_min) / (log_max - log_min)
        return min(1.0, max(0.0, t))

    def chord_d(e: dict):
        x1, y1 = layout[e["a"]]["x"], layout[e["a"]]["y"]
        x2, y2 = layout[e["b"]]["x"], layout[e["b"]]["y"]
        c1 = (x1 + (cx - x1) * CHORD_BOW, y1 + (cy - y1) * CHORD_BOW)
        c2 = (x2 + (cx - x2) * CHORD_BOW, y2 + (cy - y2) * CHORD_BOW)
        d = (f"M{x1:.2f},{y1:.2f} C{c1[0]:.2f},{c1[1]:.2f} "
             f"{c2[0]:.2f},{c2[1]:.2f} {x2:.2f},{y2:.2f}")
        return d, (x1, y1), c1, c2, (x2, y2)

    def within_d(e: dict, radius: float) -> str:
        a1 = layout[e["a"]]["angle"]
        a2 = layout[e["b"]]["angle"]
        delta = (a2 - a1) % (2 * math.pi)
        if delta > math.pi:
            delta -= 2 * math.pi
        large = 1 if abs(delta) > math.pi else 0
        sweep = 1 if delta >= 0 else 0
        x1, y1 = _pt(cx, cy, a1, radius)
        x2, y2 = _pt(cx, cy, a2, radius)
        return (f"M{x1:.2f},{y1:.2f} A{radius:.2f},{radius:.2f} 0 {large} "
                f"{sweep} {x2:.2f},{y2:.2f}")

    within = sorted([e for e in edges if not e["cross_region"]],
                    key=lambda e: e["count"])
    cross = sorted([e for e in edges if e["cross_region"]],
                   key=lambda e: e["count"])

    # --- defs: dot grid, one blended gradient per cross-continent chord, and
    # the arc paths that carry the continent names around the rim -----------
    defs = ['<pattern id="dots" width="22" height="22" patternUnits="userSpaceOnUse">'
            '<circle cx="1.4" cy="1.4" r="1.1" fill="rgba(28,21,16,.06)"/></pattern>']
    gradients = {}
    for i, e in enumerate(cross):
        x1, y1 = layout[e["a"]]["x"], layout[e["a"]]["y"]
        x2, y2 = layout[e["b"]]["x"], layout[e["b"]]["y"]
        ca = REGION_COLOR.get(e["region_a"], "#8a8278")
        cb = REGION_COLOR.get(e["region_b"], "#8a8278")
        gid = f"chordgrad{i}"
        gradients[(e["a"], e["b"])] = gid
        defs.append(
            f'<linearGradient id="{gid}" gradientUnits="userSpaceOnUse" '
            f'x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}">'
            f'<stop offset="0" stop-color="{ca}"/>'
            f'<stop offset=".16" stop-color="{ca}"/>'
            f'<stop offset=".84" stop-color="{cb}"/>'
            f'<stop offset="1" stop-color="{cb}"/></linearGradient>')
    for reg in lay["families"]:
        a0, a1 = reg["span"]
        if math.sin(reg["mid"]) < 0:      # top half reads left-to-right
            pa, pb, sweep = a0, a1, 1
        else:                             # bottom half: reverse to stay upright
            pa, pb, sweep = a1, a0, 0
        x0, y0 = _pt(cx, cy, pa, r_reg)
        x1, y1 = _pt(cx, cy, pb, r_reg)
        defs.append(
            f'<path id="famarc-{reg["id"]}" d="M{x0:.2f},{y0:.2f} '
            f'A{r_reg:.2f},{r_reg:.2f} 0 0 {sweep} {x1:.2f},{y1:.2f}"/>')

    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {NET_W} {NET_H}" '
        f'width="{NET_W}" height="{NET_H}" role="img" '
        f'aria-label="Countries arranged in a ring by continent, joined by '
        f'chords for the people who hold two citizenships">')
    parts.append("<defs>" + "".join(defs) + "</defs>")
    parts.append(f'<rect x="0" y="0" width="{NET_W}" height="{NET_H}" '
                 f'fill="url(#dots)"/>')

    # --- continent arcs on the rim ----------------------------------------
    for reg in lay["families"]:
        a0, a1 = reg["span"]
        parts.append(
            f'<path d="{_arc(cx, cy, R, a0, a1)}" fill="none" '
            f'stroke="{reg["color"]}" stroke-opacity="0.16" stroke-width="11" '
            f'stroke-linecap="round"><title>{_esc(reg["label"])}: '
            f'{len(reg["members"])} countries drawn</title></path>')

    drawn: dict = {}
    # --- within-continent ties reinforce their own arc ---------------------
    for e in within:
        t = strength(e["count"])
        radius = R - 10.0 - 4.0 * t
        d = within_d(e, radius)
        a1 = layout[e["a"]]["angle"]
        a2 = layout[e["b"]]["angle"]
        delta = (a2 - a1) % (2 * math.pi)
        if delta > math.pi:
            delta -= 2 * math.pi
        drawn[(e["a"], e["b"])] = (d, 0.7 + 2.3 * t,
                                   ("arc", a1 + delta / 2.0, radius))
        color = REGION_COLOR.get(e["region_a"], "#8a8278")
        parts.append(
            f'<path d="{d}" fill="none" stroke="{color}" '
            f'stroke-opacity="{0.22 + 0.55 * t ** 0.85:.3f}" '
            f'stroke-width="{0.8 + 2.6 * t ** 0.85:.2f}" stroke-linecap="round"><title>'
            f'{_esc(_display(e["a"]))} \u2014 {_esc(_display(e["b"]))}: '
            f'{_n(e["count"])} people hold both \u00b7 same continent</title></path>')

    # --- cross-continent chords: colour blends the two continents ----------
    for e in cross:
        t = strength(e["count"])
        d, p0, c1, c2, p1 = chord_d(e)
        width = 0.4 + 3.2 * t ** 1.35
        drawn[(e["a"], e["b"])] = (d, width, ("cubic", p0, c1, c2, p1))
        la = REGION_LABEL.get(e["region_a"], e["region_a"])
        lb = REGION_LABEL.get(e["region_b"], e["region_b"])
        parts.append(
            f'<path d="{d}" fill="none" '
            f'stroke="url(#{gradients[(e["a"], e["b"])]})" '
            f'stroke-opacity="{0.07 + 0.62 * t ** 1.7:.3f}" '
            f'stroke-width="{width:.2f}" stroke-linecap="round"><title>'
            f'{_esc(_display(e["a"]))} \u2014 {_esc(_display(e["b"]))}: '
            f'{_n(e["count"])} people hold both \u00b7 {_esc(la)} \u2194 '
            f'{_esc(lb)}</title></path>')

    # --- the strongest bridges: a soft orange halo (listed in the panel) ---
    for b in _bridge_list(payload):
        key = (b["a"], b["b"])
        if key not in drawn:
            continue
        d, width, _ctrl = drawn[key]
        parts.append(
            f'<path d="{d}" fill="none" stroke="#bf6b28" stroke-opacity="0.22" '
            f'stroke-width="{width + 7.5:.2f}" stroke-linecap="round"/>')

    # --- country dots (size is the data) -----------------------------------
    for code in sorted(layout, key=lambda m: layout[m]["r"]):
        nd = layout[code]
        n = nodes[code]
        color = REGION_COLOR.get(n["region"], "#8a8278")
        parts.append(
            f'<circle cx="{nd["x"]:.1f}" cy="{nd["y"]:.1f}" r="{nd["r"]:.1f}" '
            f'fill="{color}" stroke="#f9f8f4" stroke-width="1.7"><title>'
            f'{_esc(n["name"])}: {_n(n["total_people"])} dual nationals \u00b7 '
            f'{_esc(n["region_label"])}</title></circle>')

    # --- country labels, radial so 60 never collide ------------------------
    for code in sorted(layout, key=lambda m: -nodes[m]["total_people"]):
        nd = layout[code]
        a = nd["angle"]
        lx, ly = _pt(cx, cy, a, R + nd["r"] + 5.0)
        deg = math.degrees(a)
        if math.cos(a) >= -1e-9:
            anchor, rot = "start", deg
        else:
            anchor, rot = "end", deg + 180.0
        parts.append(
            f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="{anchor}" '
            f'dominant-baseline="middle" class="nlabel" '
            f'transform="rotate({rot:.1f} {lx:.1f} {ly:.1f})">'
            f'{_esc(nodes[code]["name"])}</text>')

    # --- continent names follow their arc ----------------------------------
    for reg in lay["families"]:
        parts.append(
            f'<text class="family-label" font-size="{reg["size"]}" '
            f'text-anchor="middle"><title>{_esc(reg["label"])}</title>'
            f'<textPath href="#famarc-{reg["id"]}" startOffset="50%">'
            f'{_esc(reg["short"])}</textPath></text>')

    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Card HTML
# ---------------------------------------------------------------------------

HTML_HEAD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>HumansMap — the dual-citizenship web</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;800&family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:#f9f8f4; --text:#1c1510;
    --chrome:#1b4d8e;          /* chrome only: wordmark, eyebrow, headline emphasis */
    --data:#bf6b28;            /* data highlight only: strongest bridges */
    --structure:#8a8278;       /* structure: dot grid, neutral marks */
    --surface:#ece8de; --line:rgba(28,21,16,.10);
    --display:'Playfair Display', Georgia, serif; --body:'DM Sans', system-ui, sans-serif;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:#d9d4c9; font-family:var(--body); padding:32px; }
  .card { width:1600px; height:1000px; background:var(--bg); color:var(--text);
    padding:30px 44px 16px; display:flex; flex-direction:column; position:relative;
    overflow:hidden; }
  .card header { display:flex; justify-content:space-between; align-items:baseline; }
  .eyebrow { font:700 15px/1 var(--body); letter-spacing:.2em; text-transform:uppercase;
    color:var(--chrome); }
  .brand { font:700 27px/1 var(--display); color:var(--chrome); }
  h1 { font:800 35px/1.06 var(--display); margin:9px 0 0; letter-spacing:-.012em;
    max-width:62ch; }
  h1 em { font-style:normal; color:var(--chrome); }
  .deck { font:400 13.6px/1.42 var(--body); color:var(--structure); margin:7px 0 0;
    max-width:1512px; }
  .deck b { color:var(--text); font-weight:700; }
  .stage { margin-top:6px; flex:0 0 740px; display:flex; gap:34px; }
  .chart { flex:0 0 1020px; width:1020px; height:740px; }
  .chart svg { display:block; width:1020px; height:740px; overflow:visible; }
  .nlabel { font:500 9.6px/1 var(--body); fill:var(--text); paint-order:stroke;
    stroke:#f9f8f4; stroke-width:2.6px; stroke-linejoin:round; }
  .family-label { font:700 11px/1 var(--body); letter-spacing:.15em;
    fill:var(--structure); paint-order:stroke; stroke:#f9f8f4; stroke-width:3px; }
  .badge { font:700 9.6px/1 var(--body); fill:#fff; }
  .keys { flex:1 1 auto; min-width:0; display:flex; flex-direction:column; }
  .plabel { font:700 11px/1 var(--body); letter-spacing:.16em; text-transform:uppercase;
    color:var(--structure); margin-bottom:9px; }
  .read { display:flex; flex-direction:column; gap:8px; font:400 12.2px/1.35 var(--body);
    color:var(--structure); }
  .read .row { display:flex; align-items:center; gap:10px; }
  .read b { color:var(--text); }
  .swatch { flex:0 0 auto; }
  .famdot { flex:0 0 auto; width:11px; height:11px; border-radius:3px;
    display:inline-block; }
  .badge-dot { flex:0 0 auto; width:17px; height:17px; border-radius:50%;
    background:var(--data); color:#fff; font:700 9.6px/17px var(--body);
    text-align:center; }
  .famgrid { display:grid; grid-template-columns:1fr 1fr; gap:5px 16px;
    margin:12px 0 4px; }
  .fam { display:flex; align-items:center; gap:8px; font:500 11.4px/1.2 var(--body);
    color:var(--text); }
  .stats { display:flex; gap:16px; margin-top:15px; padding-top:11px;
    border-top:1px solid var(--line); }
  .stat { flex:1 1 0; }
  .stat b { display:block; font:800 19px/1 var(--display); color:var(--text); }
  .stat span { display:block; font:500 9.5px/1.25 var(--body); color:var(--structure);
    text-transform:uppercase; letter-spacing:.06em; margin-top:3px; }
  .bridges { list-style:none; margin:2px 0 0; padding:0; }
  .bridges li { padding:6px 0 5px; border-bottom:1px solid var(--line); }
  .bhead { display:flex; align-items:baseline; gap:9px; }
  .bnum { flex:0 0 auto; width:16px; height:16px; border-radius:50%;
    background:var(--data); color:#fff; font:700 9.6px/16px var(--body);
    text-align:center; }
  .bname { font:600 12.8px/1.25 var(--body); flex:1 1 auto; }
  .bname b { color:var(--text); }
  .bcount { font:800 14px/1 var(--display); color:var(--data); white-space:nowrap; }
  .bmeta { font:400 10.3px/1.28 var(--body); color:var(--structure);
    margin:2px 0 0 25px; }
  .caveat { font:400 11px/1.4 var(--body); color:var(--structure); margin-top:auto;
    padding-top:9px; border-top:1px solid var(--line); }
  .caveat b { color:var(--text); }
  .card footer { margin-top:8px; display:flex; justify-content:space-between;
    align-items:baseline; font:400 12.3px/1.4 var(--body); color:var(--structure); }
</style></head>
<body>"""


def render_html(payload: dict) -> str:
    lay = build_layout(payload)
    scene = build_scene(payload, lay)

    scale = payload["scale"]
    thresholds = payload["thresholds"]
    shown = _bridge_list(payload)

    biggest = shown[0] if shown else None
    nationalities = scale.get("dual_nationals", 0)

    headline = "Countries, linked by the people who hold two or more passports."

    deck = ""
    if biggest:
        a, b = biggest["a"], biggest["b"]
        totals = {n["code"]: n["total_people"] for n in payload["nodes"]}
        if totals.get(a, 0) < totals.get(b, 0):
            a, b = b, a
        deck = (
            f"Every person whose Wikidata record lists two or more citizenships "
            f"(<b>{_n(nationalities)}</b> people). A person with three or more "
            f"counts in each pair they belong to. The "
            f"<b>{_n(scale['nodes_drawn'])}</b> best-documented countries and their "
            f"<b>{_n(scale['edges_drawn'])}</b> pairs are arranged in a "
            f"ring, grouped and coloured by <b>continent</b>: ties inside one "
            f"continent hug the rim, while ties that cross the middle are bridges "
            f"<b>between</b> continents. Dot size is how many dual nationals a "
            f"country holds, chord colour blends the two continents it joins, and "
            f"chord thickness is how many people hold both. The largest single "
            f"bridge is <b>{_esc(_display(a))}&ndash;{_esc(_display(b))}</b> "
            f"({_n(biggest['count'])} people)."
        )

    def bridge_li(e: dict, idx: int) -> str:
        totals = {n["code"]: n["total_people"] for n in payload["nodes"]}
        a, b = ((e["a"], e["b"]) if totals.get(e["a"], 0) >= totals.get(e["b"], 0)
                else (e["b"], e["a"]))
        ex = e.get("examples", [])[:3]
        shown_ex = " \u00b7 ".join(ex)
        if len(e.get("examples", [])) > 3:
            shown_ex += f" +{len(e['examples']) - 3}"
        meta = f'<div class="bmeta">{_esc(shown_ex)}</div>' if shown_ex else ""
        return (
            f'<li><div class="bhead"><span class="bnum">{idx}</span>'
            f'<span class="bname"><b>{_esc(_display(a))}</b> \u2194 '
            f'{_esc(_display(b))}</span>'
            f'<span class="bcount">{_n(e["count"])}</span></div>{meta}</li>'
        )

    caveat = (
        "<b>Read with care.</b> Citizenship is not residency or birthplace. Only "
        "people whose Wikidata record lists more than one citizenship are counted, "
        "and that coverage skews to the US and Europe, so this is partly a map of "
        "Wikipedia; richer Wikipedia communities can make some regions look far "
        "more connected than they are in life. Only the <b>"
        f"{_n(scale['nodes_drawn'])} best-documented countries</b> are drawn; the "
        "rest are left out so the ring stays legible."
    )

    americas = REGION_COLOR["americas"]
    europe = REGION_COLOR["europe"]
    legend = (
        '<div class="read">'
        '<div class="row"><svg class="swatch" width="58" height="20">'
        '<circle cx="7" cy="10" r="3" fill="#8a8278"/>'
        '<circle cx="24" cy="10" r="5.4" fill="#8a8278"/>'
        '<circle cx="45" cy="10" r="8" fill="#8a8278"/></svg>'
        '<span>Each dot is a country; its <b>size</b> = dual nationals there</span></div>'
        '<div class="row"><svg class="swatch" width="58" height="16">'
        '<defs><linearGradient id="lg-chord" x1="0" y1="0" x2="1" y2="0">'
        f'<stop offset="0" stop-color="{americas}"/>'
        f'<stop offset="1" stop-color="{europe}"/></linearGradient></defs>'
        '<path d="M2,12 C20,2 38,2 56,12" fill="none" stroke="url(#lg-chord)" '
        'stroke-width="3"/></svg>'
        '<span>A chord links two countries; <b>thickness</b> = people who hold '
        'both</span></div>'
        f'<div class="row"><span class="famdot" style="background:{americas}"></span>'
        '<span>Colour = <b>region</b> (key below); a chord blends the two '
        'regions it joins</span></div>'
        '<div class="row"><svg class="swatch" width="58" height="18">'
        '<line x1="2" y1="15" x2="56" y2="15" stroke="#c9c1b4" stroke-width="1.4"/>'
        '<path d="M9,15 A19,10 0 0 1 49,15" fill="none" stroke="#8a8278" '
        'stroke-width="2.2" stroke-linecap="round"/></svg>'
        '<span>A tie inside one continent hugs the rim; a chord across the middle '
        'links two <b>different continents</b></span></div>'
        '<div class="row"><svg class="swatch" width="58" height="14">'
        '<path d="M2,7 C20,2 38,2 56,7" fill="none" stroke="#bf6b28" '
        'stroke-opacity="0.28" stroke-width="8" stroke-linecap="round"/>'
        '<path d="M2,7 C20,2 38,2 56,7" fill="none" stroke="#bf6b28" '
        'stroke-width="2.2" stroke-linecap="round"/></svg>'
        '<span>An orange halo marks one of the <b>strongest bridges</b> '
        '(listed at right)</span></div>'
        '</div>'
    )

    famgrid = "".join(
        f'<div class="fam"><span class="famdot" style="background:{reg["color"]}">'
        f'</span><span>{_esc(reg["label"])}</span></div>'
        for reg in lay["families"])

    stats = (
        f'<div class="stat"><b>{_n(nationalities)}</b>'
        '<span>dual nationals</span></div>'
        f'<div class="stat"><b>{_n(scale["nodes_drawn"])}</b>'
        '<span>countries drawn</span></div>'
        f'<div class="stat"><b>{_n(scale["edges_drawn"])}</b>'
        '<span>linked pairs</span></div>'
        f'<div class="stat"><b>{_n(scale["regions_drawn"])}</b>'
        '<span>regions</span></div>'
    )

    html = f"""<section class="card">
  <header>
    <span class="eyebrow">Dual nationality</span>
    <span class="brand">Humans&nbsp;Map</span>
  </header>
  <h1>{headline}</h1>
  <p class="deck">{deck}</p>
  <div class="stage">
    <div class="chart">{scene}</div>
    <aside class="keys">
      <div class="plabel">How to read it</div>
      {legend}
      <div class="famgrid">{famgrid}</div>
      <div class="stats">{stats}</div>
      <div class="plabel" style="margin-top:12px">Strongest bridges &mdash; people who hold both</div>
      <ul class="bridges">{''.join(bridge_li(e, i) for i, e in enumerate(shown, start=1))}</ul>
      <div class="caveat">{caveat}</div>
    </aside>
  </div>
  <footer>
    <span>Data: Wikidata (CC BY-SA) &middot; humansmap.com</span>
    <span>{_n(scale['nodes_drawn'])} of {_n(scale['countries'])} countries &middot; {_n(scale['edges_drawn'])} linked pairs &middot; &ge;{_n(thresholds['min_pair'])} people per pair</span>
  </footer>
</section>"""

    return HTML_HEAD + html + "\n</body></html>\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default="docs/marketing/dualnat")
    parser.add_argument("--min-statements", type=int, default=30)
    parser.add_argument("--min-pair", type=int, default=8)
    parser.add_argument("--max-persons", type=int, default=250000)
    parser.add_argument("--max-nodes", type=int, default=60,
                        help="countries kept by dual-national count (default: 60)")
    parser.add_argument("--from-json", default=None,
                        help="re-render the HTML from an existing dualnat.json")
    parser.add_argument("--no-html", action="store_true")
    args = parser.parse_args()

    if args.from_json:
        payload = json.loads(Path(args.from_json).read_text())
    else:
        db = get_db(get_arango_client(request_timeout=600))
        payload = collect(db, args.min_statements, args.min_pair, args.max_persons)

    analyze(payload, args.max_nodes)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if not args.from_json:
        json_path = out_dir / "dualnat.json"
        json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        print(f"wrote {json_path}")

    if not args.no_html:
        html_path = out_dir / "dualnat.html"
        html_path.write_text(render_html(payload))
        print(f"wrote {html_path}")

    print(json.dumps({
        "thresholds": payload["thresholds"],
        "scale": payload["scale"],
        "regions": [
            {"id": r["id"], "label": r["label"], "countries": r["countries"]}
            for r in payload.get("regions", [])
        ],
        "top_pairs": [
            {"a": e["a"], "b": e["b"], "count": e["count"]}
            for e in payload["top_pairs"][:12]
        ],
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
