#!/usr/bin/env python3
"""Marketing gender miner — read-only, emits the "when did each field open up?" card.

The insight: the share of women in each profession, traced across the birth
decades of the people who held it — and, now, the **rate** at which that share
kept moving. Women entered journalism, teaching and law long before they entered
physics, engineering or the military; the heatmap is the "when", the pooled
curve and the pace panel are the "how fast".

It never writes to ArangoDB. There is exactly **one bounded aggregation** over
the live graph:

  * A single pass over ``persons`` where ``wikidata_statements_count`` is at or
    above ``--min-statements`` (default 30 — the persistent index on that field
    bounds the scan). ``birth_date`` and ``gender`` are not indexed, so the
    statement floor is what keeps this cheap on a live DB; ``--scan-limit``
    (default 900000) is a hard ceiling that at the default floor does not bite.
  * ``occupations[]`` is UNNESTed so a person counts toward **every** field
    Wikidata lists for them, not just ``primary_occupation``. That is what gets
    each cell above the owner's sub-1k sample worry while still allowing a
    per-cell N floor.
  * Rows are bucketed by ``FLOOR(birth_year/10)*10``. Only the binary Wikidata
    gender values ``male`` / ``female`` are counted; every other recorded value
    (non-binary, trans, genid) is excluded from both numerator and denominator
    and the count is reported so the exclusion is visible. Negative (BCE) years
    are excluded — they are all low-quality ancient records.

A small companion COUNT returns how many people were considered. It is bounded
by the same statement floor and is used only for the card's sample line.

The display window runs to the **2010s**. The 2000s and 2010s cohorts are
right-censored — people born then are still early in their careers and are
under-documented — so they are marked ``*`` and are not allowed to define when
a field "opened". They are, however, what makes the rate-of-progress story
visible, so they are drawn and measured.

Rate metric (honest and precise): for each field, ``base_share`` is the pooled
share of women among everyone born in the 1970s–80s, and ``now_share`` the same
for the 2000s–10s; ``pace_pp = (now_share − base_share) / 3`` is the average
change in **percentage points per birth decade** across the three decades
between the window midpoints. Fields need ``PACE_MIN_N`` (200) records in *both*
windows to be ranked; thinner fields are left off the list rather than given a
noisy number. The pooled overall curve is measured the same way.

Run it manually inside the backend container:

    docker compose exec -T backend python - --out-dir /tmp/gender \\
        < backend/scripts/marketing_gender.py
    docker compose exec -T backend cat /tmp/gender/gender.json \\
        > docs/marketing/gender/gender.json
    docker compose exec -T backend cat /tmp/gender/gender.html \\
        > docs/marketing/gender/gender.html

Flags:
    --out-dir PATH       output directory (default: docs/marketing/gender)
    --min-statements N   Wikidata-statement floor, the notability bound (default 30)
    --min-cell N         per (field, decade) cell floor; below it is grey (default 30)
    --scan-limit N       max people fed into the aggregation (default 900000)
    --from-json PATH     re-render an existing gender.json without touching the DB
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

DEFAULT_MIN_STATEMENTS = 30
DEFAULT_MIN_CELL = 30
DEFAULT_SCAN_LIMIT = 900_000

YEAR_MIN = 1400
YEAR_MAX = 2029
# Display window: the documented record is thin before 1800, and the 2000s /
# 2010s are right-censored, so the heatmap runs from 1800 to 2010 and marks the
# last two columns. The trend shows the same window.
DECADE_MIN = 1800
DECADE_MAX = 2010
CENSORED_DECADES = (2000, 2010)   # drawn, marked *, excluded from "opened"
OPEN_SCAN_MAX = 1990              # only cohorts through the 1990s may define "opened"

# A field needs a solid overall sample before it can be drawn (owner: avoid
# sub-1k aggregates). The display set is curated for breadth — creative,
# knowledge, power, technical and sport — then ordered by when it first opened.
FIELD_FLOOR = 1000
EARLY_SCAN_MIN = 1800
EARLY_SCAN_MAX = 1910
EARLY_POOL_N = 200       # pooled sample for a defensible "before" number
CROSS_THRESHOLD = 0.25   # "opened" = first decade at/above 25% women

# "Late" pools. ``RECENT_DECADES`` is the robust post-1970 window used for the
# long-run "change" number; the pace metric deliberately compares two tighter
# pools three decades apart so its label ("per decade") is exact.
RECENT_DECADES = (1970, 1980, 1990, 2000, 2010)
PACE_BASE_DECADES = (1970, 1980)
PACE_NOW_DECADES = (2000, 2010)
PACE_SPAN = 3.0          # decades between the midpoints of the two pace pools
PACE_MIN_N = 200         # records required in EACH pace pool to rank a field
# Records required in the crossing cell before a decade may define "opened".
# The heatmap may show thinner cells (it prints the n under each one), but a
# ranking claim must not rest on a handful of people: at 30, "modelling opened
# in the 1890s" was decided by a single cell of 33 records.
CROSS_MIN_N = 200
PACE_RANK_MIN_TOTAL = 2000  # overall sample required before a pace is ranked on the card

CURATED_FIELDS = [
    "journalist", "teacher", "lawyer", "politician", "university_teacher",
    "film_director", "writer", "painter", "architect", "historian", "poet",
    "chemist", "diplomat", "economist", "physicist", "philosopher", "actor",
    "composer", "mathematician", "engineer", "military_person",
    "football_player", "singer", "model",
]

# One aggregation: UNNEST occupations, group by (field, birth decade, gender).
GENDER_AQL = """
FOR p IN persons
  FILTER p.wikidata_statements_count >= @min_statements
    AND p.gender IN ['male', 'female']
    AND p.birth_date != null AND p.birth_date[0] != '-'
    AND LENGTH(p.occupations) > 0
  LET by = TO_NUMBER(SUBSTRING(p.birth_date, 0, 4))
  FILTER by >= @year_min AND by <= @year_max
  LET dec = FLOOR(by / 10) * 10
  LIMIT @scan_limit
  FOR occ IN p.occupations
    COLLECT field = occ, decade = dec, g = p.gender WITH COUNT INTO n
    RETURN {field: field, decade: decade, gender: g, n: n}
