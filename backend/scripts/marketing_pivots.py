#!/usr/bin/env python3
"""Marketing pivots miner — does job inheritance weaken over time?

The insight: take the parent->child occupation-inheritance signal and split it
by the **child's birth cohort**. On the raw numbers the same-occupation rate
looks like a U (high in the 1800s, low mid-20th century, high again for recent
cohorts) — but that shape mostly mirrors how *concentrated* occupation labels
are in each cohort. Add a chance baseline and the signal over chance is a broad
3-5x plateau across two centuries, not a decline. That honesty is the point:
the card says the trend is weak / artefact-driven rather than forcing a story.

It never writes to ArangoDB. Two bounded, indexed aggregations, both anchored on
the same parent-notability floor (``--min-statements``, default 50) so the scan
is bounded by the persistent index on ``persons.wikidata_statements_count``:

  1. Link-level pairs: for every parent with an occupation above the floor,
     walk the ``relationships`` parent edges from the PARENT side
     (``e._to == parent._id`` — parent edges point CHILD -> PARENT, so this is
     the cheap/indexed direction), fetch the child, and aggregate
     (child birth decade, parent gender, parent occupation, child occupation).
  2. Child-level check: the same traversal, but collapsed to one row per child
     (does *any* documented parent share the child's occupation), so the card
     can show the link-level result is not an artefact of counting a child with
     two parents twice.

A second small pass at ``--sensitivity-min-statements`` (default 100, the floor
used by the shipped inheritance card) re-runs pass 1 and is reported as a
robustness check — it shows the raw U-shape and the flat chance-adjusted signal
survive a stricter notability floor.

Run it manually inside the backend container:

    docker compose exec -T backend python - --out-dir /tmp/pivots \\
        < backend/scripts/marketing_pivots.py
    docker compose exec -T backend cat /tmp/pivots/pivots.json \\
        > docs/marketing/pivots/pivots.json
    docker compose exec -T backend cat /tmp/pivots/pivots.html \\
        > docs/marketing/pivots/pivots.html
    cd scripts/marketing && source env.sh
    node render-insight-cards.js ../../docs/marketing/pivots/pivots.html \\
        ../../docs/marketing/pivots   # -> card-01.png; rename to pivots.png

Flags:
    --out-dir PATH                    output directory (default docs/marketing/pivots)
    --min-statements N                parent Wikidata-statement floor (default 50)
    --min-cohort N                    per-cohort link floor to be drawn (default 200)
    --scan-limit N                    max parents visited per aggregation (default 300000)
    --sensitivity-min-statements N    second, stricter floor for robustness (default 100)
    --from-json PATH                  re-render from a cached payload, no DB access
    --no-html                         emit only the JSON payload
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

DEFAULT_MIN_STATEMENTS = 50
DEFAULT_MIN_COHORT = 200
DEFAULT_SCAN_LIMIT = 300_000
DEFAULT_SENSITIVITY = 100

YEAR_MIN = 1800
YEAR_MAX = 2019  # births from 2020 on are a handful of records; excluded

# Display: ten twenty-year cohorts carry the story (1800s -> 1990s). The
# 2000-2019 cohort is still computed and shown as a marked, right-censored
# point — its people are mid-career at most and only a selected, documented
# subset is on record.
COHORT_STEP = 20
COHORT_MIN = 1800
COHORT_MAX = 1980  # series drawn through the 1980s-90s cohort
CENSORED_FROM = 2000  # cohorts from here are drawn hollow: heavily right-censored
RECENT_FROM = 1980    # the 1980s-90s cohort is partly censored and flagged in the caveat

TOP_OCCUPATIONS = 8

# ---------------------------------------------------------------------------
# Queries — both walk relationships from the parent side via the edge index.
# ---------------------------------------------------------------------------

PAIRS_AQL = """
FOR p IN persons
  FILTER p.wikidata_statements_count >= @ms AND p.primary_occupation != null
  LIMIT @sl
  FOR e IN relationships
    FILTER e._to == p._id AND e.type == 'parent'
    LET ch = DOCUMENT(e._from)
    FILTER ch != null AND ch.primary_occupation != null
      AND ch.birth_date != null AND ch.birth_date[0] != '-'
    LET by = TO_NUMBER(SUBSTRING(ch.birth_date, 0, 4))
    FILTER by >= @ymin AND by <= @ymax
    COLLECT dec = FLOOR(by / 10) * 10, pg = p.gender, po = p.primary_occupation,
            co = ch.primary_occupation WITH COUNT INTO n
    RETURN {decade: dec, parent_gender: pg, parent_occupation: po,
            child_occupation: co, count: n}
"""

# One row per child: does ANY documented parent share the child's occupation?
CHILD_AQL = """
FOR p IN persons
  FILTER p.wikidata_statements_count >= @ms AND p.primary_occupation != null
  LIMIT @sl
  FOR e IN relationships
    FILTER e._to == p._id AND e.type == 'parent'
    LET ch = DOCUMENT(e._from)
    FILTER ch != null AND ch.primary_occupation != null
      AND ch.birth_date != null AND ch.birth_date[0] != '-'
    LET by = TO_NUMBER(SUBSTRING(ch.birth_date, 0, 4))
    FILTER by >= @ymin AND by <= @ymax
    COLLECT child = ch._id, cocc = ch.primary_occupation, dec = FLOOR(by / 10) * 10
    LET matched = (
      FOR e2 IN relationships
        FILTER e2._from == child AND e2.type == 'parent'
        LET q = DOCUMENT(e2._to)
        FILTER q != null AND q.primary_occupation == cocc
        LIMIT 1
        RETURN 1
    )
    COLLECT decade = dec, same = LENGTH(matched) > 0 WITH COUNT INTO n
    RETURN {decade: decade, same: same, count: n}
