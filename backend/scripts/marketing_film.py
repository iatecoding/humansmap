#!/usr/bin/env python3
"""Marketing film miner — read-only, emits the "a century of film people" card.

The insight: Wikidata's film occupations are dense enough to trace the film
workforce by birth decade and to expose the gender gap behind the camera. The
workforce takes off with the cinema generation (born 1880s–1900s); acting is
near parity, while directing and writing stay around one in five women. Most of
the workforce is **still alive** (no ``death_date``): the hero splits each birth
cohort into living and deceased, so the shrinking recent cohorts read as living
people who are still early in their careers, not as missing data.

It never writes to ArangoDB. There is exactly **one bounded aggregation** over
the live graph:

  * A single pass over ``persons`` where ``wikidata_statements_count`` is at or
    above ``--min-statements`` (default 30 — the persistent index on that field
    bounds the scan), restricted to people with a birth date in
    ``--year-min``..``--year-max`` and at least one film occupation from the
    nine that clear the floor (``actor``, ``film_actor``, ``screenwriter``,
    ``film_director``, ``television_actor``, ``film_producer``,
    ``voice_actor``, ``television_presenter``, ``theatre_director``).
  * Every row carries the person's birth decade, gender, the *intersection* of
    their occupations with the film set, and their first nationality. Python
    does all the grouping — top countries and per-occupation counts included —
    so there are no per-row document joins and no second scan.
  * ``--scan-limit`` (default 500000) is a hard ceiling that at the default
    floor does not bite (~132k eligible).

**One primary role per person.** A person is counted once, in the
highest-priority family they belong to, so the stacked chart never
double-counts:

    film_director > screenwriter > film_producer >
    actor family (actor, film_actor, television_actor, voice_actor) >
    television_presenter / theatre_director

Only the binary Wikidata gender values ``male`` / ``female`` enter the shares;
every other recorded value is excluded from both numerator and denominator and
its count is reported so the omission is visible. There is **no death-date
filter**: the aggregation returns a living flag per person, so the card can
show that recent cohorts are living, not missing. The 1980s–2020s birth
cohorts are right-censored — people born then are still early in their careers
and under-documented — so they are drawn and marked ``*`` but never allowed to
define the trend.

Run it manually inside the backend container:

    docker compose exec -T backend python - --out-dir /tmp/film \\
        < backend/scripts/marketing_film.py
    docker compose exec -T backend cat /tmp/film/film.json \\
        > docs/marketing/film/film.json
    docker compose exec -T backend cat /tmp/film/film.html \\
        > docs/marketing/film/film.html

Flags:
    --out-dir PATH       output directory (default: docs/marketing/film)
    --min-statements N   Wikidata-statement floor, the notability bound (default 30)
    --year-min N         earliest birth year counted (default 1800)
    --year-max N         latest birth year counted (default 2029)
    --scan-limit N       max people fed into the aggregation (default 500000)
    --from-json PATH     re-render an existing film.json without touching the DB
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
DEFAULT_SCAN_LIMIT = 500_000
DEFAULT_YEAR_MIN = 1800
DEFAULT_YEAR_MAX = 2029

# The nine film occupations that clear the statement floor. Order matters only
# for readability; the role priority below is what assigns a person.
FILM_OCCUPATIONS = [
    "actor",
    "film_actor",
    "screenwriter",
    "film_director",
    "television_actor",
    "film_producer",
    "voice_actor",
    "television_presenter",
    "theatre_director",
]

# Priority: first family present wins. A person is counted exactly once.
ROLE_PRIORITY = [
    ("director", ("film_director",)),
    ("writer", ("screenwriter",)),
    ("producer", ("film_producer",)),
    ("actor", ("actor", "film_actor", "television_actor", "voice_actor")),
    ("presenter", ("television_presenter", "theatre_director")),
]
ROLE_MEMBERS = {role: members for role, members in ROLE_PRIORITY}

# Stack order (bottom -> top) and the shared colour for every family. Data
# colours only; blue stays chrome-only.
STACK_ORDER = ["actor", "director", "writer", "producer", "presenter"]
ROLE_LABEL = {
    "actor": "Actor",
    "director": "Director",
    "writer": "Screenwriter",
    "producer": "Producer",
    "presenter": "Presenter",
}
ROLE_COLOR = {
    # Actor is brick red, not the brand orange: the hero reserves #bf6b28 for
    # "living", so no colour carries two meanings (see docs/marketing/README.md).
    "actor": "#a8443c",
    "director": "#2e6f6b",
    "writer": "#8a5a91",
    "producer": "#5f7d4f",
    "presenter": "#9a7b3f",
}
ROLE_NOTE = {
    "actor": "actor &middot; film &middot; TV &middot; voice",
    "director": "film director",
    "writer": "screenwriter",
    "producer": "film producer",
    "presenter": "TV presenter &middot; theatre",
}

# Birth decades, 1800s -> 2020s. The 1980s onward are right-censored.
DECADE_MIN = 1800
DECADE_MAX = 2020
CENSORED_DECADES = (1980, 1990, 2000, 2010, 2020)
TAKEOFF_DECADES = (1870, 1900)   # the cinema generation enters the record
BEHIND_CAMERA = ("director", "writer", "producer")
GEO_TOP = 8

# Hero bands: status, bottom -> top, and the two data colours. Living is the
# warm data orange; deceased is a muted warm neutral, distinct in hue and value.
# Both appear in the legend.
STATUS_ORDER = ["living", "deceased"]
STATUS_LABEL = {"living": "Living", "deceased": "Deceased"}
STATUS_COLOR = {"living": "#bf6b28", "deceased": "#b9b2a6"}

ISO2_NAME = {
    "US": "United States", "GB": "United Kingdom", "DE": "Germany", "FR": "France",
    "JP": "Japan", "IT": "Italy", "CA": "Canada", "ES": "Spain", "RU": "Russia",
    "PL": "Poland", "SE": "Sweden", "KR": "South Korea", "AT": "Austria",
    "CZ": "Czech Republic", "IN": "India", "AU": "Australia", "NL": "Netherlands",
    "BE": "Belgium", "DK": "Denmark", "NO": "Norway", "FI": "Finland",
    "PT": "Portugal", "CH": "Switzerland", "IE": "Ireland", "BR": "Brazil",
    "MX": "Mexico", "AR": "Argentina", "CN": "China", "TW": "Taiwan",
    "TR": "Turkey", "HU": "Hungary", "RO": "Romania", "GR": "Greece",
    "IL": "Israel", "NZ": "New Zealand", "ZA": "South Africa", "UA": "Ukraine",
    "RS": "Serbia", "HR": "Croatia", "SK": "Slovakia", "SI": "Slovenia",
    "LT": "Lithuania", "LV": "Latvia", "EE": "Estonia", "IS": "Iceland",
    "LU": "Luxembourg", "ID": "Indonesia", "PH": "Philippines", "TH": "Thailand",
    "PK": "Pakistan", "IR": "Iran", "IQ": "Iraq", "SA": "Saudi Arabia",
    "EG": "Egypt", "NG": "Nigeria", "KE": "Kenya", "CU": "Cuba",
    "UY": "Uruguay", "CL": "Chile", "CO": "Colombia", "PE": "Peru",
    "VE": "Venezuela", "BG": "Bulgaria", "BY": "Belarus", "GE": "Georgia",
    "AM": "Armenia", "AZ": "Azerbaijan", "KZ": "Kazakhstan", "MY": "Malaysia",
    "SG": "Singapore", "HK": "Hong Kong", "VN": "Vietnam", "BD": "Bangladesh",
    "MA": "Morocco", "DZ": "Algeria", "TN": "Tunisia", "ET": "Ethiopia",
    "GH": "Ghana", "NL": "Netherlands",
}

# One aggregation: birth decade, gender, film-occupation intersection, the
# living/deceased flag, and the first nationality (aggregated in Python, so no
# second scan).
FILM_AQL = """
FOR p IN persons
  FILTER p.wikidata_statements_count >= @min_statements
    AND p.birth_date != null AND p.birth_date[0] != '-'
  LET f = INTERSECTION(p.occupations, @film)
  FILTER LENGTH(f) > 0
  LET by = TO_NUMBER(SUBSTRING(p.birth_date, 0, 4))
  FILTER by >= @year_min AND by <= @year_max
  LIMIT @scan_limit
  RETURN {dec: FLOOR(by / 10) * 10, g: p.gender, f: f, c: FIRST(p.nationalities),
          alive: p.death_date == null}
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


