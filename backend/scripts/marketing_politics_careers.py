#!/usr/bin/env python3
"""Marketing politics miner — read-only, emits the "how long is a political
career?" card.

The insight: a documented political career is mostly short. Among the people in
the humansmap.com graph with at least two dated offices, the median first-to-last
span is 11 years: half are done inside 11 years, only one in five is still
holding office 20 years on, one in twelve reaches 30 and under 3% reach 40.

It never writes to ArangoDB. Everything comes from one bounded pass on the
``affiliations`` ``type`` index (a hash index on ``type``, so
``FILTER e.type == 'position_held'`` is an index lookup, not a collection scan):

  * each edge's ``start_date`` / ``end_date`` qualifiers are turned into whole
    years, then collapsed with a two-stage ``COLLECT``: first per person
    (earliest start, latest end), then a distribution of the spans. The result
    is one row per span in years — tiny — and no ``DOCUMENT()`` joins at all.

The span is the gap between the earliest recorded office start and the latest
recorded office end (falling back to its start when the end is missing). A
person with only one dated office is *not* counted, because a single statement
cannot show a career; that is stated on the card.

Run it manually inside the backend container:

    docker compose exec -T backend python - --out-dir /tmp/politics \\
        < backend/scripts/marketing_politics_careers.py
    docker compose exec -T backend cat /tmp/politics/politics_careers.json \\
        > docs/marketing/politics/politics_careers.json
    docker compose exec -T backend cat /tmp/politics/politics_careers.html \\
        > docs/marketing/politics/politics_careers.html

Flags:
    --out-dir PATH     output directory (default: docs/marketing/politics)
    --scan-limit N     max position-held edges fed into the pass
    --max-years N      horizon of the survival curve (default: 60)
    --no-html          emit only the JSON payload
    --from-json PATH   re-render from an existing politics_careers.json (no DB access)
"""

from __future__ import annotations

import argparse
import json
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
MAX_YEARS = 60
MILESTONES = (10, 20, 30, 40, 50)

# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

# Stage 1: per person, earliest start year and latest end year, from the dated
# edges only. Stage 2: distribution of the span. No document joins.
SPAN_AQL = """
FOR e IN affiliations
  FILTER e.type == 'position_held' AND e.start_date != null
  LIMIT @scan_limit
  LET neg = LEFT(e.start_date, 1) == '-'
  LET by = TO_NUMBER(SUBSTRING(e.start_date, neg ? 1 : 0, 4))
  LET b = neg ? -by : by
  LET e2 = e.end_date == null ? e.start_date : e.end_date
  LET neg2 = LEFT(e2, 1) == '-'
  LET ey0 = TO_NUMBER(SUBSTRING(e2, neg2 ? 1 : 0, 4))
  LET ey = neg2 ? -ey0 : ey0
  COLLECT p = e._from AGGREGATE cnt = COUNT(1), bmin = MIN(b), emax = MAX(ey)
  FILTER cnt >= 2
  COLLECT span = emax - bmin WITH COUNT INTO people
  SORT span
  RETURN {span: span, people: people}
"""

