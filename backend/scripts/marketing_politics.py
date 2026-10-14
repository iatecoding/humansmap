#!/usr/bin/env python3
"""Marketing politics miner — read-only, emits the "political power is a pyramid" card.

The insight: across the humansmap.com graph, most people who ever held a
political office held exactly one; a tiny cadre accumulated dozens. The offices
are mostly parliaments and municipal councils — the densest, most reliable
records in Wikidata — so the shape is real, not a sampling artefact.

It never writes to ArangoDB. Everything comes from one bounded pass on the
``affiliations`` ``type`` index (a hash index on ``type``, so
``FILTER e.type == 'position_held'`` is an index lookup, not a collection scan):

  * distribution: edges per person -> how many people held 1, 2, 3 ... offices.
    Two stacked ``COLLECT``s return a tiny result (one row per office count).
  * top office-holders: ``COLLECT p = e._from WITH COUNT`` sorted and limited to
    12, then ``DOCUMENT()`` each ``_from`` for the display fields.
  * most-held offices: the same shape on ``_to``, limited to 14.
  * totals: edge count, and a distinct-office ``COLLECT`` + ``LENGTH``.

No per-edge DOCUMENT joins over the whole 1.4M-edge set; names are resolved only
for the small top-N lists. A ``--scan-limit`` safety net caps the aggregation.

A "position held" is one Wikidata statement. Wikidata stores each parliamentary
term as its own statement, so re-election to the same office counts again; the
card says so, because it measures the *documented* record, not real power.

Run it manually inside the backend container:

    docker compose exec -T backend python - --out-dir /tmp/politics \\
        < backend/scripts/marketing_politics.py
    docker compose exec -T backend cat /tmp/politics/politics.json \\
        > docs/marketing/politics/politics.json
    docker compose exec -T backend cat /tmp/politics/politics.html \\
        > docs/marketing/politics/politics.html

Flags:
    --out-dir PATH     output directory (default: docs/marketing/politics)
    --scan-limit N     max position-held edges fed into the aggregation
    --top-people N     how many top office-holders to resolve (default 12)
    --top-offices N    how many top offices to resolve (default 14)
    --no-html          emit only the JSON payload
    --from-json PATH   re-render from an existing politics.json (no DB access)
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import OrderedDict
from datetime import date
from pathlib import Path

# When run as a file inside the container this resolves to /app; when piped
# over stdin (``python -``) __file__ is "<stdin>" and /app is cwd, so fall back.
try:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
except (NameError, OSError):  # pragma: no cover - stdin invocation
    sys.path.insert(0, str(Path.cwd()))

from app.database import get_arango_client, get_db  # noqa: E402

DEFAULT_SCAN_LIMIT = 2_000_000
TOP_PEOPLE = 12
TOP_OFFICES = 14
DISPLAY_PEOPLE = 6
DISPLAY_OFFICES = 8

# Bins for the hero. Single counts where the eye wants them; the long tail is
# grouped so every bar stays legible. ``None`` upper bound means "and above".
BINS = [
    (1, 1, "1"),
    (2, 2, "2"),
    (3, 3, "3"),
    (4, 4, "4"),
    (5, 6, "5\u20136"),
    (7, 9, "7\u20139"),
    (10, 14, "10\u201314"),
    (15, 19, "15\u201319"),
    (20, 24, "20\u201324"),
    (25, None, "25+"),
]

# Long official names that would swamp the panel. Display-only.
SHORT_NAME = {
    "Antonio Gonz\u00e1lez de Aguilar, 8th Marquis of la Vega de Armijo": "Antonio Aguilar y Correa",
}

# A few hand-written office labels so the panel reads in plain English.
OFFICE_LABEL = {
    "member of the Massachusetts House of Representatives": "Massachusetts House of Representatives",
    "member of the French National Assembly": "French National Assembly",
    "municipal councillor in Finland": "Finnish municipal council",
    "member of the House of Commons of Canada": "Canadian House of Commons",
    "member of the Chamber of Deputies of the Italian Republic": "Italian Chamber of Deputies",
    "member of the Chamber of Deputies of the Kingdom of Italy": "Italian Chamber of Deputies (Kingdom)",
    "member of the National Assembly of Hungary": "Hungarian National Assembly",
    "member of the Parliament of Norway": "Parliament of Norway",
    "Member of Parliament in the Parliament of England": "Parliament of England",
    "deputy member of the Parliament of Norway": "Parliament of Norway (deputy)",
    "member of the French National Assembly": "French National Assembly",
}

ISO2_NAME = {
    "IT": "Italy", "ES": "Spain", "GB": "United Kingdom", "AU": "Australia",
    "GR": "Greece", "CZ": "Czech Republic", "US": "United States", "FR": "France",
    "DE": "Germany", "NL": "Netherlands", "BE": "Belgium", "SE": "Sweden",
    "NO": "Norway", "DK": "Denmark", "FI": "Finland", "PL": "Poland",
    "PT": "Portugal", "AT": "Austria", "CH": "Switzerland", "IE": "Ireland",
    "CA": "Canada", "IN": "India", "BR": "Brazil", "AR": "Argentina",
    "MX": "Mexico", "RU": "Russia", "TR": "Turkey", "JP": "Japan",
    "CN": "China", "ZA": "South Africa", "HU": "Hungary", "RO": "Romania",
    "RS": "Serbia", "HR": "Croatia", "SK": "Slovakia", "SI": "Slovenia",
    "IL": "Israel", "EG": "Egypt", "NG": "Nigeria", "NZ": "New Zealand",
    "CL": "Chile", "CO": "Colombia", "PE": "Peru", "VE": "Venezuela",
    "UA": "Ukraine", "LT": "Lithuania", "LV": "Latvia", "EE": "Estonia",
    "IS": "Iceland", "LU": "Luxembourg", "ID": "Indonesia", "PH": "Philippines",
    "TH": "Thailand", "KR": "South Korea", "TW": "Taiwan", "PK": "Pakistan",
    "BD": "Bangladesh", "IR": "Iran", "IQ": "Iraq", "SA": "Saudi Arabia",
    "MA": "Morocco", "DZ": "Algeria", "TN": "Tunisia", "KE": "Kenya",
    "ET": "Ethiopia", "GH": "Ghana", "CU": "Cuba", "UY": "Uruguay",
}

# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

# One bounded pass: group edges by person, then group those per-person counts
# into a distribution. The result is one row per distinct office count, not one
# row per person, so it is tiny regardless of how many edges there are.
DISTRIBUTION_AQL = """
FOR e IN affiliations
  FILTER e.type == 'position_held'
  LIMIT @scan_limit
  COLLECT p = e._from WITH COUNT INTO n
  COLLECT offices = n WITH COUNT INTO people
  SORT offices
  RETURN {offices: offices, people: people}
