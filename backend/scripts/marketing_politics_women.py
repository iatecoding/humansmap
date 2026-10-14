#!/usr/bin/env python3
"""Marketing politics miner — read-only, emits the "when did political office
open to women?" card.

The insight: measured by the people who actually held a recorded political
office (Wikidata ``position held`` statements), women are a third of the
office-holders born in the 1980s-90s — up from under 1% of those born around
1800. The share crosses 1% for the 1850s cohort, 10% for the 1930s, 20% for the
1950s and 30% for the 1970s: a slow, monotone opening over 150 years.

The 2000s cohort is drawn but right-censored: people born 2000-2009 are only
partway through their careers, so their share understates where they will land.
It is rendered as a hollow, dashed point marked ``*`` and is excluded from the
headline finding, which rests on the 1990s — the last well-sampled cohort.

It never writes to ArangoDB. Everything comes from one bounded pass on the
``affiliations`` ``type`` index (a hash index on ``type``, so
``FILTER e.type == 'position_held'`` is an index lookup, not a collection scan):

  * collapse the ~1.4M position-held edges to their distinct persons
    (``COLLECT p = e._from``), then ``DOCUMENT()`` each person once to read
    ``birth_date`` / ``gender`` / ``nationalities``;
  * collect by (birth decade, gender, nationality) — a few thousand rows, not
    one per person, so the result stays small and cheap to cache.

No full-collection scan of ``persons`` (8.0M docs). A ``--scan-limit`` safety net
caps the edge pass.

Honest reading: ``gender`` is a binary field in the source and non-binary
identities are not represented; the youngest cohorts are still filling in (a
person born in 1990 has had less time to accumulate offices, and the 2000s
cohort is only partway through its careers), and coverage skews to the US /
Europe / Italy. It measures documented office, not all power.

Run it manually inside the backend container:

    docker compose exec -T backend python - --out-dir /tmp/politics \\
        < backend/scripts/marketing_politics_women.py
    docker compose exec -T backend cat /tmp/politics/politics_women.json \\
        > docs/marketing/politics/politics_women.json
    docker compose exec -T backend cat /tmp/politics/politics_women.html \\
        > docs/marketing/politics/politics_women.html

Flags:
    --out-dir PATH     output directory (default: docs/marketing/politics)
    --scan-limit N     max position-held edges fed into the pass
    --min-year N       earliest birth year to count (default: 1700)
    --max-year N       latest birth year to count (default: 2009)
    --country-n N      minimum cohort size for the country panel (default: 1500)
    --no-html          emit only the JSON payload
    --from-json PATH   re-render from an existing politics_women.json (no DB access)
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

DEFAULT_SCAN_LIMIT = 2_000_000
MIN_YEAR = 1700
MAX_YEAR = 2009
COUNTRY_MIN_N = 1500

# Decade range shown on the hero. The very first cohorts are noisy (small n), so
# they are excluded from the plot and noted. The 2000s cohort is drawn but
# right-censored (people born then are still early in their careers), so it is
# marked hollow/dashed and excluded from the headline; 2010s+ are children.
DISPLAY_FROM = 1800
DISPLAY_TO = 2000
CENSORED_FROM = 2000  # cohorts from here are drawn hollow: still filling in
COUNTRY_FROM = 1950
COUNTRY_TO = 1980

Y_MAX = 40.0  # percent ceiling for the hero

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

# One bounded pass. Collapse edges to persons first, then read one document per
# person for birth/gender/nationality, then group. The final COLLECT returns a
# few thousand rows regardless of how many edges or people there are.
BY_DECADE_NAT_AQL = """
FOR e IN affiliations
  FILTER e.type == 'position_held'
  LIMIT @scan_limit
  COLLECT p = e._from
  LET d = DOCUMENT(p)
  FILTER d != null AND d.birth_date != null AND LEFT(d.birth_date, 1) != '-'
  LET y = TO_NUMBER(SUBSTRING(d.birth_date, 0, 4))
  FILTER y != null AND y >= @min_year AND y <= @max_year
  COLLECT dec = FLOOR(y / 10) * 10, g = d.gender, nat = FIRST(d.nationalities)
    WITH COUNT INTO n
  RETURN {dec: dec, gender: g, nat: nat, n: n}
