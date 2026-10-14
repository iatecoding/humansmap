#!/usr/bin/env python3
"""Marketing inheritance miner — read-only, emits the "is your job like your job?" card.

The insight: does a child tend to share a parent's occupation — and does it
matter whether the parent passing it on is the father, the mother, or whether
*both* parents work in the same field?

It never writes to ArangoDB. Two bounded, indexed aggregations:

  1. ``relationships`` parent edges are walked from the PARENT side using the
     edge's ``_to`` index (``e._to == parent._id``) — parent edges point
     CHILD -> PARENT, so this is the cheap direction. One ``DOCUMENT`` per
     edge for the child. Each row is a (parent gender, parent occupation,
     child occupation) flow with a count.
  2. A second per-child pass collects every parent with a recorded occupation
     for children of at least one well-documented parent, classifies the child
     as father-only / mother-only / both-same-field / both-different-field and
     measures the same-field rate in each bucket.

Both queries are bounded by ``--min-statements`` (default 100, the parent
notability floor) and ``--scan-limit`` (a hard ceiling on how many parents are
visited, default 250000 — at the default floor there are only ~34k, so it is a
safety net, not a sampling cut).

Run it manually inside the backend container:

    docker compose exec -T backend python - --out-dir /tmp/inheritance \\
        < backend/scripts/marketing_inheritance.py
    docker compose exec -T backend cat /tmp/inheritance/inheritance.json \\
        > docs/marketing/inheritance/inheritance.json
    docker compose exec -T backend cat /tmp/inheritance/inheritance.html \\
        > docs/marketing/inheritance/inheritance.html

Flags:
    --out-dir PATH       output directory (default: docs/marketing/inheritance)
    --min-statements N   parent Wikidata-statement floor (default: 100)
    --scan-limit N       max parents visited per query (default: 250000)
    --no-html            emit only the JSON payload
"""

from __future__ import annotations

import argparse
import json
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

DEFAULT_MIN_STATEMENTS = 100
DEFAULT_SCAN_LIMIT = 250_000
TOP_PARENTS = 10
TOP_CHILDREN = 10


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

# Parent -> child occupation flows. ``relationships`` is an edge collection, so
# ArangoDB's built-in edge index serves ``e._to == p._id`` — no full scan and
# no DOCUMENT-per-edge over the whole 1.7M parent edges.
FLOWS_AQL = """
FOR p IN persons
  FILTER p.wikidata_statements_count >= @min_statements
    AND p.primary_occupation != null
  LIMIT @scan_limit
  FOR e IN relationships
    FILTER e._to == p._id AND e.type == 'parent'
    LET ch = DOCUMENT(e._from)
    FILTER ch != null AND ch.primary_occupation != null
    COLLECT pg = p.gender, po = p.primary_occupation, co = ch.primary_occupation WITH COUNT INTO n
    SORT n DESC
    LIMIT @flow_limit
    RETURN {parent_gender: pg, parent_occupation: po, child_occupation: co, count: n}
"""

# Per-child classification. Children are reached from a well-documented parent,
# then every parent with an occupation is gathered for that child (so a
# less-documented second parent still counts). Result rows are aggregated by
# (role, matched?, shared occupation), never returned per child.
ROLES_AQL = """
FOR p IN persons
  FILTER p.wikidata_statements_count >= @min_statements
    AND p.primary_occupation != null
    AND p.gender IN ['male', 'female']
  LIMIT @scan_limit
  FOR e IN relationships
    FILTER e._to == p._id AND e.type == 'parent'
    LET ch = DOCUMENT(e._from)
    FILTER ch != null AND ch.primary_occupation != null
    COLLECT child = ch._id, cocc = ch.primary_occupation INTO grp
    LET allp = (
      FOR e2 IN relationships
        FILTER e2._from == child AND e2.type == 'parent'
        LET q = DOCUMENT(e2._to)
        FILTER q != null AND q.primary_occupation != null
        RETURN {g: q.gender, o: q.primary_occupation}
    )
    LET mset = UNIQUE(allp[* FILTER CURRENT.g == 'male' RETURN CURRENT.o])
    LET fset = UNIQUE(allp[* FILTER CURRENT.g == 'female' RETURN CURRENT.o])
    LET has_m = LENGTH(mset) > 0
    LET has_f = LENGTH(fset) > 0
    LET f_only = has_m AND NOT has_f
    LET m_only = has_f AND NOT has_m
    LET both = has_m AND has_f
    LET both_same = both AND LENGTH(UNION_DISTINCT(mset, fset)) == 1
    LET role = f_only ? 'father_only'
      : (m_only ? 'mother_only' : (both_same ? 'both_same_field' : 'both_diff_field'))
    LET same = f_only ? (cocc IN mset)
      : (m_only ? (cocc IN fset)
      : (both_same ? cocc == FIRST(mset)
      : (cocc IN mset OR cocc IN fset)))
    LET shared = both_same ? FIRST(mset) : null
    COLLECT r = role, s = same, o = shared WITH COUNT INTO n
    SORT n DESC
    RETURN {role: r, same: s, occupation: o, count: n}
"""

