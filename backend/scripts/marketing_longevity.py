#!/usr/bin/env python3
"""Marketing longevity miner — read-only, emits the "which jobs live longest?" card.

The insight: age at death per primary occupation, from ``birth_date`` /
``death_date`` on the ``persons`` collection, plus a birth-century breakdown so
the era effect is visible instead of hidden.

It never writes to ArangoDB. There is exactly **one bounded aggregation**:

  * A single pass over ``persons`` where ``wikidata_statements_count`` is at or
    above ``--min-statements`` (default 30 — the persistent index on that field
    bounds the scan; ``death_date`` is not indexed, so the floor is what keeps
    this cheap on a live DB).
  * People must have both dates and a primary occupation. Age is computed with
    ``DATE_DIFF(...,'y')`` when the dates are complete, and falls back to a year
    subtraction for partial dates (Wikidata stores month/day ``00`` for unknown,
    which ``DATE_DIFF`` rejects). Negative (BCE) years are excluded — they are
    all low-quality ancient records, never notables.
  * ``COLLECT occ = primary_occupation, century = FLOOR(birth_year/100)*100
    INTO g KEEP age`` returns, per (occupation, century) pair, the array of
    ages. Python then merges the arrays to get per-occupation percentiles and a
    per-century mean — so the era breakdown rides along in the same pass.

A ``--scan-limit`` safety net (default 600000) caps input rows; at the default
statement floor the eligible set is ~498k, so it never bites.

Run it manually inside the backend container:

    docker compose exec -T backend python - --out-dir /tmp/longevity \\
        < backend/scripts/marketing_longevity.py
    docker compose exec -T backend cat /tmp/longevity/longevity.json \\
        > docs/marketing/longevity/longevity.json
    docker compose exec -T backend cat /tmp/longevity/longevity.html \\
        > docs/marketing/longevity/longevity.html

Flags:
    --out-dir PATH       output directory (default: docs/marketing/longevity)
    --min-sample N       occupation sample floor (default: 50)
    --min-statements N   Wikidata-statement floor, the notability bound (default: 30)
    --scan-limit N       max people fed into the aggregation (default: 600000)
    --no-html            emit only the JSON payload
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
# over stdin (``python -``) __file__ is "<stdin>" and /app is cwd, so fall back.
try:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
except (NameError, OSError):  # pragma: no cover - stdin invocation
    sys.path.insert(0, str(Path.cwd()))

from app.database import get_arango_client, get_db  # noqa: E402

DEFAULT_MIN_SAMPLE = 50
DEFAULT_MIN_STATEMENTS = 30
DEFAULT_SCAN_LIMIT = 600_000
AGE_MIN = 15
AGE_MAX = 110
ERA_MIN = 1400
ERA_MAX = 1900
DISPLAY_TOP = 10
DISPLAY_BOTTOM = 6
DISPLAY_MIN_N = 1000  # card only shows occupations with a solid sample (owner: avoid sub-1k N)
DISPLAY_EXCLUDE = {"criminal"}  # not a profession; avoid stigmatising framing

# One aggregation, grouped by (occupation, birth century). ``KEEP age`` means
# the group only carries the integer ages, not whole person documents.
LONGEVITY_AQL = """
FOR p IN persons
  FILTER p.wikidata_statements_count >= @min_statements
    AND p.birth_date != null AND p.death_date != null
    AND p.primary_occupation != null
    AND p.birth_date[0] != '-' AND p.death_date[0] != '-'
  LET by = TO_NUMBER(SUBSTRING(p.birth_date, 0, 4))
  LET exact = DATE_DIFF(p.birth_date, p.death_date, "y")
  LET age = exact != null ? exact : (TO_NUMBER(SUBSTRING(p.death_date, 0, 4)) - by)
  FILTER age >= @age_min AND age <= @age_max
  LIMIT @scan_limit
  COLLECT occ = p.primary_occupation, century = FLOOR(by / 100) * 100 INTO g KEEP age
  RETURN {occupation: occ, century: century, ages: g[*].age}