"""

TOP_PEOPLE_AQL = """
FOR e IN affiliations
  FILTER e.type == 'position_held'
  LIMIT @scan_limit
  COLLECT p = e._from WITH COUNT INTO n
  SORT n DESC
  LIMIT @top
  LET d = DOCUMENT(p)
  RETURN {
    qid: d.wikidata_id, name: d.name_en, name_it: d.name_it,
    nationalities: d.nationalities, birth: d.birth_date, death: d.death_date,
    n: n
  }
"""

TOP_OFFICES_AQL = """
FOR e IN affiliations
  FILTER e.type == 'position_held'
  LIMIT @scan_limit
  COLLECT o = e._to WITH COUNT INTO n
  SORT n DESC
  LIMIT @top
  LET d = DOCUMENT(o)
  RETURN {qid: d.wikidata_id, name: d.name_en, kind: d.type, n: n}
"""

TOTAL_EDGES_AQL = """
FOR e IN affiliations
  FILTER e.type == 'position_held'
  COLLECT AGGREGATE edges = COUNT(1)
  RETURN edges
"""

DISTINCT_OFFICES_AQL = """
RETURN LENGTH(
  FOR e IN affiliations
    FILTER e.type == 'position_held'
    COLLECT o = e._to
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


def collect(scan_limit: int, top_people: int, top_offices: int) -> dict:
    db = get_db(get_arango_client(request_timeout=900))
    bind = {"scan_limit": scan_limit}

    raw_dist = _rows(db, DISTRIBUTION_AQL, bind)
    raw_people = _rows(db, TOP_PEOPLE_AQL, {**bind, "top": top_people})
    raw_offices = _rows(db, TOP_OFFICES_AQL, {**bind, "top": top_offices})
    total_edges = _rows(db, TOTAL_EDGES_AQL)
    distinct_offices = _rows(db, DISTINCT_OFFICES_AQL)

    distribution = [
        {"offices": int(r.get("offices") or 0), "people": int(r.get("people") or 0)}
        for r in raw_dist
        if int(r.get("people") or 0) > 0
    ]
    distribution.sort(key=lambda r: r["offices"])

    people_total = sum(r["people"] for r in distribution)
    one = next((r["people"] for r in distribution if r["offices"] == 1), 0)
    ten_plus = sum(r["people"] for r in distribution if r["offices"] >= 10)
    twentyfive_plus = sum(r["people"] for r in distribution if r["offices"] >= 25)
    max_offices = max((r["offices"] for r in distribution), default=0)

    top_people_rows = []
    for r in raw_people:
        name = r.get("name") or r.get("name_it") or r.get("qid")
        top_people_rows.append(
            OrderedDict(
                qid=r.get("qid"),
                name=name,
                name_it=r.get("name_it"),
                nationality=(r.get("nationalities") or [None])[0],
                birth=r.get("birth"),
                death=r.get("death"),
                n=int(r.get("n") or 0),
            )
        )

    top_office_rows = []
    for r in raw_offices:
        top_office_rows.append(
            OrderedDict(
                qid=r.get("qid"),
                name=r.get("name") or r.get("qid"),
                kind=r.get("kind"),
                n=int(r.get("n") or 0),
            )
        )

    edges = total_edges[0] if total_edges else sum(
        r["offices"] * r["people"] for r in distribution
    )
    offices_distinct = distinct_offices[0] if distinct_offices else None

    return {
        "generated": date.today().isoformat(),
        "params": {
            "scan_limit": scan_limit,
            "top_people": top_people,
            "top_offices": top_offices,
            "bins": [[lo, hi, label] for lo, hi, label in BINS],
        },
        "scale": {
            "edges": int(edges),
            "people": people_total,
            "offices": int(offices_distinct) if offices_distinct is not None else None,
            "one_office_people": one,
            "one_office_share": (one / people_total) if people_total else 0.0,
            "ten_plus_people": ten_plus,
            "ten_plus_share": (ten_plus / people_total) if people_total else 0.0,
            "twentyfive_plus_people": twentyfive_plus,
            "max_offices": max_offices,
        },
        "distribution": distribution,
        "top_people": top_people_rows,
        "top_offices": top_office_rows,
    }


def bin_distribution(distribution: list[dict]) -> list[dict]:
    """Fold the raw (offices -> people) rows into the hero's bins."""
    bins = []
    for lo, hi, label in BINS:
        people = sum(
            r["people"]
            for r in distribution
            if r["offices"] >= lo and (hi is None or r["offices"] <= hi)
        )
        bins.append(OrderedDict(lo=lo, hi=hi, label=label, people=people))
    return bins


# ---------------------------------------------------------------------------
# Formatting helpers
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
        return f"{int(round(float(value))):,}"
    except (TypeError, ValueError):
        return "0"


def _compact(value: int) -> str:
    if value >= 1000:
        return f"{value / 1000:.0f}k"
    return str(int(value))


def _pretty(name: str) -> str:
    return (name or "").replace("_", " ").strip()


def _office_label(name: str) -> str:
    key = (name or "").strip()
    if key in OFFICE_LABEL:
        return OFFICE_LABEL[key]
    pretty = _pretty(key)
    return pretty[:1].upper() + pretty[1:] if pretty else pretty


def _person_label(name: str) -> str:
    key = (name or "").strip()
    return SHORT_NAME.get(key, key)


def _country(code: str | None) -> str:
    if not code:
        return ""
    return ISO2_NAME.get(code.upper(), code.upper())


def _year(value) -> str:
    if not value:
        return ""
    s = str(value)
    if s.startswith("-"):
        return ""
    return s[:4]


def _lifespan(birth, death) -> str:
    b, d = _year(birth), _year(death)
    if b and d:
        return f"{b}\u2013{d}"
    if b:
        return f"b. {b}"
    if d:
        return f"d. {d}"
    return ""


# ---------------------------------------------------------------------------
# Hero chart: the pyramid (inline SVG, log y-axis)
# ---------------------------------------------------------------------------

PLOT_W = 950
PLOT_H = 670
PAD_L = 66
PAD_R = 16
PAD_T = 72
PAD_B = 54
PLOT_L = PAD_L
PLOT_R = PLOT_W - PAD_R
PLOT_T = PAD_T
PLOT_B = PLOT_H - PAD_B
PLOT_H_INNER = PLOT_B - PLOT_T

Y_MIN = 10.0
Y_MAX = 800_000.0
LOG_MIN = math.log10(Y_MIN)
LOG_SPAN = math.log10(Y_MAX) - LOG_MIN

BAR_GAP = 0.44  # fraction of a slot left empty


def _y(value: float) -> float:
    v = max(Y_MIN, float(value))
    frac = (math.log10(v) - LOG_MIN) / LOG_SPAN
    return PLOT_T + PLOT_H_INNER * (1.0 - frac)


def build_plot(payload: dict) -> str:
    bins = bin_distribution(payload["distribution"])
    scale = payload["scale"]
    n_bins = len(bins)
    slot = (PLOT_R - PLOT_L) / n_bins
    bar_w = slot * (1.0 - BAR_GAP)

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {PLOT_W} {PLOT_H}" '
        f'width="{PLOT_W}" height="{PLOT_H}" role="img" '
        f'aria-label="People by number of political offices held, log scale">'
    ]

    # --- gridlines + y ticks (log decades) ---------------------------------
    for decade in (1, 2, 3, 4, 5):
        value = 10 ** decade
        yy = _y(value)
        parts.append(f'<line x1="{PLOT_L}" y1="{yy:.1f}" x2="{PLOT_R}" y2="{yy:.1f}" class="grid"/>')
        label = f"{_compact(int(value))}" if decade >= 3 else str(int(value))
        parts.append(f'<text x="{PLOT_L - 8:.1f}" y="{yy + 3.5:.1f}" class="ytick" text-anchor="end">{label}</text>')

    # --- bars ---------------------------------------------------------------
    baseline = _y(Y_MIN)
    for i, b in enumerate(bins):
        cx = PLOT_L + slot * (i + 0.5)
        top = _y(b["people"])
        x = cx - bar_w / 2
        h = max(1.0, baseline - top)
        parts.append(
            f'<rect x="{x:.1f}" y="{top:.1f}" width="{bar_w:.1f}" height="{h:.1f}" rx="3" class="bar">'
            f'<title>{_n(b["people"])} people held {b["label"]} office{"s" if b["lo"] != 1 else ""}</title></rect>'
        )
        parts.append(
            f'<text x="{cx:.1f}" y="{top - 7:.1f}" class="bval" text-anchor="middle">{_n(b["people"])}</text>'
        )
        parts.append(
            f'<text x="{cx:.1f}" y="{PLOT_B + 20:.1f}" class="xtick" text-anchor="middle">{b["label"]}</text>'
        )

    # --- x axis caption -----------------------------------------------------
    parts.append(
        f'<text x="{(PLOT_L + PLOT_R) / 2:.1f}" y="{PLOT_H - 8:.1f}" class="xcap" '
        f'text-anchor="middle">offices held per person (each is one Wikidata &ldquo;position held&rdquo; statement)</text>'
    )

    # --- annotation: the tallest bar ---------------------------------------
    first = bins[0]
    cx0 = PLOT_L + slot * 0.5
    parts.append(
        f'<text x="{cx0 + bar_w / 2 + 12:.1f}" y="{_y(first["people"]) + 4:.1f}" class="callout" '
        f'text-anchor="start">held exactly one office: {scale["one_office_share"] * 100:.0f}% of all office-holders</text>'
    )

    # --- annotation: the tail ----------------------------------------------
    last = bins[-1]
    cxl = PLOT_L + slot * (n_bins - 0.5)
    last_top = _y(last["people"])
    callout_y = PLOT_T + 0.42 * PLOT_H_INNER
    parts.append(
        f'<line x1="{cxl:.1f}" y1="{last_top - 6:.1f}" x2="{cxl:.1f}" y2="{callout_y + 26:.1f}" class="leader"/>'
    )
    parts.append(
        f'<text x="{PLOT_R:.1f}" y="{callout_y:.1f}" class="tail1" text-anchor="end">'
        f'{_n(scale["twentyfive_plus_people"])} people held 25 or more</text>'
    )
    parts.append(
        f'<text x="{PLOT_R:.1f}" y="{callout_y + 18:.1f}" class="tail2" text-anchor="end">'
        f'the most by one person is {_n(scale["max_offices"])}</text>'
    )

    # --- context block, top right ------------------------------------------
    parts.append(f'<line x1="{PLOT_R - 250:.1f}" y1="{PLOT_T - 18:.1f}" x2="{PLOT_R:.1f}" y2="{PLOT_T - 18:.1f}" class="rule"/>')
    parts.append(
        f'<text x="{PLOT_R:.1f}" y="{PLOT_T - 4:.1f}" class="ctxlabel" text-anchor="end">ON RECORD</text>'
    )
    parts.append(
        f'<text x="{PLOT_R:.1f}" y="{PLOT_T + 22:.1f}" class="ctxnum" text-anchor="end">{_n(scale["people"])}</text>'
    )
    parts.append(
        f'<text x="{PLOT_R:.1f}" y="{PLOT_T + 40:.1f}" class="ctxsub" text-anchor="end">people with at least one political office</text>'
    )
    parts.append(
        f'<text x="{PLOT_R:.1f}" y="{PLOT_T + 72:.1f}" class="ctxnum" text-anchor="end">{_n(scale["offices"])}</text>'
    )
    parts.append(
        f'<text x="{PLOT_R:.1f}" y="{PLOT_T + 90:.1f}" class="ctxsub" text-anchor="end">distinct offices recorded</text>'
    )

    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Card HTML