PARENT_EDGES_AQL = """
RETURN LENGTH(FOR e IN relationships FILTER e.type == 'parent' LIMIT 2000000 RETURN 1)
"""

PARENTS_CONSIDERED_AQL = """
RETURN LENGTH(
  FOR p IN persons
    FILTER p.wikidata_statements_count >= @min_statements AND p.primary_occupation != null
    RETURN 1
)
"""


def _rows(db, aql: str, bind: dict | None = None) -> list:
    try:
        return list(db.aql.execute(aql, bind_vars=bind or {}))
    except Exception as exc:  # noqa: BLE001 - a marketing job must not crash
        print(f"[warn] query failed: {exc}", file=sys.stderr)
        return []


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------


def _rate(same: float, total: float) -> float:
    return (same / total) if total else 0.0


def collect(min_statements: int, scan_limit: int) -> dict:
    db = get_db(get_arango_client(request_timeout=300))
    bind = {"min_statements": min_statements, "scan_limit": scan_limit}

    flows_raw = _rows(db, FLOWS_AQL, {**bind, "flow_limit": 400_000})
    roles_raw = _rows(db, ROLES_AQL, bind)
    parent_edges = _rows(db, PARENT_EDGES_AQL)
    parents_considered = _rows(db, PARENTS_CONSIDERED_AQL, {"min_statements": min_statements})

    # --- flows -------------------------------------------------------------
    flows = [
        OrderedDict(
            parent_occupation=r.get("parent_occupation") or "",
            child_occupation=r.get("child_occupation") or "",
            parent_gender=r.get("parent_gender"),
            count=int(r.get("count") or 0),
        )
        for r in flows_raw
    ]
    flows.sort(key=lambda f: f["count"], reverse=True)

    pairs = sum(f["count"] for f in flows)
    same_pairs = sum(f["count"] for f in flows if f["parent_occupation"] == f["child_occupation"])
    baseline = {"pairs": pairs, "same": same_pairs, "same_rate": _rate(same_pairs, pairs)}

    # --- per parent gender (link-level) -----------------------------------
    by_parent_gender: dict[str, dict] = {}
    for g in ("male", "female"):
        links = sum(f["count"] for f in flows if f["parent_gender"] == g)
        same = sum(
            f["count"]
            for f in flows
            if f["parent_gender"] == g and f["parent_occupation"] == f["child_occupation"]
        )
        by_parent_gender[g] = {"links": links, "same": same, "same_rate": _rate(same, links)}

    # --- per-role (child-level) -------------------------------------------
    role_totals: dict[str, dict] = defaultdict(lambda: {"children": 0, "same": 0})
    both_same_by_occ: dict[str, dict] = defaultdict(lambda: {"children": 0, "same": 0})
    for r in roles_raw:
        role = r.get("role") or "other"
        n = int(r.get("count") or 0)
        is_same = bool(r.get("same"))
        role_totals[role]["children"] += n
        if is_same:
            role_totals[role]["same"] += n
        if role == "both_same_field":
            occ = r.get("occupation") or ""
            both_same_by_occ[occ]["children"] += n
            if is_same:
                both_same_by_occ[occ]["same"] += n

    by_role: dict[str, dict] = {}
    for role, vals in role_totals.items():
        by_role[role] = {
            "children": vals["children"],
            "same": vals["same"],
            "same_rate": _rate(vals["same"], vals["children"]),
        }

    both_same_occ = [
        {
            "occupation": occ,
            "children": v["children"],
            "same": v["same"],
            "same_rate": _rate(v["same"], v["children"]),
            "multiple_of_baseline": (_rate(v["same"], v["children"]) / baseline["same_rate"])
            if baseline["same_rate"]
            else 0.0,
        }
        for occ, v in both_same_by_occ.items()
        if v["children"] > 0
    ]
    both_same_occ.sort(key=lambda r: (-r["same_rate"], -r["children"]))

    # --- top occupations with father/mother same-field rates ---------------
    occ_gender: dict[str, dict] = defaultdict(
        lambda: {"male": {"links": 0, "same": 0}, "female": {"links": 0, "same": 0}}
    )
    for f in flows:
        pg = f["parent_gender"]
        if pg not in ("male", "female"):
            continue
        bucket = occ_gender[f["parent_occupation"]][pg]
        bucket["links"] += f["count"]
        if f["parent_occupation"] == f["child_occupation"]:
            bucket["same"] += f["count"]

    top_occupations = []
    for occ, g in sorted(
        occ_gender.items(),
        key=lambda kv: kv[1]["male"]["links"] + kv[1]["female"]["links"],
        reverse=True,
    )[:12]:
        m = g["male"]
        fm = g["female"]
        top_occupations.append(
            {
                "occupation": occ,
                "links": m["links"] + fm["links"],
                "same_rate": _rate(
                    m["same"] + fm["same"], m["links"] + fm["links"]
                ),
                "father": {"links": m["links"], "same": m["same"], "same_rate": _rate(m["same"], m["links"])},
                "mother": {"links": fm["links"], "same": fm["same"], "same_rate": _rate(fm["same"], fm["links"])},
            }
        )

    # --- headline ----------------------------------------------------------
    candidates = [r for r in both_same_occ if r["children"] >= 25]
    headline = None
    if candidates:
        best = max(candidates, key=lambda r: (r["same_rate"], r["children"]))
        headline = {
            "occupation": best["occupation"],
            "children": best["children"],
            "same": best["same"],
            "same_rate": best["same_rate"],
            "baseline_rate": baseline["same_rate"],
            "multiple": best["multiple_of_baseline"],
            "text": (
                f"Children of two {_plural(_pretty(best['occupation']))} are "
                f"{best['multiple_of_baseline']:.1f}\u00d7 more likely to become "
                f"{_plural(_pretty(best['occupation']))}."
            ),
        }

    return {
        "generated": date.today().isoformat(),
        "params": {"min_statements": min_statements, "scan_limit": scan_limit},
        "scale": {
            "parent_edges": parent_edges[0] if parent_edges else None,
            "parents_considered": parents_considered[0] if parents_considered else None,
            "flow_pairs": pairs,
            "distinct_parent_occupations": len({f["parent_occupation"] for f in flows}),
        },
        "baseline": baseline,
        "by_parent_gender": by_parent_gender,
        "by_role": by_role,
        "flows": flows,
        "top_occupations": top_occupations,
        "both_same_field_by_occupation": both_same_occ,
        "headline": headline,
    }


