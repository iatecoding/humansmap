#!/usr/bin/env python3
"""Marketing insight miner — read-only, produces shareable stats and cards.

HumansMap is a data story waiting to be posted. This script mines the live
graph + the pre-computed feed cards for genuinely surprising, citation-ready
findings, then emits:

  docs/marketing/insights-<YYYY-MM-DD>.json   machine-readable payload
  docs/marketing/insights-<YYYY-MM-DD>.md     ready-to-post copy per insight
  docs/marketing/insight-cards.html           brand-styled card sheet to screenshot

It never writes to ArangoDB. All queries are bounded (LIMIT / COLLECT) and
intended to run as a manual job inside the backend container:

    docker compose exec -T backend python /app/scripts/marketing_insights.py

or from the repo root:

    docker compose exec -T backend python - < backend/scripts/marketing_insights.py

Flags:
    --out-dir PATH   output directory (default: docs/marketing)
    --no-cards       skip HTML card sheet generation
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import (  # noqa: E402
    AFFILIATIONS,
    EDGE_COLLECTIONS,
    EVENTS,
    ORGANIZATIONS,
    PERSONS,
    RELATIONSHIPS,
    get_arango_client,
    get_db,
)
from app.services.cache import get_redis  # noqa: E402

FEED_CARDMAP_KEY = "feed:cardmap:v2"

# ISO 3166-1 alpha-2 → English names for the country cards (persons store codes).
COUNTRY_NAMES: dict[str, str] = {
    "US": "United States", "FR": "France", "DE": "Germany", "GB": "United Kingdom",
    "IT": "Italy", "ES": "Spain", "JP": "Japan", "SE": "Sweden", "PL": "Poland",
    "CA": "Canada", "RU": "Russia", "AT": "Austria", "CH": "Switzerland",
    "HU": "Hungary", "BE": "Belgium", "NL": "Netherlands", "CZ": "Czechia",
    "NO": "Norway", "DK": "Denmark", "FI": "Finland", "IE": "Ireland",
    "PT": "Portugal", "GR": "Greece", "AU": "Australia", "BR": "Brazil",
    "IN": "India", "CN": "China", "KR": "South Korea", "MX": "Mexico",
    "AR": "Argentina", "ZA": "South Africa", "TR": "Turkey", "IL": "Israel",
    "UA": "Ukraine", "RO": "Romania", "BG": "Bulgaria", "HR": "Croatia",
    "RS": "Serbia", "SI": "Slovenia", "SK": "Slovakia", "LT": "Lithuania",
    "LV": "Latvia", "EE": "Estonia", "IS": "Iceland", "NZ": "New Zealand",
    "CL": "Chile", "CO": "Colombia", "PE": "Peru", "EG": "Egypt",
    "NG": "Nigeria", "KE": "Kenya", "MA": "Morocco", "IR": "Iran",
    "PK": "Pakistan", "BD": "Bangladesh", "ID": "Indonesia", "PH": "Philippines",
    "TH": "Thailand", "VN": "Vietnam", "SG": "Singapore", "TW": "Taiwan",
    "AD": "Andorra", "AG": "Antigua and Barbuda", "AL": "Albania", "AM": "Armenia",
    "AZ": "Azerbaijan", "BA": "Bosnia and Herzegovina", "BF": "Burkina Faso",
    "BJ": "Benin", "BO": "Bolivia", "BS": "Bahamas", "BY": "Belarus", "BZ": "Belize",
    "CD": "DR Congo", "CF": "Central African Republic", "CG": "Congo",
    "CI": "Ivory Coast", "CM": "Cameroon", "CR": "Costa Rica", "CU": "Cuba",
    "CV": "Cape Verde", "CY": "Cyprus", "DO": "Dominican Republic", "DZ": "Algeria",
    "EC": "Ecuador", "ET": "Ethiopia", "GA": "Gabon", "GE": "Georgia", "GH": "Ghana",
    "GN": "Guinea", "GT": "Guatemala", "GW": "Guinea-Bissau", "GY": "Guyana",
    "HN": "Honduras", "HT": "Haiti", "IQ": "Iraq", "JM": "Jamaica", "JO": "Jordan",
    "KG": "Kyrgyzstan", "KH": "Cambodia", "KP": "North Korea", "KZ": "Kazakhstan",
    "LB": "Lebanon", "LI": "Liechtenstein", "LR": "Liberia", "LU": "Luxembourg",
    "MC": "Monaco", "MD": "Moldova", "ME": "Montenegro", "MG": "Madagascar",
    "MK": "North Macedonia", "ML": "Mali", "MT": "Malta", "MU": "Mauritius",
    "MY": "Malaysia", "MZ": "Mozambique", "PA": "Panama", "PS": "Palestine",
    "PY": "Paraguay", "RW": "Rwanda", "SA": "Saudi Arabia", "SD": "Sudan",
    "SL": "Sierra Leone", "SN": "Senegal", "SO": "Somalia", "SS": "South Sudan",
    "SV": "El Salvador", "SY": "Syria", "TD": "Chad", "TG": "Togo",
    "TJ": "Tajikistan", "TM": "Turkmenistan", "TN": "Tunisia",
    "TT": "Trinidad and Tobago", "UG": "Uganda", "UY": "Uruguay",
    "UZ": "Uzbekistan", "VA": "Vatican City", "VE": "Venezuela", "WS": "Samoa",
    "ZM": "Zambia", "AE": "United Arab Emirates", "AO": "Angola",
}


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


def _rows(db, aql: str, bind: dict | None = None) -> list:
    try:
        return list(db.aql.execute(aql, bind_vars=bind or {}))
    except Exception as exc:  # noqa: BLE001 - a marketing job must not crash
        print(f"[warn] query failed: {exc}", file=sys.stderr)
        return []


def _count(db, collection: str) -> int:
    try:
        return db.collection(collection).count()
    except Exception:  # noqa: BLE001
        return 0


# ---------------------------------------------------------------------------
# Insight builders
# ---------------------------------------------------------------------------


def build_scale(db) -> dict:
    persons = _count(db, PERSONS)
    orgs = _count(db, ORGANIZATIONS)
    events = _count(db, EVENTS)
    edges = sum(_count(db, c) for c in EDGE_COLLECTIONS)
    return {
        "persons": persons,
        "organizations": orgs,
        "events": events,
        "connections": edges,
        "headline": f"{persons:,} people · {orgs:,} organizations · {edges:,} connections",
    }


def build_most_awarded(db) -> dict:
    rows = _rows(
        db,
        "FOR p IN persons FILTER p.awards_count > 0 "
        "SORT p.awards_count DESC LIMIT 15 "
        "RETURN {name:p.name_en, value:p.awards_count, qid:p.wikidata_id, "
        "       top:(p.awards_top5[0] OR null), nat:(p.nationalities[0] OR null)}",
    )
    return {
        "id": "most_awarded",
        "eyebrow": "MOST DECORATED",
        "headline": "Who collected the most honours?",
        "unit": "awards",
        "rows": rows,
    }


def build_academic_genealogy(db) -> dict:
    total = _rows(
        db,
        "RETURN LENGTH(FOR e IN relationships "
        "FILTER e.type IN ['doctoral_advisor','doctoral_student'] LIMIT 2000000 RETURN 1)",
    )
    advisors = _rows(
        db,
        "FOR e IN relationships FILTER e.type == 'doctoral_student' "
        "COLLECT a = e._from WITH COUNT INTO n SORT n DESC LIMIT 15 "
        "LET p = DOCUMENT(a) "
        "RETURN {name:p.name_en, value:n, qid:p.wikidata_id, nat:(p.nationalities[0] OR null)}",
    )
    return {
        "id": "academic_genealogy",
        "eyebrow": "ACADEMIC GENEALOGY",
        "headline": "Who trained the most doctoral students?",
        "stat": total[0] if total else 0,
        "stat_label": "advisor → student links",
        "unit": "doctoral students",
        "rows": advisors,
    }


def build_dynasties() -> dict:
    """Read pre-computed dynasty cards from the feed cache (no DB load).

    The feed caps a family's member list at 15, so ranking families by size
    produced 15 identical full bars (every top lineage reports "15 members").
    Count instead how many notable lineages the graph finds in each field — a
    quantity that actually varies and needs no cap disclosure beyond the note.
    """
    counts: dict[str, int] = {}
    total = 0
    try:
        r = get_redis()
        for raw in r.hgetall(FEED_CARDMAP_KEY).values():
            card = json.loads(raw)
            if card.get("type") != "dynasty":
                continue
            field = (card.get("field") or "").strip() or "Other"
            counts[field] = counts.get(field, 0) + 1
            total += 1
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] dynasty cards unavailable: {exc}", file=sys.stderr)
    rows = [
        {"name": field, "value": n}
        for field, n in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:10]
    ]
    return {
        "id": "dynasties",
        "eyebrow": "FAMILY DYNASTIES",
        "headline": "Politics is the most common family trade.",
        "unit": "family lineages",
        "stat": total,
        "stat_label": "notable families",
        "note": "Political and noble families are the best-documented, so this "
        "field mix partly reflects richer records.",
        "rows": rows,
    }


def build_cross_domain_couples() -> dict:
    """Relationship cards already carry a domain cross-over flag."""
    rows: list[dict] = []
    try:
        r = get_redis()
        for raw in r.hgetall(FEED_CARDMAP_KEY).values():
            card = json.loads(raw)
            if card.get("type") != "relationship":
                continue
            domains = [d for d in (card.get("domains") or []) if d and d != "Unknown"]
            a, b = card.get("a") or {}, card.get("b") or {}
            # "Two entirely different worlds" means two distinct fields: the feed
            # emits some same-field pairs (e.g. Film x Film) and "Unknown" tags,
            # which contradicted the headline. Require two genuinely different ones.
            if len(set(domains)) < 2 or not a.get("name") or not b.get("name"):
                continue
            rows.append(
                {
                    "name": f"{a['name']} & {b['name']}",
                    "value": round(card.get("score", 0) * 100),
                    "detail": " × ".join(domains),
                    "relation": card.get("relation"),
                    "a_qid": a.get("qid"),
                    "b_qid": b.get("qid"),
                }
            )
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] relationship cards unavailable: {exc}", file=sys.stderr)
    rows.sort(key=lambda x: x["value"], reverse=True)
    return {
        "id": "cross_domain_couples",
        "eyebrow": "UNLIKELY PAIRS",
        "headline": "Marriages across two different fields.",
        "unit": "cross-over score",
        "rows": rows[:15],
    }


def build_countries(db) -> dict:
    rows = _rows(
        db,
        "FOR p IN persons "
        "FILTER LENGTH(p.nationalities) > 0 AND p.wikidata_statements_count >= 50 "
        "COLLECT c = p.nationalities[0] WITH COUNT INTO n SORT n DESC LIMIT 15 "
        "RETURN {name:c, value:n}",
    )
    for row in rows:
        row["name"] = COUNTRY_NAMES.get(row.get("name"), row.get("name"))
    return {
        "id": "countries",
        "eyebrow": "WHERE WIKIDATA IS RICH",
        "headline": "Which countries' people are best documented?",
        "unit": "documented people",
        "note": "This measures Wikipedia/Wikidata coverage, not real-world prominence.",
        "rows": rows,
    }


def build_occupations(db) -> dict:
    rows = _rows(
        db,
        "FOR p IN persons "
        "FILTER p.wikidata_statements_count >= 120 AND LENGTH(p.occupations) > 0 "
        "COLLECT o = p.occupations[0] WITH COUNT INTO n SORT n DESC LIMIT 15 "
        "RETURN {name:o, value:n}",
    )
    for row in rows:
        row["name"] = str(row.get("name", "")).replace("_", " ").capitalize()
    return {
        "id": "occupations",
        "eyebrow": "WHAT PEOPLE DO",
        "headline": "The most common occupations.",
        "unit": "notable people",
        "rows": rows,
    }


def build_sports_clubs(db) -> dict:
    rows = _rows(
        db,
        "FOR e IN affiliations FILTER e.type == 'sports_team_member' LIMIT 400000 "
        "COLLECT to = e._to WITH COUNT INTO n SORT n DESC LIMIT 15 "
        "LET o = DOCUMENT(to) RETURN {name:o.name_en, value:n, country:(o.country OR null)}",
    )
    return {
        "id": "sports_clubs",
        "eyebrow": "SPORTING PIPELINES",
        "headline": "Which clubs have fielded the most notable players?",
        "unit": "notable players",
        "note": "Italian football records are by far the most complete in Wikidata, "
        "which is why this list is Italy-heavy.",
        "rows": rows,
    }


def build_most_married(db) -> dict:
    rows = _rows(
        db,
        "FOR e IN relationships FILTER e.type == 'spouse' "
        "COLLECT p = e._from INTO g "
        "LET partners = UNIQUE(g[*].e._to) "
        "SORT LENGTH(partners) DESC LIMIT 15 "
        "LET d = DOCUMENT(p) "
        "RETURN {name:d.name_en, value:LENGTH(partners), qid:d.wikidata_id, "
        "       nat:(d.nationalities[0] OR null)}",
    )
    return {
        "id": "most_married",
        "eyebrow": "MOST MARRIAGES",
        "headline": "Who married the most people?",
        "unit": "spouses",
        "rows": rows,
    }


def build_most_positions(db) -> dict:
    rows = _rows(
        db,
        "FOR e IN affiliations FILTER e.type == 'position_held' "
        "COLLECT p = e._from WITH COUNT INTO n SORT n DESC LIMIT 15 "
        "LET d = DOCUMENT(p) "
        "RETURN {name:d.name_en, value:n, qid:d.wikidata_id, "
        "       nat:(d.nationalities[0] OR null)}",
    )
    return {
        "id": "most_positions",
        "eyebrow": "OFFICES HELD",
        "headline": "Who held the most public offices?",
        "unit": "offices held",
        "rows": rows,
    }


def build_most_founded(db) -> dict:
    rows = _rows(
        db,
        "FOR e IN affiliations FILTER e.type == 'founder' "
        "COLLECT p = e._from WITH COUNT INTO n SORT n DESC LIMIT 15 "
        "LET d = DOCUMENT(p) "
        "RETURN {name:d.name_en, value:n, qid:d.wikidata_id, "
        "       nat:(d.nationalities[0] OR null)}",
    )
    return {
        "id": "most_founded",
        "eyebrow": "SERIAL FOUNDERS",
        "headline": "Who started the most organizations?",
        "unit": "organizations founded",
        "rows": rows,
    }


def build_most_teams(db) -> dict:
    rows = _rows(
        db,
        "FOR e IN affiliations FILTER e.type == 'sports_team_member' "
        "COLLECT p = e._from INTO g "
        "LET teams = UNIQUE(g[*].e._to) "
        "SORT LENGTH(teams) DESC LIMIT 15 "
        "LET d = DOCUMENT(p) "
        "RETURN {name:d.name_en, value:LENGTH(teams), qid:d.wikidata_id, "
        "       nat:(d.nationalities[0] OR null)}",
    )
    return {
        "id": "most_teams",
        "eyebrow": "WELL-TRAVELLED",
        "headline": "Which athletes played for the most different clubs?",
        "unit": "clubs",
        "rows": rows,
    }


def build_office_distribution(db) -> dict:
    """The long tail of political office — how concentrated office-holding is.

    Complements ``build_most_positions`` (the ranked insiders) with the shape of
    the whole population: how many people held one office, two, and so on. One
    bounded pass on the indexed ``affiliations.type`` filter, then two stacked
    ``COLLECT``s so the result is one row per office count, not per person.
    """
    raw = _rows(
        db,
        "FOR e IN affiliations FILTER e.type == 'position_held' LIMIT @scan_limit "
        "COLLECT p = e._from WITH COUNT INTO n "
        "COLLECT offices = n WITH COUNT INTO people "
        "SORT offices RETURN {offices: offices, people: people}",
        {"scan_limit": 2_000_000},
    )
    dist = {int(r["offices"]): int(r["people"]) for r in raw if r.get("people")}
    total = sum(dist.values())

    def band(lo: int, hi: int | None) -> int:
        return sum(v for k, v in dist.items() if k >= lo and (hi is None or k <= hi))

    rows = [
        {"name": "Exactly 1 office", "value": band(1, 1)},
        {"name": "Exactly 2 offices", "value": band(2, 2)},
        {"name": "3 to 4 offices", "value": band(3, 4)},
        {"name": "5 to 9 offices", "value": band(5, 9)},
        {"name": "10 or more offices", "value": band(10, None)},
    ]
    return {
        "id": "office_distribution",
        "eyebrow": "POLITICAL OFFICE",
        "headline": "Most office-holders hold exactly one office.",
        "unit": "people",
        "kind": "distribution",
        "stat": total,
        "stat_label": "people with at least one office",
        "note": "One office is one Wikidata \u201cposition held\u201d statement, so re-election "
        "counts again. Parliaments and local councils are far better recorded than "
        f"cabinets or party posts; only {band(25, None):,} people reached 25 or more.",
        "rows": rows,
    }


# ---------------------------------------------------------------------------
# Card sheet (self-contained HTML; screenshot or print to PNG/PDF)
# ---------------------------------------------------------------------------


def render_cards_html(payload: dict) -> str:
    cards_html = []
    for ins in payload["insights"]:
        rows = (ins.get("rows") or [])[:15]
        if not rows:
            continue
        top = rows[0]["value"] or 1
        is_dist = ins.get("kind") == "distribution"
        bars = []
        for i, row in enumerate(rows):
            pct = max(2.0, (row["value"] or 0) / top * 100)
            detail = row.get("detail") or row.get("field") or row.get("country") or ""
            rank = "" if is_dist else f"{i + 1:02d}"
            bars.append(
                f'<li class="row"><span class="rank">{rank}</span>'
                f'<span class="bar" style="width:{pct:.1f}%"></span>'
                f'<span class="label"><b>{_esc(row["name"])}</b>'
                f'{f"<i>{_esc(detail)}</i>" if detail else ""}</span>'
                f'<span class="value">{row["value"]:,}<em>{_esc(ins.get("unit", ""))}</em></span></li>'
            )
        stat = ""
        if ins.get("stat"):
            stat = (
                f'<div class="stat"><span class="bignum">{ins["stat"]:,}</span>'
                f'<span class="statlabel">{_esc(ins.get("stat_label", ""))}</span></div>'
            )
        note = ""
        if ins.get("note"):
            note = f'<p class="note">{_esc(ins["note"])}</p>'
        cards_html.append(
            f"""<section class="card">
  <header><span class="eyebrow">{_esc(ins["eyebrow"])}</span>
    <span class="brand">Humans&nbsp;Map</span></header>
  <h2>{_esc(ins["headline"])}</h2>
  {stat}
  <ol class="chart">{''.join(bars)}</ol>
  {note}
  <footer>Data: Wikidata (CC BY-SA) · humansmap.com</footer>