# ---------------------------------------------------------------------------

HTML_HEAD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>HumansMap: is political power a pyramid?</title>
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
    padding:38px 44px 24px; display:flex; flex-direction:column; position:relative; overflow:hidden; }
  .card header { display:flex; justify-content:space-between; align-items:baseline; }
  .eyebrow { font:700 15px/1 var(--body); letter-spacing:.2em; text-transform:uppercase; color:var(--accent); }
  .brand { font:700 27px/1 var(--display); color:var(--accent); }
  h1 { font:800 44px/1.05 var(--display); margin:14px 0 0; letter-spacing:-.012em; max-width:34ch; }
  h1 em { font-style:normal; color:var(--accent); }
  .deck { font:400 16px/1.45 var(--body); color:var(--muted); margin:9px 0 0; max-width:1190px; }
  .deck b { color:var(--text); font-weight:700; }
  .body { display:flex; gap:30px; margin-top:12px; flex:1; min-height:0; }
  .hero { width:950px; flex:0 0 950px; display:flex; flex-direction:column; min-height:0; }
  .charthead { display:flex; justify-content:space-between; align-items:baseline; border-bottom:2px solid var(--text); padding-bottom:7px; }
  .charthead .plabel { font:700 11px/1 var(--body); letter-spacing:.18em; text-transform:uppercase; color:var(--text); }
  .charthead .unit { font:500 11.5px/1 var(--body); color:var(--muted); }
  .hero svg { display:block; width:950px; height:670px; overflow:visible; }
  .grid { stroke:rgba(28,21,16,.08); stroke-width:1; }
  .ytick { font:500 11px/1 var(--body); fill:var(--muted); font-variant-numeric:tabular-nums; }
  .xtick { font:600 12px/1 var(--body); fill:var(--text); }
  .xcap { font:400 11.5px/1 var(--body); fill:var(--muted); }
  .bar { fill:var(--person); }
  .bval { font:700 12px/1 var(--body); fill:var(--text); font-variant-numeric:tabular-nums; }
  .callout { font:700 12.5px/1 var(--body); fill:var(--accent); }
  .leader { stroke:rgba(28,21,16,.35); stroke-width:1; }
  .tail1 { font:700 13px/1 var(--body); fill:var(--text); }
  .tail2 { font:400 11.5px/1 var(--body); fill:var(--muted); }
  .rule { stroke:rgba(28,21,16,.16); stroke-width:1; }
  .ctxlabel { font:700 10px/1 var(--body); letter-spacing:.16em; fill:var(--muted); }
  .ctxnum { font:800 24px/1 var(--display); fill:var(--text); }
  .ctxsub { font:400 11px/1 var(--body); fill:var(--muted); }
  .legend { display:flex; gap:20px; align-items:center; margin-top:9px;
    font:400 12px/1.35 var(--body); color:var(--muted); }
  .legend span { display:flex; align-items:center; gap:7px; }
  .legend .swatch { width:18px; height:11px; border-radius:3px; background:var(--person); }
  .panel { flex:1; min-width:0; display:flex; flex-direction:column; }
  .block { border-top:2px solid var(--text); padding-top:9px; }
  .block + .block { margin-top:14px; }
  .plabel { font:700 11px/1 var(--body); letter-spacing:.18em; text-transform:uppercase; color:var(--muted); }
  ul.people { list-style:none; margin:7px 0 0; padding:0; }
  ul.people li { display:grid; grid-template-columns:1fr auto; align-items:center;
    gap:10px; padding:6px 0; border-bottom:1px solid var(--line); }
  .pname { font:600 15px/1.1 var(--body); }
  .pname em { display:block; font:400 11px/1.45 var(--body); font-style:normal; color:var(--muted); margin-top:2px; }
  .pn { font:800 23px/1 var(--display); font-variant-numeric:tabular-nums; }
  ul.offices { list-style:none; margin:8px 0 0; padding:0; }
  ul.offices li { display:grid; grid-template-columns:1fr 118px 56px; align-items:center;
    gap:10px; padding:5px 0; }
  .oname { font:600 13px/1.15 var(--body); }
  .obar { position:relative; height:9px; background:var(--surface); border-radius:5px; }
  .obar i { position:absolute; left:0; top:0; height:100%; border-radius:5px; background:var(--person); }
  .oval { font:700 13px/1 var(--body); text-align:right; font-variant-numeric:tabular-nums; }
  .note { font:400 11.5px/1.4 var(--body); color:var(--muted); margin-top:8px; }
  .note b { color:var(--text); }
  .caveat { font:400 11.5px/1.42 var(--body); color:var(--muted); margin-top:auto; padding-top:12px; }
  .caveat b { color:var(--text); }
  .card footer { margin-top:auto; padding-top:9px; display:flex; justify-content:space-between;
    align-items:baseline; font:400 13px/1.4 var(--body); color:var(--muted); }
  .card footer b { color:var(--text); font-weight:700; }
