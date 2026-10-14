#!/usr/bin/env python3
"""Marketing organizations miner — read-only; "the rise and fall of organizations".

What the data actually supports (probed 2026-09): only a small slice of the
``organizations`` collection records a lifespan. Of 324,101 organizations,
151,830 have a founding date and 22,274 a dissolution date, but only **20,284
have both** — the set a lifespan chart needs. ``number_of_employees`` is empty;
``revenue`` exists for ~8.8k, too sparse to draw. So the card is built from the
20,284 two-date organizations plus the oldest still-operating institutions, and
it says so.

Two views, one finding ("how long an organization lasts depends on what kind it
is — and a handful break every rule"):

  * ``spans``            — hand-curated longest-lived institutions with a bar
    from founded → dissolved (or → today); the display list is curated (see
    DISPLAY) because the raw tail is full of Wikidata artifacts: French
    notarial offices, cemetery plots and 16th-century Venetian printing
    partnerships, all recorded as "organizations".
  * ``type_lifespans``   — median years-to-closure by type for organizations
    founded 1900–1970 and closed by today. The fixed birth window gives every
    type the same chance to close, so the comparison is fair.
  * ``waves``            — foundings and closures per decade (typed orgs only,
    1800→2020), the "rise and fall" backdrop.
  * ``oldest_active`` / ``notable_closures`` — the extremes.

It never writes to ArangoDB. Everything is bounded: one field-presence
aggregation over ``organizations``, one indexed pull for the type cohort
(~5.5k rows), two decade aggregations, and a ``LIMIT``-bounded tail pull.

Run it manually inside the backend container:

    docker compose exec -T backend python - --out-dir /tmp/orgs \\
        < backend/scripts/marketing_orgs.py
    docker compose exec -T backend cat /tmp/orgs/orgs.json \\
        > docs/marketing/orgs/orgs.json
    docker compose exec -T backend cat /tmp/orgs/orgs.html \\
        > docs/marketing/orgs/orgs.html

Flags:
    --out-dir PATH   output directory (default: docs/marketing/orgs)
    --no-html        emit only the JSON payload
    --from-json P    re-render the card from an existing orgs.json (no DB)
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

# --- filters ---------------------------------------------------------------
# Types that are actual organizations. ``position`` is an office, not a body;
# ``museum`` in this graph is mostly ancient churches/castles with a modern
# museum record, so its "founding" is an antiquity date, not an institution.
TYPE_KEEP = [
    "company", "university", "bank", "newspaper", "magazine", "publisher",
    "political_party", "sports_club", "government_agency", "nonprofit",
    "foundation", "broadcaster", "television_station", "international_organization",
    "news_agency", "educational_institution", "secondary_school",
]

# Type-cohort window: everyone founded in this span has had decades to close.
COHORT_LO, COHORT_HI = 1900, 1970
MIN_TYPE_N = 250          # display floor for the type panel (n is printed)
YEAR_MIN, YEAR_MAX = 1000, 2026
WAVE_LO, WAVE_HI = 1800, 2020

# The lifespan Gantt is curated. These are the recognisable, well-recorded
# long-lived institutions left after removing Wikidata's non-organisation noise
# (notarial offices, cemeteries, printer partnerships) — see module docstring.
DISPLAY = [
    ("Q1639969", "Suzhou High School"),
    ("Q578269", "HM Treasury"),
    ("Q131262", "University of Bologna"),
    ("Q334064", "Neresheim Abbey"),
    ("Q34433", "University of Oxford"),
    ("Q1143779", "St Thomas' Hospital"),
    ("Q697221", "Heiligenkreuz Abbey"),
    ("Q339332", "Premonstratensians"),
    ("Q1919732", "Melrose Abbey"),
    ("Q1775481", "Monastery of La Espina"),
    ("Q756910", "Order of Calatrava"),
    ("Q1234169", "Roman Rota"),
    ("Q1094147", "City of London Corporation"),
    ("Q2845896", "University of Paris"),
    ("Q28150181", "Guild of the Four Crowned Ones"),
    ("Q42908", "Hanseatic League"),
    ("Q3349678", "Office of Works"),
    ("Q3633695", "Banca Carige"),
    ("Q672420", "University of Königsberg"),
    ("Q731830", "Congregation for Bishops"),
]


def _rows(db, aql: str, bind: dict | None = None) -> list:
    try:
        return list(db.aql.execute(aql, bind_vars=bind or {}))
    except Exception as exc:  # noqa: BLE001 - a marketing job must not crash
        print(f"[warn] query failed: {exc}", file=sys.stderr)
        return []


def _year(value) -> int | None:
    if not value:
        return None
    try:
        y = int(str(value)[:4])
    except (TypeError, ValueError):
        return None
    return y if 0 < y <= YEAR_MAX else None


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def collect(scan_limit: int = 2_000_000) -> dict:
    db = get_db(get_arango_client(request_timeout=600))

    # 1. Field coverage — one bounded aggregation.
    scale = _rows(
        db,
        "FOR o IN organizations "
        "COLLECT AGGREGATE total=LENGTH(1), "
        "  founded=SUM(o.founded != null ? 1 : 0), "
        "  dissolved=SUM(o.dissolved != null ? 1 : 0), "
        "  both=SUM(o.founded != null AND o.dissolved != null ? 1 : 0), "
        "  revenue=SUM(o.revenue != null ? 1 : 0) "
        "RETURN {total, founded, dissolved, both, revenue}",
    )

    # 2. Curated lifespan bars (bounded DOCUMENT lookups by QID).
    qids = [q for q, _ in DISPLAY]
    docs = _rows(
        db,
        "FOR o IN organizations FILTER o.wikidata_id IN @q "
        "RETURN {qid: o.wikidata_id, name: o.name_en, type: o.type, "
        "  country: o.country, founded: o.founded, dissolved: o.dissolved}",
        {"q": qids},
    )
    by_qid = {d["qid"]: d for d in docs}
    spans = []
    for qid, label in DISPLAY:
        d = by_qid.get(qid)
        if not d:
            continue
        fy, dy = _year(d.get("founded")), _year(d.get("dissolved"))
        if fy is None:
            continue
        end = dy if dy else YEAR_MAX
        spans.append(OrderedDict(
            qid=qid, name=label, type=d.get("type"), country=d.get("country"),
            founded=fy, dissolved=dy, span=end - fy,
            active=dy is None,
        ))
    spans.sort(key=lambda s: (s["founded"], s["name"]))

    # 3. Median lifespan by type, fixed birth window (fair comparison).
    cohort = _rows(
        db,
        "FOR o IN organizations "
        "FILTER o.founded != null AND o.dissolved != null AND o.type IN @types "
        "LET fy = TO_NUMBER(SUBSTRING(o.founded, 0, 4)) "
        "LET dy = TO_NUMBER(SUBSTRING(o.dissolved, 0, 4)) "
        "FILTER fy >= @lo AND fy <= @hi AND dy >= fy AND dy <= @ymax "
        "LIMIT @lim "
        "RETURN {type: o.type, span: dy - fy}",
        {"types": TYPE_KEEP, "lo": COHORT_LO, "hi": COHORT_HI,
         "ymax": YEAR_MAX, "lim": scan_limit},
    )
    by_type: dict[str, list[int]] = defaultdict(list)
    for r in cohort:
        by_type[r["type"]].append(int(r["span"]))
    type_lifespans = []
    for t, vals in by_type.items():
        vals.sort()
        n = len(vals)
        if n < MIN_TYPE_N:
            continue
        type_lifespans.append(OrderedDict(
            type=t, n=n,
            p25=vals[n // 4], median=vals[n // 2], p75=vals[(3 * n) // 4],
        ))
    type_lifespans.sort(key=lambda r: r["median"])

    # 4. Founding vs closure waves by decade (typed orgs only).
    wave_found = _rows(
        db,
        "FOR o IN organizations "
        "FILTER o.founded != null AND o.type IN @types "
        "LET fy = TO_NUMBER(SUBSTRING(o.founded, 0, 4)) "
        "FILTER fy >= @lo AND fy <= @hi "
        "COLLECT dec = FLOOR(fy / 10) * 10 WITH COUNT INTO c "
        "SORT dec RETURN {dec, c}",
        {"types": TYPE_KEEP, "lo": WAVE_LO, "hi": WAVE_HI},
    )
    wave_closed = _rows(
        db,
        "FOR o IN organizations "
        "FILTER o.dissolved != null AND o.type IN @types "
        "LET dy = TO_NUMBER(SUBSTRING(o.dissolved, 0, 4)) "
        "FILTER dy >= @lo AND dy <= @hi "
        "COLLECT dec = FLOOR(dy / 10) * 10 WITH COUNT INTO c "
        "SORT dec RETURN {dec, c}",
        {"types": TYPE_KEEP, "lo": WAVE_LO, "hi": WAVE_HI},
    )
    waves = {
        "founded": [OrderedDict(dec=r["dec"], n=r["c"]) for r in wave_found],
        "closed": [OrderedDict(dec=r["dec"], n=r["c"]) for r in wave_closed],
    }

    # 5. Oldest still-operating organizations (bounded tail pull).
    active = _rows(
        db,
        "FOR o IN organizations "
        "FILTER o.founded != null AND o.dissolved == null AND o.type IN @types "
        "LET fy = TO_NUMBER(SUBSTRING(o.founded, 0, 4)) "
        "FILTER fy >= @ymin AND fy < @cut "
        "SORT fy ASC LIMIT @lim "
        "RETURN {qid: o.wikidata_id, name: o.name_en, type: o.type, "
        "  country: o.country, founded: fy}",
        {"types": TYPE_KEEP, "ymin": YEAR_MIN, "cut": 1400, "lim": 60},
    )
    for r in active:
        r["age"] = YEAR_MAX - r["founded"]

    # 6. Longest-lived closed organizations (raw tail, for the JSON record).
    closed = _rows(
        db,
        "FOR o IN organizations "
        "FILTER o.founded != null AND o.dissolved != null AND o.type IN @types "
        "LET fy = TO_NUMBER(SUBSTRING(o.founded, 0, 4)) "
        "LET dy = TO_NUMBER(SUBSTRING(o.dissolved, 0, 4)) "
        "FILTER fy >= @ymin AND dy > fy AND dy <= @ymax "
        "LET span = dy - fy SORT span DESC LIMIT @lim "
        "RETURN {qid: o.wikidata_id, name: o.name_en, type: o.type, "
        "  country: o.country, founded: fy, dissolved: dy, span: span}",
        {"types": TYPE_KEEP, "ymin": YEAR_MIN, "ymax": YEAR_MAX, "lim": 40},
    )

    oldest_active = active[0] if active else {}
    longest_closed = closed[0] if closed else {}
    longest = max(spans, key=lambda s: s["span"]) if spans else {}

    return OrderedDict(
        generated=date.today().isoformat(),
        params=OrderedDict(
            year_min=YEAR_MIN, year_max=YEAR_MAX,
            cohort=(COHORT_LO, COHORT_HI), min_type_n=MIN_TYPE_N,
            wave=(WAVE_LO, WAVE_HI), type_keep=TYPE_KEEP,
        ),
        scale=OrderedDict(
            total=(scale[0]["total"] if scale else 0),
            with_founded=(scale[0]["founded"] if scale else 0),
            with_dissolved=(scale[0]["dissolved"] if scale else 0),
            with_both=(scale[0]["both"] if scale else 0),
            with_revenue=(scale[0]["revenue"] if scale else 0),
        ),
        headline=OrderedDict(
            oldest_active_name=oldest_active.get("name"),
            oldest_active_year=oldest_active.get("founded"),
            oldest_active_age=oldest_active.get("age"),
            longest_closed_name=longest_closed.get("name"),
            longest_closed_span=longest_closed.get("span"),
            longest_closed_founded=longest_closed.get("founded"),
            longest_closed_dissolved=longest_closed.get("dissolved"),
            shortest_type=(type_lifespans[0]["type"] if type_lifespans else None),
            shortest_type_median=(type_lifespans[0]["median"] if type_lifespans else None),
            longest_type=(type_lifespans[-1]["type"] if type_lifespans else None),
            longest_type_median=(type_lifespans[-1]["median"] if type_lifespans else None),
        ),
        spans=spans,
        type_lifespans=type_lifespans,
        waves=waves,
        oldest_active=active[:15],
        notable_closures=closed[:15],
    )


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _esc(value) -> str:
    return (str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _n(value) -> str:
    try:
        return f"{int(round(float(value))):,}"
    except (TypeError, ValueError):
        return "0"


def _label(value: str) -> str:
    s = str(value or "").replace("_", " ")
    return s[:1].upper() + s[1:] if s else s


def _plural(word: str) -> str:
    if word.endswith(("s", "x", "z", "ch", "sh")):
        return word + "es"
    if word.endswith("y") and len(word) > 1 and word[-2] not in "aeiou":
        return word[:-1] + "ies"
    return word + "s"


# ---------------------------------------------------------------------------
# Inline SVG — lifespan Gantt (main) + wave + type panels
# ---------------------------------------------------------------------------

PLOT_W, PLOT_H = 980, 660
LABEL_R = 286
PLOT_X0, PLOT_X1 = 306, 900
PLOT_Y0, PLOT_Y1 = 40, PLOT_H - 14
X_MIN, X_MAX = 1000, 2030

PANEL_W = 502
WAVE_H = 208
TYPE_H = 238


def _px(year: float) -> float:
    return PLOT_X0 + (year - X_MIN) / (X_MAX - X_MIN) * (PLOT_X1 - PLOT_X0)


def build_gantt(payload: dict) -> str:
    spans = payload["spans"]
    n = max(1, len(spans))
    row_h = (PLOT_Y1 - PLOT_Y0) / n
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {PLOT_W} {PLOT_H}" '
        f'width="{PLOT_W}" height="{PLOT_H}" role="img" '
        f'aria-label="Lifespans of long-lived organizations, founded to dissolved or to today">'
    ]
    for year in range(1100, 1901, 100):
        x = _px(year)
        parts.append(f'<line x1="{x:.1f}" y1="{PLOT_Y0 - 10}" x2="{x:.1f}" y2="{PLOT_Y1}" class="grid"/>')
        parts.append(f'<text x="{x:.1f}" y="{PLOT_Y0 - 18}" class="tick" text-anchor="middle">{year}</text>')
    # today marker
    xt = _px(payload["params"]["year_max"])
    parts.append(f'<line x1="{xt:.1f}" y1="{PLOT_Y0 - 10}" x2="{xt:.1f}" y2="{PLOT_Y1}" class="today"/>')
    parts.append(f'<text x="{xt + 2:.1f}" y="{PLOT_Y0 - 18}" class="tick todaytag" text-anchor="start">today</text>')

    for i, s in enumerate(spans):
        cy = PLOT_Y0 + i * row_h + row_h / 2
        x_f = _px(s["founded"])
        x_e = _px(s["dissolved"] or payload["params"]["year_max"])
        cls = "bar open" if s["active"] else "bar closed"
        parts.append(
            f'<text x="{LABEL_R}" y="{cy + 4.4:.1f}" class="rowlabel" text-anchor="end">{_esc(s["name"])}</text>'
        )
        parts.append(
            f'<rect x="{x_f:.1f}" y="{cy - 8:.1f}" width="{max(2.0, x_e - x_f):.1f}" height="16" '
            f'rx="2.5" class="{cls}"><title>{_esc(s["name"])} ({s["type"]}): '
            f'{s["founded"]}&ndash;{s["dissolved"] or "today"}, {s["span"]} years</title></rect>'
        )
        if s["active"]:
            parts.append(f'<path d="M{x_e:.1f},{cy - 6} l7,6 l-7,6 z" class="arrowopen"/>')
        else:
            parts.append(f'<line x1="{x_e:.1f}" y1="{cy - 9:.1f}" x2="{x_e:.1f}" y2="{cy + 9:.1f}" class="endcap"/>')
        parts.append(
            f'<text x="{x_e + 12:.1f}" y="{cy + 3.8:.1f}" class="dur">{s["span"]} yrs</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def build_waves(payload: dict) -> str:
    found = {r["dec"]: r["n"] for r in payload["waves"]["founded"]}
    closed = {r["dec"]: r["n"] for r in payload["waves"]["closed"]}
    lo, hi = payload["params"]["wave"]
    decs = list(range(lo, hi + 1, 10))
    pad_l, pad_r = 6, 6
    x0, x1 = pad_l, PANEL_W - pad_r
    bw = (x1 - x0) / len(decs) - 2.0
    fmax = max(found.values() or [1])
    cmax = max(closed.values() or [1])

    def bars(series, maxv, top, height, cls):
        out = []
        for i, d in enumerate(decs):
            v = series.get(d, 0)
            h = (v / maxv) * height if maxv else 0
            bx = x0 + i * (bw + 2.0)
            out.append(
                f'<rect x="{bx:.1f}" y="{top + height - h:.1f}" width="{bw:.1f}" '
                f'height="{max(0.6, h):.1f}" rx="1" class="{cls}"><title>{d}s: {v:,}</title></rect>'
            )
        return "".join(out)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {PANEL_W} {WAVE_H}" '
        f'width="{PANEL_W}" height="{WAVE_H}" role="img" '
        f'aria-label="Organizations founded and closed per decade, 1800 to 2020">'
    ]
    # top: founded (grey structure backdrop)
    parts.append(f'<text x="{x0}" y="12" class="minilabel">founded per decade</text>')
    parts.append(bars(found, fmax, 20, 66, "wave-found"))
    parts.append(f'<text x="{x1}" y="12" class="minival" text-anchor="end">{_n(fmax)} peak</text>')
    parts.append(f'<line x1="{x0}" y1="88" x2="{x1}" y2="88" class="axis"/>')
    # bottom: closed (orange data)
    parts.append(f'<text x="{x0}" y="110" class="minilabel">closed per decade</text>')
    parts.append(bars(closed, cmax, 118, 66, "wave-closed"))
    parts.append(f'<text x="{x1}" y="110" class="minival" text-anchor="end">{_n(cmax)} peak</text>')
    parts.append(f'<line x1="{x0}" y1="186" x2="{x1}" y2="186" class="axis"/>')
    for d in decs:
        if d % 50 == 0:
            i = decs.index(d)
            bx = x0 + i * (bw + 2.0) + bw / 2
            parts.append(f'<text x="{bx:.1f}" y="{WAVE_H - 4}" class="tick" text-anchor="middle">{d}</text>')
    parts.append("</svg>")
    return "".join(parts)


def build_types(payload: dict) -> str:
    rows = payload["type_lifespans"]
    if not rows:
        return ""
    h = TYPE_H
    label_w = 158
    x0 = label_w + 12
    x1 = PANEL_W - 78
    val_x = PANEL_W - 70
    top, bottom = 16, h - 12
    row_h = (bottom - top) / len(rows)
    vmax = max(r["median"] for r in rows)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {PANEL_W} {h}" '
        f'width="{PANEL_W}" height="{h}" role="img" '
        f'aria-label="Median years to closure by organization type">'
    ]
    for g in range(0, int(vmax) + 1, 20):
        x = x0 + (g / vmax) * (x1 - x0)
        parts.append(f'<line x1="{x:.1f}" y1="{top - 6}" x2="{x:.1f}" y2="{bottom}" class="grid"/>')
        parts.append(f'<text x="{x:.1f}" y="{top - 10}" class="tick" text-anchor="middle">{g}</text>')
    for i, r in enumerate(rows):
        cy = top + i * row_h + row_h / 2
        w = (r["median"] / vmax) * (x1 - x0)
        parts.append(
            f'<text x="{label_w}" y="{cy + 4.2:.1f}" class="rowlabel" text-anchor="end">'
            f'{_esc(_plural(_label(r["type"])))}</text>'
        )
        parts.append(
            f'<rect x="{x0}" y="{cy - 8:.1f}" width="{max(2.0, w):.1f}" height="16" rx="2.5" class="bar closed">'
            f'<title>{_esc(_label(r["type"]))}: median {r["median"]} years (p25&ndash;p75 {r["p25"]}&ndash;{r["p75"]}, n={_n(r["n"])})</title></rect>'
        )
        parts.append(f'<text x="{val_x}" y="{cy + 4:.1f}" class="typeval">{r["median"]} yrs</text>')
        parts.append(f'<text x="{PANEL_W - 2}" y="{cy + 4:.1f}" class="typen" text-anchor="end">{_n(r["n"])}</text>')
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Card HTML
# ---------------------------------------------------------------------------

HTML_HEAD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>HumansMap: the rise and fall of organizations</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;800&family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:#f9f8f4; --text:#1c1510; --accent:#1b4d8e; --person:#bf6b28;
    --open:#d79a5b; --closed:#a4521d;
    --muted:#8a8278; --surface:#ece8de; --line:rgba(28,21,16,.10);
    --display:'Playfair Display', Georgia, serif; --body:'DM Sans', system-ui, sans-serif;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:#d9d4c9; font-family:var(--body); padding:32px; }
  .card { width:1600px; height:1000px; background:var(--bg); color:var(--text);
    padding:36px 44px 22px; display:flex; flex-direction:column; position:relative; overflow:hidden; }
  .card header { display:flex; justify-content:space-between; align-items:baseline; }
  .eyebrow { font:700 15px/1 var(--body); letter-spacing:.2em; text-transform:uppercase; color:var(--accent); }
  .brand { font:700 27px/1 var(--display); color:var(--accent); }
  h1 { font:800 40px/1.08 var(--display); margin:13px 0 0; letter-spacing:-.012em; max-width:44ch; }
  h1 em { font-style:normal; color:var(--person); }
  .deck { font:400 15.5px/1.5 var(--body); color:var(--muted); margin:8px 0 0; max-width:1400px; }
  .deck b { color:var(--text); font-weight:700; }
  .body { display:flex; gap:30px; margin-top:8px; flex:1; min-height:0; }
  .plot { width:980px; flex:0 0 980px; display:flex; flex-direction:column; min-height:0; }
  .plot svg { display:block; width:980px; height:660px; overflow:visible; }
  .grid { stroke:rgba(28,21,16,.07); stroke-width:1; }
  .axis { stroke:rgba(28,21,16,.16); stroke-width:1; }
  .today { stroke:rgba(27,77,142,.45); stroke-width:1.4; stroke-dasharray:3 4; }
  .todaytag { fill:var(--accent); font-weight:700; }
  .tick { font:500 11px/1 var(--body); fill:var(--muted); }
  .rowlabel { font:600 14px/1 var(--body); fill:var(--text); }
  .dur { font:500 11.5px/1 var(--body); fill:var(--muted); }
  .bar.open { fill:var(--open); }
  .bar.closed { fill:var(--closed); }
  .arrowopen { fill:var(--open); }
  .endcap { stroke:#7a3c12; stroke-width:2.4; }
  .wave-found { fill:rgba(138,130,120,.34); }
  .wave-closed { fill:var(--closed); }
  .minilabel { font:700 10.5px/1 var(--body); fill:var(--muted); letter-spacing:.04em; text-transform:uppercase; }
  .minival { font:500 10.5px/1 var(--body); fill:var(--muted); }
  .typeval { font:700 11.5px/1 var(--body); fill:var(--text); }
  .typen { font:500 10.5px/1 var(--body); fill:var(--muted); }
  .legend { display:flex; gap:24px; align-items:center; margin-top:4px;
    font:400 12.5px/1 var(--body); color:var(--muted); }
  .legend span { display:flex; align-items:center; }
  .legend i { display:inline-block; margin-right:7px; }
  .panel { flex:1; min-width:0; display:flex; flex-direction:column; }
  .block { border-top:2px solid var(--text); padding-top:8px; }
  .block + .block { margin-top:14px; }
  .plabel { font:700 11px/1 var(--body); letter-spacing:.18em; text-transform:uppercase; color:var(--muted); }
  .waves svg, .types svg { display:block; overflow:visible; margin-top:3px; }
  .note { font:400 12px/1.45 var(--body); color:var(--muted); margin-top:5px; }
  .note b { color:var(--text); }
  .caveat { font:400 11.5px/1.45 var(--body); color:var(--muted); }
  .caveat b { color:var(--text); }
  .card footer { margin-top:auto; padding-top:9px; display:flex; justify-content:space-between;
    align-items:baseline; font:400 13px/1.4 var(--body); color:var(--muted); }
  .card footer b { color:var(--text); font-weight:700; }
</style></head>
<body>"""