"""

# Bounded companion count (same statement floor) for the sample line only.
PEOPLE_AQL = """
RETURN LENGTH(
  FOR p IN persons
    FILTER p.wikidata_statements_count >= @min_statements
      AND p.gender IN ['male', 'female']
      AND p.birth_date != null AND p.birth_date[0] != '-'
      AND LENGTH(p.occupations) > 0
    LET by = TO_NUMBER(SUBSTRING(p.birth_date, 0, 4))
    FILTER by >= @year_min AND by <= @year_max
    RETURN 1
)
"""

# Gender values outside male/female are excluded from every rate but counted
# here so the omission is stated rather than hidden.
EXCLUDED_AQL = """
FOR p IN persons
  FILTER p.wikidata_statements_count >= @min_statements
    AND p.gender != null AND p.gender NOT IN ['male', 'female']
    AND p.birth_date != null AND p.birth_date[0] != '-'
    AND LENGTH(p.occupations) > 0
  LET by = TO_NUMBER(SUBSTRING(p.birth_date, 0, 4))
  FILTER by >= @year_min AND by <= @year_max
  COLLECT g = p.gender WITH COUNT INTO n
  SORT n DESC
  LIMIT 12
  RETURN {gender: g, n: n}
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


def _share(female: int, male: int) -> float:
    total = female + male
    return (female / total) if total else 0.0


def _pool(fields: dict, decades: tuple[int, ...]) -> dict:
    """Pool a {decade: {female, male}} map over the given decades."""
    female = male = 0
    for dec in decades:
        cell = fields.get(dec)
        if cell:
            female += cell["female"]
            male += cell["male"]
    total = female + male
    return {
        "female": female,
        "male": male,
        "total": total,
        "female_share": _share(female, male),
    }


def _overall_pool(overall: list, decades: tuple[int, ...]) -> dict:
    """Pool the overall per-decade list (same shape as _pool)."""
    female = male = 0
    for p in overall:
        if p["decade"] in decades:
            female += p["female"]
            male += p["male"]
    total = female + male
    return {
        "female": female,
        "male": male,
        "total": total,
        "female_share": _share(female, male),
    }


def _early_window(cells: dict) -> tuple[int | None, dict | None]:
    """Earliest two-decade pool at/after 1800 with a defensible sample."""
    for dec in range(EARLY_SCAN_MIN, EARLY_SCAN_MAX + 1, 10):
        pooled = _pool(cells, (dec, dec + 10))
        if pooled["total"] >= EARLY_POOL_N:
            return dec, pooled
    return None, None


def _first_crossing(cells: dict, min_cell: int) -> int | None:
    """First *stable* decade where women reach CROSS_THRESHOLD, N permitting.

    The 2000s and 2010s cohorts are right-censored (still early in their
    careers), so they are not allowed to define "opened".
    """
    for dec in range(DECADE_MIN, OPEN_SCAN_MAX + 1, 10):
        cell = cells.get(dec)
        if not cell or (cell["female"] + cell["male"]) < min_cell:
            continue
        if _share(cell["female"], cell["male"]) >= CROSS_THRESHOLD:
            return dec
    return None