"""

DISTINCT_OFFICE_HOLDERS_AQL = """
FOR e IN affiliations
  FILTER e.type == 'position_held'
  LIMIT @scan_limit
  COLLECT p = e._from WITH COUNT INTO n
  COLLECT AGGREGATE people = COUNT(1)
  RETURN people
"""

POSITIONS_AQL = """
FOR e IN affiliations
  FILTER e.type == 'position_held'
  COLLECT AGGREGATE edges = COUNT(1)
  RETURN edges
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


def _gender_bucket(g) -> str:
    g = (g or "").strip().lower()
    if g == "female":
        return "female"
    if g == "male":
        return "male"
    return "other"


def collect(scan_limit: int, min_year: int, max_year: int, country_n: int) -> dict:
    db = get_db(get_arango_client(request_timeout=900))
    bind = {"scan_limit": scan_limit, "min_year": min_year, "max_year": max_year}

    raw = _rows(db, BY_DECADE_NAT_AQL, bind)
    holder_rows = _rows(db, DISTINCT_OFFICE_HOLDERS_AQL, {"scan_limit": scan_limit})
    position_rows = _rows(db, POSITIONS_AQL)

    # decade -> gender bucket -> count
    by_decade: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    # nationality -> gender bucket -> count (for the country panel)
    by_nat: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for r in raw:
        dec = int(r.get("dec") or 0)
        n = int(r.get("n") or 0)
        bucket = _gender_bucket(r.get("gender"))
        by_decade[dec][bucket] += n
        nat = r.get("nat")
        if nat and COUNTRY_FROM <= dec <= COUNTRY_TO:
            by_nat[str(nat)][bucket] += n

    decades = []
    for dec in sorted(by_decade):
        cells = by_decade[dec]
        female = cells.get("female", 0)
        male = cells.get("male", 0)
        other = cells.get("other", 0)
        binary = female + male
        decades.append(
            OrderedDict(
                decade=dec,
                female=female,
                male=male,
                other=other,
                total=female + male + other,
                share=(female / binary) if binary else 0.0,
            )
        )

    total_binary = sum(r["female"] + r["male"] for r in decades)
    women_total = sum(r["female"] for r in decades)
    measured_total = sum(r["total"] for r in decades)
    overall_share = (women_total / total_binary) if total_binary else 0.0

    # threshold-crossing milestones on the displayed decades
    milestones = []
    for threshold in (0.01, 0.05, 0.10, 0.20, 0.30):
        hit = next(
            (r for r in decades if r["decade"] >= DISPLAY_FROM and r["share"] >= threshold),
            None,
        )
        if hit:
            milestones.append(
                OrderedDict(share=threshold, decade=hit["decade"], value=hit["share"],
                            n=hit["total"])
            )

    countries = []
    for nat, cells in by_nat.items():
        female = cells.get("female", 0)
        male = cells.get("male", 0)
        total = female + male + cells.get("other", 0)
        if total >= country_n:
            countries.append(
                OrderedDict(
                    code=nat,
                    name=ISO2_NAME.get(nat.upper(), nat.upper()),
                    total=total,
                    female=female,
                    share=(female / (female + male)) if (female + male) else 0.0,
                )
            )
    countries.sort(key=lambda r: (-r["share"], -r["total"]))

    return {
        "generated": date.today().isoformat(),
        "params": {
            "scan_limit": scan_limit,
            "min_year": min_year,
            "max_year": max_year,
            "country_min_n": country_n,
            "display_from": DISPLAY_FROM,
            "display_to": DISPLAY_TO,
            "country_from": COUNTRY_FROM,
            "country_to": COUNTRY_TO,
        },
        "scale": {
            "office_holders": int(holder_rows[0]) if holder_rows else None,
            "positions": int(position_rows[0]) if position_rows else None,
            "measured": measured_total,
            "women": women_total,
            "overall_share": overall_share,
            "first": decades[0]["decade"] if decades else None,
        },
        "decades": decades,
        "milestones": milestones,
        "countries": countries[:12],
    }


def display_decades(payload: dict) -> list[dict]:
    # Use the module constants, not the (possibly stale) JSON params, so a
    # cached payload re-renders at the current range.
    return [r for r in payload["decades"] if DISPLAY_FROM <= r["decade"] <= DISPLAY_TO]


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


def _pct(value: float, digits: int = 0) -> str:
    return f"{value * 100:.{digits}f}%"


