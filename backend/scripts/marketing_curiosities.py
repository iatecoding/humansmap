#!/usr/bin/env python3
"""Marketing curiosities miner — read-only, TWO light curiosity cards.

Card 1 — *The polymaths*: the people with the most distinct occupations
recorded in Wikidata, plus a rarity histogram of how uncommon that is.

Card 2 — *The biggest age gaps in married couples*: spouse pairs ranked by the
difference in birth years, with both lifespans shown.

Both are Wikidata-only (no SEC). It never writes to ArangoDB.

Cost / bounding
---------------
* Card 1 is bounded by the persistent index on ``wikidata_statements_count``
  (``--min-statements``, default 50). With the floor it scans ~247k people; the
  rarity histogram and the ranked list are two cheap passes over that set.
* Card 2 is bounded by the hash index on ``relationships.type``; it walks only
  the ~846k ``spouse`` edges (``--scan-limit`` is a hard 1,000,000 ceiling that
  does not bite). Each edge joins both endpoint persons via the primary index,
  so the single pass takes roughly 1.5 minutes on the live host. Spouse edges
  are stored symmetrically, so unordered pairs are de-duplicated in Python;
  both birth dates are required for a couple to count.

Outputs (all four written by default):

  polymaths.json / polymaths.html
  agegap.json    / agegap.html

The HTML is a brand-styled single-card sheet (1080x1620) to screenshot to PNG
with ``scripts/marketing/render-insight-cards.js`` (rename ``card-01.png``).

Run it manually inside the backend container:

    docker compose exec -T backend python - --out-dir /tmp/curiosities \\
        < backend/scripts/marketing_curiosities.py
    docker compose exec -T backend cat /tmp/curiosities/polymaths.json \\
        > docs/marketing/curiosities/polymaths.json
    ...

Flags:
    --out-dir PATH       output directory (default: docs/marketing/curiosities)
    --min-statements N   Wikidata-statement floor for the polymath card (default 50)
    --poly-top N         people shown on the polymath card (default 15)
    --agegap-top N       couples shown on the age-gap card (default 15)
    --scan-limit N       hard ceiling on spouse edges walked (default 1000000)
    --min-gap N          smallest birth-year gap worth ranking (default 0)
    --from-json PATH     re-render both HTML files from an existing JSON pair
    --no-html            emit only the JSON payloads
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
DEFAULT_POLY_TOP = 15
DEFAULT_AGEGAP_TOP = 15
DEFAULT_SCAN_LIMIT = 1_000_000
DEFAULT_MAX_GAP = 80  # humanly plausible ceiling, rejects placeholder/mislabelled dates
DEFAULT_AGEGAP_MIN_STATEMENTS = 30  # documentation floor for both partners

# Bins for the "how rare is this?" histogram on the polymath card.
POLY_BINS = [16, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2]
# Gap bins (years) for the age-gap distribution.
GAP_BINS = [(0, 4), (5, 9), (10, 14), (15, 19), (20, 24), (25, 29), (30, 34), (35, 39), (40, 10_000)]


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

POLY_HIST_AQL = """
FOR p IN persons
  FILTER p.wikidata_statements_count >= @min_statements
    AND LENGTH(p.occupations) >= 2
  LET occ = UNIQUE(p.occupations)
  COLLECT n = LENGTH(occ) WITH COUNT INTO people
  SORT n DESC
  RETURN {occupations: n, people: people}
"""

POLY_TOP_AQL = """
FOR p IN persons
  FILTER p.wikidata_statements_count >= @min_statements
    AND LENGTH(p.occupations) >= 3
  LET occ = UNIQUE(p.occupations)
  LET fow = UNIQUE(NOT_NULL(p.field_of_work, []))
  SORT LENGTH(occ) DESC, p.wikidata_statements_count DESC, p.name_en ASC
  LIMIT @top
  RETURN {
    qid: p.wikidata_id,
    name: p.name_en,
    occupations: occ,
    field_of_work: fow,
    statements: p.wikidata_statements_count,
    nationalities: p.nationalities,
    birth_date: p.birth_date,
    death_date: p.death_date
  }