# ---------------------------------------------------------------------------
# Formatting helpers (shared by the SVG and the HTML panel)
# ---------------------------------------------------------------------------


def _pretty(occ: str) -> str:
    return (occ or "").replace("_", " ").strip()


def _label(occ: str) -> str:
    s = _pretty(occ)
    return s[:1].upper() + s[1:] if s else s


def _plural(word: str) -> str:
    if word.endswith("person"):
        return word[:-6] + "people"
    if word.endswith("man"):
        return word[:-3] + "men"
    if word.endswith(("s", "x", "z", "ch", "sh")):
        return word + "es"
    if word.endswith("y") and len(word) > 1 and word[-2] not in "aeiou":
        return word[:-1] + "ies"
    return word + "s"


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


# ---------------------------------------------------------------------------
# Inheritance matrix (inline SVG)
#
# The old alluvial became a hairball: hundreds of sub-pixel ribbons, two
# independently-ordered columns, and no way to follow a single trade. A square
# matrix is honest and legible: rows are the parent's occupation, columns the
# child's, and each cell's shade is the share of that parent's children who took
# that job. The diagonal is the same trade, so "which trades are sticky" reads
# at a glance and no occupation is too small to see.
# ---------------------------------------------------------------------------

MATRIX_W = 1056
MATRIX_LABEL_W = 172
MATRIX_HEAD_H = 132
MATRIX_ROW_H = 32
MATRIX_N = 14
SHARE_MAX = 0.65  # cell shade saturates here (painters peak at ~65%)