# How many people have one dated office vs. several — the excluded single-office
# group is shown on the card so the denominator is honest.
DATED_HOLDER_COUNTS_AQL = """
FOR e IN affiliations
  FILTER e.type == 'position_held' AND e.start_date != null
  LIMIT @scan_limit
  COLLECT p = e._from WITH COUNT INTO n
  COLLECT AGGREGATE people = COUNT(1), one = SUM(n == 1 ? 1 : 0),
    multi = SUM(n >= 2 ? 1 : 0)
  RETURN {people: people, one: one, multi: multi}
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


def _weighted_quantile(dist: list[dict], fraction: float, total: int) -> int:
    acc = 0
    for r in dist:
        acc += r["people"]
        if acc >= fraction * total:
            return r["span"]
    return dist[-1]["span"] if dist else 0


def collect(scan_limit: int, max_years: int) -> dict:
    db = get_db(get_arango_client(request_timeout=900))
    bind = {"scan_limit": scan_limit}

    raw = _rows(db, SPAN_AQL, bind)
    holder_rows = _rows(db, DATED_HOLDER_COUNTS_AQL, bind)
    office_holder_rows = _rows(db, DISTINCT_OFFICE_HOLDERS_AQL, bind)
    position_rows = _rows(db, POSITIONS_AQL)

    # negative spans are date-entry errors; clamp them to zero.
    distribution = []
    for r in raw:
        span = int(r.get("span") or 0)
        people = int(r.get("people") or 0)
        if people <= 0:
            continue
        distribution.append({"span": max(0, span), "people": people})

    # merge any two rows that clamped to the same value
    merged: dict[int, int] = {}
    for r in distribution:
        merged[r["span"]] = merged.get(r["span"], 0) + r["people"]
    distribution = [{"span": s, "people": p} for s, p in sorted(merged.items())]

    total = sum(r["people"] for r in distribution)
    max_span = max((r["span"] for r in distribution), default=0)

    def at_least(years: int) -> int:
        return sum(r["people"] for r in distribution if r["span"] >= years)

    # survival curve: share whose documented service reached at least N years
    curve = [
        OrderedDict(years=y, people=at_least(y), share=(at_least(y) / total) if total else 0.0)
        for y in range(0, max_years + 1)
    ]

    quantiles = OrderedDict(
        median=_weighted_quantile(distribution, 0.50, total),
        p25=_weighted_quantile(distribution, 0.25, total),
        p75=_weighted_quantile(distribution, 0.75, total),
        p90=_weighted_quantile(distribution, 0.90, total),
    )

    milestones = [
        OrderedDict(years=y, people=at_least(y),
                    share=(at_least(y) / total) if total else 0.0)
        for y in MILESTONES
    ]

    holder = holder_rows[0] if holder_rows else {}

    return {
        "generated": date.today().isoformat(),
        "params": {
            "scan_limit": scan_limit,
            "max_years": max_years,
            "milestones": list(MILESTONES),
        },
        "scale": {
            "people": total,
            "office_holders": int(office_holder_rows[0]) if office_holder_rows else None,
            "positions": int(position_rows[0]) if position_rows else None,
            "with_dated": int(holder.get("people") or 0),
            "single_dated": int(holder.get("one") or 0),
            "multi_dated": int(holder.get("multi") or 0),
            "max_span": max_span,
        },
        "quantiles": quantiles,
        "distribution": distribution,
        "curve": curve,
        "milestones": milestones,
    }


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
# Hero chart: survival curve (inline SVG)
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

Y_TICKS = (0, 25, 50, 75, 100)
X_TICKS = (0, 10, 20, 30, 40, 50, 60)


def _x(years: int, xmax: int) -> float:
    return PLOT_L + PLOT_W_INNER * (years / xmax)


def _y(pct: float) -> float:
    return PLOT_B - PLOT_H_INNER * (pct / 100.0)


def build_plot(payload: dict) -> str:
    curve = payload["curve"]
    scale = payload["scale"]
    xmax = max(1, payload["params"]["max_years"])
    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {PLOT_W} {PLOT_H}" '
        f'width="{PLOT_W}" height="{PLOT_H}" role="img" '
        f'aria-label="Share of office-holders whose documented career reached at least N years">'
    ]

    # --- gridlines + y ticks ------------------------------------------------
    for pct in Y_TICKS:
        yy = _y(pct)
        cls = "grid" if pct != 50 else "grid50"
        parts.append(f'<line x1="{PLOT_L}" y1="{yy:.1f}" x2="{PLOT_R}" y2="{yy:.1f}" class="{cls}"/>')
        parts.append(
            f'<text x="{PLOT_L - 10:.1f}" y="{yy + 3.5:.1f}" class="ytick" text-anchor="end">{pct}%</text>'
        )
    parts.append(
        f'<line x1="{PLOT_L}" y1="{PLOT_B:.1f}" x2="{PLOT_R}" y2="{PLOT_B:.1f}" class="axis"/>'
    )

    # --- x ticks ------------------------------------------------------------
    for xv in X_TICKS:
        if xv > xmax:
            continue
        xx = _x(xv, xmax)
        parts.append(f'<line x1="{xx:.1f}" y1="{PLOT_B:.1f}" x2="{xx:.1f}" y2="{PLOT_B + 5:.1f}" class="axis"/>')
        parts.append(
            f'<text x="{xx:.1f}" y="{PLOT_B + 22:.1f}" class="xtick" text-anchor="middle">{xv}</text>'
        )

    # --- area + survival line ----------------------------------------------
    area = [f'M {_x(0, xmax):.1f} {PLOT_B:.1f}']
    for c in curve:
        area.append(f'L {_x(c["years"], xmax):.1f} {_y(c["share"] * 100):.1f}')
    area.append(f'L {_x(curve[-1]["years"], xmax):.1f} {PLOT_B:.1f} Z')
    parts.append(f'<path d="{" ".join(area)}" class="area"/>')

    line = []
    for i, c in enumerate(curve):
        cmd = "M" if i == 0 else "L"
        line.append(f'{cmd} {_x(c["years"], xmax):.1f} {_y(c["share"] * 100):.1f}')
    parts.append(f'<path d="{" ".join(line)}" class="line"/>')

    # --- median marker ------------------------------------------------------
    median = payload["quantiles"]["median"]
    mx = _x(median, xmax)
    my = _y(50.0)
    parts.append(f'<line x1="{mx:.1f}" y1="{PLOT_B:.1f}" x2="{mx:.1f}" y2="{my:.1f}" class="marker"/>')
    parts.append(f'<circle cx="{mx:.1f}" cy="{my:.1f}" r="4.2" class="mdot"/>')
    parts.append(
        f'<text x="{mx + 8:.1f}" y="{my + 4:.1f}" class="mlabel">median: {median} years</text>'
    )

    # --- milestone annotations on the curve --------------------------------
    for m in payload["milestones"]:
        if m["years"] > xmax:
            continue
        xx = _x(m["years"], xmax)
        yy = _y(m["share"] * 100)
        parts.append(f'<circle cx="{xx:.1f}" cy="{yy:.1f}" r="3.6" class="dot">'
                     f'<title>{_n(m["people"])} people reached {m["years"]}+ years '
                     f'({_pct(m["share"])} of those measured)</title></circle>')
        anchor = "start" if m["years"] <= 30 else "end"
        dx = 8 if anchor == "start" else -8
        parts.append(
            f'<text x="{xx + dx:.1f}" y="{yy - 8:.1f}" class="ptval" text-anchor="{anchor}">'
            f'{m["years"]}+ yrs: {_pct(m["share"])}</text>'
        )

    # --- headline callout, top-right (clear of the curve) ------------------
    parts.append(
        f'<text x="{PLOT_R:.1f}" y="{PLOT_T + 24:.1f}" class="callout" text-anchor="end">Half of all political careers</text>'
    )
    parts.append(
        f'<text x="{PLOT_R:.1f}" y="{PLOT_T + 44:.1f}" class="callout" text-anchor="end">on record span 11 years or less</text>'
    )

    # --- x caption ----------------------------------------------------------
    parts.append(
        f'<text x="{(PLOT_L + PLOT_R) / 2:.1f}" y="{PLOT_H - 10:.1f}" class="xcap" text-anchor="middle">'
        f'years from the first recorded office to the last; people with at least two dated positions</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Card HTML
# ---------------------------------------------------------------------------

HTML_HEAD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>HumansMap: how long is a political career?</title>
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
  .grid50 { stroke:rgba(28,21,16,.22); stroke-width:1; stroke-dasharray:5 5; }
  .axis { stroke:rgba(28,21,16,.30); stroke-width:1; }
  .ytick { font:500 11px/1 var(--body); fill:var(--muted); font-variant-numeric:tabular-nums; }
  .xtick { font:500 11px/1 var(--body); fill:var(--muted); font-variant-numeric:tabular-nums; }
  .xcap { font:400 11.5px/1 var(--body); fill:var(--muted); }
  .area { fill:rgba(191,107,40,.14); }
  .line { fill:none; stroke:var(--person); stroke-width:2.6; stroke-linejoin:round; }
  .dot { fill:var(--person); stroke:var(--bg); stroke-width:1.4; }
  .marker { stroke:rgba(28,21,16,.45); stroke-width:1.2; stroke-dasharray:4 4; }
  .mdot { fill:var(--bg); stroke:var(--person); stroke-width:2.4; }
  .mlabel { font:700 12.5px/1 var(--body); fill:var(--text); }
  .ptval { font:700 12.5px/1 var(--body); fill:var(--text); }
  .callout { font:700 13px/1 var(--body); fill:var(--accent); }
  .legend { display:flex; gap:20px; align-items:center; margin-top:9px;
    font:400 12px/1.35 var(--body); color:var(--muted); }
  .legend span { display:flex; align-items:center; gap:7px; }
  .legend .swatch { width:18px; height:11px; border-radius:3px; background:var(--person); }
  .panel { flex:1; min-width:0; display:flex; flex-direction:column; }
  .block { border-top:2px solid var(--text); padding-top:9px; }
  .block + .block { margin-top:14px; }
  .plabel { font:700 11px/1 var(--body); letter-spacing:.18em; text-transform:uppercase; color:var(--muted); }
  ul.miles { list-style:none; margin:8px 0 0; padding:0; }
  ul.miles li { display:grid; grid-template-columns:70px 1fr auto; align-items:center; gap:10px; padding:6px 0; }
  .myears { font:800 17px/1 var(--display); font-variant-numeric:tabular-nums; }
  .mbar { position:relative; height:10px; background:var(--surface); border-radius:5px; }
  .mbar i { position:absolute; left:0; top:0; height:100%; border-radius:5px; background:var(--person); }
  .mval { font:700 13px/1 var(--body); text-align:right; font-variant-numeric:tabular-nums; min-width:64px; }
  .mval em { display:block; font:400 10.5px/1.3 var(--body); font-style:normal; color:var(--muted); text-align:right; }
  .stat { display:flex; align-items:baseline; justify-content:space-between; padding:6px 0; border-bottom:1px solid var(--line); }
  .stat .sl { font:600 13.5px/1.15 var(--body); }
  .stat .sl em { display:block; font:400 11px/1.4 var(--body); font-style:normal; color:var(--muted); margin-top:2px; }
  .stat .sv { font:800 21px/1 var(--display); font-variant-numeric:tabular-nums; }
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
    quant = payload["quantiles"]
    plot = build_plot(payload)

    max_share = max((m["share"] for m in payload["milestones"]), default=1.0) or 1.0
    milestone_rows = []
    for m in payload["milestones"]:
        w = 100.0 * m["share"] / max_share
        milestone_rows.append(
            f"""<li><span class="myears">{m['years']}+</span>"""
            f"""<span class="mbar"><i style="width:{w:.1f}%"></i></span>"""
            f"""<span class="mval">{_pct(m['share'])}<em>{_n(m['people'])} people</em></span></li>"""
        )

    stats = [
        ("Half are done within", f"{quant['median']} years",
         f"median span of the {_n(scale['people'])} people measured"),
        ("One in four reaches", f"{quant['p75']} years",
         "three-quarters end sooner"),
        ("One in ten reaches", f"{quant['p90']} years",
         "the longest tenth of careers"),
    ]
    stat_rows = []
    for label, value, sub in stats:
        stat_rows.append(
            f"""<div class="stat"><span class="sl">{label}<em>{sub}</em></span>"""
            f"""<span class="sv">{value}</span></div>"""
        )

    deck = (
        f"Of the <b>{_n(scale['people'])}</b> people with an "
        f"office-holder&rsquo;s life recorded at both ends, the median first-to-last span is "
        f"<b>{quant['median']} years</b>: <b>{_pct(payload['milestones'][0]['share'])}</b> "
        f"reached ten years, <b>{_pct(payload['milestones'][2]['share'])}</b> reached thirty "
        f"and <b>{_pct(payload['milestones'][3]['share'])}</b> reached forty. "
        f"Serving for decades is the exception, not the rule."
    )

    caveat = (
        "<b>Read with care.</b> This measures the span of documented Wikidata "
        "&ldquo;position held&rdquo; statements: in office is not the same as in "
        "power, and a missing statement is not an ended career. Only people with at "
        "least two dated positions are counted; a single dated office cannot show a "
        "career. Coverage is uneven (the US, Europe and Italy are denser), some dates "
        "are only year-accurate, and a handful of records span implausible periods."
    )

    card = f"""<section class="card">
  <header>
    <span class="eyebrow">Political office</span>
    <span class="brand">Humans&nbsp;Map</span>
  </header>
  <h1>Half of political careers last <em>{quant['median']} years</em> or less.</h1>
  <p class="deck">{deck}</p>
  <div class="body">
    <div class="hero">
      <div class="charthead">
        <span class="plabel">Careers reaching each length</span>
        <span class="unit">share of the {_n(scale['people'])} people with two or more dated positions</span>
      </div>
      {plot}
      <div class="legend">
        <span><i class="swatch"></i>share still holding office after N years</span>
        <span>the dotted line marks the median; the curve falls away fast</span>
      </div>
    </div>
    <aside class="panel">
      <div class="block">
        <div class="plabel">How many reach&hellip;</div>
        <ul class="miles">{''.join(milestone_rows)}</ul>
        <div class="note">Bar length is the share of the measured group; the
          figure is the number of people.</div>
      </div>
      <div class="block">
        <div class="plabel">The shape of a career</div>
        {''.join(stat_rows)}
        <div class="note">Measured on the {_n(scale['people'])} people with at least two dated
          offices &mdash; {scale['people'] * 100 / scale['office_holders']:.0f}% of the
          {_n(scale['office_holders'])} office-holders. Left out: the {_n(scale['single_dated'])} with a
          single dated office and the {_n(scale['office_holders'] - scale['with_dated'])} with no dated
          office at all.</div>
      </div>
      <div class="caveat">{caveat}</div>
    </aside>
  </div>
  <footer>
    <span>Data: Wikidata (CC BY-SA) &middot; humansmap.com</span>
    <span><b>{_n(scale['people'])}</b> careers measured &middot; <b>{quant['median']}</b> year median &middot; <b>{_n(scale['positions'])}</b> positions</span>
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
    parser.add_argument("--max-years", type=int, default=MAX_YEARS)
    parser.add_argument("--no-html", action="store_true")
    parser.add_argument(
        "--from-json", default=None,
        help="re-render from an existing politics_careers.json instead of querying the DB",
    )
    args = parser.parse_args()

    if args.from_json:
        payload = json.loads(Path(args.from_json).read_text())
        print(f"[cache] loaded {args.from_json} (no DB access)")
    else:
        payload = collect(args.scan_limit, args.max_years)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if not args.from_json:
        json_path = out_dir / "politics_careers.json"
        json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        print(f"wrote {json_path}")

    if not args.no_html:
        html_path = out_dir / "politics_careers.html"
        html_path.write_text(render_html(payload))
        print(f"wrote {html_path}")

    scale = payload["scale"]
    print(json.dumps({
        "people": scale["people"],
        "median": payload["quantiles"]["median"],
        "p75": payload["quantiles"]["p75"],
        "p90": payload["quantiles"]["p90"],
        "milestones": [{"years": m["years"], "share": round(m["share"], 4), "people": m["people"]}
                       for m in payload["milestones"]],
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