"""

# One bounded pass over the spouse edges. LIMIT sits after the type filter and
# is a safety ceiling only (846k edges today, so it never bites).
AGEGAP_AQL = """
FOR r IN relationships
  FILTER r.type == 'spouse'
  LIMIT @scan_limit
  LET a = DOCUMENT(r._from)
  LET b = DOCUMENT(r._to)
  FILTER a != null AND b != null
    AND a.birth_date != null AND b.birth_date != null
  LET ay = TO_NUMBER(SUBSTRING(a.birth_date, 0, 4))
  LET by = TO_NUMBER(SUBSTRING(b.birth_date, 0, 4))
  FILTER ay > 0 AND by > 0
  LET ad = a.death_date != null ? TO_NUMBER(SUBSTRING(a.death_date, 0, 4)) : null
  LET bd = b.death_date != null ? TO_NUMBER(SUBSTRING(b.death_date, 0, 4)) : null
  LET amn = LENGTH(a.birth_date) >= 7 ? TO_NUMBER(SUBSTRING(a.birth_date, 5, 2)) : 0
  LET adn = LENGTH(a.birth_date) >= 10 ? TO_NUMBER(SUBSTRING(a.birth_date, 8, 2)) : 0
  LET bmn = LENGTH(b.birth_date) >= 7 ? TO_NUMBER(SUBSTRING(b.birth_date, 5, 2)) : 0
  LET bdn = LENGTH(b.birth_date) >= 10 ? TO_NUMBER(SUBSTRING(b.birth_date, 8, 2)) : 0
  LET sd = r.start_date != null ? TO_NUMBER(SUBSTRING(r.start_date, 0, 4)) : null
  RETURN {a: PARSE_IDENTIFIER(r._from).key, b: PARSE_IDENTIFIER(r._to).key,
          ay: ay, by: by,
          ad: ad != null AND ad > 0 ? ad : null,
          bd: bd != null AND bd > 0 ? bd : null,
          am: amn != null ? amn : 0, aday: adn != null ? adn : 0,
          bm: bmn != null ? bmn : 0, bday: bdn != null ? bdn : 0,
          sd: sd != null AND sd > 0 ? sd : null,
          ast: a.wikidata_statements_count,
          bst: b.wikidata_statements_count}
"""

PERSON_DETAIL_AQL = """
FOR p IN persons
  FILTER p._key IN @keys
  RETURN {
    key: p._key,
    qid: p.wikidata_id,
    name: p.name_en,
    birth_date: p.birth_date,
    death_date: p.death_date,
    nationalities: p.nationalities
  }