def _primary_role(film_set: set[str]) -> str | None:
    for role, members in ROLE_PRIORITY:
        if any(m in film_set for m in members):
            return role
    return None


def collect(min_statements: int, year_min: int, year_max: int, scan_limit: int) -> dict:
    # Imported lazily so --from-json can re-render a cached payload without the
    # ArangoDB driver being installed at all.
    from app.database import get_arango_client, get_db  # noqa: E402

    db = get_db(get_arango_client(request_timeout=900))
    raw = _rows(
        db,
        FILM_AQL,
        {
            "min_statements": min_statements,
            "year_min": year_min,
            "year_max": year_max,
            "scan_limit": scan_limit,
            "film": FILM_OCCUPATIONS,
        },
    )

    role_counts: dict[str, dict[str, int]] = defaultdict(
        lambda: {"n": 0, "male": 0, "female": 0, "other": 0}
    )
    # decade -> role -> n, and decade -> gender -> n, and decade -> status -> n
    dec_role: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    dec_gender: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    dec_status: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    occ_counts: dict[str, int] = defaultdict(int)
    geo_counts: dict[str, int] = defaultdict(int)
    other_gender = 0

    for row in raw:
        film = set(row.get("f") or [])
        role = _primary_role(film)
        if role is None:
            continue
        dec = int(row.get("dec") or 0)
        g = row.get("g")
        bucket = g if g in ("male", "female") else "other"
        status = "living" if row.get("alive") else "deceased"
        role_counts[role]["n"] += 1
        role_counts[role][bucket] += 1
        dec_role[dec][role] += 1
        dec_gender[dec][bucket] += 1
        dec_status[dec][status] += 1
        if bucket == "other":
            other_gender += 1
        for occ in film:
            occ_counts[occ] += 1
        code = row.get("c")
        if code:
            geo_counts[code] += 1

    people = sum(v["n"] for v in role_counts.values())
    occupation_records = sum(occ_counts.values())
    women = sum(v["female"] for v in role_counts.values())
    men = sum(v["male"] for v in role_counts.values())
    living = sum(v.get("living", 0) for v in dec_status.values())
    deceased = sum(v.get("deceased", 0) for v in dec_status.values())

    roles = []
    for role in STACK_ORDER:
        v = role_counts.get(role, {"n": 0, "male": 0, "female": 0, "other": 0})
        roles.append(
            OrderedDict(
                role=role,
                label=ROLE_LABEL[role],
                n=v["n"],
                male=v["male"],
                female=v["female"],
                other=v["other"],
            )
        )

    decades = []
    for dec in range(DECADE_MIN, DECADE_MAX + 1, 10):
        dr = dec_role.get(dec, {})
        dg = dec_gender.get(dec, {})
        ds = dec_status.get(dec, {})
        decades.append(
            OrderedDict(
                decade=dec,
                label=f"{dec}s",
                censored=dec in CENSORED_DECADES,
                total=sum(dr.values()),
                living=ds.get("living", 0),
                deceased=ds.get("deceased", 0),
                male=dg.get("male", 0),
                female=dg.get("female", 0),
                other=dg.get("other", 0),
                roles=OrderedDict((r, dr.get(r, 0)) for r in STACK_ORDER),
            )
        )

    top_countries = [
        OrderedDict(
            code=code,
            label=ISO2_NAME.get(code.upper(), code.upper()),
            n=n,
        )
        for code, n in sorted(geo_counts.items(), key=lambda kv: -kv[1])[:GEO_TOP]
    ]

    occupations = [
        OrderedDict(occupation=o, label=_occ_label(o), n=occ_counts[o])
        for o in sorted(occ_counts, key=lambda o: -occ_counts[o])
    ]

    payload = {
        "generated": date.today().isoformat(),
        "params": {
            "min_statements": min_statements,
            "year_min": year_min,
            "year_max": year_max,
            "scan_limit": scan_limit,
            "decade_min": DECADE_MIN,
            "decade_max": DECADE_MAX,
            "censored_decades": list(CENSORED_DECADES),
            "takeoff_decades": list(TAKEOFF_DECADES),
            "behind_camera": list(BEHIND_CAMERA),
            "film_occupations": list(FILM_OCCUPATIONS),
            "role_priority": [list(x) for x in ROLE_PRIORITY],
        },
        "scale": {
            "people": people,
            "living": living,
            "deceased": deceased,
            "occupation_records": occupation_records,
            "distinct_occupations": len(occ_counts),
            "countries": len(geo_counts),
        },
        "coverage_note": {
            "excluded_other_gender": other_gender,
            "missing_country": sum(1 for row in raw if not row.get("c")),
        },
        "roles": roles,
        "decades": decades,
        "top_countries": top_countries,
        "occupations": occupations,
        "headline": {},
    }
    derive(payload)
    return payload