"""


def _rows(db, aql: str, bind: dict | None = None) -> list:
    try:
        return list(db.aql.execute(aql, bind_vars=bind or {}))
    except Exception as exc:  # noqa: BLE001 - a marketing job must not crash
        print(f"[warn] query failed: {exc}", file=sys.stderr)
        return []


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------


def _percentile(sorted_vals: list[int], q: float) -> float:
    """Linear-interpolation percentile (numpy default / R-7)."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    rank = (q / 100.0) * (len(sorted_vals) - 1)
    lo = int(math.floor(rank))
    hi = int(math.ceil(rank))
    if lo == hi:
        return float(sorted_vals[lo])
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (rank - lo)


def _summarise(ages: list[int]) -> dict:
    ordered = sorted(ages)
    n = len(ordered)
    total = sum(ordered)
    return {
        "n": n,
        "mean": (total / n) if n else 0.0,
        "p25": _percentile(ordered, 25),
        "p75": _percentile(ordered, 75),
        "min": ordered[0] if n else 0,
        "max": ordered[-1] if n else 0,
    }


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------


def collect(min_statements: int, min_sample: int, scan_limit: int) -> dict:
    db = get_db(get_arango_client(request_timeout=600))
    raw = _rows(
        db,
        LONGEVITY_AQL,
        {
            "min_statements": min_statements,
            "scan_limit": scan_limit,
            "age_min": AGE_MIN,
            "age_max": AGE_MAX,
        },
    )

    occ_ages: dict[str, list[int]] = defaultdict(list)
    cen_ages: dict[int, list[int]] = defaultdict(list)
    for row in raw:
        ages = row.get("ages") or []
        if not ages:
            continue
        occ = row.get("occupation")
        cen = row.get("century")
        if occ:
            occ_ages[occ].extend(ages)
        if cen is not None:
            cen_ages[int(cen)].extend(ages)

    # --- century means (era panel), and the per-century centring baseline ----
    centuries = []
    cen_mean: dict[int, float] = {}
    for cen in sorted(cen_ages):
        s = _summarise(cen_ages[cen])
        cen_mean[cen] = s["mean"]
        centuries.append(
            OrderedDict(
                century=cen,
                label=f"{cen}s",
                n=s["n"],
                mean=s["mean"],
            )
        )

    # --- per occupation -----------------------------------------------------
    occupations = []
    for occ, ages in occ_ages.items():
        s = _summarise(ages)
        if s["n"] < min_sample:
            continue
        occupations.append(
            OrderedDict(
                occupation=occ,
                n=s["n"],
                mean=s["mean"],
                p25=s["p25"],
                p75=s["p75"],
                min=s["min"],
                max=s["max"],
            )
        )
    occupations.sort(key=lambda r: r["mean"], reverse=True)

    # Era-adjusted mean: age minus the mean of the person's own birth century.
    # Computed from the same (occ, century) groups, so it costs nothing extra.
    adj: dict[str, float] = {}
    adj_n: dict[str, int] = {}
    for row in raw:
        occ = row.get("occupation")
        cen = row.get("century")
        if not occ or cen is None:
            continue
        base = cen_mean.get(int(cen))
        if base is None:
            continue
        ages = row.get("ages") or []
        adj[occ] = adj.get(occ, 0.0) + (sum(ages) - base * len(ages))
        adj_n[occ] = adj_n.get(occ, 0) + len(ages)
    for r in occupations:
        r["mean_adj"] = (adj.get(r["occupation"], 0.0) / adj_n[r["occupation"]]) if adj_n.get(r["occupation"]) else 0.0

    all_ages = [a for ages in occ_ages.values() for a in ages]
    overall = _summarise(all_ages)

    eligible = [
        r
        for r in occupations
        if r["n"] >= max(min_sample, DISPLAY_MIN_N) and r["occupation"] not in DISPLAY_EXCLUDE
    ]
    top = eligible[0] if eligible else {}
    bottom = eligible[-1] if eligible else {}
    display = list(
        dict.fromkeys(
            [r["occupation"] for r in eligible[:DISPLAY_TOP]]
            + [r["occupation"] for r in eligible[-DISPLAY_BOTTOM:]]
        )
    )

    # Era-adjusted extremes among occupations with a solid sample.
    solid = [r for r in occupations if r["n"] >= 500]
    adj_top = max(solid, key=lambda r: r["mean_adj"]) if solid else {}
    adj_bottom = min(solid, key=lambda r: r["mean_adj"]) if solid else {}

    era_view = [c for c in centuries if ERA_MIN <= c["century"] <= ERA_MAX]
    era_rise = (era_view[-1]["mean"] - era_view[0]["mean"]) if len(era_view) >= 2 else 0.0

    return {
        "generated": date.today().isoformat(),
        "params": {
            "min_statements": min_statements,
            "min_sample": min_sample,
            "display_min_n": DISPLAY_MIN_N,
            "scan_limit": scan_limit,
            "age_min": AGE_MIN,
            "age_max": AGE_MAX,
        },
        "scale": {
            "people_analysed": overall["n"],
            "distinct_occupations": len(occ_ages),
            "occupations_shown": len(occupations),
            "occupations_at_display_n": sum(
                1 for r in occupations if r["n"] >= DISPLAY_MIN_N),
            "centuries": len(centuries),
            "overall_mean": overall["mean"],
            "overall_p25": overall["p25"],
            "overall_p75": overall["p75"],
        },
        "headline": {
            "top_occupation": top.get("occupation"),
            "top_mean": top.get("mean"),
            "top_n": top.get("n"),
            "bottom_occupation": bottom.get("occupation"),
            "bottom_mean": bottom.get("mean"),
            "bottom_n": bottom.get("n"),
            "era_rise": era_rise,
            "era_from": era_view[0]["label"] if era_view else None,
            "era_to": era_view[-1]["label"] if era_view else None,
            "adj_top": adj_top.get("occupation"),
            "adj_top_delta": adj_top.get("mean_adj"),
            "adj_bottom": adj_bottom.get("occupation"),
            "adj_bottom_delta": adj_bottom.get("mean_adj"),
        },
        "occupations": occupations,
        "display": display,
        "centuries": centuries,
    }


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _pretty(occ: str) -> str:
    return (occ or "").replace("_", " ").strip()