"""


def _rows(db, aql: str, bind: dict | None = None) -> list:
    try:
        return list(db.aql.execute(aql, bind_vars=bind or {}))
    except Exception as exc:  # noqa: BLE001 - a marketing job must not crash
        print(f"[warn] query failed: {exc}", file=sys.stderr)
        return []


# ---------------------------------------------------------------------------
# Polymaths
# ---------------------------------------------------------------------------


def collect_polymaths(db, min_statements: int, top: int) -> dict:
    hist_rows = _rows(db, POLY_HIST_AQL, {"min_statements": min_statements})
    top_rows = _rows(db, POLY_TOP_AQL, {"min_statements": min_statements, "top": top})

    histogram = []
    people_two_plus = 0
    people_3_plus = 0
    for r in hist_rows:
        n = int(r.get("people") or 0)
        occ = int(r.get("occupations") or 0)
        people_two_plus += n
        if occ >= 3:
            people_3_plus += n
        histogram.append(OrderedDict(occupations=occ, people=n))

    people = []
    for r in top_rows:
        if not r.get("name"):
            continue
        people.append(
            OrderedDict(
                qid=r.get("qid"),
                name=r.get("name"),
                occupations=list(r.get("occupations") or []),
                field_of_work=list(r.get("field_of_work") or []),
                statements=r.get("statements"),
                nationalities=list(r.get("nationalities") or []),
                birth_date=r.get("birth_date"),
                death_date=r.get("death_date"),
            )
        )

    max_occ = len(people[0]["occupations"]) if people else 0
    ge13 = sum(h["people"] for h in histogram if h["occupations"] >= 13)
    return {
        "generated": date.today().isoformat(),
        "params": {"min_statements": min_statements, "top": top},
        "scale": {
            "people_two_plus": people_two_plus,
            "people_three_plus": people_3_plus,
            "people_13_plus": ge13,
            "max_occupations": max_occ,
        },
        "histogram": histogram,
        "people": people,
    }


# ---------------------------------------------------------------------------
# Age gaps
# ---------------------------------------------------------------------------


def fetch_agegap_pairs(db, scan_limit: int) -> list:
    """One bounded pass over spouse edges; returns raw deduped candidate rows."""
    raw = _rows(db, AGEGAP_AQL, {"scan_limit": scan_limit})
    pairs: dict[tuple[str, str], dict] = {}
    for r in raw:
        a, b = r.get("a"), r.get("b")
        ay, by = r.get("ay"), r.get("by")
        if not a or not b or ay is None or by is None or a == b:
            continue
        # Canonicalise the pair, and keep every paired value with its own person
        # (years, dates, statements). Sorting only the keys but not the values
        # mismatched names with birth years for half of all couples.
        if a < b:
            (ka, kb, kay, kby, kad, kbd, kam, kaday, kbm, kbday, kast, kbst) = (
                a, b, ay, by, r.get("ad"), r.get("bd"), r.get("am"), r.get("aday"),
                r.get("bm"), r.get("bday"), r.get("ast"), r.get("bst"))
        else:
            (ka, kb, kay, kby, kad, kbd, kam, kaday, kbm, kbday, kast, kbst) = (
                b, a, by, ay, r.get("bd"), r.get("ad"), r.get("bm"), r.get("bday"),
                r.get("am"), r.get("aday"), r.get("bst"), r.get("ast"))
        key = (ka, kb)
        if key not in pairs:
            pairs[key] = OrderedDict(
                a=ka, b=kb, ay=int(kay), by=int(kby),
                ad=kad, bd=kbd,
                am=int(kam or 0), aday=int(kaday or 0),
                bm=int(kbm or 0), bday=int(kbday or 0),
                sd=r.get("sd"),
                ast=kast or 0, bst=kbst or 0,
            )
    return list(pairs.values())


def load_raw_pairs(path: Path) -> list:
    return json.loads(path.read_text())


def dump_raw_pairs(rows: list, path: Path) -> None:
    path.write_text(json.dumps(rows, ensure_ascii=False) + "\n")


def _valid_lifespan(birth: int, death) -> bool:
    """Reject a recorded death that precedes the birth (placeholder dates)."""
    return death is None or int(death) >= birth


def _keep_couple(row: dict, max_gap: int, require_month: bool = True,
                 require_day: bool = False, min_statements: int = 0,
                 drop_jan1: bool = False, require_marriage_date: bool = False) -> bool:
    """Sanity gate for the age-gap ranking.

    Wikidata contains placeholder and mis-linked records for historical people
    (``0500-00-00``, a 14th-century queen wed to a 20th-century man, spouses who
    died before the other was born, birth years rounded to the nearest 50, and
    ``YYYY-01-01`` placeholder days). The raw maximum is therefore a bug list.
    A couple is kept only when the record is internally plausible:

    * both birth dates carry a month (rejects century-rounded years);
    * optionally, both carry a full day (rejects month-only approximations);
    * optionally, both people clear a Wikidata-statement floor (well documented);
    * optionally, neither date is a bare ``January 1`` placeholder;
    * optionally, the marriage itself has a recorded year;
    * both birth years are positive;
    * neither recorded death precedes its own birth;
    * the two lifespans could overlap (both partners were alive together);
    * when a marriage year exists, both partners were alive to marry in it;
    * if neither death date is recorded, both are recent (born since 1900);
    * the gap is within a humanly plausible ceiling (``max_gap``).
    """
    ay, by = row["ay"], row["by"]
    ad, bd = row.get("ad"), row.get("bd")
    sd = row.get("sd")
    am, aday = int(row.get("am") or 0), int(row.get("aday") or 0)
    bm, bday = int(row.get("bm") or 0), int(row.get("bday") or 0)
    if require_month and not (am > 0 and bm > 0):
        return False
    if require_day and not (am > 0 and aday > 0 and bm > 0 and bday > 0):
        return False
    if min_statements and min(int(row.get("ast") or 0), int(row.get("bst") or 0)) < min_statements:
        return False
    if drop_jan1 and ((am == 1 and aday == 1) or (bm == 1 and bday == 1)):
        return False
    if require_marriage_date and sd is None:
        return False
    if not _valid_lifespan(ay, ad) or not _valid_lifespan(by, bd):
        return False
    older_y, younger_y = (ay, by) if ay <= by else (by, ay)
    older_d, younger_d = (ad, bd) if ay <= by else (bd, ad)
    # Both partners must have been alive at the same time.
    if older_d is not None and younger_y > older_d:
        return False
    if younger_d is not None and older_y > younger_d:
        return False
    # A recorded marriage year must be after both births and before either death.
    if sd is not None:
        if sd < younger_y:
            return False
        death_years = [x for x in (older_d, younger_d) if x is not None]
        if death_years and sd > min(death_years) + 1:
            return False
    # No recorded lifespan at all -> only trust modern records.
    if older_d is None and younger_d is None and (older_y < 1900 or younger_y < 1900):
        return False
    return (younger_y - older_y) <= max_gap


def collect_agegaps(db, rows: list | None, top: int, scan_limit: int,
                    min_gap: int, max_gap: int, require_month: bool = True,
                    require_day: bool = False, min_statements: int = 0,
                    drop_jan1: bool = False, require_marriage_date: bool = False) -> dict:
    if rows is None:
        rows = fetch_agegap_pairs(db, scan_limit)
    raw_count = len(rows)

    kept = [
        r for r in rows
        if _keep_couple(r, max_gap, require_month, require_day, min_statements,
                        drop_jan1, require_marriage_date)
    ]
    gaps = [abs(r["ay"] - r["by"]) for r in kept]
    total = len(gaps)

    buckets = OrderedDict()
    for lo, hi in GAP_BINS:
        label = f"{lo}\u2013{hi}" if hi < 100 else f"{lo}+"
        buckets[label] = 0
    for g in gaps:
        for lo, hi in GAP_BINS:
            if lo <= g <= hi:
                label = f"{lo}\u2013{hi}" if hi < 100 else f"{lo}+"
                buckets[label] += 1
                break

    ranked = sorted(kept, key=lambda r: abs(r["ay"] - r["by"]), reverse=True)
    ranked = [r for r in ranked if abs(r["ay"] - r["by"]) >= min_gap][:top]

    detail_keys = sorted({k for r in ranked for k in (r["a"], r["b"])})
    details = {d.get("key"): d for d in _rows(db, PERSON_DETAIL_AQL, {"keys": detail_keys})}

    couples = []
    for r in ranked:
        ay, by = r["ay"], r["by"]
        # "older" = born earlier (smaller year); "younger" = born later.
        older_key, younger_key = (r["a"], r["b"]) if ay <= by else (r["b"], r["a"])
        older_y, younger_y = (ay, by) if ay <= by else (by, ay)
        older, younger = details.get(older_key, {}), details.get(younger_key, {})

        def _person(key: str, y: int, d: dict) -> OrderedDict:
            return OrderedDict(
                qid=d.get("qid") or key,
                name=d.get("name") or key,
                birth_year=y,
                birth_date=d.get("birth_date"),
                death_date=d.get("death_date"),
                nationalities=list(d.get("nationalities") or []),
            )

        couples.append(
            OrderedDict(
                gap=younger_y - older_y,
                older=_person(older_key, older_y, older),
                younger=_person(younger_key, younger_y, younger),
            )
        )

    ordered = sorted(gaps)
    median = ordered[len(ordered) // 2] if ordered else 0
    mean = (sum(ordered) / len(ordered)) if ordered else 0.0
    return {
        "generated": date.today().isoformat(),
        "params": {
            "top": top, "scan_limit": scan_limit,
            "min_gap": min_gap, "max_gap": max_gap,
            "require_month": require_month, "require_day": require_day,
            "min_statements": min_statements, "drop_jan1": drop_jan1,
            "require_marriage_date": require_marriage_date,
        },
        "scale": {
            "spouse_edges_scanned": raw_count,
            "couples_considered": total,
            "median_gap": median,
            "mean_gap": mean,
            "gaps_ge_10": sum(1 for g in gaps if g >= 10),
            "gaps_ge_20": sum(1 for g in gaps if g >= 20),
            "gaps_ge_30": sum(1 for g in gaps if g >= 30),
            "gaps_ge_40": sum(1 for g in gaps if g >= 40),
            "largest_gap": ordered[-1] if ordered else 0,
        },
        "histogram": [OrderedDict(bucket=k, couples=v) for k, v in buckets.items()],
        "couples": couples,
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
        .replace('"', "&quot;")
    )


def _n(value) -> str:
    try:
        return f"{int(round(float(value))):,}"
    except (TypeError, ValueError):
        return "0"


def _label(occ: str) -> str:
    s = (occ or "").replace("_", " ").strip()
    return s[:1].upper() + s[1:] if s else s


def _years(person: dict) -> str:
    b = person.get("birth_year")
    d = person.get("death_date")
    dyear = ""
    if d:
        dyear = str(d)[:4]
        if d and str(d)[0] == "-":
            dyear = str(d)[:5]
    if dyear:
        return f"{b}\u2013{dyear}"
    return f"b. {b}"


def _short(name: str, limit: int = 30) -> str:
    """Trim a name at a word boundary so it never breaks mid-word."""
    name = (name or "").strip()
    if len(name) <= limit:
        return name
    cut = name[:limit].rsplit(" ", 1)[0].rstrip(",;")
    if len(cut) < limit * 0.5:
        cut = name[: limit - 1].rstrip()
    return cut + "\u2026"


HTML_HEAD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>HumansMap: __TITLE__</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;800&family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:#f9f8f4; --text:#1c1510;
    --chrome:#1b4d8e;          /* chrome only: wordmark, eyebrow, headline emphasis */
    --data:#bf6b28;            /* data only: the measured quantity */
    --structure:#8a8278;       /* structure: grids, rules, secondary labels */
    --surface:#ece8de; --line:rgba(28,21,16,.10);
    --display:'Playfair Display', Georgia, serif; --body:'DM Sans', system-ui, sans-serif;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:#d9d4c9; font-family:var(--body); padding:32px; }
  .card { width:1080px; height:1620px; background:var(--bg); color:var(--text);
    padding:58px 62px 38px; display:flex; flex-direction:column; position:relative;
    overflow:hidden; }
  .card header { display:flex; justify-content:space-between; align-items:baseline; }
  .eyebrow { font:700 18px/1 var(--body); letter-spacing:.2em; text-transform:uppercase;
    color:var(--chrome); }
  .brand { font:700 27px/1 var(--display); color:var(--chrome); }
  h1 { font:800 47px/1.07 var(--display); margin:18px 0 0; letter-spacing:-.012em;
    max-width:30ch; }
  h1 em { font-style:normal; color:var(--chrome); }
  .deck { font:400 16px/1.42 var(--body); color:var(--structure); margin:10px 0 0;
    max-width:56ch; }
  .deck b { color:var(--text); font-weight:700; }
  .stats3 { display:grid; grid-template-columns:repeat(3,1fr); margin:10px 0 0;
    border-top:2px solid var(--text); border-bottom:1px solid var(--line); }
  .stats3 > div { padding:8px 16px 7px 0; }
  .stats3 > div + div { border-left:1px solid var(--line); padding-left:16px; }
  .stats3 .v { font:800 33px/1 var(--display); color:var(--data); }
  .stats3 .k { font:500 10.5px/1.35 var(--body); color:var(--structure);
    text-transform:uppercase; letter-spacing:.08em; margin-top:6px; }
  .pgrid { list-style:none; padding:0; display:grid; grid-template-columns:1fr 1fr;
    gap:0 30px; margin:2px 0 0; }
  .prow { padding:4px 0 3px; border-bottom:1px solid var(--line); }
  .who { min-width:0; }
  .name { display:flex; align-items:baseline; gap:7px; }
  .name .rank { font:700 12px/1.2 var(--body); color:var(--structure); }
  .name b { font:700 17px/1.1 var(--body); }
  .name .nat { font:500 10.5px/1.3 var(--body); color:var(--structure); }
  .name .count { margin-left:auto; font:500 10.5px/1.3 var(--body); color:var(--structure);
    white-space:nowrap; }
  .name .count b { font:800 18px/1 var(--display); color:var(--data); margin-right:3px; }
  .chips { display:flex; flex-wrap:wrap; gap:3px; margin-top:3px; }
  .chip { font:500 10px/1; color:var(--text); background:var(--surface);
    border:1px solid var(--line); border-radius:10px; padding:2px 6px;
    white-space:nowrap; }
  .tl-name { font:700 15px/1 var(--body); fill:var(--text); }
  .tl-year { font:500 12.5px/1 var(--body); fill:var(--structure); }
  .tl-rank { font:700 15px/1 var(--body); fill:var(--structure); }
  .tl-gap { font:800 22px/1 var(--display); fill:var(--data); }
  .tl-gaplab { font:500 9.5px/1 var(--body); fill:var(--structure);
    text-transform:uppercase; letter-spacing:.08em; }
  .tl-tick { font:500 11px/1 var(--body); fill:var(--structure); }
  .legend { display:flex; flex-wrap:wrap; gap:10px 26px; align-items:center;
    margin-top:16px; font:400 12.5px/1.3 var(--body); color:var(--structure); }
  .legend span { display:flex; align-items:center; gap:8px; }
  .legend i { display:inline-block; flex:0 0 auto; }
  .note { margin-top:14px; padding-top:11px; border-top:1px solid var(--line);
    font:400 13px/1.42 var(--body); color:var(--structure); }
  .note b { color:var(--text); }
  .card footer { margin-top:auto; padding-top:12px; display:flex; justify-content:space-between;
    align-items:baseline; font:400 13px/1.4 var(--body); color:var(--structure); }
  .card footer b { color:var(--text); font-weight:700; }
  .barwrap { position:relative; height:100%; }
</style></head>
<body>"""