def _cell_fill(share: float) -> str:
    a = 0.05 + 0.90 * min(1.0, max(0.0, share) / SHARE_MAX)
    return f"rgba(191,107,40,{a:.3f})"


def _cell_ink(share: float) -> str:
    return "#f9f8f4" if share >= SHARE_MAX * 0.52 else "#1c1510"


def _inheritance_axes(payload: dict):
    """Top occupations on both axes + the parent→child count matrix."""
    flows = payload["flows"]
    p_tot: dict[str, float] = defaultdict(float)
    c_tot: dict[str, float] = defaultdict(float)
    for f in flows:
        p_tot[f["parent_occupation"]] += f["count"]
        c_tot[f["child_occupation"]] += f["count"]
    comb = {o: p_tot[o] + c_tot[o] for o in set(p_tot) | set(c_tot)}
    order = [o for o, _ in sorted(comb.items(), key=lambda kv: -kv[1])[:MATRIX_N]]
    o_set = set(order)
    matrix: dict[tuple, float] = defaultdict(float)
    for f in flows:
        if f["parent_occupation"] in o_set and f["child_occupation"] in o_set:
            matrix[(f["parent_occupation"], f["child_occupation"])] += f["count"]
    return order, p_tot, c_tot, matrix


def build_matrix(payload: dict):
    order, p_tot, c_tot, matrix = _inheritance_axes(payload)
    n = len(order)
    cell = (MATRIX_W - MATRIX_LABEL_W) / n
    grid_h = MATRIX_HEAD_H + n * MATRIX_ROW_H
    total = sum(f["count"] for f in payload["flows"]) or 1.0
    o_set = set(order)
    shown = sum(f["count"] for f in payload["flows"] if f["parent_occupation"] in o_set and f["child_occupation"] in o_set)
    coverage = shown / total

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {MATRIX_W} {grid_h}" '
        f'width="{MATRIX_W}" height="{grid_h}" role="img" '
        f'aria-label="Share of each occupation&rsquo;s children who took each occupation">'
    ]
    # Rotated column headers.
    for j, c in enumerate(order):
        cx = MATRIX_LABEL_W + j * cell + cell / 2
        parts.append(
            f'<text x="{cx:.1f}" y="{MATRIX_HEAD_H - 10:.1f}" class="mcol" '
            f'transform="rotate(-52 {cx:.1f} {MATRIX_HEAD_H - 10:.1f})" '
            f'text-anchor="start">{_esc(_label(c))}</text>'
        )
    # Rows: label + n, then one cell per child occupation.
    for i, p in enumerate(order):
        y = MATRIX_HEAD_H + i * MATRIX_ROW_H
        cy = y + MATRIX_ROW_H / 2
        parts.append(
            f'<text x="{MATRIX_LABEL_W - 12:.1f}" y="{cy - 1.5:.1f}" class="mrow" text-anchor="end">{_esc(_label(p))}</text>'
        )
        parts.append(
            f'<text x="{MATRIX_LABEL_W - 12:.1f}" y="{cy + 11.5:.1f}" class="mrow-n" text-anchor="end">{_n(p_tot[p])} children</text>'
        )
        for j, c in enumerate(order):
            x = MATRIX_LABEL_W + j * cell
            share = matrix.get((p, c), 0.0) / (p_tot[p] or 1.0)
            cls = "mcell diag" if p == c else "mcell"
            parts.append(
                f'<rect x="{x + 1.5:.1f}" y="{y + 1.5:.1f}" width="{cell - 3:.1f}" height="{MATRIX_ROW_H - 3:.1f}" '
                f'rx="3" class="{cls}" fill="{_cell_fill(share)}">'
                f'<title>{_esc(_label(p))} \u2192 {_esc(_label(c))}: {share * 100:.1f}% of {_n(p_tot[p])} children</title></rect>'
            )
            if share >= 0.03:
                parts.append(
                    f'<text x="{x + cell / 2:.1f}" y="{cy + 4:.1f}" class="mval" '
                    f'fill="{_cell_ink(share)}" text-anchor="middle">{share * 100:.0f}</text>'
                )
    parts.append("</svg>")
    return "".join(parts), coverage, order, p_tot, matrix