def _decades(cells: dict) -> list:
    """Serialisable per-decade cells for the display window."""
    out = []
    for dec in range(DECADE_MIN, DECADE_MAX + 1, 10):
        c = cells.get(dec)
        if not c:
            out.append(
                OrderedDict(decade=dec, label=f"{dec}s", female=0, male=0,
                            total=0, female_share=None)
            )
            continue
        t = c["female"] + c["male"]
        out.append(
            OrderedDict(
                decade=dec,
                label=f"{dec}s",
                female=c["female"],
                male=c["male"],
                total=t,
                female_share=(_share(c["female"], c["male"]) if t else None),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


def collect(min_statements: int, min_cell: int, scan_limit: int) -> dict:
    # Imported lazily so --from-json can re-render a cached payload without the
    # ArangoDB driver being installed at all.
    from app.database import get_arango_client, get_db  # noqa: E402

    db = get_db(get_arango_client(request_timeout=900))
    bind = {"min_statements": min_statements, "scan_limit": scan_limit,
            "year_min": YEAR_MIN, "year_max": YEAR_MAX}

    raw = _rows(db, GENDER_AQL, bind)
    people = _rows(db, PEOPLE_AQL, {"min_statements": min_statements,
                                    "year_min": YEAR_MIN, "year_max": YEAR_MAX})
    excluded = _rows(db, EXCLUDED_AQL, {"min_statements": min_statements,
                                        "year_min": YEAR_MIN, "year_max": YEAR_MAX})

    # field -> decade -> {female, male}
    grid: dict[str, dict[int, dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: {"female": 0, "male": 0})
    )
    for row in raw:
        field = row.get("field")
        dec = row.get("decade")
        n = int(row.get("n") or 0)
        if not field or dec is None:
            continue
        bucket = "female" if row.get("gender") == "female" else "male"
        grid[field][int(dec)][bucket] += n

    # --- overall trend: pooled across every measured field, per decade -------
    decade_tot: dict[int, dict[str, int]] = defaultdict(lambda: {"female": 0, "male": 0})
    fields: dict[str, dict] = {}
    for field, cells in grid.items():
        total = sum(c["female"] + c["male"] for c in cells.values())
        if total < FIELD_FLOOR:
            continue
        for dec, c in cells.items():
            decade_tot[dec]["female"] += c["female"]
            decade_tot[dec]["male"] += c["male"]
        female = sum(c["female"] for c in cells.values())
        early_dec, early = _early_window(cells)
        cross_min = max(min_cell, CROSS_MIN_N)
        opens = _first_crossing(cells, cross_min)
        opens_cell = cells.get(opens) if opens is not None else None
        fields[field] = OrderedDict(
            field=field,
            label=_label(field),
            female=female,
            male=total - female,
            total=total,
            female_share=_share(female, total - female),
            early_decade=early_dec,
            early_share=early["female_share"] if early else None,
            early_n=early["total"] if early else 0,
            opens_decade=opens,
            opens_n=(opens_cell["female"] + opens_cell["male"]) if opens_cell else 0,
            decades=_decades(cells),
        )

    overall = []
    for dec in sorted(decade_tot):
        c = decade_tot[dec]
        overall.append(
            OrderedDict(
                decade=dec,
                label=f"{dec}s",
                female=c["female"],
                male=c["male"],
                total=c["female"] + c["male"],
                female_share=_share(c["female"], c["male"]),
            )
        )

    mentions = sum(c["total"] for r in fields.values() for c in r["decades"])
    payload = {
        "generated": date.today().isoformat(),
        "params": {
            "min_statements": min_statements,
            "min_cell": min_cell,
            "scan_limit": scan_limit,
            "year_min": YEAR_MIN,
            "year_max": YEAR_MAX,
            "decade_min": DECADE_MIN,
            "decade_max": DECADE_MAX,
            "censored_decades": list(CENSORED_DECADES),
            "field_floor": FIELD_FLOOR,
            "cross_threshold": CROSS_THRESHOLD,
            "pace_base_decades": list(PACE_BASE_DECADES),
            "pace_now_decades": list(PACE_NOW_DECADES),
            "pace_span_decades": PACE_SPAN,
            "pace_min_n": PACE_MIN_N,
        },
        "scale": {
            "people_considered": people[0] if people else None,
            "occupation_records": mentions,
            "distinct_fields": len(grid),
            "fields_measured": len(fields),
        },
        "coverage_note": {
            "excluded_gender_values": excluded,
            "excluded_people": sum(int(r.get("n") or 0) for r in excluded),
        },
        "overall": overall,
        "fields": fields,
    }
    derive(payload)
    return payload


# ---------------------------------------------------------------------------
# Derived stats (also re-run when re-rendering --from-json)
# ---------------------------------------------------------------------------


def derive(payload: dict) -> dict:
    """Compute pools, pace, ranking, display order and headline from raw cells.

    Everything here is a pure function of ``payload["fields"]`` and
    ``payload["overall"]``, so a cached JSON re-renders identically without the
    DB.
    """
    fields: dict[str, dict] = payload["fields"]
    params = payload.get("params", {})
    cross_min = max(int(params.get("min_cell", DEFAULT_MIN_CELL)), CROSS_MIN_N)
    for r in fields.values():
        cells = {c["decade"]: c for c in r["decades"]}
        # Recomputed here (not only in collect) so a cached JSON re-renders with
        # the current crossing floor: the claim "field X opened in the 1890s"
        # must not survive a change to how much evidence it requires.
        opens = _first_crossing(cells, cross_min)
        cell = cells.get(opens) if opens is not None else None
        r["opens_decade"] = opens
        r["opens_n"] = (cell["female"] + cell["male"]) if cell else 0
        recent = _pool(cells, RECENT_DECADES)
        r["recent_share"] = recent["female_share"]
        r["recent_n"] = recent["total"]
        r["change"] = (
            recent["female_share"] - r["early_share"]
            if r.get("early_share") is not None
            else 0.0
        )
        base = _pool(cells, PACE_BASE_DECADES)
        now = _pool(cells, PACE_NOW_DECADES)
        r["base_share"] = base["female_share"]
        r["base_n"] = base["total"]
        r["now_share"] = now["female_share"]
        r["now_n"] = now["total"]
        if base["total"] >= PACE_MIN_N and now["total"] >= PACE_MIN_N:
            r["pace_pp"] = (now["female_share"] - base["female_share"]) / PACE_SPAN
        else:
            r["pace_pp"] = None
        op = r.get("opens_decade")
        r["years_since_quarter"] = (DECADE_MAX - op) if op is not None else None

    # --- display set: curated fields, ordered by when they first opened ------
    display = [f for f in CURATED_FIELDS if f in fields]
    display.sort(
        key=lambda f: (
            fields[f]["opens_decade"] is None,
            fields[f]["opens_decade"] if fields[f]["opens_decade"] is not None else 9999,
            -fields[f]["change"],
        )
    )
    payload["display"] = display

    measured = list(fields.values())
    most_changed = sorted(
        (
            r for r in measured
            if r["early_n"] >= EARLY_POOL_N and r["recent_n"] >= EARLY_POOL_N
        ),
        key=lambda r: r["change"],
        reverse=True,
    )
    payload["most_changed"] = [
        OrderedDict(
            field=r["field"],
            label=r["label"],
            early_decade=r["early_decade"],
            early_share=r["early_share"],
            early_n=r["early_n"],
            recent_share=r["recent_share"],
            recent_n=r["recent_n"],
            now_share=r["now_share"],
            now_n=r["now_n"],
            change=r["change"],
            pace_pp=r["pace_pp"],
            total=r["total"],
        )
        for r in most_changed[:8]
    ]

    paced = [r for r in measured if r["pace_pp"] is not None]
    paced.sort(key=lambda r: r["pace_pp"], reverse=True)
    payload["pace_rank"] = [
        OrderedDict(
            field=r["field"],
            label=r["label"],
            pace_pp=r["pace_pp"],
            base_share=r["base_share"],
            base_n=r["base_n"],
            now_share=r["now_share"],
            now_n=r["now_n"],
            opens_decade=r["opens_decade"],
            years_since_quarter=r["years_since_quarter"],
            total=r["total"],
        )
        for r in paced
    ]

    # --- pooled overall endpoints + rates ------------------------------------
    overall = payload.get("overall") or []
    ov = {p["decade"]: p for p in overall}
    first = ov.get(DECADE_MIN)
    overall_now = _overall_pool(overall, PACE_NOW_DECADES)
    overall_base = _overall_pool(overall, PACE_BASE_DECADES)
    p50, p70 = ov.get(1950), ov.get(1970)
    pace_early = (
        (p70["female_share"] - p50["female_share"]) / 2.0
        if p50 and p70
        else None
    )
    pace_recent = (
        (overall_now["female_share"] - overall_base["female_share"]) / PACE_SPAN
        if overall_base["total"] and overall_now["total"]
        else None
    )

    lead = max((fields[f] for f in display), key=lambda r: r["change"]) if display else {}
    ranked_display = [r for r in paced if r["field"] in set(display)]
    fastest = ranked_display[0] if ranked_display else {}
    slowest = ranked_display[-1] if ranked_display else {}
    opened = [r for r in measured if r.get("opens_decade") is not None]
    never = [r for r in measured if r.get("opens_decade") is None]

    payload["headline"] = OrderedDict(
        lead_field=lead.get("field"),
        lead_label=lead.get("label"),
        lead_early_decade=lead.get("early_decade"),
        lead_early_share=lead.get("early_share"),
        lead_recent_share=lead.get("recent_share"),
        lead_change=lead.get("change"),
        lead_recent_n=lead.get("recent_n"),
        overall_first=first,
        overall_now=overall_now,
        overall_base=overall_base,
        pace_early=pace_early,
        pace_recent=pace_recent,
        fastest_field=fastest.get("field"),
        fastest_label=fastest.get("label"),
        fastest_pace=fastest.get("pace_pp"),
        fastest_now=fastest.get("now_share"),
        fastest_now_n=fastest.get("now_n"),
        slowest_field=slowest.get("field"),
        slowest_label=slowest.get("label"),
        slowest_pace=slowest.get("pace_pp"),
        slowest_now=slowest.get("now_share"),
        slowest_now_n=slowest.get("now_n"),
        quarter_earliest=min((r["opens_decade"] for r in opened), default=None),
        quarter_latest=max((r["opens_decade"] for r in opened), default=None),
        never_quarter=len(never),
        fields=len(display),
        measured=len(measured),
        decades=len(range(DECADE_MIN, DECADE_MAX + 1, 10)),
    )
    return payload


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _pretty(occ: str) -> str:
    return (occ or "").replace("_", " ").strip()


def _label(occ: str) -> str:
    s = _pretty(occ)
    return s[:1].upper() + s[1:] if s else s


def _plural(word: str) -> str:
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


def _pct(value: float | None, digits: int = 0) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.{digits}f}%"


def _pp(value: float | None, digits: int = 1) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:+.{digits}f}"