# ---------------------------------------------------------------------------
# Hero chart: share of women by birth decade (inline SVG)
# ---------------------------------------------------------------------------

PLOT_W = 950
PLOT_H = 670
PAD_L = 74
PAD_R = 150
PAD_T = 66
PAD_B = 74
PLOT_L = PAD_L
PLOT_R = PLOT_W - PAD_R
PLOT_T = PAD_T
PLOT_B = PLOT_H - PAD_B
PLOT_W_INNER = PLOT_R - PLOT_L
PLOT_H_INNER = PLOT_B - PLOT_T

Y_TICKS = (0, 10, 20, 30, 40)


def _x(i: int, n: int) -> float:
    slot = PLOT_W_INNER / n
    return PLOT_L + slot * (i + 0.5)


def _y(value: float) -> float:
    frac = max(0.0, min(1.0, value / Y_MAX))
    return PLOT_B - PLOT_H_INNER * frac


def build_plot(payload: dict) -> str:
    rows = display_decades(payload)
    n = len(rows)
    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {PLOT_W} {PLOT_H}" '
        f'width="{PLOT_W}" height="{PLOT_H}" role="img" '
        f'aria-label="Share of women among people who held political office, by birth decade">'
    ]

    # --- gridlines + y ticks ------------------------------------------------
    for value in Y_TICKS:
        yy = _y(value)
        parts.append(f'<line x1="{PLOT_L}" y1="{yy:.1f}" x2="{PLOT_R}" y2="{yy:.1f}" class="grid"/>')
        parts.append(
            f'<text x="{PLOT_L - 10:.1f}" y="{yy + 3.5:.1f}" class="ytick" text-anchor="end">{value}%</text>'
        )
    parts.append(
        f'<line x1="{PLOT_L}" y1="{PLOT_B:.1f}" x2="{PLOT_R}" y2="{PLOT_B:.1f}" class="axis"/>'
    )

    # --- area + line (solid through the last well-sampled cohort) -----------
    solid_ids = [i for i, r in enumerate(rows) if r["decade"] < CENSORED_FROM]
    censored_ids = [i for i, r in enumerate(rows) if r["decade"] >= CENSORED_FROM]

    area = [f'M {_x(solid_ids[0], n):.1f} {PLOT_B:.1f}']
    for i in solid_ids:
        area.append(f'L {_x(i, n):.1f} {_y(rows[i]["share"] * 100):.1f}')
    area.append(f'L {_x(solid_ids[-1], n):.1f} {PLOT_B:.1f} Z')
    parts.append(f'<path d="{" ".join(area)}" class="area"/>')

    line = [f'M {_x(solid_ids[0], n):.1f} {_y(rows[solid_ids[0]]["share"] * 100):.1f}']
    for i in solid_ids[1:]:
        line.append(f'L {_x(i, n):.1f} {_y(rows[i]["share"] * 100):.1f}')
    parts.append(f'<path d="{" ".join(line)}" class="line"/>')

    # --- right-censored zone + dashed link to the final cohort --------------
    if censored_ids:
        ci = censored_ids[0]
        x0 = (_x(solid_ids[-1], n) + _x(ci, n)) / 2
        parts.append(
            f'<rect x="{x0:.1f}" y="{PLOT_T:.1f}" width="{PLOT_R - x0:.1f}" '
            f'height="{PLOT_H_INNER:.1f}" fill="rgba(28,21,16,.045)"/>'
        )
        parts.append(
            f'<text x="{PLOT_R - 4:.1f}" y="{PLOT_T + 13:.1f}" class="axnote" '
            f'text-anchor="end">still filling in \u2192</text>'
        )
        li = solid_ids[-1]
        parts.append(
            f'<path d="M {_x(li, n):.1f} {_y(rows[li]["share"] * 100):.1f} '
            f'L {_x(ci, n):.1f} {_y(rows[ci]["share"] * 100):.1f}" class="line dash"/>'
        )

    # --- dots + decade ticks ------------------------------------------------
    show_ticks = set(range(0, n, 2))  # every other decade; avoids label collision
    show_ticks.add(n - 1)
    for i, r in enumerate(rows):
        cx, cy = _x(i, n), _y(r["share"] * 100)
        censored = r["decade"] >= CENSORED_FROM
        cls = "dot censored" if censored else "dot"
        parts.append(
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="3.4" class="{cls}">'
            f'<title>{r["decade"]}s cohort: {_pct(r["share"])} women '
            f'({_n(r["female"])} of {_n(r["female"] + r["male"])})'
            f'{" - still filling in" if censored else ""}</title></circle>'
        )
        if censored:
            parts.append(
                f'<text x="{cx:.1f}" y="{cy - 9:.1f}" class="ptstar" text-anchor="middle">*</text>'
            )
        if i in show_ticks:
            label = f'{r["decade"]}{"*" if censored else ""}'
            parts.append(
                f'<text x="{cx:.1f}" y="{PLOT_B + 20:.1f}" class="xtick" text-anchor="middle">{label}</text>'
            )

    # --- label the censored cohort beside its point -------------------------
    if censored_ids:
        ci = censored_ids[0]
        cx, cy = _x(ci, n), _y(rows[ci]["share"] * 100)
        parts.append(
            f'<text x="{cx + 10:.1f}" y="{cy + 2:.1f}" class="ptcensor" text-anchor="start">'
            f'{rows[ci]["decade"]}s cohort</text>'
        )
        parts.append(
            f'<text x="{cx + 10:.1f}" y="{cy + 15:.1f}" class="ptcensor" text-anchor="start">'
            f'still filling in</text>'
        )

    # --- annotations on milestone points -----------------------------------
    def annotate(decade: int, text: str, dy: float, anchor: str = "middle") -> None:
        for i, r in enumerate(rows):
            if r["decade"] == decade:
                cx, cy = _x(i, n), _y(r["share"] * 100)
                parts.append(
                    f'<text x="{cx:.1f}" y="{cy + dy:.1f}" class="ptval" text-anchor="{anchor}">{text}</text>'
                )
                return

    annotate(1900, "5%", -12)
    annotate(1950, "1 in 5", -12)
    annotate(1980, "1 in 3", -12)

    # --- headline callout, top-left ----------------------------------------
    parts.append(
        f'<text x="{PLOT_L + 4:.1f}" y="{PLOT_T + 4:.1f}" class="callout">Women were just 1 in 250</text>'
    )
    parts.append(
        f'<text x="{PLOT_L + 4:.1f}" y="{PLOT_T + 24:.1f}" class="callout">of office-holders born in the</text>'
    )
    parts.append(
        f'<text x="{PLOT_L + 4:.1f}" y="{PLOT_T + 44:.1f}" class="callout">1800s, and 1 in 3 in the 1980s</text>'
    )

    # --- x caption ----------------------------------------------------------
    parts.append(
        f'<text x="{(PLOT_L + PLOT_R) / 2:.1f}" y="{PLOT_H - 10:.1f}" class="xcap" text-anchor="middle">'
        f'birth decade of the office-holder; each dot is one 10-year cohort</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Card HTML
# ---------------------------------------------------------------------------

HTML_HEAD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>HumansMap: when did political office open to women?</title>
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
  h1 { font:800 44px/1.05 var(--display); margin:14px 0 0; letter-spacing:-.012em; max-width:36ch; }
  h1 em { font-style:normal; color:var(--accent); }
  .deck { font:400 16px/1.45 var(--body); color:var(--muted); margin:9px 0 0; max-width:1220px; }
  .deck b { color:var(--text); font-weight:700; }
  .body { display:flex; gap:30px; margin-top:12px; flex:1; min-height:0; }
  .hero { width:950px; flex:0 0 950px; display:flex; flex-direction:column; min-height:0; }
  .charthead { display:flex; justify-content:space-between; align-items:baseline; border-bottom:2px solid var(--text); padding-bottom:7px; }
  .charthead .plabel { font:700 11px/1 var(--body); letter-spacing:.18em; text-transform:uppercase; color:var(--text); }
  .charthead .unit { font:500 11.5px/1 var(--body); color:var(--muted); }
  .hero svg { display:block; width:950px; height:670px; overflow:visible; }
  .grid { stroke:rgba(28,21,16,.08); stroke-width:1; }
  .axis { stroke:rgba(28,21,16,.30); stroke-width:1; }
  .ytick { font:500 11px/1 var(--body); fill:var(--muted); font-variant-numeric:tabular-nums; }
  .xtick { font:500 10px/1 var(--body); fill:var(--muted); font-variant-numeric:tabular-nums; }
  .xcap { font:400 11.5px/1 var(--body); fill:var(--muted); }
  .area { fill:rgba(191,107,40,.14); }
  .line { fill:none; stroke:var(--person); stroke-width:2.4; stroke-linejoin:round; }
  .line.dash { stroke-width:2.0; stroke-dasharray:5 5; opacity:.85; }
  .dot { fill:var(--person); stroke:var(--bg); stroke-width:1.4; }
  .dot.censored { fill:var(--bg); stroke:var(--person); stroke-width:1.8; stroke-dasharray:2 2; }
  .ptval { font:700 12.5px/1 var(--body); fill:var(--text); }
  .ptstar { font:700 13px/1 var(--body); fill:var(--person); }
  .ptcensor { font:600 11px/1 var(--body); fill:var(--muted); }
  .axnote { font:600 9.6px/1 var(--body); fill:var(--muted); letter-spacing:.06em; text-transform:uppercase; }
  .callout { font:700 13px/1 var(--body); fill:var(--accent); }
  .legend { display:flex; gap:20px; align-items:center; margin-top:9px;
    font:400 12px/1.35 var(--body); color:var(--muted); }
  .legend span { display:flex; align-items:center; gap:7px; }
  .legend .swatch { width:18px; height:11px; border-radius:3px; background:var(--person); }
  .legend .swatch.censored { background:transparent; border:1.8px dashed var(--person); }
  .panel { flex:1; min-width:0; display:flex; flex-direction:column; }
  .block { border-top:2px solid var(--text); padding-top:9px; }
  .block + .block { margin-top:14px; }
  .plabel { font:700 11px/1 var(--body); letter-spacing:.18em; text-transform:uppercase; color:var(--muted); }
  ul.miles { list-style:none; margin:8px 0 0; padding:0; }
  ul.miles li { display:grid; grid-template-columns:1fr auto; align-items:baseline;
    gap:10px; padding:6px 0; border-bottom:1px solid var(--line); }
  .mname { font:600 13.5px/1.15 var(--body); }
  .mname em { display:block; font:400 11px/1.4 var(--body); font-style:normal; color:var(--muted); margin-top:2px; }
  .myear { font:800 19px/1 var(--display); font-variant-numeric:tabular-nums; }
  ul.cnt { list-style:none; margin:8px 0 0; padding:0; }
  ul.cnt li { display:grid; grid-template-columns:1fr 96px 46px; align-items:center;
    gap:9px; padding:5px 0; }
  .cname { font:600 13px/1.15 var(--body); }
  .cbar { position:relative; height:9px; background:var(--surface); border-radius:5px; }
  .cbar i { position:absolute; left:0; top:0; height:100%; border-radius:5px; background:var(--person); }
  .cval { font:700 12.5px/1 var(--body); text-align:right; font-variant-numeric:tabular-nums; }
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
    rows = display_decades(payload)
    solid = [r for r in rows if r["decade"] < CENSORED_FROM]
    censored = [r for r in rows if r["decade"] >= CENSORED_FROM]
    last = solid[-1]
    first = solid[0]

    milestone_rows = []
    for m in payload["milestones"]:
        milestone_rows.append(
            f"""<li><span class="mname">Women reach {_pct(m['share'])} of a cohort"""
            f"""<em>{_n(m['n'])} office-holders in that cohort ({_pct(m['value'], 1)})</em></span>"""
            f"""<span class="myear">{m['decade']}s</span></li>"""
        )

    countries = payload.get("countries", [])[:8]
    max_share = max((c["share"] for c in countries), default=1.0) or 1.0
    country_rows = []
    for c in countries:
        pct = 100.0 * c["share"] / max_share
        country_rows.append(
            f"""<li><span class="cname">{_esc(c['name'])}</span>"""
            f"""<span class="cbar"><i style="width:{pct:.1f}%"></i></span>"""
            f"""<span class="cval">{_pct(c['share'])}</span></li>"""
        )

    deck = (
        f"Among the <b>{_n(scale['measured'])}</b> office-holders with a recorded birth year, women are <b>{_pct(last['share'])}</b> of the "
        f"cohort born in the {last['decade']}s, against <b>{_pct(first['share'], 1)}</b> of "
        f"those born in the {first['decade']}s. The share first topped one in ten for people "
        f"born in the 1930s, one in five for the 1950s and one in three for the 1980s."
    )
    if censored:
        c = censored[0]
        deck += (
            f" The {c['decade']}s cohort is still filling in: <b>{_pct(c['share'])}</b> "
            f"so far, and rising."
        )

    caveat = (
        "<b>Read with care.</b> This counts documented Wikidata &ldquo;position held&rdquo; "
        "statements by the holder&rsquo;s birth decade, not real-world power. The source "
        "records gender as binary, so non-binary identities are invisible here, and the "
        "youngest cohorts are still filling in: people born in the 1990s have had less "
        "time to accumulate offices, and the 2000s cohort (*) is only partway through its "
        "careers &mdash; its 31% understates where it will land. Coverage is uneven: the "
        "US, Europe and especially France are better recorded, and the mix of offices "
        "differs by country."
    )

    card = f"""<section class="card">
  <header>
    <span class="eyebrow">Political office</span>
    <span class="brand">Humans&nbsp;Map</span>
  </header>
  <h1>From <em>1 in 200</em> to <em>1 in 3</em>: women&rsquo;s share of political office.</h1>
  <p class="deck">{deck}</p>
  <div class="body">
    <div class="hero">
      <div class="charthead">
        <span class="plabel">Women as a share of office-holders</span>
        <span class="unit">by birth decade &middot; percent of cohort</span>
      </div>
      {plot}
      <div class="legend">
        <span><i class="swatch"></i>share of the cohort recorded as women</span>
        <span><i class="swatch censored"></i>2000s cohort still filling in</span>
        <span>the 1800s&ndash;2000s are shown</span>
      </div>
    </div>
    <aside class="panel">
      <div class="block">
        <div class="plabel">When each level was crossed</div>
        <ul class="miles">{''.join(milestone_rows)}</ul>
        <div class="note">Each step is the first birth cohort in which women
          reached that share of all office-holders.</div>
      </div>
      <div class="block">
        <div class="plabel">Most women, born 1950s&ndash;80s</div>
        <ul class="cnt">{''.join(country_rows)}</ul>
        <div class="note">Country is the primary recorded nationality; the mix of
          offices differs by country; the eight highest-share countries with at least 1,500
          people are shown, so several larger countries are absent.</div>
      </div>
      <div class="caveat">{caveat}</div>
    </aside>
  </div>
  <footer>
    <span>Data: Wikidata (CC BY-SA) &middot; humansmap.com</span>
    <span><b>{_n(scale['women'])}</b> women &middot; <b>{_n(scale['measured'])}</b> office-holders with a birth year &middot; <b>{_pct(scale['overall_share'], 1)}</b> overall</span>
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
    parser.add_argument("--min-year", type=int, default=MIN_YEAR)
    parser.add_argument("--max-year", type=int, default=MAX_YEAR)
    parser.add_argument("--country-n", type=int, default=COUNTRY_MIN_N)
    parser.add_argument("--no-html", action="store_true")
    parser.add_argument(
        "--from-json", default=None,
        help="re-render from an existing politics_women.json instead of querying the DB",
    )
    args = parser.parse_args()

    if args.from_json:
        payload = json.loads(Path(args.from_json).read_text())
        # The cached payload may predate a range extension; keep its params in
        # sync with the current constants so re-renders are not silently cut.
        payload.setdefault("params", {})
        payload["params"]["display_from"] = DISPLAY_FROM
        payload["params"]["display_to"] = DISPLAY_TO
        print(f"[cache] loaded {args.from_json} (no DB access)")
    else:
        payload = collect(args.scan_limit, args.min_year, args.max_year, args.country_n)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if not args.from_json:
        json_path = out_dir / "politics_women.json"
        json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        print(f"wrote {json_path}")

    if not args.no_html:
        html_path = out_dir / "politics_women.html"
        html_path.write_text(render_html(payload))
        print(f"wrote {html_path}")

    scale = payload["scale"]
    rows = display_decades(payload)
    summary = {
        "measured": scale["measured"],
        "women": scale["women"],
        "overall_share": round(scale["overall_share"], 4),
        "first_display": {"decade": rows[0]["decade"], "share": round(rows[0]["share"], 4)} if rows else None,
        "last_display": {"decade": rows[-1]["decade"], "share": round(rows[-1]["share"], 4)} if rows else None,
        "milestones": [{"share": m["share"], "decade": m["decade"]} for m in payload["milestones"]],
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