# ---------------------------------------------------------------------------
# Derived stats (also re-run when re-rendering --from-json)
# ---------------------------------------------------------------------------


def derive(payload: dict) -> dict:
    """Compute shares, ordering and the headline from the raw counts.

    Everything here is a pure function of ``roles``, ``decades`` and
    ``top_countries``, so a cached JSON re-renders identically without the DB.
    """
    roles = payload["roles"]
    total = payload["scale"].get("people") or 0
    for r in roles:
        denom = r["male"] + r["female"]
        r["female_share"] = (r["female"] / denom) if denom else 0.0
        r["overall_share"] = (r["n"] / total) if total else 0.0

    by_role = {r["role"]: r for r in roles}
    gap_order = sorted(roles, key=lambda r: r["female_share"], reverse=True)
    comp_order = sorted(roles, key=lambda r: r["n"], reverse=True)
    payload["gap_order"] = [r["role"] for r in gap_order]
    payload["comp_order"] = [r["role"] for r in comp_order]

    women = sum(r["female"] for r in roles)
    men = sum(r["male"] for r in roles)
    overall_share = (women / (women + men)) if (women + men) else 0.0

    bf = sum(by_role[r]["female"] for r in BEHIND_CAMERA if r in by_role)
    bt = sum(
        by_role[r]["male"] + by_role[r]["female"]
        for r in BEHIND_CAMERA
        if r in by_role
    )
    behind_share = (bf / bt) if bt else 0.0

    peaks = [d for d in payload["decades"] if not d["censored"] and d["total"] > 0]
    peak = max(peaks, key=lambda d: d["total"]) if peaks else {}
    actor = by_role.get("actor", {})
    director = by_role.get("director", {})
    writer = by_role.get("writer", {})

    # Living vs deceased, and where the living share first passes 50%.
    living = payload["scale"].get("living", 0)
    deceased = payload["scale"].get("deceased", 0)
    status_total = living + deceased
    living_share = (living / status_total) if status_total else 0.0
    crossover = next(
        (
            d
            for d in payload["decades"]
            if d["total"] > 0 and d.get("living", 0) > d.get("deceased", 0)
        ),
        None,
    )

    payload["scale"]["women"] = women
    payload["scale"]["men"] = men
    payload["scale"]["overall_female_share"] = overall_share
    payload["scale"]["behind_camera_female_share"] = behind_share
    payload["scale"]["living_share"] = living_share
    payload["headline"] = OrderedDict(
        actor_share=actor.get("female_share", 0.0),
        actor_n=actor.get("n", 0),
        behind_share=behind_share,
        behind_n=bt,
        ratio=(actor.get("female_share", 0.0) / behind_share) if behind_share else 0.0,
        director_share=director.get("female_share", 0.0),
        director_n=director.get("n", 0),
        writer_share=writer.get("female_share", 0.0),
        writer_n=writer.get("n", 0),
        overall_share=overall_share,
        living=living,
        deceased=deceased,
        living_share=living_share,
        crossover_decade=crossover.get("decade") if crossover else None,
        crossover_label=crossover.get("label") if crossover else None,
        crossover_living=crossover.get("living", 0) if crossover else 0,
        crossover_deceased=crossover.get("deceased", 0) if crossover else 0,
        peak_decade=peak.get("decade"),
        peak_label=peak.get("label"),
        peak_total=peak.get("total", 0),
        first_decade=payload["decades"][0]["decade"] if payload["decades"] else None,
        last_decade=payload["decades"][-1]["decade"] if payload["decades"] else None,
        takeoff=list(TAKEOFF_DECADES),
        censored=list(CENSORED_DECADES),
    )
    return payload


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _occ_label(occ: str) -> str:
    s = (occ or "").replace("_", " ").strip()
    return s[:1].upper() + s[1:] if s else s


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