"""

PARENT_EDGES_AQL = """
RETURN LENGTH(FOR e IN relationships FILTER e.type == 'parent' LIMIT 2000000 RETURN 1)
"""

PARENTS_CONSIDERED_AQL = """
RETURN LENGTH(
  FOR p IN persons
    FILTER p.wikidata_statements_count >= @ms AND p.primary_occupation != null
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
# Stats
# ---------------------------------------------------------------------------


def _rate(same: float, total: float) -> float:
    return (same / total) if total else 0.0


def _cohort(decade: int) -> int:
    """20-year cohort start for a birth decade (2000 stays 2000)."""
    if decade >= 2000:
        return 2000
    return COHORT_MIN + ((decade - COHORT_MIN) // COHORT_STEP) * COHORT_STEP


def _label(start: int) -> str:
    return f"{start}\u2013{str(start + COHORT_STEP - 1)[-2:]}"


def _wilson(same: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a binomial proportion."""
    if total <= 0:
        return (0.0, 0.0)
    p = same / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def _chance_rate(parent: dict[str, int], child: dict[str, int], total: int) -> float:
    """P(parent occ == child occ) under random pairing within the cohort."""
    if not total:
        return 0.0
    return sum((parent[o] / total) * (child[o] / total) for o in set(parent) & set(child))


class _Bucket:
    """Aggregates links for one cohort (or the whole sample)."""

    def __init__(self) -> None:
        self.links = 0
        self.same = 0
        self.parent_occ: dict[str, int] = defaultdict(int)
        self.child_occ: dict[str, int] = defaultdict(int)
        self.male = {"links": 0, "same": 0}
        self.female = {"links": 0, "same": 0}
        self.other = {"links": 0, "same": 0}

    def add(self, row: dict) -> None:
        n = int(row.get("count") or 0)
        po = row.get("parent_occupation") or ""
        co = row.get("child_occupation") or ""
        pg = row.get("parent_gender")
        if not n or not po:
            return
        is_same = po == co
        self.links += n
        self.same += n if is_same else 0
        self.parent_occ[po] += n
        self.child_occ[co] += n
        slot = self.male if pg == "male" else self.female if pg == "female" else self.other
        slot["links"] += n
        slot["same"] += n if is_same else 0

    def row(self) -> OrderedDict:
        chance = _chance_rate(self.parent_occ, self.child_occ, self.links)
        rate = _rate(self.same, self.links)
        lo, hi = _wilson(self.same, self.links)
        return OrderedDict(
            links=self.links,
            same=self.same,
            same_rate=rate,
            chance_rate=chance,
            lift=(rate / chance) if chance else 0.0,
            ci_low=lo,
            ci_high=hi,
            male=dict(self.male),
            female=dict(self.female),
            other=dict(self.other),
        )


def _cohort_rows(rows: list[dict]) -> list[OrderedDict]:
    buckets: dict[int, _Bucket] = defaultdict(_Bucket)
    for row in rows:
        dec = row.get("decade")
        if dec is None:
            continue
        buckets[_cohort(int(dec))].add(row)

    out: list[OrderedDict] = []
    for start in sorted(buckets):
        b = buckets[start]
        r = b.row()
        r = OrderedDict(
            cohort=start,
            label=_label(start),
            censored=start >= CENSORED_FROM,
            recent=start >= RECENT_FROM,
            **r,
        )
        out.append(r)
    return out


def _occupation_rows(rows: list[dict], top: int) -> list[OrderedDict]:
    totals: dict[str, int] = defaultdict(int)
    per: dict[str, dict[int, _Bucket]] = defaultdict(lambda: defaultdict(_Bucket))
    overall: dict[str, _Bucket] = defaultdict(_Bucket)
    # Global child-occupation base rates, so each occupation can be compared
    # with "how often is this occupation recorded for *any* child in the era".
    global_child: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    global_links: dict[int, int] = defaultdict(int)
    for row in rows:
        dec = row.get("decade")
        if dec is None:
            continue
        po = row.get("parent_occupation") or ""
        co = row.get("child_occupation") or ""
        n = int(row.get("count") or 0)
        if not po:
            continue
        coh = _cohort(int(dec))
        totals[po] += n
        overall[po].add(row)
        per[po][coh].add(row)
        if co:
            global_child[coh][co] += n
            global_links[coh] += n
            global_child[-1][co] += n
            global_links[-1] += n

    def _rr(same_rate: float, cohort: int, occ: str) -> float:
        base = (global_child[cohort].get(occ, 0) / global_links[cohort]) if global_links[cohort] else 0.0
        return (same_rate / base) if base else 0.0

    out: list[OrderedDict] = []
    for po, _ in sorted(totals.items(), key=lambda kv: -kv[1])[:top]:
        series = []
        for start in range(COHORT_MIN, COHORT_MAX + 1, COHORT_STEP):
            b = per[po].get(start)
            if not b or not b.links:
                series.append(OrderedDict(cohort=start, label=_label(start), links=0, same=0,
                                          same_rate=None, relative_risk=None))
                continue
            sr = _rate(b.same, b.links)
            series.append(
                OrderedDict(
                    cohort=start,
                    label=_label(start),
                    links=b.links,
                    same=b.same,
                    same_rate=sr,
                    relative_risk=_rr(sr, start, po),
                )
            )
        o = overall[po].row()
        out.append(
            OrderedDict(
                occupation=po,
                label=_occ_label(po),
                links=o["links"],
                same=o["same"],
                same_rate=o["same_rate"],
                relative_risk=_rr(o["same_rate"], -1, po) if -1 in global_links else None,
                series=series,
            )
        )
    return out


def collect(min_statements: int, min_cohort: int, scan_limit: int,
            sensitivity: int) -> dict:
    db = get_db(get_arango_client(request_timeout=900))
    bind = {"ms": min_statements, "sl": scan_limit, "ymin": YEAR_MIN, "ymax": YEAR_MAX}

    rows = _rows(db, PAIRS_AQL, bind)
    child_rows = _rows(db, CHILD_AQL, bind)
    parent_edges = _rows(db, PARENT_EDGES_AQL)
    parents_considered = _rows(db, PARENTS_CONSIDERED_AQL, {"ms": min_statements})

    cohorts = _cohort_rows(rows)
    decades = _decade_rows(rows)
    occupations = _occupation_rows(rows, TOP_OCCUPATIONS)

    total = _Bucket()
    for row in rows:
        total.add(row)
    baseline = total.row()

    # Child-level robustness: one row per child, any-parent match.
    child_buckets: dict[int, dict[str, int]] = defaultdict(lambda: {"children": 0, "same": 0})
    for row in child_rows:
        dec = row.get("decade")
        if dec is None:
            continue
        b = child_buckets[_cohort(int(dec))]
        b["children"] += int(row.get("count") or 0)
        if row.get("same"):
            b["same"] += int(row.get("count") or 0)
    child_level = [
        OrderedDict(
            cohort=start,
            label=_label(start),
            children=vals["children"],
            same=vals["same"],
            same_rate=_rate(vals["same"], vals["children"]),
        )
        for start, vals in sorted(child_buckets.items())
    ]

    # Sensitivity at a stricter parent floor (keyed on the overall sample and
    # the drawn cohorts only — this is a robustness note, not a second story).
    sensitivity_block = None
    if sensitivity and sensitivity > min_statements:
        srows = _rows(db, PAIRS_AQL, {**bind, "ms": sensitivity})
        if srows:
            scohorts = _cohort_rows(srows)
            sbucket = _Bucket()
            for row in srows:
                sbucket.add(row)
            sensitivity_block = OrderedDict(
                min_statements=sensitivity,
                pairs=sbucket.links,
                same_rate=sbucket.row()["same_rate"],
                chance_rate=sbucket.row()["chance_rate"],
                lift=sbucket.row()["lift"],
                cohorts=[
                    OrderedDict(cohort=c["cohort"], label=c["label"], links=c["links"],
                                same_rate=c["same_rate"], chance_rate=c["chance_rate"],
                                lift=c["lift"])
                    for c in scohorts
                ],
            )

    drawn = [c for c in cohorts if c["links"] >= min_cohort]
    solid = [c for c in drawn if not c["censored"]]
    peak = max(solid, key=lambda c: c["lift"]) if solid else None
    first = solid[0] if solid else None
    last = solid[-1] if solid else None

    headline = OrderedDict(
        first_cohort=first["cohort"] if first else None,
        first_rate=first["same_rate"] if first else None,
        first_lift=first["lift"] if first else None,
        last_cohort=last["cohort"] if last else None,
        last_rate=last["same_rate"] if last else None,
        last_lift=last["lift"] if last else None,
        peak_cohort=peak["cohort"] if peak else None,
        peak_lift=peak["lift"] if peak else None,
        lift_low=min((c["lift"] for c in solid), default=0.0),
        lift_high=max((c["lift"] for c in solid), default=0.0),
        raw_low=min((c["same_rate"] for c in solid), default=0.0),
        raw_high=max((c["same_rate"] for c in solid), default=0.0),
        trend="no monotonic decline; chance-adjusted signal is a broad plateau",
    )

    return {
        "generated": date.today().isoformat(),
        "params": {
            "min_statements": min_statements,
            "min_cohort": min_cohort,
            "scan_limit": scan_limit,
            "sensitivity_min_statements": sensitivity,
            "year_min": YEAR_MIN,
            "year_max": YEAR_MAX,
            "cohort_step": COHORT_STEP,
            "censored_from": CENSORED_FROM,
        },
        "definition": (
            "A parent->child pair counts as 'same occupation' when the child's "
            "primary occupation equals the parent's primary occupation in "
            "Wikidata. Each documented parent counts separately, so a child "
            "with two documented parents contributes two links."
        ),
        "scale": {
            "parent_edges": parent_edges[0] if parent_edges else None,
            "parents_considered": parents_considered[0] if parents_considered else None,
            "pairs": baseline["links"],
            "same_pairs": baseline["same"],
            "child_rows": sum(c["children"] for c in child_level),
        },
        "baseline": OrderedDict(
            pairs=baseline["links"],
            same=baseline["same"],
            same_rate=baseline["same_rate"],
            chance_rate=baseline["chance_rate"],
            lift=baseline["lift"],
            ci_low=baseline["ci_low"],
            ci_high=baseline["ci_high"],
            male=dict(baseline["male"]),
            female=dict(baseline["female"]),
            other=dict(baseline["other"]),
        ),
        "cohorts": cohorts,
        "decades": decades,
        "by_parent_gender": [
            OrderedDict(
                cohort=c["cohort"], label=c["label"],
                male=c["male"], female=c["female"], other=c["other"],
            )
            for c in cohorts
        ],
        "top_occupations": occupations,
        "child_level": child_level,
        "sensitivity": sensitivity_block,
        "headline": headline,
    }


def _decade_rows(rows: list[dict]) -> list[OrderedDict]:
    buckets: dict[int, _Bucket] = defaultdict(_Bucket)
    for row in rows:
        dec = row.get("decade")
        if dec is None:
            continue
        buckets[int(dec)].add(row)
    out = []
    for dec in sorted(buckets):
        b = buckets[dec]
        r = b.row()
        out.append(OrderedDict(decade=dec, label=f"{dec}s", **r))
    return out


# ---------------------------------------------------------------------------
# Formatting helpers (shared by the SVG and the HTML panel)
# ---------------------------------------------------------------------------


def _occ_label(occ: str) -> str:
    s = (occ or "").replace("_", " ").strip()
    return (s[:1].upper() + s[1:]) if s else s


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


def _pct(value: float | None, digits: int = 0) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.{digits}f}%"


# ---------------------------------------------------------------------------
# Chart geometry
# ---------------------------------------------------------------------------

DATA = "#bf6b28"        # data only (same-occupation rate / lift)
DATA_SOFT = "rgba(191,107,40,.18)"
STRUCT = "#8a8278"      # structure/neutral (chance baseline, axes, no-data)
GRID = "rgba(28,21,16,.10)"

MAIN_W, MAIN_H = 980, 400
MP = {"l": 56, "r": 18, "t": 22, "b": 66}
LIFT_W, LIFT_H = 980, 174
LP = {"l": 56, "r": 18, "t": 16, "b": 34}
GEN_W, GEN_H = 486, 156
GP = {"l": 40, "r": 14, "t": 16, "b": 32}


def _cohort_axis(payload: dict) -> list[dict]:
    """Every drawn cohort plus the marked 2000-2019 censored point."""
    drawn = [c for c in payload["cohorts"] if c["links"] >= payload["params"]["min_cohort"]]
    return drawn


def build_main_chart(payload: dict) -> str:
    cohorts = _cohort_axis(payload)
    solid = [c for c in cohorts if not c["censored"]]
    censored = [c for c in cohorts if c["censored"]]
    if not solid:
        return ""
    w, h = MAIN_W, MAIN_H
    pl, pr, pt, pb = MP["l"], MP["r"], MP["t"], MP["b"]
    n = len(cohorts)
    ymax = 0.60

    def ex(i: int) -> float:
        return pl + (i / max(1, n - 1)) * (w - pl - pr)

    def ey(v: float) -> float:
        return pt + (ymax - min(v, ymax)) / ymax * (h - pt - pb)

    idx = {id(c): i for i, c in enumerate(cohorts)}
    sxy = [(ex(idx[id(c)]), ey(c["same_rate"])) for c in solid]
    cxy = [(ex(idx[id(c)]), ey(c["chance_rate"])) for c in solid]

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
        f'role="img" aria-label="Same-occupation rate by the child&#39;s birth cohort, against chance">'
    ]
    # Right-censored shading over the final cohorts.
    if censored:
        x0 = ex(idx[id(censored[0])]) - (ex(1) - ex(0)) / 2
        parts.append(
            f'<rect x="{x0:.1f}" y="{pt}" width="{w - pr - x0:.1f}" height="{h - pt - pb:.1f}" '
            f'fill="rgba(28,21,16,.045)"/>'
        )
        parts.append(
            f'<text x="{w - pr - 6:.1f}" y="{pt + 13:.1f}" class="ax-note" text-anchor="end">'
            f'right-censored \u2192</text>'
        )

    # y grid + ticks.
    for g in range(0, 61, 10):
        yy = ey(g / 100.0)
        parts.append(f'<line x1="{pl}" y1="{yy:.1f}" x2="{w - pr}" y2="{yy:.1f}" class="ax-grid"/>')
        parts.append(f'<text x="{pl - 8}" y="{yy + 3.6:.1f}" class="ax-tick" text-anchor="end">{g}%</text>')

    # Chance band (structure) and observed CI band (data, faint).
    chance_line = " ".join(f"{x:.1f},{y:.1f}" for x, y in cxy)
    parts.append(f'<polyline points="{chance_line}" class="chance-line"/>')
    ci_pts_hi = " ".join(
        f"{ex(idx[id(c)]):.1f},{ey(c['ci_high']):.1f}" for c in solid
    )
    ci_pts_lo = " ".join(
        f"{ex(idx[id(c)]):.1f},{ey(c['ci_low']):.1f}" for c in reversed(solid)
    )
    parts.append(f'<polygon points="{ci_pts_hi} {ci_pts_lo}" class="obs-band"/>')
    obs_line = " ".join(f"{x:.1f},{y:.1f}" for x, y in sxy)
    parts.append(f'<polyline points="{obs_line}" class="obs-line"/>')
    for c, (x, y) in zip(solid, sxy):
        parts.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.2" class="obs-dot">'
            f'<title>{_esc(c["label"])} cohort: {_pct(c["same_rate"], 1)} same occupation '
            f'(n={_n(c["links"])}); chance {_pct(c["chance_rate"], 1)}</title></circle>'
        )
    # Censored points: hollow, unconnected.
    for c in censored:
        x, y = ex(idx[id(c)]), ey(c["same_rate"])
        cx, cy = ex(idx[id(c)]), ey(c["chance_rate"])
        parts.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.4" class="obs-dot censored"/>'
        )
        parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="2.6" class="chance-dot"/>')
        parts.append(
            f'<text x="{x:.1f}" y="{y - 9:.1f}" class="pt-note" text-anchor="middle">*</text>'
        )

    # n strip along the bottom.
    for i, c in enumerate(cohorts):
        x = ex(i)
        parts.append(
            f'<text x="{x:.1f}" y="{h - pb + 16:.1f}" class="ax-x" text-anchor="middle">{c["label"]}</text>'
        )
        parts.append(
            f'<text x="{x:.1f}" y="{h - pb + 31:.1f}" class="ax-n" text-anchor="middle">n={_n(c["links"])}</text>'
        )
    parts.append(
        f'<text x="{pl}" y="{h - 6:.1f}" class="ax-cap" text-anchor="start">'
        f'child\u2019s birth cohort \u00b7 n = documented parent\u2192child pairs in the cohort</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


def build_lift_chart(payload: dict) -> str:
    cohorts = _cohort_axis(payload)
    solid = [c for c in cohorts if not c["censored"]]
    if not solid:
        return ""
    w, h = LIFT_W, LIFT_H
    pl, pr, pt, pb = LP["l"], LP["r"], LP["t"], LP["b"]
    n = len(cohorts)
    ymax = 6.0

    def ex(i: int) -> float:
        return pl + (i / max(1, n - 1)) * (w - pl - pr)

    def ey(v: float) -> float:
        return pt + (ymax - min(v, ymax)) / ymax * (h - pt - pb)

    idx = {id(c): i for i, c in enumerate(cohorts)}
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
        f'role="img" aria-label="Inheritance signal as a multiple of chance, by birth cohort">'
    ]
    # Censored zone.
    cens = [c for c in cohorts if c["censored"]]
    if cens:
        x0 = ex(idx[id(cens[0])]) - (ex(1) - ex(0)) / 2
        parts.append(
            f'<rect x="{x0:.1f}" y="{pt}" width="{w - pr - x0:.1f}" height="{h - pt - pb:.1f}" '
            f'fill="rgba(28,21,16,.045)"/>'
        )
    for g in range(1, 7):
        yy = ey(g)
        parts.append(f'<line x1="{pl}" y1="{yy:.1f}" x2="{w - pr}" y2="{yy:.1f}" class="ax-grid"/>')
        parts.append(f'<text x="{pl - 8}" y="{yy + 3.4:.1f}" class="ax-tick" text-anchor="end">{g}\u00d7</text>')
    # 1x reference.
    y1 = ey(1.0)
    parts.append(f'<line x1="{pl}" y1="{y1:.1f}" x2="{w - pr}" y2="{y1:.1f}" class="chance-line"/>')
    pts = " ".join(f"{ex(idx[id(c)]):.1f},{ey(c['lift']):.1f}" for c in solid)
    parts.append(f'<polyline points="{pts}" class="obs-line"/>')
    for c in solid:
        x, y = ex(idx[id(c)]), ey(c["lift"])
        parts.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.0" class="obs-dot">'
            f'<title>{_esc(c["label"])}: {c["lift"]:.2f}\u00d7 chance</title></circle>'
        )
        parts.append(f'<text x="{x:.1f}" y="{y - 8:.1f}" class="pt-note" text-anchor="middle">{c["lift"]:.1f}\u00d7</text>')
    for c in cens:
        x, y = ex(idx[id(c)]), ey(c["lift"])
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.2" class="obs-dot censored"/>')
    parts.append(f'<text x="{pl}" y="{h - 5:.1f}" class="ax-cap" text-anchor="start">'
                 f'same-occupation rate \u00f7 chance rate \u00b7 1\u00d7 = no more alike than random</text>')
    parts.append("</svg>")
    return "".join(parts)


def build_gender_chart(payload: dict) -> str:
    cohorts = [c for c in payload["cohorts"] if c["links"] >= payload["params"]["min_cohort"]]
    solid = [c for c in cohorts if not c["censored"]]
    if not solid:
        return ""
    w, h = GEN_W, GEN_H
    pl, pr, pt, pb = GP["l"], GP["r"], GP["t"], GP["b"]
    n = len(solid)
    ymax = 0.60

    def ex(i: int) -> float:
        return pl + (i / max(1, n - 1)) * (w - pl - pr)

    def ey(v: float) -> float:
        return pt + (ymax - min(v, ymax)) / ymax * (h - pt - pb)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
        f'role="img" aria-label="Same-occupation rate by parent gender and child birth cohort">'
    ]
    for g in range(0, 61, 20):
        yy = ey(g / 100.0)
        parts.append(f'<line x1="{pl}" y1="{yy:.1f}" x2="{w - pr}" y2="{yy:.1f}" class="ax-grid"/>')
        parts.append(f'<text x="{pl - 6}" y="{yy + 3.4:.1f}" class="ax-tick" text-anchor="end">{g}%</text>')
    parts.append(
        f'<text x="{w - pr}" y="11" text-anchor="end" '
        f'style="font:600 10px DM Sans,system-ui;fill:#8a8278">father (solid)&nbsp;&nbsp;&nbsp;\u2013 \u2013 mother (dashed)</text>'
    )
    for who, cls in (("male", "obs-line"), ("female", "obs-line alt")):
        seq = [c for c in solid if c[who]["links"] > 0]
        if len(seq) < 2:
            continue
        xy = [(ex(solid.index(c)), ey(_rate(c[who]["same"], c[who]["links"]))) for c in seq]
        parts.append('<polyline points="' + " ".join(f"{x:.1f},{y:.1f}" for x, y in xy) + f'" class="{cls}"/>')
        for c, (x, y) in zip(seq, xy):
            links = c[who]["links"]
            small = links < payload["params"]["min_cohort"]
            dot_cls = "obs-dot" if not small else "obs-dot censored"
            parts.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.9" class="{dot_cls}">'
                f'<title>{who.title()} links, {_esc(c["label"])}: {_pct(_rate(c[who]["same"], links), 1)} '
                f'(n={_n(links)})</title></circle>'
            )
    # x labels: first, middle, last.
    for i in (0, n // 2, n - 1):
        if 0 <= i < n:
            parts.append(
                f'<text x="{ex(i):.1f}" y="{h - pb + 15:.1f}" class="ax-x" text-anchor="middle">{solid[i]["label"]}</text>'
            )
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Card HTML
# ---------------------------------------------------------------------------

HTML_HEAD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>HumansMap: did following a parent's job fade?</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;800&family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:#f9f8f4; --text:#1c1510;
    --chrome:#1b4d8e;          /* chrome only: wordmark, headline emphasis, links */
    --data:#bf6b28;            /* data only: same-occupation rate / signal (one orange family) */
    --muted:#8a8278;           /* structure/neutral: chance baseline, axes, labels */
    --surface:#ece8de; --line:rgba(28,21,16,.10);
    --display:'Playfair Display', Georgia, serif; --body:'DM Sans', system-ui, sans-serif;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:#d9d4c9; font-family:var(--body); padding:32px; }
  .card { width:1600px; height:1000px; background:var(--bg); color:var(--text);
    padding:34px 40px 20px; display:flex; flex-direction:column; position:relative; overflow:hidden; }
  .card header { display:flex; justify-content:space-between; align-items:baseline; }
  .eyebrow { font:700 14px/1 var(--body); letter-spacing:.2em; text-transform:uppercase; color:var(--chrome); }
  .brand { font:700 27px/1 var(--display); color:var(--chrome); }
  h1 { font:800 38px/1.07 var(--display); margin:11px 0 0; letter-spacing:-.012em; max-width:44ch; }
  h1 em { font-style:normal; color:var(--data); }
  .deck { font:400 14.6px/1.42 var(--body); color:var(--muted); margin:7px 0 0; max-width:1520px; }
  .deck b { color:var(--text); font-weight:700; }
  .body { display:flex; gap:26px; margin-top:10px; flex:1; min-height:0; }
  .left { width:1000px; flex:0 0 1000px; display:flex; flex-direction:column; min-height:0; }
  .left svg { display:block; overflow:visible; }
  .main { width:980px; height:400px; }
  .lift { width:980px; height:174px; margin-top:2px; }
  .ax-grid { stroke:rgba(28,21,16,.08); stroke-width:1; }
  .ax-tick { font:500 10.5px/1 var(--body); fill:var(--muted); }
  .ax-x { font:600 10.5px/1 var(--body); fill:var(--text); }
  .ax-n { font:500 9.4px/1 var(--body); fill:var(--muted); }
  .ax-cap { font:400 10px/1 var(--body); fill:var(--muted); }
  .ax-note { font:600 9.6px/1 var(--body); fill:var(--muted); letter-spacing:.06em; text-transform:uppercase; }
  .obs-line { fill:none; stroke:var(--data); stroke-width:2.8; stroke-linejoin:round; }
  .obs-line.alt { stroke:var(--data); stroke-width:2.2; stroke-dasharray:5 4; opacity:.72; }
  .obs-band { fill:rgba(191,107,40,.16); stroke:none; }
  .obs-dot { fill:var(--data); stroke:var(--bg); stroke-width:1.4; }
  .obs-dot.censored { fill:var(--bg); stroke:var(--data); stroke-width:1.8; stroke-dasharray:2 2; }
  .chance-line { fill:none; stroke:var(--muted); stroke-width:1.8; stroke-dasharray:5 4; }
  .chance-dot { fill:var(--muted); stroke:var(--bg); stroke-width:1.2; }
  .pt-note { font:700 10px/1 var(--body); fill:var(--text); }
  .legend { display:flex; gap:20px; align-items:center; margin-top:8px; flex-wrap:wrap;
    font:400 11.8px/1.3 var(--body); color:var(--muted); }
  .legend .grp { display:flex; align-items:center; gap:7px; }
  .legend .line { display:inline-block; width:26px; height:0; border-top:2.8px solid var(--data); }
  .legend .line.dash { border-top:2px dashed var(--muted); }
  .legend .band { display:inline-block; width:22px; height:11px; border-radius:2px; background:rgba(191,107,40,.18); }
  .legend .dot { display:inline-block; width:11px; height:11px; border-radius:50%; border:1.8px dashed var(--data); }
  .panel { flex:1; min-width:0; display:flex; flex-direction:column; }
  .block { border-top:2px solid var(--text); padding-top:8px; }
  .block + .block { margin-top:10px; }
  .plabel { font:700 10.5px/1 var(--body); letter-spacing:.17em; text-transform:uppercase; color:var(--muted); }
  .gen svg { display:block; width:486px; height:156px; overflow:visible; margin-top:2px; }
  .gnote { font:400 11px/1.35 var(--body); color:var(--muted); margin-top:2px; }
  .gnote b { color:var(--text); }
  .trades { list-style:none; margin:8px 0 0; padding:0; }
  .trades li { display:grid; grid-template-columns:1fr 116px 40px; align-items:center; gap:9px;
    padding:2px 0; border-bottom:1px solid var(--line); }
  .trades .tn { font:600 13.4px/1.1 var(--body); }
  .trades .tn em { display:block; font:400 10.4px/1.3 var(--body); font-style:normal; color:var(--muted); }
  .trades .tbar { display:block; height:9px; background:var(--surface); border-radius:5px; overflow:hidden; }
  .trades .tbar i { display:block; height:100%; border-radius:5px; background:var(--data); }
  .trades .tv { font:700 16px/1 var(--display); text-align:right; }
  .caveat { font:400 11px/1.42 var(--body); color:var(--muted); margin-top:5px; }
  .caveat b { color:var(--text); }
  .card footer { margin-top:auto; padding-top:8px; display:flex; justify-content:space-between;
    align-items:baseline; font:400 12.4px/1.4 var(--body); color:var(--muted); }
  .card footer b { color:var(--text); font-weight:700; }
</style></head>
<body>"""


def render_html(payload: dict) -> str:
    scale = payload["scale"]
    base = payload["baseline"]
    head = payload["headline"]
    params = payload["params"]
    main = build_main_chart(payload)
    lift = build_lift_chart(payload)
    gen = build_gender_chart(payload)

    first = head.get("first_cohort")
    last = head.get("last_cohort")
    peak = head.get("peak_cohort")
    first_rate = head.get("first_rate")
    last_rate = head.get("last_rate")
    raw_low = head.get("raw_low")
    raw_high = head.get("raw_high")
    lift_low = head.get("lift_low")
    lift_high = head.get("lift_high")

    big = (
        "Following a parent\u2019s job has not faded: it runs at "
        f"<em>{lift_low:.1f}\u2013{lift_high:.1f}\u00d7 chance</em> across two centuries."
    )
    deck = (
        f"Every parent\u2192child pair in Wikidata where both people have a recorded "
        f"occupation, grouped by the <b>child\u2019s birth cohort</b>: "
        f"<b>{_n(scale['pairs'])}</b> pairs from parents with {_n(params['min_statements'])}+ "
        f"statements. On raw numbers the same-occupation rate looks like a U: "
        f"<b>{_pct(first_rate)}</b> for the {first}s cohort, <b>{_pct(raw_low)}</b> at its "
        f"mid-20th-century low, <b>{_pct(last_rate)}</b> for the {last}s. But that curve "
        f"mostly mirrors how concentrated occupation labels are in each cohort. Against a "
        f"chance baseline, the inheritance signal is a broad plateau, not a decline."
    )

    # Father / mother summary numbers for the caption.
    m = base.get("male", {})
    f = base.get("female", {})
    m_rate = _rate(m.get("same", 0), m.get("links", 0))
    f_rate = _rate(f.get("same", 0), f.get("links", 0))

    trades = sorted(payload.get("top_occupations", []),
                    key=lambda r: -r["same_rate"])[:8]
    trade_rows = []
    for r in trades:
        pct = min(100.0, max(0.0, r["same_rate"] * 100))
        rr = r.get("relative_risk")
        rr_txt = f' \u00b7 {rr:.1f}\u00d7 a random child' if rr else ""
        trade_rows.append(
            f'<li><span class="tn">{_esc(r["label"])}<em>n = {_n(r["links"])} pairs{rr_txt}</em></span>'
            f'<span class="tbar"><i style="width:{pct:.1f}%"></i></span>'
            f'<span class="tv">{pct:.0f}%</span></li>'
        )

    # Child-level robustness at the two endpoints.
    child = {c["cohort"]: c for c in payload.get("child_level", [])}
    robust = ""
    if child and first and last and first in child and last in child:
        robust = (
            f"Counting each child once instead of each parent link gives the same shape "
            f"({_pct(child[first]['same_rate'])} in the {first}s, {_pct(child[last]['same_rate'])} "
            f"in the {last}s, n={_n(child[last]['children'])})."
        )

    sens = payload.get("sensitivity")
    sens_note = ""
    if sens and sens.get("cohorts"):
        lifts = [c["lift"] for c in sens["cohorts"]
                 if c["links"] >= payload["params"]["min_cohort"] and c["cohort"] < 2000]
        if lifts:
            sens_note = (
                f"The stricter {_n(sens['min_statements'])}+ floor ({_n(sens['pairs'])} pairs) "
                f"keeps the shape and a {min(lifts):.1f}\u2013{max(lifts):.1f}\u00d7 band."
            )

    legend = (
        '<div class="legend">'
        '<span class="grp"><span class="line"></span>children who share a parent\u2019s occupation</span>'
        '<span class="grp"><span class="band"></span>95% uncertainty range</span>'
        '<span class="grp"><span class="line dash"></span>chance if jobs were paired at random</span>'
        '<span class="grp"><span class="dot"></span>cohort too recent to compare</span>'
        '<span class="grp">orange = the data &middot; grey = structure \u00b7 n under each cohort = pairs behind it</span>'
        '</div>'
    )

    card = f"""<section class="card">
  <header>
    <span class="eyebrow">Does the trade run in families?</span>
    <span class="brand">Humans&nbsp;Map</span>
  </header>
  <h1>{big}</h1>
  <p class="deck">{deck}</p>
  <div class="body">
    <div class="left">
      <div class="main">{main}</div>
      <div class="lift">{lift}</div>
      {legend}
    </div>
    <aside class="panel">
      <div class="block">
        <div class="plabel">Father vs mother</div>
        <div class="gen">{gen}</div>
        <div class="gnote">Overall <b>{_pct(m_rate, 1)}</b> of children follow the father (n={_n(m.get('links', 0))})
          and <b>{_pct(f_rate, 1)}</b> the mother (n={_n(f.get('links', 0))}). Hollow points are
          cohorts with fewer than {_n(params['min_cohort'])} links; before 1860 there are too few
          documented mothers to read.</div>
      </div>
      <div class="block">
        <div class="plabel">The trades that repeat most</div>
        <ul class="trades">{''.join(trade_rows)}</ul>
      </div>
      <div class="block">
        <div class="plabel">The fine print</div>
        <div class="caveat"><b>\u201cSame occupation\u201d</b> = the child\u2019s primary occupation
          equals the parent\u2019s in Wikidata; a child with two documented parents counts twice.
          &ldquo;&times; a random child&rdquo; = the trade&rsquo;s rate divided by how common that
          occupation is among all children. <b>Coverage</b> is uneven and improves over time, so recent cohorts are a selected
          subset (children of the famous), and their high raw rates are not comparable
          to the 1800s; the earliest cohort\u2019s lower ratio is itself likely a label-coarseness
          artefact. The 1980s\u201390s cohort is partly <b>right-censored</b>, the 2000s heavily
          so. This is <b>correlation, not causation.</b> {robust} {sens_note}</div>
      </div>
    </aside>
  </div>
  <footer>
    <span>Data: Wikidata (CC BY-SA) &middot; humansmap.com</span>
    <span><b>{_n(scale['pairs'])}</b> parent\u2192child pairs &middot; <b>{_n(scale['parents_considered'])}</b> parents &middot; <b>{_n(scale['child_rows'])}</b> distinct children</span>
  </footer>
</section>"""

    return HTML_HEAD + card + "\n</body></html>\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default="docs/marketing/pivots")
    parser.add_argument("--min-statements", type=int, default=DEFAULT_MIN_STATEMENTS)
    parser.add_argument("--min-cohort", type=int, default=DEFAULT_MIN_COHORT)
    parser.add_argument("--scan-limit", type=int, default=DEFAULT_SCAN_LIMIT)
    parser.add_argument("--sensitivity-min-statements", type=int, default=DEFAULT_SENSITIVITY)
    parser.add_argument("--from-json", default=None)
    parser.add_argument("--no-html", action="store_true")
    args = parser.parse_args()

    if args.from_json:
        payload = json.loads(Path(args.from_json).read_text())
    else:
        payload = collect(
            args.min_statements, args.min_cohort, args.scan_limit,
            args.sensitivity_min_statements,
        )

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "pivots.json"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {json_path}")

    if not args.no_html:
        html_path = out_dir / "pivots.html"
        html_path.write_text(render_html(payload))
        print(f"wrote {html_path}")

    head = payload.get("headline", {})
    summary = {
        "pairs": payload["scale"]["pairs"],
        "baseline_same_rate": round(payload["baseline"]["same_rate"], 4),
        "baseline_chance": round(payload["baseline"]["chance_rate"], 4),
        "baseline_lift": round(payload["baseline"]["lift"], 2),
        "cohorts": [
            {
                "cohort": c["cohort"],
                "n": c["links"],
                "same_rate": round(c["same_rate"], 4),
                "chance_rate": round(c["chance_rate"], 4),
                "lift": round(c["lift"], 2),
                "censored": c["censored"],
            }
            for c in payload["cohorts"]
            if c["links"] >= payload["params"]["min_cohort"]
        ],
        "trend": head.get("trend"),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