# ---------------------------------------------------------------------------
# Card 1 — the polymaths
# ---------------------------------------------------------------------------


def render_polymaths_html(payload: dict) -> str:
    scale = payload["scale"]
    people = payload["people"]
    top = people[0] if people else {}
    top_n = len(top.get("occupations") or [])
    top_name = top.get("name") or "One person"

    h1 = (
        f"<em>{top_n} occupations</em> in one life: the most "
        f"varied careers on record."
    )
    deck = (
        f"Among <b>{_n(scale['people_two_plus'])}</b> people credited with "
        f"more than one occupation, only "
        f"<b>{_n(scale['people_13_plus'])}</b> are recorded with <b>13 or more</b>. "
        f"Their occupations are listed as recorded: actors and musicians cluster "
        f"at the top because Wikidata describes their craft at the finest grain."
    )

    rows = []
    for i, p in enumerate(people, 1):
        chips = "".join(
            f'<span class="chip">{_esc(_label(o))}</span>' for o in p.get("occupations", [])
        )
        nat = ", ".join(p.get("nationalities") or [])
        meta = f'<span class="nat">{_esc(nat)}</span>' if nat else ""
        n = len(p.get("occupations") or [])
        rows.append(
            f'<li class="prow"><div class="who"><div class="name">'
            f'<span class="rank">{i:02d}</span><b>{_esc(p.get("name"))}</b>{meta}'
            f'<span class="count"><b>{n}</b>occupations</span></div>'
            f'<div class="chips">{chips}</div></div></li>'
        )

    stats = (
        '<div class="stats3">'
        f'<div><div class="v">{_n(scale["people_two_plus"])}</div>'
        '<div class="k">people with 2+ occupations</div></div>'
        f'<div><div class="v">{_n(scale["people_13_plus"])}</div>'
        '<div class="k">recorded with 13 or more</div></div>'
        f'<div><div class="v">{scale["max_occupations"]}</div>'
        '<div class="k">most ever recorded (one person)</div></div>'
        "</div>"
    )

    legend = (
        '<div class="legend">'
        '<span><i style="width:26px;height:6px;border-radius:3px;background:#bf6b28"></i>'
        "the count of occupations, the ranking</span>"
        '<span><i class="chip" style="padding:3px 7px">occupation</i>'
        "a label exactly as Wikidata records it</span>"
        "</div>"
    )
    note = (
        "<b>Read with care.</b> Wikidata records occupations unevenly and at different "
        "granularities: \u201cactor\u201d, \u201cfilm actor\u201d and \u201ctelevision actor\u201d "
        "count as three. People with many occupations are also the best documented, so this "
        "is a map of the record, not of talent."
    )

    footer = (
        f'<span>Data: Wikidata (CC BY-SA) &middot; humansmap.com</span>'
        f'<span><b>{_n(scale["people_two_plus"])}</b> people &middot; '
        f'<b>{_n(scale["people_13_plus"])}</b> with 13+ occupations</span>'
    )

    return (
        HTML_HEAD.replace("__TITLE__", "the polymaths")
        + f"""<section class="card">
  <header>
    <span class="eyebrow">The polymath index</span>
    <span class="brand">Humans&nbsp;Map</span>
  </header>
  <h1>{h1}</h1>
  <p class="deck">{deck}</p>
  {stats}
  <ol class="pgrid">
    {''.join(rows)}
  </ol>
  {legend}
  <div class="note">{note}</div>
  <footer>{footer}</footer>
</section>
</body></html>
"""
    )