</style></head>
<body>"""


def render_html(payload: dict) -> str:
    scale = payload["scale"]
    plot = build_plot(payload)

    people = payload.get("top_people", [])[:DISPLAY_PEOPLE]
    offices = payload.get("top_offices", [])[:DISPLAY_OFFICES]

    people_rows = []
    for p in people:
        meta = " \u00b7 ".join(x for x in (_country(p.get("nationality")), _lifespan(p.get("birth"), p.get("death"))) if x)
        people_rows.append(
            f"""<li><span class="pname">{_esc(_person_label(p.get('name') or ''))}<em>{_esc(meta)}</em></span>"""
            f"""<span class="pn">{_n(p.get('n'))}</span></li>"""
        )

    max_office = max((o.get("n", 0) for o in offices), default=1) or 1
    office_rows = []
    for o in offices:
        pct = 100.0 * o.get("n", 0) / max_office
        office_rows.append(
            f"""<li><span class="oname">{_esc(_office_label(o.get('name') or ''))}</span>"""
            f"""<span class="obar"><i style="width:{pct:.1f}%"></i></span>"""
            f"""<span class="oval">{_n(o.get('n'))}</span></li>"""
        )

    deck = (
        f"Of the <b>{_n(scale['people'])}</b> people in the humansmap.com graph who held a "
        f"recorded political office, <b>{_n(scale['one_office_people'])}</b> "
        f"(<b>{scale['one_office_share'] * 100:.0f}%</b>) held exactly one. "
        f"<b>{_n(scale['ten_plus_people'])}</b> held ten or more and "
        f"<b>{_n(scale['twentyfive_plus_people'])}</b> held twenty-five or more. "
        f"Every entry is one Wikidata &ldquo;position held&rdquo; statement, so a "
        f"parliamentary term counts as one office."
    )

    caveat = (
        "<b>Read with care.</b> This counts documented Wikidata &ldquo;position held&rdquo; "
        "statements, not real-world power: a re-elected politician counts again each term, "
        "and parliaments and municipal councils are far denser than cabinets, party posts "
        "or behind-the-scenes influence. Coverage is uneven: the US, Europe and "
        "especially Italy are better recorded than the rest of the world. A missing "
        "statement is not an absent career."
    )

    card = f"""<section class="card">
  <header>
    <span class="eyebrow">Political office</span>
    <span class="brand">Humans&nbsp;Map</span>
  </header>
  <h1>Most office-holders hold <em>a single office</em> ({scale['one_office_share'] * 100:.0f}%).</h1>
  <p class="deck">{deck}</p>
  <div class="body">
    <div class="hero">
      <div class="charthead">
        <span class="plabel">People by number of offices held</span>
        <span class="unit">people &middot; log scale</span>
      </div>
      {plot}
      <div class="legend">
        <span><i class="swatch"></i>people who held that many offices</span>
        <span>rarer counts are grouped into wider bars; the record is {_n(scale['max_offices'])}</span>
      </div>
    </div>
    <aside class="panel">
      <div class="block">
        <div class="plabel">Most offices on record</div>
        <ul class="people">{''.join(people_rows)}</ul>
        <div class="note">Every &ldquo;position held&rdquo; statement is counted, so repeated
          terms of the same office add up, and these are the longest documented careers,
          not the most powerful people.</div>
      </div>
      <div class="block">
        <div class="plabel">Most-held offices</div>
        <ul class="offices">{''.join(office_rows)}</ul>
        <div class="note">National parliaments and local councils dominate: they are
          the best-documented offices in Wikidata.</div>
      </div>
      <div class="caveat">{caveat}</div>
    </aside>
  </div>
  <footer>
    <span>Data: Wikidata (CC BY-SA) &middot; humansmap.com</span>
    <span><b>{_n(scale['people'])}</b> people &middot; <b>{_n(scale['edges'])}</b> positions &middot; <b>{_n(scale['offices'])}</b> offices</span>
  </footer>