def _same_field_ranking(payload: dict, min_n: int = 250) -> list[dict]:
    """Same-trade rate for every occupation with a solid sample, best first."""
    flows = payload["flows"]
    p_tot: dict[str, float] = defaultdict(float)
    same: dict[str, float] = defaultdict(float)
    for f in flows:
        p_tot[f["parent_occupation"]] += f["count"]
        if f["parent_occupation"] == f["child_occupation"]:
            same[f["parent_occupation"]] += f["count"]
    rows = [
        {"occupation": o, "n": int(p_tot[o]), "share": same.get(o, 0.0) / (p_tot[o] or 1.0)}
        for o in p_tot
        if p_tot[o] >= min_n
    ]
    rows.sort(key=lambda r: -r["share"])
    return rows


# ---------------------------------------------------------------------------
# Card HTML
# ---------------------------------------------------------------------------

HTML_HEAD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>HumansMap: is your job like your parents' job?</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;800&family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:#f9f8f4; --text:#1c1510; --accent:#1b4d8e; --person:#bf6b28;
    --muted:#8a8278; --surface:#ece8de; --line:rgba(28,21,16,.10);
    --display:'Playfair Display', Georgia, serif; --body:'DM Sans', system-ui, sans-serif;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:#d9d4c9; font-family:var(--body); padding:32px; }
  .card { width:1600px; height:1000px; background:var(--bg); color:var(--text);
    padding:38px 44px 26px; display:flex; flex-direction:column; position:relative; overflow:hidden; }
  .card header { display:flex; justify-content:space-between; align-items:baseline; }
  .eyebrow { font:700 15px/1 var(--body); letter-spacing:.2em; text-transform:uppercase; color:var(--accent); }
  .brand { font:700 27px/1 var(--display); color:var(--accent); }
  h1 { font:800 43px/1.06 var(--display); margin:15px 0 0; letter-spacing:-.012em; max-width:34ch; }
  h1 em { font-style:normal; color:var(--person); }
  h1 small { font:500 17px/1 var(--body); color:var(--muted); white-space:nowrap; }
  .deck { font:400 16px/1.45 var(--body); color:var(--muted); margin:9px 0 0; max-width:1180px; }
  .deck b { color:var(--text); font-weight:700; }
  .body { display:flex; gap:30px; margin-top:14px; flex:1; min-height:0; }
  .flow { width:1056px; flex:0 0 1056px; display:flex; flex-direction:column; min-height:0; }
  .flow svg { display:block; width:1056px; height:auto; overflow:visible; }
  .mcol { font:600 11px/1 var(--body); fill:var(--text); }
  .mrow { font:700 13.5px/1 var(--body); fill:var(--text); }
  .mrow-n { font:400 10.5px/1 var(--body); fill:var(--muted); }
  .mval { font:700 11px/1 var(--body); font-variant-numeric:tabular-nums; }
  .diag { stroke:#1c1510; stroke-width:1.6; }
  .legend { display:flex; gap:22px; align-items:center; margin-top:9px;
    font:400 12px/1.35 var(--body); color:var(--muted); }
  .legend span { display:flex; align-items:center; gap:7px; }
  .legend .bar { width:92px; height:11px; border-radius:3px;
    background:linear-gradient(90deg, rgba(191,107,40,.05), rgba(191,107,40,.95)); }
  .legend .diagkey { width:13px; height:13px; border-radius:3px; background:rgba(191,107,40,.35);
    border:1.6px solid #1c1510; }
  .panel { flex:1; min-width:0; display:flex; flex-direction:column; }
  .block { border-top:2px solid var(--text); padding-top:9px; }
  .block + .block { margin-top:14px; }
  .plabel { font:700 11px/1 var(--body); letter-spacing:.18em; text-transform:uppercase; color:var(--muted); }
  ul.same { list-style:none; margin:9px 0 0; padding:0; }
  ul.same li { display:grid; grid-template-columns:1fr 92px 44px; align-items:center;
    gap:10px; padding:5.5px 0; border-bottom:1px solid var(--line); }
  .sname { font:600 14px/1.1 var(--body); }
  .sname em { display:block; font:400 11px/1.4 var(--body); font-style:normal; color:var(--muted); }
  .sbar { position:relative; display:block; height:9px; background:var(--surface); border-radius:5px; }
  .sbar i { position:absolute; left:0; top:0; height:100%; border-radius:5px; background:var(--person); }
  .sbar b { position:absolute; top:-2px; bottom:-2px; width:2px; background:var(--text); opacity:.5; }
  .sval { font:700 17px/1 var(--display); text-align:right; }
  .role { margin-top:8px; }
  .rolehead { display:flex; justify-content:space-between; align-items:baseline; }
  .rn { font:700 14px/1 var(--body); }
  .rv { font:800 26px/1 var(--display); }
  .rv small { font-size:14px; }
  .track { position:relative; height:10px; background:var(--surface); border-radius:5px; margin-top:5px; }
  .fill { position:absolute; left:0; top:0; height:100%; border-radius:5px; background:var(--person); }
  .base { position:absolute; top:-2px; bottom:-2px; width:2px; background:var(--text); opacity:.5; }
  .rsub { font:400 11px/1.3 var(--body); color:var(--muted); margin-top:4px; }
  .basenote { font:400 11.5px/1.35 var(--body); color:var(--muted); margin-top:8px; }
  .caveat { font:400 11.5px/1.4 var(--body); color:var(--muted); margin-top:auto; }
  .card footer { margin-top:auto; padding-top:10px; display:flex; justify-content:space-between;
    align-items:baseline; font:400 13px/1.4 var(--body); color:var(--muted); }
  .card footer b { color:var(--text); font-weight:700; }
</style></head>
<body>"""


def render_html(payload: dict) -> str:
    scene, coverage, order, p_tot, matrix = build_matrix(payload)
    row_min = min((p_tot[o] for o in order), default=0)
    row_max = max((p_tot[o] for o in order), default=0)
    base = payload["baseline"]["same_rate"]
    roles = payload.get("by_role", {})
    father = roles.get("father_only", {"children": 0, "same": 0, "same_rate": 0.0})
    mother = roles.get("mother_only", {"children": 0, "same": 0, "same_rate": 0.0})
    both_rate = roles.get("both_same_field", {}).get("same_rate", 0.0)
    scale = payload["scale"]

    # Two denominators live in this payload and the card used to print the
    # wrong one under child-level numbers:
    #   * link level  - every qualifying parent->child link; a child with two
    #                   documented parents contributes twice (the old "34.0%").
    #   * child level - every child once (all roles pooled).
    #   * single-parent level - children whose only documented-occupation parent
    #                   is the father or the mother; the fair comparison for the
    #                   father-vs-mother panel.
    role_children = sum(r.get("children", 0) for r in roles.values())
    role_same = sum(r.get("same", 0) for r in roles.values())
    child_base = (role_same / role_children) if role_children else base
    single_children = father.get("children", 0) + mother.get("children", 0)
    single_same = father.get("same", 0) + mother.get("same", 0)
    single_base = (single_same / single_children) if single_children else base

    rank = _same_field_ranking(payload, min_n=250)
    stick = rank[:6]
    top, low = rank[0], rank[-1]
    top_lift = top["share"] / base if base else 0.0
    low_lift = low["share"] / base if base else 0.0

    big = (
        f"How often children follow a parent&rsquo;s trade: "
        f"<em>{_esc(_plural(_label(top['occupation'])))}</em> {top_lift:.1f}&times; the average, "
        f"<em>{_esc(_plural(_label(low['occupation'])))}</em> {low_lift:.1f}&times;."
    )

    deck = (
        f"Parent&rarr;child links where both people have a recorded occupation, from "
        f"parents with a rich Wikidata record (100+ statements): "
        f"<b>{_n(payload['baseline']['pairs'])} links</b>. Rows are the parent&rsquo;s job, "
        f"columns the child&rsquo;s; the shade is the share of that parent&rsquo;s children "
        f"who took the job. The outlined diagonal is the same trade. Counting each link, "
        f"<b>{base * 100:.1f}%</b> are same-trade; counting each child once, "
        f"<b>{child_base * 100:.1f}%</b> of children share an occupation with a documented "
        f"parent. The two trades in the headline rest on "
        f"{_n(top['n'])} and {_n(low['n'])} links."
    )

    bl = min(100.0, max(0.0, base * 100))
    stick_rows = []
    for r in stick:
        pct = min(100.0, max(0.0, r["share"] * 100))
        stick_rows.append(
            f"""<li><span class="sname">{_esc(_label(r['occupation']))}
        <em>n = {_n(r['n'])} children</em></span>
      <span class="sbar"><i style="width:{pct:.1f}%"></i><b style="left:{bl:.1f}%"></b></span>
      <span class="sval">{pct:.0f}%</span></li>"""
        )

    def role_row(name, data, baseline):
        rate = data.get("same_rate", 0.0) * 100
        left = min(100.0, max(0.0, rate))
        bline = min(100.0, max(0.0, baseline * 100))
        return f"""<div class="role">
      <div class="rolehead"><span class="rn">{name}</span><span class="rv">{rate:.1f}<small>%</small></span></div>
      <div class="track"><span class="fill" style="width:{left:.2f}%"></span><span class="base" style="left:{bline:.2f}%"></span></div>
      <div class="rsub">{_n(data.get('children'))} children whose only recorded-occupation parent is {name.lower()}</div>
    </div>"""

    card = f"""<section class="card">
  <header>
    <span class="eyebrow">Job inheritance</span>
    <span class="brand">Humans&nbsp;Map</span>
  </header>
  <h1>{big}</h1>
  <p class="deck">{deck}</p>
  <div class="body">
    <div class="flow">
      {scene}
      <div class="legend">
        <span><i class="bar"></i>share of that parent&rsquo;s children (0&ndash;65%+)</span>
        <span><i class="diagkey"></i>same trade</span>
        <span>showing the {len(order)} largest occupations on both axes ({coverage * 100:.0f}% of links;
          row samples <b>{_n(row_min)}</b>&ndash;<b>{_n(row_max)}</b>)</span>
      </div>
    </div>
    <aside class="panel">
      <div class="block">
        <div class="plabel">The stickiest trades</div>
        <ul class="same">{''.join(stick_rows)}</ul>
        <div class="basenote">Share of a parent&rsquo;s children who took the same job. The tick is
          the <b>{base * 100:.1f}%</b> baseline per link (a child with two documented parents
          counts twice); with <b>both</b> parents in the field, <b>{both_rate * 100:.1f}%</b>.</div>
      </div>
      <div class="block">
        <div class="plabel">Who passes it on?</div>
        {role_row('Father', father, single_base)}
        {role_row('Mother', mother, single_base)}
        <div class="basenote">The tick is the <b>{single_base * 100:.1f}%</b> baseline for children
          with a single documented-occupation parent. Fathers and mothers come out close
          ({father.get('same_rate', 0) * 100:.1f}% vs {mother.get('same_rate', 0) * 100:.1f}%),
          but there are {round(father.get('children', 0) / max(1, mother.get('children', 1)))}&times;
          more documented fathers.</div>
      </div>
      <div class="caveat"><b>Read with care.</b> This measures <em>documented</em> inheritance, not
        real rates: densely-covered professions (actors, politicians, painters, writers) are
        over-represented, and &ldquo;occupation&rdquo; is Wikidata&rsquo;s label set, not a job title.</div>
    </aside>
  </div>
  <footer>
    <span>Data: Wikidata (CC BY-SA) &middot; humansmap.com</span>
    <span><b>{_n(scale.get('flow_pairs'))}</b> parent&rarr;child links analysed</span>
  </footer>
</section>"""

    return HTML_HEAD + card + "\n</body></html>\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default="docs/marketing/inheritance")
    parser.add_argument("--min-statements", type=int, default=DEFAULT_MIN_STATEMENTS)
    parser.add_argument("--scan-limit", type=int, default=DEFAULT_SCAN_LIMIT)
    parser.add_argument("--no-html", action="store_true")
    parser.add_argument(
        "--from-json", default=None,
        help="re-render from an existing inheritance.json instead of querying the DB",
    )
    args = parser.parse_args()

    if args.from_json:
        payload = json.loads(Path(args.from_json).read_text())
        print(f"[cache] loaded {args.from_json} (no DB access)")
    else:
        payload = collect(args.min_statements, args.scan_limit)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if not args.from_json:
        json_path = out_dir / "inheritance.json"
        json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        print(f"wrote {json_path}")

    if not args.no_html:
        html_path = out_dir / "inheritance.html"
        html_path.write_text(render_html(payload))
        print(f"wrote {html_path}")

    summary = {
        "baseline_same_rate": round(payload["baseline"]["same_rate"], 4),
        "father_only": round(payload["by_role"].get("father_only", {}).get("same_rate", 0), 4),
        "mother_only": round(payload["by_role"].get("mother_only", {}).get("same_rate", 0), 4),
        "both_same_field": round(payload["by_role"].get("both_same_field", {}).get("same_rate", 0), 4),
        "headline": (payload.get("headline") or {}).get("text"),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