def render_html(payload: dict) -> str:
    scale = payload["scale"]
    head = payload["headline"]
    gantt = build_gantt(payload)
    waves = build_waves(payload)
    types = build_types(payload)

    oa_age = head.get("oldest_active_age") or 0
    oa_year = head.get("oldest_active_year") or 0
    oa_name = head.get("oldest_active_name") or ""
    lc_span = head.get("longest_closed_span") or 0
    lc_from = head.get("longest_closed_founded") or 0
    lc_to = head.get("longest_closed_dissolved") or 0
    lc_name = head.get("longest_closed_name") or ""

    big = (
        f"The oldest organization still running was founded in <em>{_esc(oa_year)}</em>. "
        f"The longest to close lasted <em>{_n(lc_span)} years</em>."
    )
    closed_recent = [s for s in payload["spans"] if not s["active"]]
    n_active = sum(1 for s in payload["spans"] if s["active"])
    deck = (
        f"Lifespans of organizations &mdash; not offices or museums, which Wikidata also files "
        f"as organisations. Only <b>{_n(scale['with_both'])}</b> "
        f"of <b>{_n(scale['total'])}</b> record both a founding and a closure date, so the long view is "
        f"drawn from those; the timeline (left) shows <b>{len(payload['spans'])}</b> of the longest-lived: "
        f"<b>{n_active}</b> still operating, <b>{len(closed_recent)}</b> closed. "
        f"<b>{_esc(oa_name)}</b> has run {_n(oa_age)} years; <b>{_esc(lc_name)}</b> ran "
        f"{_n(lc_span)} before closing in {lc_to}. Most organizations last far less (panel, right)."
    )

    kinds = ", ".join(
        f"{_esc(_plural(_label(r['type'])))} {r['median']}" for r in payload["type_lifespans"][:3]
    )
    type_note = (
        f"Median years from founding to closure for organizations founded "
        f"<b>{payload['params']['cohort'][0]}&ndash;{payload['params']['cohort'][1]}</b>. "
        f"The shortest-lived are {kinds} years; the longest, "
        f"<b>{_esc(_plural(_label(head.get('longest_type') or '')))}</b> at "
        f"<b>{head.get('longest_type_median')} years</b>. Numbers are the count behind each bar."
    )

    wave_note = (
        "Organizations founded and closed per decade. Both curves rise because recent decades are "
        "far better documented, so read the <i>shape</i>, not the height."
    )

    caveat = (
        "<b>Read with care.</b> Founding and closure dates exist for a minority of organizations and "
        "are often approximate; Wikidata's coverage is uneven, over-representing Europe and the last "
        "century. The longest-lived names are hand-picked from the record, and the handful of "
        "artifacts it carries (notarial offices, printer partnerships, cemetery plots) were left out."
    )

    legend = (
        '<div class="legend">'
        f'<span><i style="width:26px;height:12px;border-radius:3px;background:#d79a5b;display:inline-block"></i>still operating</span>'
        f'<span><i style="width:26px;height:12px;border-radius:3px;background:#a4521d;display:inline-block"></i>closed</span>'
        '<span><i style="width:20px;height:12px;border-radius:3px;background:rgba(138,130,120,.34);display:inline-block"></i>founded per decade (context)</span>'
        '<span>one bar = one institution, from founding to closure or to today</span>'
        '</div>'
    )

    card = f"""<section class="card">
  <header>
    <span class="eyebrow">The rise and fall of organizations</span>
    <span class="brand">Humans&nbsp;Map</span>
  </header>
  <h1>{big}</h1>
  <p class="deck">{deck}</p>
  <div class="body">
    <div class="plot">
      {gantt}
      {legend}
    </div>
    <aside class="panel">
      <div class="block">
        <div class="plabel">Founded vs closed, by decade</div>
        <div class="waves">{waves}</div>
        <div class="note">{wave_note}</div>
      </div>
      <div class="block">
        <div class="plabel">Typical lifespan by type</div>
        <div class="types">{types}</div>
        <div class="note">{type_note}</div>
      </div>
      <div class="block">
        <div class="plabel">The fine print</div>
        <div class="caveat">{caveat}</div>
      </div>
    </aside>
  </div>
  <footer>
    <span>Data: Wikidata (CC BY-SA) &middot; humansmap.com</span>
    <span><b>{_n(scale['total'])}</b> organizations &middot; <b>{_n(scale['with_both'])}</b> with both dates &middot; <b>{_n(scale['with_founded'])}</b> founded</span>
  </footer>
</section>"""

    return HTML_HEAD + card + "\n</body></html>\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default="docs/marketing/orgs")
    parser.add_argument("--no-html", action="store_true")
    parser.add_argument("--from-json", default=None)
    args = parser.parse_args()

    if args.from_json:
        payload = json.loads(Path(args.from_json).read_text())
    else:
        payload = collect()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "orgs.json"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {json_path}")

    if not args.no_html:
        html_path = out_dir / "orgs.html"
        html_path.write_text(render_html(payload))
        print(f"wrote {html_path}")

    print(json.dumps(payload["headline"], indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