</section>"""

    return HTML_HEAD + card + "\n</body></html>\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default="docs/marketing/politics")
    parser.add_argument("--scan-limit", type=int, default=DEFAULT_SCAN_LIMIT)
    parser.add_argument("--top-people", type=int, default=TOP_PEOPLE)
    parser.add_argument("--top-offices", type=int, default=TOP_OFFICES)
    parser.add_argument("--no-html", action="store_true")
    parser.add_argument(
        "--from-json", default=None,
        help="re-render from an existing politics.json instead of querying the DB",
    )
    args = parser.parse_args()

    if args.from_json:
        payload = json.loads(Path(args.from_json).read_text())
        print(f"[cache] loaded {args.from_json} (no DB access)")
    else:
        payload = collect(args.scan_limit, args.top_people, args.top_offices)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if not args.from_json:
        json_path = out_dir / "politics.json"
        json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        print(f"wrote {json_path}")

    if not args.no_html:
        html_path = out_dir / "politics.html"
        html_path.write_text(render_html(payload))
        print(f"wrote {html_path}")

    scale = payload["scale"]
    summary = {
        "people": scale["people"],
        "edges": scale["edges"],
        "offices": scale["offices"],
        "one_office_people": scale["one_office_people"],
        "one_office_share": round(scale["one_office_share"], 4),
        "ten_plus_people": scale["ten_plus_people"],
        "twentyfive_plus_people": scale["twentyfive_plus_people"],
        "max_offices": scale["max_offices"],
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