def _k(value) -> str:
    if value >= 1000:
        return f"{value / 1000:.0f}k"
    return str(int(value))


def _pct(value: float | None, digits: int = 0) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.{digits}f}%"


# ---------------------------------------------------------------------------
# Hero: stacked area of the film workforce by birth decade (inline SVG)
# ---------------------------------------------------------------------------

HERO_W = 950
HERO_H = 620
PAD_L = 64
PAD_R = 18
PAD_T = 58
PAD_B = 54
PLOT_L = PAD_L
PLOT_R = HERO_W - PAD_R
PLOT_T = PAD_T
PLOT_B = HERO_H - PAD_B
Y_MAX = 21000.0
Y_TICKS = [0, 5000, 10000, 15000, 20000]
BG = "#f9f8f4"


def _decades(payload: dict) -> list[dict]:
    return payload["decades"]


def build_hero(payload: dict) -> str:
    decades = _decades(payload)
    n = len(decades)
    if n == 0:
        return ""

    def ex(i: int) -> float:
        return PLOT_L + (i / max(1, n - 1)) * (PLOT_R - PLOT_L)

    def ey(v: float) -> float:
        return PLOT_B - (v / Y_MAX) * (PLOT_B - PLOT_T)

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {HERO_W} {HERO_H}" '
        f'width="{HERO_W}" height="{HERO_H}" role="img" '
        f'aria-label="Film people by birth decade, split into living and deceased">'
    ]

    # --- gridlines + y ticks ------------------------------------------------
    for tick in Y_TICKS:
        yy = ey(tick)
        parts.append(f'<line x1="{PLOT_L}" y1="{yy:.1f}" x2="{PLOT_R}" y2="{yy:.1f}" class="grid"/>')
        parts.append(
            f'<text x="{PLOT_L - 9:.1f}" y="{yy + 3.6:.1f}" class="ytick" text-anchor="end">'
            f'{"0" if tick == 0 else _k(tick)}</text>'
        )

    # --- decade gridlines ---------------------------------------------------
    for i in range(n):
        parts.append(f'<line x1="{ex(i):.1f}" y1="{PLOT_T}" x2="{ex(i):.1f}" y2="{PLOT_B}" class="grid vgrid"/>')

    # --- right-censored zone (1980s onward) ---------------------------------
    cens_i = next((i for i, d in enumerate(decades) if d["decade"] in CENSORED_DECADES), None)
    if cens_i is not None:
        x0 = ex(cens_i)
        parts.append(
            f'<rect x="{x0:.1f}" y="{PLOT_T}" width="{PLOT_R - x0:.1f}" '
            f'height="{PLOT_B - PLOT_T}" class="censzone"/>'
        )
        parts.append(f'<line x1="{x0:.1f}" y1="{PLOT_T}" x2="{x0:.1f}" y2="{PLOT_B}" class="censline"/>')
        parts.append(
            f'<text x="{PLOT_R - 6:.1f}" y="{PLOT_T + 15:.1f}" class="censlabel" text-anchor="end">'
            f'1980s&ndash;2020s* still being written</text>'
        )

    # --- stacked bands: living (bottom) / deceased (top) --------------------
    tops: dict[str, list[float]] = {s: [] for s in STATUS_ORDER}
    for i, d in enumerate(decades):
        cum = 0.0
        for s in STATUS_ORDER:
            cum += d.get(s, 0)
            tops[s].append(cum)

    prev = [0.0] * n
    for status in STATUS_ORDER:
        top = tops[status]
        if max(top) <= 0:
            prev = top
            continue
        top_pts = [(ex(i), ey(top[i])) for i in range(n)]
        bot_pts = [(ex(i), ey(prev[i])) for i in range(n)]
        d = "M" + " L".join(f"{x:.1f},{yy:.1f}" for x, yy in top_pts)
        d += " L" + " L".join(f"{x:.1f},{yy:.1f}" for x, yy in reversed(bot_pts))
        d += " Z"
        parts.append(
            f'<path d="{d}" class="band" fill="{STATUS_COLOR[status]}">'
            f'<title>{_esc(STATUS_LABEL[status])}: film people by birth decade</title></path>'
        )
        prev = top

    # --- where the living first outnumber the deceased ----------------------
    head = payload["headline"]
    cross_i = next(
        (i for i, d in enumerate(decades) if d["decade"] == head.get("crossover_decade")),
        None,
    )
    if cross_i is not None:
        cx = ex(cross_i)
        cy = ey(decades[cross_i].get("living", 0))
        parts.append(f'<line x1="{cx:.1f}" y1="{PLOT_T}" x2="{cx:.1f}" y2="{PLOT_B}" class="crossline"/>')
        parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="3.2" class="crossdot"/>')
        parts.append(
            f'<text x="{cx:.1f}" y="{PLOT_T + 54:.1f}" class="callout" text-anchor="middle">'
            f'living outnumber the deceased</text>'
        )

    # --- take-off bracket over the cinema generation ------------------------
    t0 = next((i for i, d in enumerate(decades) if d["decade"] == TAKEOFF_DECADES[0]), None)
    t1 = next((i for i, d in enumerate(decades) if d["decade"] == TAKEOFF_DECADES[1]), None)
    if t0 is not None and t1 is not None:
        xa, xb = ex(t0), ex(t1)
        yb = PLOT_T + 26
        parts.append(
            f'<path d="M{xa:.1f},{yb + 8:.1f} L{xa:.1f},{yb:.1f} L{xb:.1f},{yb:.1f} L{xb:.1f},{yb + 8:.1f}" class="bracket"/>'
        )
        parts.append(
            f'<text x="{(xa + xb) / 2:.1f}" y="{yb - 8:.1f}" class="callout" text-anchor="middle">'
            f'the take-off</text>'
        )
        parts.append(
            f'<text x="{(xa + xb) / 2:.1f}" y="{yb - 22:.1f}" class="callout-sub" text-anchor="middle">'
            f'cinema&rsquo;s first generations are born, 1870s&ndash;1900s</text>'
        )

    # --- peak marker --------------------------------------------------------
    peak_i = next((i for i, d in enumerate(decades) if d["decade"] == head.get("peak_decade")), None)
    if peak_i is not None:
        px, py = ex(peak_i), ey(decades[peak_i]["total"])
        parts.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="3.4" class="peakdot"/>')
        parts.append(
            f'<text x="{px:.1f}" y="{py - 11:.1f}" class="callout" text-anchor="middle">'
            f'{_n(head.get("peak_total"))} born in the {_esc(head.get("peak_label") or "")}</text>'
        )

    # --- baseline + x labels ------------------------------------------------
    parts.append(f'<line x1="{PLOT_L}" y1="{PLOT_B}" x2="{PLOT_R}" y2="{PLOT_B}" class="baseline"/>')
    for i, d in enumerate(decades):
        if i % 2 == 0 or i == n - 1:
            cls = "xtick cens" if d["censored"] else "xtick"
            mark = "*" if d["censored"] else ""
            parts.append(
                f'<text x="{ex(i):.1f}" y="{PLOT_B + 19:.1f}" class="{cls}" text-anchor="middle">'
                f'{d["decade"]}s{mark}</text>'
            )

    parts.append(
        f'<text x="{PLOT_L:.1f}" y="{PLOT_T - 34:.1f}" class="axis">film people born in each decade</text>'
    )
    parts.append(
        f'<text x="{PLOT_R:.1f}" y="{PLOT_B + 40:.1f}" class="axis" text-anchor="end">birth decade</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Card HTML
# ---------------------------------------------------------------------------

HTML_HEAD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>HumansMap: a century of film people</title>
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
    padding:36px 42px 20px; display:flex; flex-direction:column; position:relative; overflow:hidden; }
  .card header { display:flex; justify-content:space-between; align-items:baseline; }
  .eyebrow { font:700 15px/1 var(--body); letter-spacing:.2em; text-transform:uppercase; color:var(--accent); }
  .brand { font:700 27px/1 var(--display); color:var(--accent); }
  h1 { font:800 43px/1.06 var(--display); margin:13px 0 0; letter-spacing:-.012em; max-width:42ch; }
  h1 em { font-style:normal; color:var(--accent); }
  .deck { font:400 15.5px/1.45 var(--body); color:var(--muted); margin:9px 0 0; max-width:1120px; }
  .deck b { color:var(--text); font-weight:700; }
  .body { display:flex; gap:30px; margin-top:12px; flex:1; min-height:0; }
  .hero { width:950px; flex:0 0 950px; display:flex; flex-direction:column; min-height:0; }
  .hero svg { display:block; width:950px; height:620px; overflow:visible; }
  .grid { stroke:rgba(28,21,16,.07); stroke-width:1; }
  .vgrid { stroke:rgba(28,21,16,.045); }
  .baseline { stroke:rgba(28,21,16,.30); stroke-width:1.2; }
  .ytick { font:500 11px/1 var(--body); fill:var(--muted); font-variant-numeric:tabular-nums; }
  .xtick { font:600 11px/1 var(--body); fill:var(--text); }
  .xtick.cens { fill:#b3aca0; }
  .axis { font:600 11px/1 var(--body); fill:var(--muted); letter-spacing:.02em; }
  .band { stroke:#f9f8f4; stroke-width:1.2; stroke-linejoin:round; }
  .censzone { fill:rgba(28,21,16,.05); }
  .censline { stroke:rgba(28,21,16,.28); stroke-width:1; stroke-dasharray:3 4; }
  .censlabel { font:700 10.5px/1 var(--body); fill:var(--muted);
    paint-order:stroke; stroke:#f9f8f4; stroke-width:3px; stroke-linejoin:round; }
  .callout { font:700 12.5px/1 var(--body); fill:var(--text);
    paint-order:stroke; stroke:#f9f8f4; stroke-width:3.2px; stroke-linejoin:round; }
  .callout-sub { font:400 11px/1 var(--body); fill:var(--muted);
    paint-order:stroke; stroke:#f9f8f4; stroke-width:3px; stroke-linejoin:round; }
  .bracket { stroke:rgba(28,21,16,.40); stroke-width:1.2; fill:none; }
  .peakdot { fill:none; stroke:var(--text); stroke-width:1.5; }
  .crossline { stroke:rgba(28,21,16,.30); stroke-width:1; stroke-dasharray:2 4; }
  .crossdot { fill:#f9f8f4; stroke:var(--text); stroke-width:1.5; }
  .stats { display:flex; align-items:center; gap:20px; margin-top:11px; flex-wrap:wrap;
    font:500 13.5px/1 var(--body); color:var(--text); }
  .stats .item { display:flex; align-items:center; gap:7px; }
  .stats .sw { width:15px; height:11px; border-radius:3px; }
  .stats .item b { font-weight:700; }
  .stats .item em { font-style:normal; color:var(--muted); font-weight:400; }
  .stats .note { color:var(--muted); font-weight:400; }
  .stats .note b { color:var(--text); font-weight:700; }
  .caption { font:400 11.3px/1.45 var(--body); color:var(--muted); margin-top:7px; max-width:940px; }
  .caption b { color:var(--text); font-weight:700; }
  .ruleline { font:400 11.3px/1.4 var(--body); color:var(--muted); margin-top:5px; max-width:940px; }
  .ruleline b { color:var(--text); font-weight:700; }
  .panel { flex:1; min-width:0; display:flex; flex-direction:column; }
  .block { border-top:2px solid var(--text); padding-top:8px; }
  .block + .block { margin-top:13px; }
  .plabel { font:700 11px/1 var(--body); letter-spacing:.17em; text-transform:uppercase; color:var(--muted); }
  .gap { margin-top:8px; }
  .gaprow { display:grid; grid-template-columns:138px 1fr 52px; gap:12px; align-items:center; padding:5px 0; }
  .gname { font:600 14.5px/1.05 var(--body); }
  .gname em { display:block; font:400 10.3px/1.35 var(--body); font-style:normal; color:var(--muted); }
  .gtrack { position:relative; height:13px; background:var(--surface); border-radius:6px; }
  .gfill { position:absolute; left:0; top:0; height:100%; border-radius:6px; }
  .gbase { position:absolute; top:-3px; bottom:-3px; width:2px; background:var(--text); opacity:.55; }
  .gval { font:800 20px/1 var(--display); text-align:right; }
  .gapnote { font:400 11.3px/1.4 var(--body); color:var(--muted); margin-top:7px; }
  .gapnote b { color:var(--text); }
  .comp { display:flex; height:19px; border-radius:5px; overflow:hidden; margin-top:9px; }
  .comp i { height:100%; }
  .complegend { display:flex; flex-wrap:wrap; gap:4px 14px; margin-top:8px;
    font:400 11px/1.3 var(--body); color:var(--muted); }
  .complegend span { display:flex; align-items:center; gap:5px; }
  .complegend i { width:11px; height:9px; border-radius:2px; }
  .complegend b { color:var(--text); font-weight:700; }
  ul.geo { list-style:none; display:grid; grid-template-columns:1fr 1fr; gap:0 24px;
    margin:8px 0 0; padding:0; }
  ul.geo li { display:flex; justify-content:space-between; align-items:baseline;
    padding:3.5px 0; border-bottom:1px solid var(--line); font:400 12.5px/1.2 var(--body); }
  ul.geo .cn { color:var(--text); font-weight:600; }
  ul.geo .cv { color:var(--muted); font-variant-numeric:tabular-nums; }
  .geonote { font:400 11px/1.4 var(--body); color:var(--muted); margin-top:7px; }
  .caveat { font:400 11.2px/1.42 var(--body); color:var(--muted); margin-top:auto; padding-top:11px; }
  .caveat b { color:var(--text); }
  .card footer { margin-top:auto; padding-top:9px; display:flex; justify-content:space-between;
    align-items:baseline; font:400 13px/1.4 var(--body); color:var(--muted); }
  .card footer b { color:var(--text); font-weight:700; }
</style></head>
<body>"""


def _gap_block(payload: dict) -> tuple[str, str]:
    by_role = {r["role"]: r for r in payload["roles"]}
    overall = payload["scale"]["overall_female_share"]
    scale_max = 0.50
    rows = []
    for role in payload["gap_order"]:
        r = by_role[role]
        share = r["female_share"]
        fill = min(100.0, max(0.0, share / scale_max * 100))
        base = min(100.0, max(0.0, overall / scale_max * 100))
        rows.append(
            f'<div class="gaprow">'
            f'<span class="gname">{_esc(ROLE_LABEL[role])}<em>{ROLE_NOTE[role]}</em></span>'
            f'<span class="gtrack"><span class="gfill" style="width:{fill:.1f}%;background:{ROLE_COLOR[role]}"></span>'
            f'<span class="gbase" style="left:{base:.1f}%"></span></span>'
            f'<span class="gval">{_pct(share)}</span>'
            f"</div>"
        )
    note = (
        f'Acting sits near parity; the three behind-the-camera roles cluster at one in five. '
        f'The black tick is the <b>{_pct(overall)}</b> women&rsquo;s share across the '
        f'<b>{_n(payload["scale"]["women"] + payload["scale"]["men"])}</b> film people with a '
        f'recorded binary gender; bars are each role&rsquo;s share, '
        f'scaled to 0&ndash;50%.'
    )
    return "".join(rows), note


def _comp_block(payload: dict) -> str:
    by_role = {r["role"]: r for r in payload["roles"]}
    segs, keys = [], []
    for role in payload["comp_order"]:
        r = by_role[role]
        w = r["overall_share"] * 100
        segs.append(
            f'<i style="width:{w:.2f}%;background:{ROLE_COLOR[role]}" '
            f'title="{_esc(ROLE_LABEL[role])}: {_n(r["n"])} people"></i>'
        )
        keys.append(
            f'<span><i style="background:{ROLE_COLOR[role]}"></i>{_esc(ROLE_LABEL[role])} '
            f'<b>{_n(r["n"])}</b></span>'
        )
    return f'<div class="comp">{"".join(segs)}</div><div class="complegend">{"".join(keys)}</div>'


def _geo_block(payload: dict) -> str:
    total = payload["scale"]["people"] or 1
    items = []
    for c in payload["top_countries"]:
        items.append(
            f'<li><span class="cn">{_esc(c["label"])}</span>'
            f'<span class="cv">{c["n"] / total * 100:.0f}% &middot; {_n(c["n"])}</span></li>'
        )
    return f'<ul class="geo">{"".join(items)}</ul>'


def render_html(payload: dict) -> str:
    scale = payload["scale"]
    head = payload["headline"]
    hero = build_hero(payload)
    gap_rows, gap_note = _gap_block(payload)

    # Header stat strip — doubles as the legend for the hero's two data colours.
    living = scale.get("living", 0)
    deceased = scale.get("deceased", 0)
    living_share = scale.get("living_share", 0.0)
    stats = (
        f'<div class="stats">'
        f'<span class="item"><i class="sw" style="background:{STATUS_COLOR["living"]}"></i>'
        f'<b>{_n(living)}</b> living <em>({_pct(living_share)})</em></span>'
        f'<span class="item"><i class="sw" style="background:{STATUS_COLOR["deceased"]}"></i>'
        f'<b>{_n(deceased)}</b> deceased</span>'
        f'<span class="note">of <b>{_n(scale["people"])}</b> film people born 1800&ndash;2020s</span>'
        f"</div>"
    )

    big = (
        f'<em>{_pct(head.get("actor_share"))}</em> of film actors are women. '
        f'Behind the camera, <em>{_pct(head.get("behind_share"))}</em>.'
    )

    deck = (
        f"Every well-documented film person with a birth date &mdash; <b>no filter on death date</b>: "
        f"<b>{_n(scale['people'])}</b> people born 1800&ndash;2020s from "
        f"<b>{_n(scale['occupation_records'])}</b> film-occupation records. "
        f"<b>{_n(living)} ({_pct(living_share)}) are still alive</b>; a birth decade is a proxy "
        f"for career era, so recent cohorts are still being written, not missing."
    )

    rule = (
        "<b>One role per person.</b> Where someone held several film jobs, they are counted "
        "once, in the highest-priority family: "
        "<b>director &rsaquo; screenwriter &rsaquo; producer &rsaquo; actor &rsaquo; presenter</b>. "
        "&ldquo;Actor&rdquo; covers actor, film, television and voice actors; &ldquo;presenter&rdquo; "
        "also folds in theatre directors."
    )

    caveat = (
        "<b>Read with care.</b> Wikidata records film occupations unevenly: coverage skews to "
        "the US, Europe and Japan (the project&rsquo;s own Italy included), so this is the "
        "<em>documented</em> workforce, not the real one. There is no death-date filter &mdash; "
        f"<b>{_n(living)} ({_pct(living_share)}) of these people are still alive</b> &mdash; and a "
        "birth decade is a proxy for career era, not a date. The 1980s&ndash;2020s cohorts are "
        "<b>right-censored</b> (*): born too recently to be fully recorded, so they are drawn but "
        "excluded from the trend. "
        f"Gender is recorded as binary male/female; {_n(payload['coverage_note']['excluded_other_gender'])} "
        "people with another value are left out of every share, so non-binary identities are not "
        "represented."
    )

    caption = (
        "Each birth cohort is split by whether Wikidata records a death date. A birth decade is "
        "not a death date: recent cohorts are small because they are <b>still being written</b>, "
        "not because they are missing."
    )

    geo_note = (
        "Share of film people whose first recorded nationality is that country: "
        "a map of the public record, not of film."
    )

    card = f"""<section class="card">
  <header>
    <span class="eyebrow">A century of film people</span>
    <span class="brand">Humans&nbsp;Map</span>
  </header>
  <h1>{big}</h1>
  <p class="deck">{deck}</p>
  {stats}
  <div class="body">
    <div class="hero">
      {hero}
      <div class="caption">{caption}</div>
    </div>
    <aside class="panel">
      <div class="block">
        <div class="plabel">Women&rsquo;s share, by role</div>
        <div class="gap">{gap_rows}</div>
        <div class="gapnote">{gap_note}</div>
      </div>
      <div class="block">
        <div class="plabel">The film workforce</div>
        {_comp_block(payload)}
        <div class="ruleline">{rule}</div>
      </div>
      <div class="block">
        <div class="plabel">Where they&rsquo;re recorded</div>
        {_geo_block(payload)}
        <div class="geonote">{geo_note}</div>
      </div>
      <div class="caveat">{caveat}</div>
    </aside>
  </div>
  <footer>
    <span>Data: Wikidata (CC BY-SA) &middot; humansmap.com</span>
    <span><b>{_n(scale['people'])}</b> film people &middot; <b>{_n(living)}</b> living &middot; <b>{_n(scale['occupation_records'])}</b> film-occupation records</span>
  </footer>
</section>"""

    return HTML_HEAD + card + "\n</body></html>\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default="docs/marketing/film")
    parser.add_argument("--min-statements", type=int, default=DEFAULT_MIN_STATEMENTS)
    parser.add_argument("--year-min", type=int, default=DEFAULT_YEAR_MIN)
    parser.add_argument("--year-max", type=int, default=DEFAULT_YEAR_MAX)
    parser.add_argument("--scan-limit", type=int, default=DEFAULT_SCAN_LIMIT)
    parser.add_argument("--no-html", action="store_true")
    parser.add_argument(
        "--from-json", default=None,
        help="re-render from an existing film.json instead of querying the DB",
    )
    args = parser.parse_args()

    if args.from_json:
        payload = json.loads(Path(args.from_json).read_text())
        print(f"[cache] loaded {args.from_json} (no DB access)")
        params = payload.setdefault("params", {})
        params["decade_min"] = DECADE_MIN
        params["decade_max"] = DECADE_MAX
        params["censored_decades"] = list(CENSORED_DECADES)
        params["takeoff_decades"] = list(TAKEOFF_DECADES)
        params["behind_camera"] = list(BEHIND_CAMERA)
        derive(payload)
    else:
        payload = collect(args.min_statements, args.year_min, args.year_max, args.scan_limit)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # Always (re)write the payload: derive() is a pure function of the cached
    # cells, so the JSON stays in step with the renderer on --from-json too.
    json_path = out_dir / "film.json"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {json_path}")

    if not args.no_html:
        html_path = out_dir / "film.html"
        html_path.write_text(render_html(payload))
        print(f"wrote {html_path}")

    head = payload["headline"]
    summary = {
        "people": payload["scale"]["people"],
        "living": payload["scale"].get("living"),
        "deceased": payload["scale"].get("deceased"),
        "living_share": _pct(payload["scale"].get("living_share")),
        "crossover": head.get("crossover_label"),
        "occupation_records": payload["scale"]["occupation_records"],
        "occupations": payload["scale"]["distinct_occupations"],
        "peak": f"{head.get('peak_label')} ({_n(head.get('peak_total'))})",
        "actor_share": _pct(head.get("actor_share")),
        "behind_camera_share": _pct(head.get("behind_share")),
        "director_share": _pct(head.get("director_share")),
        "writer_share": _pct(head.get("writer_share")),
        "overall_share": _pct(head.get("overall_share")),
        "roles": [
            {"role": r["label"], "n": r["n"], "women_share": _pct(r["female_share"])}
            for r in sorted(payload["roles"], key=lambda r: -r["female_share"])
        ],
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