</section>"""
        )

    scale = payload["scale"]
    cards_html.insert(
        0,
        f"""<section class="card cover">
  <header><span class="eyebrow">THE DATASET</span>
    <span class="brand">Humans&nbsp;Map</span></header>
  <h2>A map of who is connected to whom.</h2>
  <div class="cover-nums">
    <div><span class="bignum">{scale["persons"]:,}</span><span class="statlabel">people</span></div>
    <div><span class="bignum">{scale["organizations"]:,}</span><span class="statlabel">organizations</span></div>
    <div><span class="bignum">{scale["connections"]:,}</span><span class="statlabel">connections</span></div>
  </div>
  <footer>Data: Wikidata (CC BY-SA) · humansmap.com</footer>
</section>""",
    )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>HumansMap insight cards</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;800&family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg:#f9f8f4; --surface:#ece8de; --card:#e4ddd0; --text:#1c1510;
    --accent:#1b4d8e; --person:#bf6b28; --muted:#8a8278;
    --display:'Playfair Display', Georgia, serif; --body:'DM Sans', system-ui, sans-serif;
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:#d9d4c9; font-family:var(--body); padding:32px; }}
  .sheet {{ display:flex; flex-direction:column; align-items:center; gap:32px; }}
  .card {{ width:1080px; height:1620px; background:var(--bg); color:var(--text);
    padding:72px 76px; display:flex; flex-direction:column; position:relative; overflow:hidden; }}
  .card header {{ display:flex; justify-content:space-between; align-items:baseline; }}
  .eyebrow {{ font:700 20px/1 var(--body); letter-spacing:.18em; text-transform:uppercase; color:var(--accent); }}
  .brand {{ font:700 30px/1 var(--display); color:var(--accent); }}
  .card h2 {{ font:800 52px/1.08 var(--display); margin:28px 0 8px; max-width:16ch; letter-spacing:-.01em; }}
  .stat {{ margin:20px 0 4px; }}
  .cover-nums {{ display:flex; flex-direction:column; gap:26px; margin-top:auto; margin-bottom:60px; }}
  .cover-nums > div {{ display:flex; align-items:baseline; gap:26px; }}
  .cover-nums .bignum {{ font:800 68px/1 var(--display); }}
  .bignum {{ font:800 68px/1 var(--display); color:var(--text); }}
  .statlabel {{ display:block; font:500 22px/1.4 var(--body); color:var(--muted);
    text-transform:uppercase; letter-spacing:.1em; margin-top:10px; }}
  .cover-nums .statlabel {{ margin-top:0; }}
  .chart {{ list-style:none; margin:26px 0 0; padding:0; display:flex; flex-direction:column;
    gap:0; }}
  .row {{ display:grid; grid-template-columns:46px 1fr auto; align-items:center;
    gap:18px; padding:12px 0; border-bottom:1px solid rgba(28,21,16,.08); position:relative; }}
  .rank {{ font:700 19px/1 var(--body); color:var(--muted); }}
  .bar {{ position:absolute; left:64px; top:6px; bottom:6px; background:rgba(191,107,40,.14);
    border-left:4px solid var(--person); border-radius:2px; z-index:0; }}
  .label {{ grid-column:2; z-index:1; display:flex; flex-direction:column; padding-left:14px; }}
  .label b {{ font:700 24px/1.2 var(--body); }}
  .label i {{ font:400 16px/1.4 var(--body); color:var(--muted); font-style:normal; }}
  .value {{ z-index:1; font:700 26px/1 var(--display); white-space:nowrap; }}
  .value em {{ display:block; font:400 14px/1.4 var(--body); color:var(--muted);
    font-style:normal; text-align:right; text-transform:uppercase; letter-spacing:.08em; margin-top:4px; }}
  .note {{ margin:18px 0 0; font:400 17px/1.45 var(--body); color:var(--muted); font-style:italic; }}
  .card footer {{ margin-top:auto; padding-top:20px; font:400 17px/1.4 var(--body); color:var(--muted); }}
  @media print {{
    body {{ background:#fff; padding:0; }}
    .card {{ page-break-after:always; margin:0; }}
  }}
</style></head>
<body><div class="sheet">{''.join(cards_html)}</div></body></html>"""