def _year_label(dec: int | None) -> str:
    if dec is None:
        return "n/a"
    if dec < 0:
        return f"{abs(dec)}s BC"
    return f"{dec}s"


# ---------------------------------------------------------------------------
# Orange ramp (the ONLY data colour) + helpers
# ---------------------------------------------------------------------------

# One orange hue family, light -> dark. Data meaning: women's share, 0-60%+.
RAMP = [
    (0.00, (248, 241, 231)),
    (0.12, (240, 221, 199)),
    (0.24, (230, 198, 161)),
    (0.36, (217, 168, 115)),
    (0.48, (203, 138, 73)),
    (0.60, (191, 107, 40)),   # brand orange #bf6b28
    (0.75, (162, 85, 28)),
    (0.90, (127, 65, 20)),
    (1.00, (102, 51, 15)),
]
RAMP_MAX = 0.60  # 60%+ clips to the darkest end; legend says so
NO_DATA_FILL = "#e4dfd4"
NO_DATA_DOT = "#b9b2a6"


def _lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def ramp_color(share: float) -> str:
    x = max(0.0, min(1.0, share / RAMP_MAX))
    for i in range(len(RAMP) - 1):
        p0, c0 = RAMP[i]
        p1, c1 = RAMP[i + 1]
        if p0 <= x <= p1:
            t = (x - p0) / (p1 - p0) if p1 > p0 else 0.0
            r, g, b = _lerp(c0, c1, t)
            return f"rgb({r},{g},{b})"
    r, g, b = RAMP[-1][1]
    return f"rgb({r},{g},{b})"


def _ink(share: float) -> str:
    """Text colour with contrast against the ramp."""
    return "#fbf6ee" if share / RAMP_MAX >= 0.72 else "#3a2a1d"


def _ramp_gradient_svg(x: int, y: int, w: int, h: int, gid: str = "ramp") -> str:
    stops = []
    for pos, (r, g, b) in RAMP:
        stops.append(f'<stop offset="{pos * 100:.0f}%" stop-color="rgb({r},{g},{b})"/>')
    return (
        f'<defs><linearGradient id="{gid}" x1="0" x2="1" y1="0" y2="0">'
        + "".join(stops)
        + f'</linearGradient></defs>'
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="3" fill="url(#{gid})" '
        f'stroke="rgba(28,21,16,.18)"/>'
    )


# ---------------------------------------------------------------------------
# Heatmap SVG
# ---------------------------------------------------------------------------

HM_LABEL_W = 148
HM_CROSS_W = 58
HM_CELL_W = 40
HM_HEAD_H = 26
HM_ROW_H = 25