# ---------------------------------------------------------------------------
# Card 2 — the age gaps
# ---------------------------------------------------------------------------


def _svg_agegap(payload: dict, width: int = 952) -> str:
    couples = payload["couples"]
    if not couples:
        return ""
    row_h = 74.0
    top_pad = 8.0
    axis_h = 30.0
    lab_w = 30.0        # rank gutter
    name_w = 306.0      # two stacked name/year pairs
    gap_w = 64.0        # right-hand gap figure
    x0 = lab_w + name_w + 18.0
    x1 = width - gap_w - 6.0
    chart_h = top_pad + row_h * len(couples)
    height = chart_h + axis_h

    # Shared year axis across every displayed couple.
    births = [c["older"]["birth_year"] for c in couples] + [c["younger"]["birth_year"] for c in couples]
    deaths = []
    for c in couples:
        for who in ("older", "younger"):
            d = c[who].get("death_date")
            if d:
                try:
                    deaths.append(int(str(d)[:4]))
                except ValueError:
                    pass
    y_lo = min(births) - 6
    y_hi = max(deaths + [c["older"]["birth_year"] + 40 for c in couples], default=y_lo + 100)
    if y_hi - y_lo < 60:
        y_hi = y_lo + 60

    def x(year: float) -> float:
        return x0 + (year - y_lo) / (y_hi - y_lo) * (x1 - x0)

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:.0f} {height:.0f}" '
        f'width="{width:.0f}" height="{height:.0f}" role="img" '
        f'aria-label="Married couples ranked by birth-year gap, with both lifespans">'
    ]

    # Shared gridlines, labelled at the bottom.
    step = 50
    first = int(math.ceil(y_lo / step) * step)
    for yr in range(first, int(y_hi) + 1, step):
        gx = x(yr)
        parts.append(
            f'<line x1="{gx:.1f}" y1="{top_pad:.1f}" x2="{gx:.1f}" '
            f'y2="{chart_h:.1f}" stroke="rgba(28,21,16,.07)" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{gx:.1f}" y="{chart_h + 20:.1f}" class="tl-tick" '
            f'text-anchor="middle">{yr}</text>'
        )

    for i, c in enumerate(couples):
        top = top_pad + i * row_h
        older, younger = c["older"], c["younger"]
        ob, yb = older["birth_year"], younger["birth_year"]
        od = older.get("death_date")
        yd = younger.get("death_date")

        def year_of(d):
            try:
                return int(str(d)[:4])
            except (TypeError, ValueError):
                return None

        odeath = year_of(od) or ob + 30
        ydeath = year_of(yd) or yb + 30

        # rank
        parts.append(
            f'<text x="0" y="{top + 29:.1f}" class="tl-rank">{i + 1:02d}</text>'
        )
        # names (one line per person, aligned to each lifeline)
        parts.append(
            f'<text x="{lab_w:.1f}" y="{top + 29:.1f}" class="tl-name">'
            f'{_esc(_short(older["name"]))}</text>'
        )
        parts.append(
            f'<text x="{lab_w:.1f}" y="{top + 51:.1f}" class="tl-name">'
            f'{_esc(_short(younger["name"]))}</text>'
        )
        # years, right-aligned into a reserved column that ends before the chart
        parts.append(
            f'<text x="{x0 - 12:.1f}" y="{top + 29:.1f}" class="tl-year" '
            f'text-anchor="end">{_esc(_years(older))}</text>'
        )
        parts.append(
            f'<text x="{x0 - 12:.1f}" y="{top + 51:.1f}" class="tl-year" '
            f'text-anchor="end">{_esc(_years(younger))}</text>'
        )

        # lifelines: orange bars birth -> death
        for (by, dy, yy) in ((ob, odeath, top + 15), (yb, ydeath, top + 39)):
            bx, dx = x(by), x(dy)
            w = max(3.0, dx - bx)
            parts.append(
                f'<rect x="{bx:.1f}" y="{yy:.1f}" width="{w:.1f}" height="17" rx="4" '
                f'fill="rgba(191,107,40,.78)"/>'
            )
            parts.append(
                f'<circle cx="{bx:.1f}" cy="{yy + 8.5:.1f}" r="4.6" fill="#bf6b28" '
                f'stroke="#f9f8f4" stroke-width="1.5"/>'
            )
        # birth markers + connector to show the gap being measured
        obx, ybx = x(ob), x(yb)
        parts.append(
            f'<line x1="{obx:.1f}" y1="{top + 15:.1f}" x2="{obx:.1f}" '
            f'y2="{top + 56:.1f}" stroke="rgba(138,130,120,.55)" stroke-width="1" '
            f'stroke-dasharray="2 3"/>'
        )
        parts.append(
            f'<line x1="{ybx:.1f}" y1="{top + 15:.1f}" x2="{ybx:.1f}" '
            f'y2="{top + 56:.1f}" stroke="rgba(138,130,120,.55)" stroke-width="1" '
            f'stroke-dasharray="2 3"/>'
        )
        # gap value
        parts.append(
            f'<text x="{width - 2:.1f}" y="{top + 31:.1f}" class="tl-gap" '
            f'text-anchor="end">{c["gap"]}</text>'
        )
        parts.append(
            f'<text x="{width - 2:.1f}" y="{top + 46:.1f}" class="tl-gaplab" '
            f'text-anchor="end">years apart</text>'
        )

    parts.append("</svg>")
    return "".join(parts)