def _label(occ: str) -> str:
    s = _pretty(occ)
    return s[:1].upper() + s[1:] if s else s


def _plural(word: str) -> str:
    if word.lower() == "military person":
        return "military personnel"
    if word.endswith("person"):
        return word[:-6] + "people"
    if word.endswith("man") and not word.endswith(("human", "german")):
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
        return f"{int(round(float(value))):,}"
    except (TypeError, ValueError):
        return "0"


def _signed(value: float) -> str:
    return f"{value:+.1f}"


# ---------------------------------------------------------------------------
# Inline SVG: ranked interval plot + era panel
# ---------------------------------------------------------------------------

PLOT_W = 1010
PLOT_H = 548
LABEL_X = 176
PLOT_L = 190
PLOT_R = 884
N_SEP_X = 902
N_X = 1004
AGE_LO, AGE_HI = 35.0, 105.0

ERA_W = 470
ERA_H = 246


def _x_age(age: float) -> float:
    # Clamp so long-lived outliers (age > AGE_HI) end at the plot edge instead
    # of running through the sample column on the right.
    x = PLOT_L + (age - AGE_LO) / (AGE_HI - AGE_LO) * (PLOT_R - PLOT_L)
    return min(PLOT_R, max(PLOT_L, x))


def build_plot(payload: dict) -> str:
    rows = [r for r in payload["occupations"] if r["occupation"] in set(payload["display"])]
    overall = payload["scale"]["overall_mean"]
    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {PLOT_W} {PLOT_H}" '
        f'width="{PLOT_W}" height="{PLOT_H}" role="img" '
        f'aria-label="Mean age at death with interquartile range, by occupation">'
    )
    # Axis.
    for tick in range(40, 101, 10):
        x = _x_age(tick)
        parts.append(f'<line x1="{x:.1f}" y1="30" x2="{x:.1f}" y2="{PLOT_H - 32:.1f}" class="grid"/>')
        parts.append(f'<text x="{x:.1f}" y="20" class="tick" text-anchor="middle">{tick}</text>')
    parts.append(f'<text x="{PLOT_L}" y="20" class="tick" text-anchor="end" dx="-6">age at death</text>')
    # Sample column: a hairline keeps the n figures clear of the whiskers.
    parts.append(f'<line x1="{N_SEP_X}" y1="30" x2="{N_SEP_X}" y2="{PLOT_H - 32}" class="nsep"/>')
    parts.append(f'<text x="{N_X}" y="20" class="tick" text-anchor="end">people</text>')
    # Overall reference line.
    ox = _x_age(overall)
    parts.append(f'<line x1="{ox:.1f}" y1="30" x2="{ox:.1f}" y2="{PLOT_H - 32:.1f}" class="ref"/>')
    parts.append(
        f'<text x="{ox:.1f}" y="{PLOT_H - 8}" class="reftag" text-anchor="middle">'
        f'dashed line = all {_n(payload["scale"]["people_analysed"])} people &middot; mean {overall:.0f}</text>'
    )

    top_n = len([r for r in rows if r["occupation"] in set(payload["display"][:DISPLAY_TOP])])
    row_top, row_bottom = 34.0, PLOT_H - 34.0
    row_h = (row_bottom - row_top - 16) / max(1, len(rows))
    y = row_top

    for i, r in enumerate(rows):
        if i == top_n:
            sep = y + 3
            parts.append(f'<line x1="{LABEL_X - 168}" y1="{sep:.1f}" x2="{N_X}" y2="{sep:.1f}" class="sep"/>')
            parts.append(f'<text x="{(PLOT_L + PLOT_R) / 2:.1f}" y="{sep + 20:.1f}" class="sepmark" text-anchor="middle">&hellip;</text>')
            y += 16
        cy = y + row_h / 2
        x25, x75, xm = _x_age(r["p25"]), _x_age(r["p75"]), _x_age(r["mean"])
        xlo, xhi = _x_age(r["min"]), _x_age(r["max"])
        # whisker (min-max)
        parts.append(f'<line x1="{xlo:.1f}" y1="{cy:.1f}" x2="{xhi:.1f}" y2="{cy:.1f}" class="whisker"/>')
        # interquartile bar
        parts.append(
            f'<rect x="{x25:.1f}" y="{cy - 7:.1f}" width="{max(1.0, x75 - x25):.1f}" height="14" rx="4" class="iqr">'
            f'<title>{_esc(_label(r["occupation"]))}: median-life span {r["p25"]:.0f}&ndash;{r["p75"]:.0f}, mean {r["mean"]:.1f} (n={_n(r["n"])})</title></rect>'
        )
        # mean dot
        parts.append(f'<circle cx="{xm:.1f}" cy="{cy:.1f}" r="5" class="dot"/>')
        # label
        parts.append(
            f'<text x="{LABEL_X:.1f}" y="{cy + 4.4:.1f}" class="rowlabel" text-anchor="end">{_esc(_label(r["occupation"]))}</text>'
        )
        # sample
        parts.append(
            f'<text x="{N_X:.1f}" y="{cy + 4.4:.1f}" class="row-n" text-anchor="end">{_n(r["n"])}</text>'
        )
        y += row_h

    parts.append("</svg>")
    return "".join(parts)