def build_heatmap(payload: dict, min_cell: int) -> str:
    rows = [payload["fields"][f] for f in payload["display"]]
    decades = list(range(DECADE_MIN, DECADE_MAX + 1, 10))
    grid_l = HM_LABEL_W + HM_CROSS_W
    width = grid_l + len(decades) * HM_CELL_W
    height = HM_HEAD_H + len(rows) * HM_ROW_H
    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" role="img" '
        f'aria-label="Women&#39;s share of each profession by birth decade">'
    )
    parts.append(_ramp_gradient_svg(0, 0, 1, 1, "hmramp"))

    # Column headers.
    parts.append(
        f'<text x="{grid_l - 8}" y="16" class="hm-cross-head" text-anchor="end">25% by</text>'
    )
    for i, dec in enumerate(decades):
        x = grid_l + i * HM_CELL_W + HM_CELL_W / 2
        censored = dec in CENSORED_DECADES
        cls = "hm-year censored" if censored else "hm-year"
        mark = "*" if censored else ""
        parts.append(f'<text x="{x:.1f}" y="16" class="{cls}" text-anchor="middle">{dec}s{mark}</text>')

    for r, row in enumerate(rows):
        y = HM_HEAD_H + r * HM_ROW_H
        cy = y + HM_ROW_H / 2
        # Row label.
        parts.append(
            f'<text x="{HM_LABEL_W - 8}" y="{cy + 4.2:.1f}" class="hm-field" '
            f'text-anchor="end">{_esc(row["label"])}</text>'
        )
        cross = row.get("opens_decade")
        cross_n = row.get("opens_n") or 0
        parts.append(
            f'<text x="{grid_l - 8}" y="{cy - 0.5:.1f}" class="hm-cross" '
            f'text-anchor="end">{_year_label(cross)}</text>'
        )
        if cross is not None and cross_n:
            parts.append(
                f'<text x="{grid_l - 8}" y="{cy + 8.6:.1f}" class="hm-cross-n" '
                f'text-anchor="end">n&nbsp;{_n(cross_n)}</text>'
            )
        # Cells.
        for i, dec in enumerate(decades):
            x = grid_l + i * HM_CELL_W
            cell = next((c for c in row["decades"] if c["decade"] == dec), None)
            if cell and cell["total"] >= min_cell and cell["female_share"] is not None:
                fill = ramp_color(cell["female_share"])
                ink = _ink(cell["female_share"])
                share = _pct(cell["female_share"])
                parts.append(
                    f'<rect x="{x + 1:.0f}" y="{y + 1:.0f}" width="{HM_CELL_W - 2}" '
                    f'height="{HM_ROW_H - 2}" rx="3" fill="{fill}">'
                    f'<title>{_esc(row["label"])}, born {dec}s: {share} women '
                    f'(n={_n(cell["total"])})</title></rect>'
                )
                parts.append(
                    f'<text x="{x + HM_CELL_W / 2:.1f}" y="{cy - 0.6:.1f}" class="hm-share" '
                    f'style="fill:{ink}" text-anchor="middle">{share}</text>'
                )
                parts.append(
                    f'<text x="{x + HM_CELL_W / 2:.1f}" y="{cy + 8.6:.1f}" class="hm-n" '
                    f'style="fill:{ink}" text-anchor="middle">{_n(cell["total"])}</text>'
                )
            else:
                parts.append(
                    f'<rect x="{x + 1:.0f}" y="{y + 1:.0f}" width="{HM_CELL_W - 2}" '
                    f'height="{HM_ROW_H - 2}" rx="3" fill="{NO_DATA_FILL}"/>'
                    f'<circle cx="{x + HM_CELL_W / 2:.1f}" cy="{cy:.1f}" r="2" fill="{NO_DATA_DOT}"/>'
                )
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Overall trend SVG (chrome panel)
# ---------------------------------------------------------------------------

TREND_W = 400
TREND_H = 194
TREND_PAD = {"l": 32, "r": 12, "t": 16, "b": 26}


def build_trend(payload: dict) -> str:
    pts_all = [p for p in payload["overall"] if DECADE_MIN <= p["decade"] <= DECADE_MAX]
    if not pts_all:
        return ""
    w, h = TREND_W, TREND_H
    pl, pr, pt, pb = TREND_PAD["l"], TREND_PAD["r"], TREND_PAD["t"], TREND_PAD["b"]
    vmax = 0.40
    n = len(pts_all)

    def ex(i: int) -> float:
        return pl + (i / max(1, n - 1)) * (w - pl - pr)

    def ey(v: float) -> float:
        return pt + (vmax - min(v, vmax)) / vmax * (h - pt - pb)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
        f'role="img" aria-label="Women&#39;s share of all recorded occupations by birth decade">'
    ]
    for g in range(0, 41, 10):
        yy = ey(g / 100.0)
        parts.append(f'<line x1="{pl}" y1="{yy:.1f}" x2="{w - pr}" y2="{yy:.1f}" class="trend-grid"/>')
        parts.append(f'<text x="{pl - 6}" y="{yy + 3.4:.1f}" class="trend-tick" text-anchor="end">{g}%</text>')
    xy = [(ex(i), ey(p["female_share"])) for i, p in enumerate(pts_all)]
    area = (
        f'M{xy[0][0]:.1f},{h - pb:.1f} '
        + " ".join(f'L{x:.1f},{y:.1f}' for x, y in xy)
        + f' L{xy[-1][0]:.1f},{h - pb:.1f} Z'
    )
    parts.append(f'<path d="{area}" class="trend-area"/>')
    parts.append('<polyline points="' + " ".join(f"{x:.1f},{y:.1f}" for x, y in xy) + '" class="trend-line"/>')
    for i, (x, y) in enumerate(xy):
        censored = pts_all[i]["decade"] in CENSORED_DECADES
        cls = "trend-dot hollow" if censored else "trend-dot"
        if i % 5 == 0 or i == n - 1 or censored:
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.6" class="{cls}"/>')
    lo = pts_all[0]
    hi = pts_all[-1]
    parts.append(
        f'<text x="{xy[0][0]:.1f}" y="{xy[0][1] - 8:.1f}" class="trend-val" text-anchor="start">'
        f'{lo["decade"]}s · {lo["female_share"] * 100:.0f}%</text>'
    )
    parts.append(
        f'<text x="{xy[-1][0]:.1f}" y="{xy[-1][1] - 9:.1f}" class="trend-val" text-anchor="end">'
        f'{hi["decade"]}s* · {hi["female_share"] * 100:.0f}%</text>'
    )
    parts.append(f'<text x="{pl}" y="{h - 8}" class="trend-x" text-anchor="start">{pts_all[0]["decade"]}s</text>')
    parts.append(f'<text x="{w - pr}" y="{h - 8}" class="trend-x" text-anchor="end">{pts_all[-1]["decade"]}s*</text>')
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Card HTML
# ---------------------------------------------------------------------------