def render_agegap_html(payload: dict) -> str:
    scale = payload["scale"]
    couples = payload["couples"]
    top = couples[0] if couples else {"gap": 0, "older": {"name": ""}, "younger": {"name": ""}}

    h1 = (
        f"<em>{top['gap']} years</em> apart: the widest recorded "
        f"birth&#8209;year gap between spouses."
    )
    deck = (
        f"Of <b>{_n(scale['couples_considered'])}</b> married couples with a "
        f"dated birth record for <b>both</b> partners, the "
        f"typical gap is <b>{_n(scale['median_gap'])} years</b>. "
        f"<b>{_n(scale['gaps_ge_20'])}</b> couples are 20 years apart or more, and "
        f"<b>{_n(scale['gaps_ge_40'])}</b> are at least 40. The fifteen widest are below."
    )

    legend = (
        '<div class="legend">'
        '<span><i style="width:26px;height:8px;border-radius:3px;background:rgba(191,107,40,.78)"></i>'
        "lifespan (birth to death)</span>"
        '<span><i style="width:2px;height:18px;background:#8a8278"></i>'
        "birth-year markers (the gap between them is the ranking)</span>"
        "</div>"
    )
    note = (
        "<b>Read with care.</b> Only couples with a birth date recorded for both partners "
        "can be counted, and Wikidata covers the world unevenly. This is a description of "
        "the historical record, not a value judgement about any couple."
    )

    footer = (
        f'<span>Data: Wikidata (CC BY-SA) &middot; humansmap.com</span>'
        f'<span><b>{_n(scale["couples_considered"])}</b> couples &middot; '
        f'median gap <b>{_n(scale["median_gap"])} years</b></span>'
    )

    return (
        HTML_HEAD.replace("__TITLE__", "the biggest age gaps")
        + f"""<section class="card">
  <header>
    <span class="eyebrow">Age gaps in married couples</span>
    <span class="brand">Humans&nbsp;Map</span>
  </header>
  <h1>{h1}</h1>
  <p class="deck">{deck}</p>
  <div class="barwrap">{_svg_agegap(payload)}</div>
  {legend}
  <div class="note">{note}</div>
  <footer>{footer}</footer>
</section>
</body></html>
"""
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default="docs/marketing/curiosities")
    parser.add_argument("--min-statements", type=int, default=DEFAULT_MIN_STATEMENTS)
    parser.add_argument("--poly-top", type=int, default=DEFAULT_POLY_TOP)
    parser.add_argument("--agegap-top", type=int, default=DEFAULT_AGEGAP_TOP)
    parser.add_argument("--scan-limit", type=int, default=DEFAULT_SCAN_LIMIT)
    parser.add_argument("--min-gap", type=int, default=0)
    parser.add_argument("--max-gap", type=int, default=DEFAULT_MAX_GAP)
    parser.add_argument(
        "--require-month", action=argparse.BooleanOptionalAction, default=True,
        help="require a month in both birth dates (rejects century-rounded years)",
    )
    parser.add_argument(
        "--require-day", action=argparse.BooleanOptionalAction, default=True,
        help="require a full day in both birth dates",
    )
    parser.add_argument(
        "--agegap-min-statements", type=int, default=DEFAULT_AGEGAP_MIN_STATEMENTS,
        help="Wikidata-statement floor for both partners on the age-gap card",
    )
    parser.add_argument(
        "--drop-jan1", action=argparse.BooleanOptionalAction, default=True,
        help="drop couples where either birth date is a bare January 1",
    )
    parser.add_argument(
        "--require-marriage-date", action=argparse.BooleanOptionalAction, default=True,
        help="require the marriage itself to have a recorded year",
    )
    parser.add_argument(
        "--raw-cache", default=None,
        help="write/read the deduped spouse-edge candidates as JSON (skips the DB pass on rebuild)",
    )
    parser.add_argument(
        "--from-json", default=None,
        help="re-render both HTML files from an existing JSON pair (directory)",
    )
    parser.add_argument("--no-html", action="store_true")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.from_json:
        src = Path(args.from_json)
        poly = json.loads((src / "polymaths.json").read_text())
        age = json.loads((src / "agegap.json").read_text())
    else:
        db = get_db(get_arango_client(request_timeout=900))
        print("collecting polymaths ...", file=sys.stderr)
        poly = collect_polymaths(db, args.min_statements, args.poly_top)
        rows = None
        if args.raw_cache and Path(args.raw_cache).exists():
            print(f"loading spouse edges from {args.raw_cache} ...", file=sys.stderr)
            rows = load_raw_pairs(Path(args.raw_cache))
        if rows is None:
            print("collecting age gaps (one bounded spouse-edge pass) ...", file=sys.stderr)
            rows = fetch_agegap_pairs(db, args.scan_limit)
            if args.raw_cache:
                dump_raw_pairs(rows, Path(args.raw_cache))
        age = collect_agegaps(
            db, rows, args.agegap_top, args.scan_limit, args.min_gap,
            args.max_gap, args.require_month, args.require_day,
            args.agegap_min_statements, args.drop_jan1, args.require_marriage_date,
        )

    poly_path = out_dir / "polymaths.json"
    age_path = out_dir / "agegap.json"
    poly_path.write_text(json.dumps(poly, indent=2, ensure_ascii=False) + "\n")
    age_path.write_text(json.dumps(age, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {poly_path}")
    print(f"wrote {age_path}")

    if not args.no_html:
        (out_dir / "polymaths.html").write_text(render_polymaths_html(poly))
        (out_dir / "agegap.html").write_text(render_agegap_html(age))
        print(f"wrote {out_dir / 'polymaths.html'}")
        print(f"wrote {out_dir / 'agegap.html'}")

    print(json.dumps({"polymaths": poly["scale"], "agegap": age["scale"]}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