def build_era(payload: dict) -> str:
    era = [c for c in payload["centuries"] if ERA_MIN <= c["century"] <= ERA_MAX]
    if not era:
        return ""
    pad_l, pad_r, pad_t, pad_b = 34, 20, 22, 30
    means = [c["mean"] for c in era]
    lo = math.floor((min(means) - 2) / 2) * 2
    hi = math.ceil((max(means) + 2) / 2) * 2
    n_min = min(c["century"] for c in era)
    n_max = max(c["century"] for c in era)

    def ex(c):
        return pad_l + (c - n_min) / (n_max - n_min) * (ERA_W - pad_l - pad_r)

    def ey(m):
        return pad_t + (hi - m) / (hi - lo) * (ERA_H - pad_t - pad_b)

    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {ERA_W} {ERA_H}" '
        f'width="{ERA_W}" height="{ERA_H}" role="img" aria-label="Mean age at death by birth century">'
    )
    # gridlines
    for g in range(lo, hi + 1, 4):
        yy = ey(g)
        parts.append(f'<line x1="{pad_l}" y1="{yy:.1f}" x2="{ERA_W - pad_r}" y2="{yy:.1f}" class="grid"/>')
        parts.append(f'<text x="{pad_l - 6}" y="{yy + 3.5:.1f}" class="tick" text-anchor="end">{g}</text>')
    pts = [(ex(c["century"]), ey(c["mean"])) for c in era]
    area = f'M{pts[0][0]:.1f},{ERA_H - pad_b:.1f} ' + " ".join(f'L{x:.1f},{y:.1f}' for x, y in pts) + f' L{pts[-1][0]:.1f},{ERA_H - pad_b:.1f} Z'
    parts.append(f'<path d="{area}" class="era-area"/>')
    parts.append('<polyline points="' + " ".join(f"{x:.1f},{y:.1f}" for x, y in pts) + '" class="era-line"/>')
    for (x, yy), c in zip(pts, era):
        parts.append(f'<circle cx="{x:.1f}" cy="{yy:.1f}" r="4" class="era-dot"/>')
        parts.append(f'<text x="{x:.1f}" y="{yy - 11:.1f}" class="era-val" text-anchor="middle">{c["mean"]:.0f}</text>')
        parts.append(f'<text x="{x:.1f}" y="{ERA_H - 10:.1f}" class="era-x" text-anchor="middle">{c["label"]}</text>')
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Card HTML
# ---------------------------------------------------------------------------