def _esc(value) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ---------------------------------------------------------------------------
# Markdown copy deck
# ---------------------------------------------------------------------------


def render_markdown(payload: dict) -> str:
    lines = [
        f"# HumansMap — shareable insights ({payload['generated']})",
        "",
        "Generated by `backend/scripts/marketing_insights.py` (read-only). "
        "Every figure below is computed from the live graph or the pre-computed "
        "feed cards. See `REDDIT_PLAYBOOK.md` for where and how to post.",
        "",
        f"**Scale:** {payload['scale']['headline']}",
        "",
    ]
    for ins in payload["insights"]:
        rows = ins.get("rows") or []
        if not rows:
            continue
        lines += [f"## {ins['eyebrow']} — {ins['headline']}", ""]
        if ins.get("stat"):
            lines += [f"**{ins['stat']:,}** {ins.get('stat_label', '')}", ""]
        lines += ["| # | Item | Detail | " + ins.get("unit", "value") + " |", "|---|---|---|---|"]
        for i, row in enumerate(rows, 1):
            detail = row.get("detail") or row.get("field") or row.get("country") or ""
            lines.append(f"| {i} | {row['name']} | {detail} | {row['value']:,} |")
        lines.append("")
    lines += [
        "---",
        "",
        "## Attribution footer to reuse in posts",
        "",
        "> Data: Wikidata (CC BY-SA). Built by humansmap.com.",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def collect() -> dict:
    db = get_db(get_arango_client(request_timeout=180))
    insights = [
        build_most_awarded(db),
        build_most_married(db),
        build_most_positions(db),
        build_academic_genealogy(db),
        build_dynasties(),
        build_most_founded(db),
        build_most_teams(db),
        build_sports_clubs(db),
        build_cross_domain_couples(),
        build_countries(db),
        build_occupations(db),
        build_office_distribution(db),
    ]
    return {
        "generated": date.today().isoformat(),
        "scale": build_scale(db),
        "insights": [i for i in insights if i.get("rows")],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default="docs/marketing")
    parser.add_argument("--no-cards", action="store_true")
    args = parser.parse_args()

    payload = collect()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    stamp = payload["generated"]
    json_path = out_dir / f"insights-{stamp}.json"
    md_path = out_dir / f"insights-{stamp}.md"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    md_path.write_text(render_markdown(payload) + "\n")
    print(f"wrote {json_path} and {md_path}")

    if not args.no_cards:
        cards_path = out_dir / "insight-cards.html"
        cards_path.write_text(render_cards_html(payload))
        print(f"wrote {cards_path}")

    print(json.dumps({"scale": payload["scale"], "insights": [i["id"] for i in payload["insights"]]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