HTML_HEAD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>HumansMap: when did each field open up?</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;800&family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:#f9f8f4; --text:#1c1510;
    --chrome:#1b4d8e;          /* chrome only: wordmark, headline emphasis, links */
    --data:#bf6b28;            /* data only: women's share (one orange family) */
    --muted:#8a8278;           /* structure/neutral: grids, axes, no-data, labels */
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
  h1 { font:800 40px/1.06 var(--display); margin:11px 0 0; letter-spacing:-.012em; max-width:46ch; }
  h1 em { font-style:normal; color:var(--chrome); }
  .deck { font:400 14.5px/1.4 var(--body); color:var(--muted); margin:7px 0 0; max-width:1500px; }
  .deck b { color:var(--text); font-weight:700; }
  .body { display:flex; gap:28px; margin-top:11px; flex:1; min-height:0; }
  .heat { width:1086px; flex:0 0 1086px; display:flex; flex-direction:column; min-height:0; }
  .heat svg { display:block; width:1086px; height:auto; overflow:visible; }
  .hm-share { font:700 10.5px/1 var(--body); }
  .hm-n { font:500 7.4px/1 var(--body); }
  .hm-field { font:600 12.4px/1 var(--body); fill:var(--text); }
  .hm-cross { font:500 10.5px/1 var(--body); fill:var(--muted); }
  .hm-cross-n { font:500 7.6px/1 var(--body); fill:var(--muted); }
  .hm-cross-head { font:700 9px/1 var(--body); letter-spacing:.13em; fill:var(--muted); }
  .hm-year { font:600 9px/1 var(--body); fill:var(--muted); }
  .hm-year.censored { fill:#b3aca0; }
  .legend { display:flex; gap:8px 18px; align-items:center; margin-top:8px; flex-wrap:wrap;
    font:400 11.5px/1.3 var(--body); color:var(--muted); }
  .legend .grp { display:flex; align-items:center; gap:7px; }
  .legend .bar { width:180px; height:12px; border-radius:3px; border:1px solid rgba(28,21,16,.18);
    background:linear-gradient(90deg, #f8f1e7 0%, #f0ddc7 12%, #e6c6a1 24%, #d9a873 36%, #cb8a49 48%, #bf6b28 60%, #a2551c 75%, #7f4114 90%, #66330f 100%); }
  .legend .bar-lbl { font:600 10.5px/1 var(--body); }
  .legend .sw { display:inline-block; width:20px; height:12px; border-radius:3px; }
  .legend .nodata { background:#e4dfd4; border:1px solid rgba(28,21,16,.14); }
  .legend .lineglyph { display:inline-block; width:22px; height:0; border-top:2.6px solid var(--data); }
  .panel { flex:1; min-width:0; display:flex; flex-direction:column; }
  .block { border-top:2px solid var(--text); padding-top:8px; }
  .block + .block { margin-top:12px; }
  .plabel { font:700 10.5px/1 var(--body); letter-spacing:.17em; text-transform:uppercase; color:var(--muted); }
  .sub { font:400 11px/1.3 var(--body); color:var(--muted); margin-top:4px; }
  .trend svg { display:block; width:400px; height:194px; overflow:visible; margin-top:2px; }
  .trend-grid { stroke:rgba(28,21,16,.08); stroke-width:1; }
  .trend-tick { font:500 10px/1 var(--body); fill:var(--muted); }
  .trend-x { font:500 10px/1 var(--body); fill:var(--muted); }
  .trend-area { fill:rgba(191,107,40,.16); }
  .trend-line { fill:none; stroke:var(--data); stroke-width:2.6; stroke-linejoin:round; }
  .trend-dot { fill:var(--data); stroke:var(--bg); stroke-width:1.4; }
  .trend-dot.hollow { fill:var(--bg); stroke:var(--data); stroke-width:1.6; }
  .trend-val { font:700 11px/1 var(--body); fill:var(--text); }
  .trendnote { font:400 11.5px/1.4 var(--body); color:var(--muted); margin-top:3px; }
  .trendnote b { color:var(--text); }
  ul.pace { list-style:none; margin:7px 0 0; padding:0; }
  ul.pace li { display:grid; grid-template-columns:1fr 92px 58px; gap:9px; align-items:center;
    padding:3.5px 0; border-bottom:1px solid var(--line); }
  .pn { font:600 12.4px/1.15 var(--body); }
  .pn em { display:block; font:400 10.2px/1.25 var(--body); font-style:normal; color:var(--muted); }
  .pt { position:relative; display:block; height:9px; background:var(--surface); border-radius:5px; }
  .pz { position:absolute; top:-2px; bottom:-2px; width:1.5px; background:rgba(28,21,16,.34); }
  .pb { position:absolute; top:0; height:100%; border-radius:5px; background:var(--data); }
  .pv { font:700 12.5px/1 var(--body); text-align:right; font-variant-numeric:tabular-nums; }
  .pacenote { font:400 11.3px/1.4 var(--body); color:var(--muted); margin-top:6px; }
  .pacenote b { color:var(--text); }
  ul.pace li.pacediv { display:block; border-bottom:none; padding:4px 0 1px;
    font:700 9px/1 var(--body); letter-spacing:.15em; text-transform:uppercase; color:var(--muted); }
  .runners { list-style:none; margin:7px 0 0; padding:0; }
  .runners li { display:grid; grid-template-columns:1fr auto; gap:8px; align-items:baseline;
    padding:3.5px 0; border-bottom:1px solid var(--line); }
  .runners .rn { font:600 12.4px/1.2 var(--body); }
  .runners .rn em { display:block; font:400 10.2px/1.25 var(--body); font-style:normal; color:var(--muted); }
  .runners .rv { font:700 12.4px/1 var(--body); color:var(--data); text-align:right; }
  .runners .rv small { display:block; font:400 10.2px/1.25 var(--body); color:var(--muted); }
  .fineprint { font:400 11.2px/1.42 var(--body); color:var(--muted); margin-top:10px;
    padding-top:7px; border-top:1px solid var(--line); }
  .fineprint b { color:var(--text); }
  .card footer { margin-top:auto; padding-top:8px; display:flex; justify-content:space-between;
    align-items:baseline; font:400 12.5px/1.4 var(--body); color:var(--muted); }
  .card footer b { color:var(--text); font-weight:700; }
</style></head>
<body>"""


def _pace_rows(payload: dict) -> list:
    """Fastest + slowest *drawn* fields by recent pace (so it maps to the grid)."""
    drawn = set(payload.get("display") or [])
    strong = [
        r for r in payload.get("pace_rank", [])
        if r["field"] in drawn and r["total"] >= PACE_RANK_MIN_TOTAL
    ]
    if len(strong) > 3:
        picked = strong[:3] + strong[-2:]
    else:
        picked = strong
    seen, rows = set(), []
    for r in picked:
        if r["field"] in seen:
            continue
        seen.add(r["field"])
        rows.append(r)
    return rows


def _pace_row(r: dict, pmax: float) -> str:
    p = r["pace_pp"]
    track, mid = 92.0, 46.0
    w = min(mid, (abs(p) / pmax) * mid) if pmax else 0.0
    left = (mid - w) if p < 0 else mid
    quarter = r.get("opens_decade")
    sub = (
        f"25% by the {quarter}s · {_pct(r['now_share'])} of 2000s&ndash;10s"
        if quarter is not None
        else f"never a quarter · {_pct(r['now_share'])} of 2000s&ndash;10s"
    )
    return (
        f'<li><span class="pn">{_esc(r["label"])}<em>{sub}</em></span>'
        f'<span class="pt"><span class="pb" style="left:{left:.1f}px;width:{max(1.5, w):.1f}px"></span>'
        f'<span class="pz" style="left:{mid:.1f}px"></span></span>'
        f'<span class="pv">{_pp(p)}</span></li>'
    )


def render_html(payload: dict) -> str:
    scale = payload["scale"]
    head = payload["headline"]
    fields = payload["fields"]
    display = payload["display"]
    params = payload["params"]
    heat = build_heatmap(payload, params["min_cell"])
    trend = build_trend(payload)

    first = head.get("overall_first") or {}
    now = head.get("overall_now") or {}
    pace_early = head.get("pace_early")
    pace_recent = head.get("pace_recent")
    fast = head.get("fastest_label")
    slow = head.get("slowest_label")
    slow_verb = "has slipped to" if (head.get("slowest_pace") or 0) < 0 else "sits at"

    # Headline: what the chart shows (women's share of each field, by decade),
    # then the recent stall (the rate-of-progress point).
    big = (
        f'Women&rsquo;s share of each field, by birth decade'
        f'<br><em>{first.get("female_share", 0) * 100:.0f}%</em> in the 1800s, '
        f'<em>{now.get("female_share", 0) * 100:.0f}%</em> by the 2000s &mdash; the pace now appears to stall.'
    )

    deck = (
        f"Women&rsquo;s share of <b>{_n(scale['occupation_records'])}</b> recorded occupations for "
        f"<b>{_n(scale['people_considered'])}</b> people with a birth date and a binary gender, "
        f"bucketed by the decade they were born. <b>{_n(scale['fields_measured'])}</b> fields clear "
        f"1,000 records; the <b>{_n(head.get('fields'))}</b> drawn here carry the story. The pooled line gained "
        f"<b>{pace_early * 100:.1f} points a decade</b> from the 1950s to the 1970s but only "
        f"<b>{pace_recent * 100:.1f}</b> since &mdash; a comparison that rests on the 2000s&ndash;10s "
        f"cohorts, which are still being documented. Behind that average, the fields diverge: "
        f"<b>{_esc(fast or '')}</b> is still climbing while <b>{_esc(slow or '')}</b> {slow_verb} "
        f"{_pct(head.get('slowest_now'))} of the 2000s&ndash;10s cohort."
    )

    # Rate-of-progress panel: fastest fields, a divider, then the slowest.
    pace_rows = _pace_rows(payload)
    pmax = max((abs(r["pace_pp"]) for r in pace_rows), default=1.0) or 1.0
    fast_rows = pace_rows[:3] if len(pace_rows) > 3 else pace_rows
    slow_rows = pace_rows[3:] if len(pace_rows) > 3 else []
    pace_html = "".join(_pace_row(r, pmax) for r in fast_rows)
    if slow_rows:
        pace_html += '<li class="pacediv">slowest</li>'
        pace_html += "".join(_pace_row(r, pmax) for r in slow_rows)
    q_lo = head.get("quarter_earliest")
    q_hi = head.get("quarter_latest")
    never = head.get("never_quarter") or 0

    # Biggest shift since the 1800s: ranked by the 1970s–2010s pool vs. the
    # earliest well-sampled cohort, and shown with those same two endpoints so
    # the number and the arrow never disagree.
    shift_items = payload.get("most_changed", [])[:3]
    shift_html = "".join(
        f'<li><span class="rn">{_esc(r["label"])}<em>'
        f'{_pct(r["early_share"])} in the {_year_label(r["early_decade"])}</em></span>'
        f'<span class="rv">{_pct(r["recent_share"])}'
        f'<small>{_pp(r["change"])} pts to the 1970s&ndash;10s</small></span></li>'
        for r in shift_items
    )
    still_closed = min(
        (
            fields[f] for f in display
            if fields[f].get("now_share") is not None
            and fields[f].get("now_n", 0) >= PACE_MIN_N
        ),
        key=lambda r: r["now_share"],
        default={},
    )

    fineprint = (
        "<b>Read with care.</b> Wikidata&rsquo;s <b>gender field is binary and incomplete</b>: "
        "only <code>male</code>/<code>female</code> are counted; "
        f"{_n(payload['coverage_note']['excluded_people'])} people with another recorded value "
        "(non-binary, trans, unspecified) are excluded, so <b>non-binary identities are not "
        "represented at all</b>. Coverage is <b>uneven</b> (US/Europe and the project&rsquo;s Italy "
        "skew high): this is the share of the <em>documented</em>, not the real workforce. "
        "The 2000s and 2010s cohorts are <b>right-censored</b> (*): many are not yet "
        "notable enough to be recorded. Pace = the change in women&rsquo;s share between the "
        "pooled 1970s&ndash;80s and 2000s&ndash;10s cohorts, divided by three decades. "
        "Occupation labels are Wikidata values; people with many occupations appear in each."
    )

    legend = (
        '<div class="legend">'
        '<span class="grp"><span class="bar"></span>'
        '<span class="bar-lbl">women&rsquo;s share of records, from 0% to 60%+ (one orange family, one meaning)</span></span>'
        f'<span class="grp"><span class="sw nodata"></span>fewer than {_n(params["min_cell"])} records, not shown</span>'
        '<span class="grp"><span class="lineglyph"></span>all fields pooled (same meaning)</span>'
        '<span class="grp">* 2000s&ndash;10s cohorts are still early in their careers &middot; '
        '25% by = first decade women reached a quarter &middot; small number under each % = records behind the cell</span>'
        '</div>'
    )

    card = f"""<section class="card">
  <header>
    <span class="eyebrow">When did each field open up?</span>
    <span class="brand">Humans&nbsp;Map</span>
  </header>
  <h1>{big}</h1>
  <p class="deck">{deck}</p>
  <div class="body">
    <div class="heat">
      {heat}
      {legend}
      <div class="fineprint">{fineprint}</div>
    </div>
    <aside class="panel">
      <div class="block">
        <div class="plabel">All fields pooled, by birth decade</div>
        <div class="trend">{trend}</div>
        <div class="trendnote">Gained <b>{pace_early * 100:.1f} points a decade</b> in the
          1950s&ndash;70s, then <b>{pace_recent * 100:.1f}</b> since; hollow dots are the
          censored 2000s&ndash;10s cohorts.</div>
      </div>
      <div class="block">
        <div class="plabel">Rate of progress</div>
        <div class="sub">Change in women&rsquo;s share per birth decade,
          1970s&ndash;80s &rarr; 2000s&ndash;10s</div>
        <ul class="pace">{pace_html}</ul>
        <div class="pacenote">A quarter women first reached between <b>{_year_label(q_lo)}</b> and
          <b>{_year_label(q_hi)}</b>; <b>{never}</b> of {_n(head.get('measured'))} measured fields
          still fall short. Pace needs {_n(params.get('pace_min_n', PACE_MIN_N))}+ records in each
          window, so thinly-documented fields are left off the list.</div>
      </div>
      <div class="block">
        <div class="plabel">Biggest shift since the 1800s</div>
        <ul class="runners">{shift_html}</ul>
        <div class="pacenote">Still barely open: <b>{_esc(still_closed.get('label', ''))}</b>
          at {_pct(still_closed.get('now_share'))} of the 2000s&ndash;10s cohort. Across all
          <b>{_n(head.get('measured'))}</b> measured fields, comparing the pooled
          1970s&ndash;2010s cohorts with the earliest well-sampled one.</div>
      </div>
    </aside>
  </div>
  <footer>
    <span>Data: Wikidata (CC BY-SA) &middot; humansmap.com</span>
    <span><b>{_n(scale['occupation_records'])}</b> occupation records &middot; <b>{_n(scale['people_considered'])}</b> people &middot; <b>{_n(head.get('fields'))}</b> fields drawn &middot; <b>{_n(scale['fields_measured'])}</b> with 1,000+</span>
  </footer>
</section>"""

    return HTML_HEAD + card + "\n</body></html>\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default="docs/marketing/gender")
    parser.add_argument("--min-statements", type=int, default=DEFAULT_MIN_STATEMENTS)
    parser.add_argument("--min-cell", type=int, default=DEFAULT_MIN_CELL)
    parser.add_argument("--scan-limit", type=int, default=DEFAULT_SCAN_LIMIT)
    parser.add_argument("--no-html", action="store_true")
    parser.add_argument(
        "--from-json", default=None,
        help="re-render from an existing gender.json instead of querying the DB",
    )
    args = parser.parse_args()

    if args.from_json:
        payload = json.loads(Path(args.from_json).read_text())
        print(f"[cache] loaded {args.from_json} (no DB access)")
        params = payload.setdefault("params", {})
        params["min_cell"] = args.min_cell
        params["decade_min"] = DECADE_MIN
        params["decade_max"] = DECADE_MAX
        params["censored_decades"] = list(CENSORED_DECADES)
        params["pace_base_decades"] = list(PACE_BASE_DECADES)
        params["pace_now_decades"] = list(PACE_NOW_DECADES)
        params["pace_span_decades"] = PACE_SPAN
        params["pace_min_n"] = PACE_MIN_N
        derive(payload)
    else:
        payload = collect(args.min_statements, args.min_cell, args.scan_limit)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # Always (re)write the payload: the derived pace/ranking fields are a pure
    # function of the cached cells, so this keeps the JSON in step with the
    # renderer even when the DB is not touched.
    json_path = out_dir / "gender.json"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {json_path}")

    if not args.no_html:
        html_path = out_dir / "gender.html"
        html_path.write_text(render_html(payload))
        print(f"wrote {html_path}")

    head = payload["headline"]
    summary = {
        "people_considered": payload["scale"]["people_considered"],
        "occupation_records": payload["scale"]["occupation_records"],
        "fields_shown": head.get("fields"),
        "decades": head.get("decades"),
        "overall_first": _pct((head.get("overall_first") or {}).get("female_share")),
        "overall_now_2000s10s": _pct((head.get("overall_now") or {}).get("female_share")),
        "pace_early_pp": round((head.get("pace_early") or 0) * 100, 2),
        "pace_recent_pp": round((head.get("pace_recent") or 0) * 100, 2),
        "fastest": f"{head.get('fastest_label')} {_pp(head.get('fastest_pace'))}",
        "slowest": f"{head.get('slowest_label')} {_pp(head.get('slowest_pace'))}",
        "most_changed": [
            {
                "field": r["label"],
                "early": _pct(r["early_share"]),
                "now": _pct(r["now_share"]),
                "change_pp": round(r["change"] * 100, 1),
            }
            for r in payload["most_changed"][:5]
        ],
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