HTML_HEAD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>HumansMap: which professions live longest?</title>
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
  .card { width:1600px; height:900px; background:var(--bg); color:var(--text);
    padding:38px 44px 24px; display:flex; flex-direction:column; position:relative; overflow:hidden; }
  .card header { display:flex; justify-content:space-between; align-items:baseline; }
  .eyebrow { font:700 15px/1 var(--body); letter-spacing:.2em; text-transform:uppercase; color:var(--accent); }
  .brand { font:700 27px/1 var(--display); color:var(--accent); }
  h1 { font:800 41px/1.07 var(--display); margin:14px 0 0; letter-spacing:-.012em; max-width:40ch; }
  h1 em { font-style:normal; color:var(--person); }
  .deck { font:400 15.5px/1.5 var(--body); color:var(--muted); margin:9px 0 0; max-width:1330px; }
  .deck b { color:var(--text); font-weight:700; }
  .body { display:flex; gap:30px; margin-top:10px; flex:1; min-height:0; }
  .plot { width:1010px; flex:0 0 1010px; display:flex; flex-direction:column; min-height:0; }
  .plot svg { display:block; width:1010px; height:548px; overflow:visible; }
  .grid { stroke:rgba(28,21,16,.07); stroke-width:1; }
  .ref { stroke:rgba(28,21,16,.34); stroke-width:1.4; stroke-dasharray:3 4; }
  .reftag { font:700 10.5px/1 var(--body); fill:var(--muted); letter-spacing:.04em; }
  .sep { stroke:rgba(28,21,16,.16); stroke-width:1; stroke-dasharray:2 5; }
  .sepmark { font:700 16px/1 var(--body); fill:var(--muted); }
  .nsep { stroke:rgba(28,21,16,.14); stroke-width:1; }
  .tick { font:500 11px/1 var(--body); fill:var(--muted); }
  .rowlabel { font:600 14px/1 var(--body); fill:var(--text); }
  .row-n { font:600 12px/1 var(--body); fill:var(--text); font-variant-numeric:tabular-nums; }
  .whisker { stroke:rgba(191,107,40,.45); stroke-width:2; }
  .iqr { fill:rgba(191,107,40,.22); }
  .dot { fill:var(--person); stroke:var(--bg); stroke-width:1.5; }
  .panel { flex:1; min-width:0; display:flex; flex-direction:column; }
  .block { border-top:2px solid var(--text); padding-top:9px; }
  .block + .block { margin-top:16px; }
  .plabel { font:700 11px/1 var(--body); letter-spacing:.18em; text-transform:uppercase; color:var(--muted); }
  .era svg { display:block; width:470px; height:246px; overflow:visible; margin-top:4px; }
  .era-area { fill:rgba(191,107,40,.12); }
  .era-line { fill:none; stroke:var(--person); stroke-width:2.4; stroke-linejoin:round; }
  .era-dot { fill:var(--person); stroke:var(--bg); stroke-width:1.6; }
  .era-val { font:700 11.5px/1 var(--body); fill:var(--text); }
  .era-x { font:500 10.5px/1 var(--body); fill:var(--muted); }
  .eranote { font:400 12.5px/1.45 var(--body); color:var(--muted); margin-top:6px; }
  .eranote b { color:var(--text); }
  .standout { margin-top:7px; font:400 12.5px/1.5 var(--body); color:var(--muted); }
  .standout b { color:var(--text); font-weight:700; }
  .standout .up { color:var(--person); font-weight:700; }
  .standout .dn { color:var(--muted); font-weight:700; }
  .caveat { font:400 11.5px/1.45 var(--body); color:var(--muted); margin-top:6px; }
  .caveat b { color:var(--text); }
  .legend { display:flex; gap:22px; align-items:center; margin-top:6px;
    font:400 12px/1 var(--body); color:var(--muted); }
  .legend span { display:flex; align-items:center; }
  .legend i { display:inline-block; margin-right:7px; }
  .card footer { margin-top:auto; padding-top:10px; display:flex; justify-content:space-between;
    align-items:baseline; font:400 13px/1.4 var(--body); color:var(--muted); }
  .card footer b { color:var(--text); font-weight:700; }
</style></head>
<body>"""


def render_html(payload: dict) -> str:
    scale = payload["scale"]
    head = payload["headline"]
    plot = build_plot(payload)
    era = build_era(payload)

    # Counted from the payload here, not from a pre-baked scale field, so a
    # cached JSON re-renders with the right number: the card used to say "76 jobs
    # have 1,000+ people" when 76 was the count at the *scan* floor (50) and only
    # 48 occupations actually have 1,000+.
    params = payload.get("params", {})
    display_min_n = params.get("display_min_n", params.get("min_sample", 1))
    at_display_n = sum(1 for r in payload.get("occupations", [])
                       if r.get("n", 0) >= display_min_n)

    top_o = _plural(_label(head.get("top_occupation") or ""))
    bot_o = _plural(_label(head.get("bottom_occupation") or ""))
    big = (
        f"<em>{_esc(top_o)}</em> live to {head.get('top_mean', 0):.0f} on average; "
        f"<em>{_esc(bot_o)}</em> to {head.get('bottom_mean', 0):.0f}."
    )
    deck = (
        f"Mean age at death for <b>{_n(scale['people_analysed'])}</b> people with "
        f"recorded birth and death dates and a primary occupation. "
        f"<b>{_n(at_display_n)}</b> jobs have "
        f"<b>{_n(payload['params'].get('display_min_n', payload['params']['min_sample']))}+</b> people (the ten longest- and six "
        f"shortest-lived are plotted). But the job is not the biggest factor: "
        f"<b>when you were born</b> is. Mean lifespan rises "
        f"<b>{head.get('era_rise', 0):.0f} years</b> from the {_esc(head.get('era_from') or '')} "
        f"to the {_esc(head.get('era_to') or '')} (panel, right)."
    )

    era_note = (
        f"People born in the {_esc(head.get('era_to') or '')} died about "
        f"<b>{head.get('era_rise', 0):.0f} years older</b> than those born in the "
        f"{_esc(head.get('era_from') or '')}. Most of the gap is medicine, sanitation and "
        f"childhood survival, not the profession itself."
    )

    adj_hi = _signed(head.get("adj_top_delta") or 0)
    adj_lo = _signed(head.get("adj_bottom_delta") or 0)
    standout = (
        f"Controlling crudely for birth era (age minus the mean of the person&rsquo;s own "
        f"century), <b>{_esc(_plural(_label(head.get('adj_top') or '')))}</b> still sit "
        f"<span class=\"up\">{_esc(adj_hi)} years</span> above average and "
        f"<b>{_esc(_plural(_label(head.get('adj_bottom') or '')))}</b> "
        f"<span class=\"dn\">{_esc(adj_lo)} years</span> below."
    )

    caveat = (
        "<b>Read with care.</b> This is a ranking of who gets documented, not of real "
        "risk. It is <b>survivorship-biased</b> (people who died old are likelier to "
        "survive in the record), <b>era-confounded</b> (left, and the panel), and "
        "built on <b>uneven Wikidata coverage</b> (US/Europe and the project's own "
        "Italy skew high). Only the deceased are counted, so recent birth cohorts are "
        "right-censored."
    )

    legend = (
        '<div class="legend">'
        '<span><i style="width:26px;height:5px;border-radius:3px;background:rgba(191,107,40,.34);display:inline-block"></i>min&ndash;max</span>'
        '<span><i style="width:26px;height:14px;border-radius:4px;background:rgba(191,107,40,.20);display:inline-block"></i>p25&ndash;p75</span>'
        '<span><i style="width:11px;height:11px;border-radius:50%;background:#bf6b28;display:inline-block"></i>mean</span>'
        '<span>n = people in Wikidata with a recorded occupation</span>'
        '</div>'
    )

    card = f"""<section class="card">
  <header>
    <span class="eyebrow">Longevity by occupation</span>
    <span class="brand">Humans&nbsp;Map</span>
  </header>
  <h1>{big}</h1>
  <p class="deck">{deck}</p>
  <div class="body">
    <div class="plot">
      {plot}
      {legend}
    </div>
    <aside class="panel">
      <div class="block">
        <div class="plabel">Lifespan by birth century</div>
        <div class="era">{era}</div>
        <div class="eranote">{era_note}</div>
      </div>
      <div class="block">
        <div class="plabel">Net of era</div>
        <div class="standout">{standout}</div>
      </div>
      <div class="block">
        <div class="plabel">The fine print</div>
        <div class="caveat">{caveat}</div>
      </div>
    </aside>
  </div>
  <footer>
    <span>Data: Wikidata (CC BY-SA) &middot; humansmap.com</span>
    <span><b>{_n(scale['people_analysed'])}</b> deceased people &middot; <b>{_n(at_display_n)}</b> occupations (n &ge; {_n(display_min_n)})</span>
  </footer>
</section>"""

    return HTML_HEAD + card + "\n</body></html>\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default="docs/marketing/longevity")
    parser.add_argument("--min-sample", type=int, default=DEFAULT_MIN_SAMPLE)
    parser.add_argument("--min-statements", type=int, default=DEFAULT_MIN_STATEMENTS)
    parser.add_argument("--scan-limit", type=int, default=DEFAULT_SCAN_LIMIT)
    parser.add_argument("--no-html", action="store_true")
    parser.add_argument(
        "--from-json", default=None,
        help="re-render from an existing longevity.json instead of querying the DB",
    )
    args = parser.parse_args()

    if args.from_json:
        payload = json.loads(Path(args.from_json).read_text())
        payload.setdefault("params", {})["display_min_n"] = DISPLAY_MIN_N
        # The stored display selection was computed with the previous threshold,
        # so recompute it (and the headline extremes) from the full occupation list.
        min_n = max(payload["params"].get("min_sample", DEFAULT_MIN_SAMPLE), DISPLAY_MIN_N)
        eligible = sorted(
            [r for r in payload.get("occupations", [])
             if r.get("n", 0) >= min_n and r.get("occupation") not in DISPLAY_EXCLUDE],
            key=lambda r: r.get("mean", 0.0), reverse=True,
        )
        if eligible:
            payload["display"] = list(dict.fromkeys(
                [r["occupation"] for r in eligible[:DISPLAY_TOP]]
                + [r["occupation"] for r in eligible[-DISPLAY_BOTTOM:]]
            ))
            head = payload.setdefault("headline", {})
            head.update({
                "top_occupation": eligible[0]["occupation"], "top_mean": eligible[0]["mean"], "top_n": eligible[0]["n"],
                "bottom_occupation": eligible[-1]["occupation"], "bottom_mean": eligible[-1]["mean"], "bottom_n": eligible[-1]["n"],
            })
    else:
        payload = collect(args.min_statements, args.min_sample, args.scan_limit)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "longevity.json"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {json_path}")

    if not args.no_html:
        html_path = out_dir / "longevity.html"
        html_path.write_text(render_html(payload))
        print(f"wrote {html_path}")

    print(json.dumps(payload["headline"], indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
