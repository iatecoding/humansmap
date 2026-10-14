#!/usr/bin/env python3
"""Marketing club-network miner — read-only, emits the "club-to-club player
flow" card (the advanced sport centrepiece).

The concept: two clubs are linked when *the same player* played for both
(``sports_team_member`` affiliations, Wikidata property P54). Nodes are clubs,
node size is how many shared-player links run through the club, and an edge's
weight is the number of notable players the two clubs share. Clubs are grouped
by country around a single ring: domestic ties hug the rim and cross-border
ties become chords whose colour blends the two countries' colours.

Why not directed transfers? Wikidata records *stints*, not transfer events; the
date qualifiers are present for most edges but routinely missing, reversed or
overlapping, so a directed move graph would be a best-guess. "Played for both"
is the robust, checkable signal. (A directed view can be layered on later.)

It never writes to ArangoDB. The heavy aggregation is a single bounded AQL pass
over the ``sports_team_member`` type index:

  * The ``wikidata_statements_count`` floor (``--min-statements``, default 25)
    keeps only players who are actually documented. It bounds the population to
    ~100k people (all 240k sports people have >= 5 statements).
  * Every qualifying player's club list is UNIQUE-d up in one ``COLLECT`` and
    expanded into unordered club pairs; the diagonal (a == b) is the club's
    notable-player count and the off-diagonal (a < b) is the shared-player
    count. Only pairs with ``--min-pair`` players are returned, so the millions
    of intermediate rows never leave the database.
  * A second, tiny query pulls the shared-player names for the cross-border
    bridges and the strongest overall pairs the card actually names (examples),
    so no raw edge dump is needed.

Reserve / B / youth / women's teams are filtered by name (parent-club x reserve
pairs otherwise dominate: Athletic Club x Athletic Bilbao B share 269 players).
Selection keeps the countries with the deepest club coverage plus a few
cross-border bridge countries, then round-robins clubs so no single country
swamps the frame; the country list and counts land in the JSON.

The JSON keeps 140 clubs; the card prunes to the best-connected ~56 (by shared
players) plus the endpoints of the strongest cross-border links, because 140
labelled dots cannot be read at feed size. The dropped clubs and the pruning
rule are stated on the card.

Outputs (both written by default):

  clubs.json   machine-readable payload: nodes, edges, top_pairs, examples
  clubs.html   brand-styled card sheet (1600x1000) to screenshot to clubs.png

Run it manually inside the backend container:

    docker compose exec -T backend python - --out-dir /tmp/clubs \\
        < backend/scripts/marketing_clubs.py
    docker compose exec -T backend cat /tmp/clubs/clubs.json \\
        > docs/marketing/clubs/clubs.json
    docker compose exec -T backend cat /tmp/clubs/clubs.html \\
        > docs/marketing/clubs/clubs.html

Then render (from scripts/marketing/):

    source env.sh
    node render-insight-cards.js ../../docs/marketing/clubs/clubs.html \\
        ../../docs/marketing/clubs
    mv ../../docs/marketing/clubs/card-01.png ../../docs/marketing/clubs/clubs.png

Flags:
    --out-dir PATH        output directory (default: docs/marketing/clubs)
    --min-statements N    Wikidata-statement floor for a "notable" player (25)
    --min-pair N          shared players for a pair to be returned (3)
    --node-floor N        min notable players for a club to be drawn (20)
    --max-countries N     countries taken by club coverage (12)
    --per-country N       clubs kept per country (9)
    --max-nodes N         hard cap on clubs collected (150)
    --ring-nodes N        clubs drawn on the ring, by shared-player weight (56)
    --ring-edge-min N     shared players for a link to be drawn on the ring (8)
    --extra-countries CSV additional bridge countries to force in
    --from-json PATH      re-render the HTML from an existing clubs.json
    --no-html             emit only the JSON payload
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import OrderedDict, defaultdict
from datetime import date
from pathlib import Path

# When run as a file inside the container this resolves to /app; when piped
# over stdin (``python -``) __file__ is "<stdin>" and the container's cwd
# (/app) is already on sys.path, so fall back.
try:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
except (NameError, OSError):  # pragma: no cover - stdin invocation
    sys.path.insert(0, str(Path.cwd()))

from app.database import get_arango_client, get_db  # noqa: E402

DEFAULT_MIN_STATEMENTS = 25
DEFAULT_MIN_PAIR = 3
DEFAULT_NODE_FLOOR = 20
DEFAULT_MAX_COUNTRIES = 12
DEFAULT_PER_COUNTRY = 9
DEFAULT_MAX_NODES = 150

# The card draws a pruned subset: 140 labelled club names cannot be read at
# feed size. RING_NODES is the best-connected clubs kept by shared players;
# RING_EDGE_MIN keeps the ring from turning into a hairball (thinner links stay
# in the JSON so the number is never hidden).
RING_NODES = 56
RING_EDGE_MIN = 8

FEATURED_BRIDGES = 6     # numbered cross-border callouts on ring + panel
LEADERBOARD = 6          # rows in the cross-border panel
EXAMPLE_PEOPLE = 3       # shared names shown per panel row

# Countries with a thinner club base but the cross-border pipelines this card is
# about (Uruguay -> Argentina's River/Independiente, Ukraine -> Russia, etc.).
DEFAULT_EXTRA_COUNTRIES = [
    "Uruguay", "Ukraine", "Czech Republic", "Canada",
]

COUNTRY_SHORT = {
    "United Kingdom": "UK",
    "United States": "USA",
    "People's Republic of China": "China",
    "Czech Republic": "Czechia",
    "Bosnia and Herzegovina": "Bosnia",
    "North Macedonia": "N. Macedonia",
    "United Arab Emirates": "UAE",
    "South Korea": "S. Korea",
    "South Africa": "S. Africa",
    "New Zealand": "New Zealand",
    "Saudi Arabia": "Saudi Arabia",
    "Republic of Ireland": "Ireland",
    "Democratic Republic of the Congo": "DR Congo",
}

# Display names for clubs whose official name would collide or overrun a radial
# label. Keyed by the full Wikidata name so the map stays valid for a re-render
# from the committed JSON (the JSON's `short` field is only a fallback).
CLUB_SHORT = {
    "Clube de Regatas do Flamengo": "Flamengo",
    "Sociedade Esportiva Palmeiras": "Palmeiras",
    "CR Vasco da Gama": "Vasco da Gama",
    "S.C. Corinthians Paulista": "Corinthians",
    "São Paulo FC": "São Paulo",
    "Botafogo F.R.": "Botafogo",
    "Fluminense F.C.": "Fluminense",
    "Santos F.C.": "Santos",
    "Grêmio FBPA": "Grêmio",
    "S.C. Internacional": "Internacional",
    "Cruzeiro E.C.": "Cruzeiro",
    "Club Atlético River Plate": "River Plate",
    "Club Atlético Independiente": "Independiente",
    "Club Atlético Peñarol": "Peñarol",
    "Club Nacional de Football": "Nacional",
    "Estudiantes de La Plata": "Estudiantes",
    "PFC CSKA Moscow": "CSKA Moscow",
    "FC Dynamo Kyiv": "Dynamo Kyiv",
    "FC Torpedo Moscow": "Torpedo",
    "Red Bull New York": "NY Red Bulls",
    "Colorado Rapids": "Colorado Rapids",
    "S.L. Benfica": "Benfica",
    "Sporting CP": "Sporting",
    "FC Porto": "Porto",
    "S.C. Braga": "Braga",
    "C.S. Marítimo": "Marítimo",
    "AS Monaco FC": "Monaco",
    "Olympique de Marseille": "Marseille",
    "Paris Saint-Germain FC": "PSG",
    "Stade Rennais F.C.": "Rennes",
    "FC Girondins de Bordeaux": "Bordeaux",
    "Olympique Lyonnais": "Lyon",
    "AS Saint-Étienne": "Saint-Étienne",
    "LOSC Lille": "Lille",
    "ŠK Slovan Bratislava": "Slovan Bratislava",
    "AC Sparta Prague": "Sparta Prague",
    "SK Slavia Prague": "Slavia Prague",
    "GNK Dinamo Zagreb": "Dinamo Zagreb",
    "FK Crvena zvezda": "Red Star Belgrade",
    "FK Partizan": "Partizan",
    "FK Budućnost Podgorica": "Budućnost",
    "Fenerbahçe Istanbul": "Fenerbahçe",
    "Galatasaray S.K.": "Galatasaray",
    "Beşiktaş J.K. (Football)": "Beşiktaş",
    "1. FC Nürnberg": "Nürnberg",
    "1. FC Kaiserslautern": "Kaiserslautern",
    "FC Bayern Munich": "Bayern Munich",
    "VfB Stuttgart": "Stuttgart",
    "Tokyo Verdy": "Tokyo Verdy",
    "JEF United Chiba": "JEF United",
    "Kashiwa Reysol": "Kashiwa Reysol",
    "Cerezo Osaka": "Cerezo Osaka",
    "Clube Atlético Mineiro": "Atlético Mineiro",
    "Kashima Antlers": "Kashima Antlers",
    "Yokohama F. Marinos": "Yokohama F. Marinos",
    # --- extra shortenings for radial labels on the ring (2026-09) ----------
    "ACF Fiorentina": "Fiorentina",
    "SS Lazio": "Lazio",
    "AS Roma": "Roma",
    "SSC Napoli": "Napoli",
    "Parma Calcio 1913": "Parma",
    "Atalanta BC": "Atalanta",
    "RCD Espanyol de Barcelona": "Espanyol",
    "West Ham United F.C.": "West Ham",
    "Defensor Sporting Club": "Defensor",
    "Club Atlético Huracán": "Huracán",
    "Deportivo de A Coruña": "Deportivo",
    "C.F. Os Belenenses": "Belenenses",
    "OGC Nice": "Nice",
    "Argentinos Juniors": "Argentinos",
    "Newell's Old Boys": "Newell's",
    "San Jose Earthquakes": "San Jose",
    "FC Rostov": "Rostov",
    "RCD Mallorca": "Mallorca",
    "Real Valladolid": "Valladolid",
    "Atlético Madrid": "Atlético",
    "Spartak Moscow": "Spartak",
    "Toronto FC": "Toronto",
    "Vitória S.C.": "Vitória",
    "Racing Club": "Racing",
    "FC Shakhtar Donetsk": "Shakhtar",
    "Club Atlético Rosario Central": "Rosario Central",
}

# Reserve / second / youth / women's sides: dropped from the club set (see
# docstring). Matched against the club name.
RESERVE_RE = re.compile(
    r"(\bB\b| B$| II$| III$| IV$| B\.C\.|Reserve|Academy|Youth|U-?1[5-9]|"
    r"U-?2[0-3]|Under-?1|Next Gen|Castilla|Atl[eè]tic|Baskonia|Mestalla|"
    r"Fabril|Women|WFC|Ladies|Femin|\bC$)",
    re.I,
)

HISTORICAL_COUNTRIES = {
    "Czechoslovakia", "Soviet Union", "Yugoslavia", "East Germany",
    "West Germany", "Kingdom of Italy", "Austria-Hungary", "Russian Empire",
}
BAD_COUNTRIES = {"Campeonato Brasileiro Série C"}

# ---------------------------------------------------------------------------
# Query — one bounded pass over the sports_team_member type index
# ---------------------------------------------------------------------------

CELLS_AQL = """
FOR e IN affiliations
  FILTER e.type == 'sports_team_member'
  LET o = DOCUMENT(e._to)
  FILTER o.type == 'sports_club'
  LET p = DOCUMENT(e._from)
  FILTER p.wikidata_statements_count >= @min_statements
  COLLECT person = e._from INTO grp = e._to
  LET clubs = UNIQUE(grp)
  FOR a IN clubs
    FOR b IN clubs
      FILTER a <= b
      COLLECT ca = a, cb = b WITH COUNT INTO n
      FILTER ca == cb OR n >= @min_pair
      RETURN {a: ca, b: cb, count: n}
"""

# Tiny companion: the shared, notable players behind the named bridges.
EXAMPLES_AQL = """
FOR pr IN @pairs
  LET shared = (
    FOR e1 IN affiliations
      FILTER e1.type == 'sports_team_member' AND e1._to == pr[0]
      FOR e2 IN affiliations
        FILTER e2.type == 'sports_team_member'
          AND e2._to == pr[1] AND e2._from == e1._from
        LET p = DOCUMENT(e1._from)
        FILTER p.wikidata_statements_count >= @min_statements
        RETURN DISTINCT {n: p.name_en, s: p.wikidata_statements_count}
  )
  RETURN {a: pr[0], b: pr[1], people: shared}
"""

EDGE_COUNT_AQL = """
RETURN LENGTH(
  FOR e IN affiliations
    FILTER e.type == 'sports_team_member'
    LIMIT 1000000
    RETURN 1
)
"""

# Distinct notable players across the drawn clubs (the node-sum double counts).
DISTINCT_AQL = """
RETURN LENGTH(
  FOR e IN affiliations
    FILTER e.type == 'sports_team_member' AND e._to IN @ids
    LET p = DOCUMENT(e._from)
    FILTER p.wikidata_statements_count >= @min_statements
    COLLECT person = e._from
    RETURN 1
)
"""


# ---------------------------------------------------------------------------
# Rendering geometry (card units; the PNG renders at 2x)
# ---------------------------------------------------------------------------

CARD_W = 1600
CARD_H = 1000
NET_W = 1020          # radial chord panel, left of the reading key
NET_H = 740
NODE_R_MIN = 4.6
NODE_R_MAX = 18.5

# Radial ring geometry, in card units of arc length.
DOT_GAP = 6.0         # arc room between neighbouring club dots
COUNTRY_GAP = 20.0    # arc room between country blocks
MIN_COUNTRY_ARC = 54.0  # arc length reserved so every country name fits
LABEL_SIZE = 9.5      # club label size
COUNTRY_SIZE = 11.0   # country label size (follows the rim arc)
CHORD_BOW = 0.30      # how far chord control points are pulled to the centre

# Categorical country palette: sixteen accessible, well-separated hues on the
# warm off-white. None of them is the brand orange, which stays reserved for
# the strongest-bridge highlight. Only countries actually drawn appear in the
# legend, so every colour on the card is keyed.
COUNTRY_COLOR = {
    "Italy": "#2f6f9f",           # steel blue
    "United Kingdom": "#c15a86",  # rose
    "Brazil": "#3f7a4a",          # forest green
    "Germany": "#8a6d1f",         # bronze
    "France": "#5a5aa8",          # indigo
    "Spain": "#c9a227",           # gold
    "Russia": "#1f7f8a",          # teal
    "Portugal": "#7f8a2e",        # olive
    "Argentina": "#b0422f",       # brick red
    "Turkey": "#a3547f",          # raspberry
    "Ukraine": "#8f6fb5",         # light violet
    "United States": "#6d7f9c",   # slate
    "Japan": "#b06fa0",           # orchid
    "Uruguay": "#8a5a3c",         # brown
    "Czech Republic": "#6fae6a",  # light green
    "Canada": "#a8446f",          # deep raspberry
}

def _country_arc(country: str) -> str:
    """Compact uppercase name for the rim arcs, where space is tight."""
    return COUNTRY_SHORT.get(country, country).upper()


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


def _rows(db, aql: str, bind: dict | None = None, warn: str = "") -> list:
    try:
        return list(db.aql.execute(aql, bind_vars=bind or {}))
    except Exception as exc:  # noqa: BLE001 - a marketing job must not crash
        print(f"[warn] {warn or 'query'} failed: {exc}", file=sys.stderr)
        return []


def _documents(db, ids: list[str]) -> dict[str, dict]:
    """Batched DOCUMENT() lookups returning name / country / qid."""
    out: dict[str, dict] = {}
    ids = list(ids)
    for i in range(0, len(ids), 4000):
        chunk = ids[i : i + 4000]
        for r in _rows(
            db,
            "FOR d IN DOCUMENT(@ids) "
            "RETURN {id: d._id, name: d.name_en, qid: d.wikidata_id, "
            "country: d.country}",
            {"ids": chunk},
            warn="documents",
        ):
            out[r["id"]] = r
    return out


def examples_for(db, edges: list[dict], min_statements: int) -> dict:
    """Fetch the shared notable players for a small set of club pairs."""
    pairs = [[e["a"], e["b"]] for e in edges]
    if not pairs:
        return {}
    out: dict[tuple, list] = {}
    for r in _rows(db, EXAMPLES_AQL, {"pairs": pairs,
                                      "min_statements": min_statements},
                   warn="examples"):
        people = sorted(r.get("people") or [],
                        key=lambda p: (-int(p.get("s") or 0),
                                       (p.get("n") or "").lower()))
        out[(r["a"], r["b"])] = [p["n"] for p in people[:EXAMPLE_PEOPLE]]
    return out


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


def collect(min_statements: int, min_pair: int, node_floor: int,
            max_countries: int, per_country: int, max_nodes: int,
            extra_countries: list[str]) -> dict:
    db = get_db(get_arango_client(request_timeout=900))

    cells = _rows(db, CELLS_AQL,
                  {"min_statements": min_statements, "min_pair": min_pair},
                  warn="cells")
    total_edges = _rows(db, EDGE_COUNT_AQL, warn="edge-count")
    total_edges = int(total_edges[0]) if total_edges else 0

    nodes: dict[str, int] = {}
    edges: list[OrderedDict] = []
    for r in cells:
        if r["a"] == r["b"]:
            nodes[r["a"]] = int(r["count"])
        else:
            edges.append(OrderedDict(a=r["a"], b=r["b"], count=int(r["count"])))

    meta = _documents(db, list(nodes))

    def name_of(cid: str) -> str:
        return (meta.get(cid) or {}).get("name") or cid

    def country_of(cid: str) -> str:
        return (meta.get(cid) or {}).get("country") or "Unknown"

    def eligible(cid: str) -> bool:
        n = name_of(cid)
        return (not RESERVE_RE.search(n)
                and nodes[cid] >= node_floor
                and country_of(cid) not in HISTORICAL_COUNTRIES
                and country_of(cid) not in BAD_COUNTRIES)

    by_country: dict[str, list[str]] = defaultdict(list)
    for cid in nodes:
        if eligible(cid):
            by_country[country_of(cid)].append(cid)
    for c in by_country:
        by_country[c].sort(key=lambda i: (-nodes[i], name_of(i)))

    ranked = sorted(by_country,
                    key=lambda c: -sum(nodes[i] for i in by_country[c]))
    chosen = ranked[:max_countries]
    for c in extra_countries:
        if c in by_country and c not in chosen:
            chosen.append(c)
    # A forced country with fewer clubs than the round-robin needs is fine.

    # Round-robin so no single country swamps the frame, capped per country.
    selected: list[str] = []
    per_count: dict[str, int] = defaultdict(int)
    rank = 0
    while len(selected) < max_nodes and rank < 200:
        added = False
        for c in chosen:
            if per_count[c] >= per_country:
                continue
            if len(by_country[c]) > rank:
                selected.append(by_country[c][rank])
                per_count[c] += 1
                added = True
                if len(selected) >= max_nodes:
                    break
        if not added:
            break
        rank += 1

    sel = set(selected)
    edges = [e for e in edges if e["a"] in sel and e["b"] in sel]
    edges.sort(key=lambda e: (-e["count"], name_of(e["a"]), name_of(e["b"])))

    distinct = _rows(db, DISTINCT_AQL,
                     {"ids": selected, "min_statements": min_statements},
                     warn="distinct")
    distinct_players = int(distinct[0]) if distinct else 0

    def node_row(cid: str) -> OrderedDict:
        c = country_of(cid)
        return OrderedDict(
            id=cid,
            qid=(meta.get(cid) or {}).get("qid") or cid,
            name=name_of(cid),
            short=_short_club(name_of(cid)),
            country=c,
            country_short=COUNTRY_SHORT.get(c, c),
            notable=nodes[cid],
        )

    # Cross-border bridges carry the story; fetch their shared names. The
    # selection is stored so the renderer shows exactly the named pipelines.
    cross = [e for e in edges if country_of(e["a"]) != country_of(e["b"])]
    wanted = _pick_bridges(cross, country_of, limit=18)
    example_edges = list(wanted)
    seen_keys = {(e["a"], e["b"]) for e in wanted}
    for e in edges[:12]:  # the strongest overall pairs keep their names too
        if (e["a"], e["b"]) not in seen_keys:
            example_edges.append(e)
            seen_keys.add((e["a"], e["b"]))
    ex = examples_for(db, example_edges, min_statements)

    def edge_row(e: OrderedDict) -> OrderedDict:
        return OrderedDict(
            source=(meta.get(e["a"]) or {}).get("qid") or e["a"],
            target=(meta.get(e["b"]) or {}).get("qid") or e["b"],
            source_id=e["a"],
            target_id=e["b"],
            source_name=name_of(e["a"]),
            target_name=name_of(e["b"]),
            source_short=_short_club(name_of(e["a"])),
            target_short=_short_club(name_of(e["b"])),
            source_country=country_of(e["a"]),
            target_country=country_of(e["b"]),
            shared=e["count"],
            cross=country_of(e["a"]) != country_of(e["b"]),
            examples=ex.get((e["a"], e["b"]), []),
        )

    country_rows = []
    for c in chosen:
        members = [i for i in selected if country_of(i) == c]
        if not members:
            continue
        country_rows.append(OrderedDict(
            country=c,
            short=COUNTRY_SHORT.get(c, c),
            clubs=len(members),
            notable_players=sum(nodes[i] for i in members),
        ))
    country_rows.sort(key=lambda r: -r["notable_players"])

    edge_rows = [edge_row(e) for e in edges]
    cross_rows = [e for e in edge_rows if e["cross"]]
    same_rows = [e for e in edge_rows if not e["cross"]]

    top_pairs = [_pair_row(e) for e in edge_rows[:30]]
    return OrderedDict(
        generated=date.today().isoformat(),
        concept="club_player_flow_network",
        params=OrderedDict(
            min_statements=min_statements,
            min_pair=min_pair,
            node_floor=node_floor,
            max_countries=max_countries,
            per_country=per_country,
            max_nodes=max_nodes,
            extra_countries=list(extra_countries),
        ),
        scale=OrderedDict(
            sport_affiliations=total_edges,
            clubs_total=len(nodes),
            clubs_eligible=sum(len(v) for v in by_country.values()),
            clubs_selected=len(selected),
            countries_selected=len(country_rows),
            notable_players_selected=distinct_players,
            pairs_returned=len(edges),
            pairs_cross=len(cross_rows),
            pairs_intra=len(same_rows),
            bridge_pairs_considered=len(wanted),
        ),
        countries=country_rows,
        nodes=[node_row(i) for i in sorted(selected,
                                           key=lambda i: (-nodes[i], name_of(i)))],
        edges=edge_rows,
        bridge_keys=[[e["a"], e["b"]] for e in wanted],
        top_pairs=top_pairs,
        cross_bridges=[_pair_row(e) for e in cross_rows[:24]],
        domestic_links=[_pair_row(e) for e in same_rows[:12]],
        examples={
            f'{_short_club(name_of(e["a"]))} × {_short_club(name_of(e["b"]))}':
                ex.get((e["a"], e["b"]), [])
            for e in wanted
        },
    )


def _pair_row(e: dict) -> OrderedDict:
    return OrderedDict(
        a=e["source_short"], aq=e["source"],
        b=e["target_short"], bq=e["target"],
        a_country=e["source_country"], b_country=e["target_country"],
        count=e["shared"], cross=e["cross"],
        examples=e.get("examples") or [],
    )


def _pick_bridges(cross: list[dict], country_of, limit: int = 18) -> list[dict]:
    """Top cross-border pairs that do not repeat a club or (early) a country.

    One club per row keeps the leaderboard from filling with a single MLS side;
    the country-pair guard keeps the first half of the list geographically
    varied before repeats are allowed.
    """
    picked: list[dict] = []
    used: set = set()
    seen_country_pair: set = set()
    for e in sorted(cross, key=lambda e: -e["count"]):
        if e["a"] in used or e["b"] in used:
            continue
        cp = frozenset((country_of(e["a"]), country_of(e["b"])))
        if cp in seen_country_pair and len(picked) < limit // 2:
            continue
        used.add(e["a"])
        used.add(e["b"])
        seen_country_pair.add(cp)
        picked.append(e)
        if len(picked) >= limit:
            break
    return picked


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------


def _short_club(name: str) -> str:
    if name in CLUB_SHORT:
        return CLUB_SHORT[name]
    s = re.sub(r"\s*\(.*?\)\s*", " ", name or "").strip()
    s = re.sub(
        r"\s+(F\.C\.|A\.F\.C\.|S\.C\.|S\.L\.|S\.S\.C\.|A\.C\.|C\.F\.|C\.A\.|"
        r"F\.B\.C\.|C\.D\.|C\.S\.|S\.D\.|U\.C\.|A\.S\.|S\.S\.|"
        r"FC|AFC|SC|CF|AC|AS|CD|CS|SS|SS|FK|SK|NK|HNK|KFC)$",
        "", s).strip()
    return s or name


def _display_short(node: dict) -> str:
    """Radial label for a node, preferring the curated name over the JSON."""
    return CLUB_SHORT.get(node.get("name") or "", node.get("short")
                          or node.get("name") or "")


def _n(value) -> str:
    try:
        return f"{int(round(float(value))):,}"
    except (TypeError, ValueError):
        return "0"


# ---------------------------------------------------------------------------
# Analysis: weights, pruning to the ring, radii, cross-border bridges
# ---------------------------------------------------------------------------


def analyze(payload: dict, ring_nodes: int = RING_NODES,
            ring_edge_min: int = RING_EDGE_MIN) -> dict:
    nodes_all = payload["nodes"]
    edges_all = payload["edges"]

    degree: dict[str, int] = defaultdict(int)
    weight: dict[str, int] = defaultdict(int)
    for e in edges_all:
        degree[e["source_id"]] += 1
        degree[e["target_id"]] += 1
        weight[e["source_id"]] += e["shared"]
        weight[e["target_id"]] += e["shared"]
    for n in nodes_all:
        n["degree"] = degree.get(n["id"], 0)
        n["weight"] = weight.get(n["id"], 0)

    # --- choose the drawn clubs -------------------------------------------
    # The best-connected clubs by shared players, plus the endpoints of the
    # strongest cross-border links so every bridged country stays on the ring.
    cross_all = sorted(
        (e for e in edges_all if e["source_country"] != e["target_country"]),
        key=lambda e: (-e["shared"], e["source_short"], e["target_short"]))
    must: set[str] = set()
    for e in cross_all[:FEATURED_BRIDGES]:
        must.add(e["source_id"])
        must.add(e["target_id"])

    ranked = sorted(nodes_all, key=lambda n: (-n["weight"], -n["notable"],
                                              n["short"], n["id"]))
    keep = {n["id"] for n in ranked[:ring_nodes]} | must
    cap = ring_nodes + len(must)
    if len(keep) > cap:
        ordered = [n["id"] for n in ranked if n["id"] in keep]
        keep = set([i for i in ordered if i in must]
                   + [i for i in ordered if i not in must][:cap - len(must)])

    drawn = [n for n in nodes_all if n["id"] in keep]
    drawn.sort(key=lambda n: (-n["weight"], n["short"]))
    dropped = len(nodes_all) - len(drawn)
    edges = [e for e in edges_all
             if e["source_id"] in keep and e["target_id"] in keep]

    # --- dot size = the club's shared-player weight (within the drawn set) --
    wmin = min((n["weight"] for n in drawn), default=0)
    wmax = max((n["weight"] for n in drawn), default=0)

    def radius(w: int) -> float:
        if wmax <= wmin:
            return NODE_R_MIN + 6.0
        t = (w - wmin) / (wmax - wmin)
        return NODE_R_MIN + (NODE_R_MAX - NODE_R_MIN) * math.sqrt(max(0.0, t))

    for n in drawn:
        n["radius"] = round(radius(n["weight"]), 2)

    # --- countries present on the ring ------------------------------------
    by_country: dict[str, list[dict]] = defaultdict(list)
    for n in drawn:
        by_country[n["country"]].append(n)
    country_rows = []
    for c, members in by_country.items():
        country_rows.append(OrderedDict(
            country=c,
            short=COUNTRY_SHORT.get(c, c),
            clubs=len(members),
            notable_players=sum(m["notable"] for m in members),
            weight=sum(m["weight"] for m in members),
        ))
    country_rows.sort(key=lambda r: (-r["clubs"], -r["weight"], r["country"]))

    # --- strongest cross-border bridges among the drawn clubs -------------
    cross = sorted((e for e in edges if e["cross"]),
                   key=lambda e: (-e["shared"], e["source_short"],
                                  e["target_short"]))
    picked = _pick_bridge_rows(cross, limit=LEADERBOARD)
    bridges = [
        OrderedDict(
            a=e["source_short"], b=e["target_short"],
            a_id=e["source_id"], b_id=e["target_id"],
            a_country=e["source_country"], b_country=e["target_country"],
            count=e["shared"], examples=(e.get("examples") or [])[:EXAMPLE_PEOPLE],
        )
        for e in picked
    ]

    by_key = {(e["source_id"], e["target_id"]): e for e in edges}
    dom = max((e for e in edges if not e["cross"]),
              key=lambda e: e["shared"], default=None)

    # --- payload now reflects the ring ------------------------------------
    payload["nodes"] = drawn
    payload["edges"] = edges
    payload["countries"] = country_rows
    payload["bridges"] = bridges
    payload["featured_bridges"] = [
        OrderedDict(a=b["a"], b=b["b"], a_id=b["a_id"], b_id=b["b_id"],
                    count=b["count"], a_country=b["a_country"],
                    b_country=b["b_country"])
        for b in bridges[:FEATURED_BRIDGES]
    ]
    hub_ids = sorted(drawn, key=lambda n: (-n["degree"], -n["weight"]))
    payload["hubs"] = [
        OrderedDict(id=n["id"], name=n["name"], short=n["short"],
                    country=n["country_short"], notable=n["notable"],
                    degree=n["degree"], weight=n["weight"])
        for n in hub_ids[:24]
    ]
    payload["strongest_domestic"] = (
        OrderedDict(a=dom["source_short"], b=dom["target_short"],
                    count=dom["shared"], country=dom["source_country"])
        if dom else None)
    payload["domestic_links"] = [
        _pair_row(e) for e in sorted(
            (e for e in edges if not e["cross"]), key=lambda e: -e["shared"])[:12]
    ]
    payload["top_pairs"] = [
        _pair_row(e) for e in sorted(
            edges, key=lambda e: (-e["shared"], e["source_short"]))[:30]
    ]

    scale = payload["scale"]
    drawn_edges = [e for e in edges if e["shared"] >= ring_edge_min]
    scale["clubs_drawn"] = len(drawn)
    scale["clubs_dropped"] = dropped
    scale["countries_drawn"] = len(country_rows)
    scale["edges_drawn"] = len(drawn_edges)
    scale["edges_among_drawn"] = len(edges)
    scale["edges_strong_cross"] = sum(1 for e in drawn_edges if e["cross"])
    scale["notable_players_drawn"] = sum(n["notable"] for n in drawn)
    scale["max_shared"] = max((e["shared"] for e in edges), default=0)
    scale["max_cross"] = cross[0]["shared"] if cross else 0
    scale["strongest_domestic"] = dom["shared"] if dom else 0
    payload["params"]["ring_nodes"] = ring_nodes
    payload["params"]["ring_edge_min"] = ring_edge_min
    return payload


def _pick_bridge_rows(cross: list[dict], limit: int = LEADERBOARD) -> list[dict]:
    """One club per row, new country pairs first, then the strongest repeats."""
    picked: list[dict] = []
    used: set = set()
    seen: set = set()
    for first_half in (True, False):
        for e in cross:
            key = (e["source_id"], e["target_id"])
            if key in {(x["source_id"], x["target_id"]) for x in picked}:
                continue
            if e["source_id"] in used or e["target_id"] in used:
                continue
            cp = frozenset((e["source_country"], e["target_country"]))
            if first_half and cp in seen:
                continue
            used.add(e["source_id"])
            used.add(e["target_id"])
            seen.add(cp)
            picked.append(e)
            if len(picked) >= limit:
                return picked
    return picked


# ---------------------------------------------------------------------------
# Layout: clubs grouped into country arcs on one ring
# ---------------------------------------------------------------------------


def _esc(value) -> str:
    return (str(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _ang_dist(a: float, b: float) -> float:
    d = abs(a - b) % (2 * math.pi)
    return min(d, 2 * math.pi - d)


def _ring_order(ids: list, weight: dict) -> list:
    """Order the countries around the ring so heavy bridges are short.

    A greedy chain (always step to the country sharing the most players) is
    polished with circular 2-opt; sixteen countries at most, so this is cheap.
    """
    if len(ids) <= 2:
        return list(ids)
    remaining = set(ids)
    order = [ids[0]]
    remaining.discard(ids[0])
    while remaining:
        last = order[-1]
        nxt = max(remaining,
                  key=lambda x: (weight.get(tuple(sorted((last, x))), 0.0), x))
        order.append(nxt)
        remaining.discard(nxt)

    def cost(seq: list) -> float:
        n = len(seq)
        total = 0.0
        for i in range(n):
            for j in range(i + 1, n):
                w = weight.get(tuple(sorted((seq[i], seq[j]))), 0.0)
                if w:
                    total += w * min(j - i, n - (j - i))
        return total

    improved = True
    while improved:
        improved = False
        for i in range(1, len(order) - 1):
            for j in range(i + 1, len(order)):
                cand = order[:i] + order[i:j + 1][::-1] + order[j + 1:]
                if cost(cand) < cost(order) - 1e-9:
                    order = cand
                    improved = True
    return order


def _club_chain(members: list, edges: list, nodes: dict) -> list:
    """Chain a country's clubs so strongly linked ones sit side by side."""
    if len(members) <= 1:
        return list(members)
    w: dict = defaultdict(float)
    for e in edges:
        if e["source_id"] in members and e["target_id"] in members:
            w[frozenset((e["source_id"], e["target_id"]))] += e["shared"]
    start = max(members, key=lambda m: (nodes[m]["weight"], nodes[m]["notable"],
                                        nodes[m]["short"]))
    order = [start]
    remaining = set(members) - {start}
    while remaining:
        last = order[-1]
        nxt = max(remaining, key=lambda m: (w.get(frozenset((last, m)), 0.0),
                                            nodes[m]["weight"],
                                            nodes[m]["notable"],
                                            nodes[m]["short"]))
        order.append(nxt)
        remaining.discard(nxt)
    return order


def _pt(cx: float, cy: float, ang: float, r: float) -> tuple:
    return (cx + r * math.cos(ang), cy + r * math.sin(ang))


def _label_width(text: str, size: float) -> float:
    return len(text) * 0.56 * size


def _arc(cx: float, cy: float, r: float, a0: float, a1: float) -> str:
    x0, y0 = _pt(cx, cy, a0, r)
    x1, y1 = _pt(cx, cy, a1, r)
    delta = a1 - a0
    large = 1 if abs(delta) > math.pi else 0
    sweep = 1 if delta >= 0 else 0
    return (f"M{x0:.2f},{y0:.2f} A{r:.2f},{r:.2f} 0 {large} {sweep} "
            f"{x1:.2f},{y1:.2f}")


def build_layout(payload: dict) -> dict:
    """Place the clubs on a ring, grouped into contiguous country arcs."""
    nodes = {n["id"]: n for n in payload["nodes"]}
    edges = payload["edges"]
    countries = [c for c in payload["countries"]
                 if any(n["country"] == c["country"] for n in payload["nodes"])]
    cids = [c["country"] for c in countries]

    cweight: dict = defaultdict(float)
    for e in edges:
        if e["source_country"] != e["target_country"]:
            cweight[tuple(sorted((e["source_country"], e["target_country"])))] \
                += e["shared"]
    corder = _ring_order(cids, cweight)

    chains = {c: _club_chain([n["id"] for n in payload["nodes"]
                              if n["country"] == c], edges, nodes)
              for c in cids}
    ncountry = len(corder)

    def geometry(gap: float, country_gap: float):
        sizes = {}
        for _cid, seq in chains.items():
            for nid in seq:
                sizes[nid] = 2.0 * nodes[nid]["radius"] + gap
        # Guarantee every country a minimum arc so its rim name fits; small
        # countries (one or two clubs) get empty arc padding around their dots.
        node_sum = {cid: sum(sizes[nid] for nid in chains[cid])
                    for cid in corder}
        extra = sum(max(0.0, MIN_COUNTRY_ARC - node_sum[cid])
                    for cid in corder)
        total = sum(sizes.values()) + country_gap * ncountry + extra
        r = total / (2.0 * math.pi)
        angles = {}
        spans = {}
        cursor = -math.pi / 2.0
        for cid in corder:
            start = cursor
            pad = max(0.0, MIN_COUNTRY_ARC - node_sum[cid]) / 2.0
            cursor += 2.0 * math.pi * pad / total
            for nid in chains[cid]:
                width = 2.0 * math.pi * sizes[nid] / total
                angles[nid] = cursor + width / 2.0
                cursor += width
            cursor += 2.0 * math.pi * pad / total
            spans[cid] = (start, cursor)
            cursor += 2.0 * math.pi * country_gap / total
        return r, angles, spans

    def cost(chains_try: dict) -> float:
        _r, angles, _spans = geometry(DOT_GAP, COUNTRY_GAP)
        total = 0.0
        for e in edges:
            a = angles.get(e["source_id"])
            b = angles.get(e["target_id"])
            if a is not None and b is not None:
                total += e["shared"] * _ang_dist(a, b)
        return total

    best = cost(chains)
    for _pass in range(6):
        improved = False
        for cid in corder:
            base = chains[cid]
            if len(base) <= 1:
                continue
            options = [base, base[::-1]]
            options += [base[k:] + base[:k] for k in range(1, len(base))]
            pick, pick_cost = base, best
            for opt in options:
                chains[cid] = opt
                c = cost(chains)
                if c < pick_cost - 1e-6:
                    pick, pick_cost = opt, c
            chains[cid] = pick
            if pick_cost < best - 1e-6:
                best = pick_cost
                improved = True
        if not improved:
            break

    # Fit the ring inside the panel; shrink the smallest gaps first.
    geom = None
    for gap, country_gap in ((DOT_GAP, COUNTRY_GAP), (5.0, 17.0), (4.0, 14.0),
                             (3.0, 11.0), (2.0, 8.0)):
        r, angles, spans = geometry(gap, country_gap)
        r_fam = r
        for nid in angles:
            end = (r + nodes[nid]["radius"] + 3.0
                   + _label_width(_display_short(nodes[nid]), LABEL_SIZE))
            r_fam = max(r_fam, end)
        r_fam += 15.0
        geom = (r, angles, spans, r_fam)
        if r_fam <= NET_H / 2.0 - 12.0:
            break
    r, angles, spans, r_fam = geom

    cx, cy = NET_W / 2.0, NET_H / 2.0
    lay_nodes = {}
    for nid in nodes:
        x, y = _pt(cx, cy, angles[nid], r)
        lay_nodes[nid] = {"angle": angles[nid], "x": x, "y": y,
                          "r": nodes[nid]["radius"]}

    groups = []
    for cid in corder:
        cl = next(c for c in countries if c["country"] == cid)
        a0, a1 = spans[cid]
        arc_len = r_fam * (a1 - a0)
        short = _country_arc(cid)
        size = COUNTRY_SIZE
        # 0.74 per glyph covers the tracked uppercase; keep the name inside the
        # arc so SVG textPath never clips its leading characters.
        width = len(short) * 0.74 * size
        if width > arc_len - 8.0 and arc_len > 8.0:
            size = max(7.6, size * (arc_len - 8.0) / width)
        groups.append({
            "id": cid, "label": cl["short"], "short": short,
            "color": COUNTRY_COLOR.get(cid, "#8a8278"),
            "span": (a0, a1), "mid": (a0 + a1) / 2.0,
            "size": round(size, 2),
            "members": [m for m in chains[cid] if m in lay_nodes],
        })

    return {"cx": cx, "cy": cy, "R": r, "R_fam": r_fam,
            "nodes": lay_nodes, "countries": groups, "corder": corder}


def _cubic_point(p0: tuple, c1: tuple, c2: tuple, p1: tuple, t: float) -> tuple:
    mt = 1.0 - t
    x = (mt ** 3 * p0[0] + 3 * mt * mt * t * c1[0]
         + 3 * mt * t * t * c2[0] + t ** 3 * p1[0])
    y = (mt ** 3 * p0[1] + 3 * mt * mt * t * c1[1]
         + 3 * mt * t * t * c2[1] + t ** 3 * p1[1])
    return (x, y)


# ---------------------------------------------------------------------------
# SVG scene
# ---------------------------------------------------------------------------


def build_scene(payload: dict, lay: dict) -> str:
    nodes = {n["id"]: n for n in payload["nodes"]}
    layout = lay["nodes"]
    edges = [e for e in payload["edges"]
             if e["source_id"] in layout and e["target_id"] in layout]
    cx, cy, R = lay["cx"], lay["cy"], lay["R"]
    r_fam = lay["R_fam"]
    ccolor = {c["id"]: c["color"] for c in lay["countries"]}

    counts = [e["shared"] for e in edges] or [1]
    log_min = math.log10(max(1, min(counts)))
    log_max = math.log10(max(1, max(counts)))

    def strength(count: int) -> float:
        if log_max - log_min < 1e-6:
            return 1.0
        t = (math.log10(max(1, count)) - log_min) / (log_max - log_min)
        return min(1.0, max(0.0, t))

    def chord_d(e: dict):
        x1, y1 = layout[e["source_id"]]["x"], layout[e["source_id"]]["y"]
        x2, y2 = layout[e["target_id"]]["x"], layout[e["target_id"]]["y"]
        c1 = (x1 + (cx - x1) * CHORD_BOW, y1 + (cy - y1) * CHORD_BOW)
        c2 = (x2 + (cx - x2) * CHORD_BOW, y2 + (cy - y2) * CHORD_BOW)
        d = (f"M{x1:.2f},{y1:.2f} C{c1[0]:.2f},{c1[1]:.2f} "
             f"{c2[0]:.2f},{c2[1]:.2f} {x2:.2f},{y2:.2f}")
        return d, (x1, y1), c1, c2, (x2, y2)

    def within_d(e: dict, radius: float) -> str:
        a1 = layout[e["source_id"]]["angle"]
        a2 = layout[e["target_id"]]["angle"]
        delta = (a2 - a1) % (2 * math.pi)
        if delta > math.pi:
            delta -= 2 * math.pi
        large = 1 if abs(delta) > math.pi else 0
        sweep = 1 if delta >= 0 else 0
        x1, y1 = _pt(cx, cy, a1, radius)
        x2, y2 = _pt(cx, cy, a2, radius)
        return (f"M{x1:.2f},{y1:.2f} A{radius:.2f},{radius:.2f} 0 {large} "
                f"{sweep} {x2:.2f},{y2:.2f}")

    within = sorted([e for e in edges if not e["cross"]],
                    key=lambda e: e["shared"])
    cross = sorted([e for e in edges if e["cross"]],
                   key=lambda e: e["shared"])

    # --- defs: dot grid, one blended gradient per cross-country chord, and
    # the arc paths that carry the country names around the rim -------------
    defs = ['<pattern id="dots" width="22" height="22" '
            'patternUnits="userSpaceOnUse">'
            '<circle cx="1.4" cy="1.4" r="1.1" fill="rgba(28,21,16,.06)"/>'
            '</pattern>']
    gradients = {}
    for i, e in enumerate(cross):
        x1, y1 = layout[e["source_id"]]["x"], layout[e["source_id"]]["y"]
        x2, y2 = layout[e["target_id"]]["x"], layout[e["target_id"]]["y"]
        ca = ccolor.get(e["source_country"], "#8a8278")
        cb = ccolor.get(e["target_country"], "#8a8278")
        gid = f"chordgrad{i}"
        gradients[(e["source_id"], e["target_id"])] = gid
        defs.append(
            f'<linearGradient id="{gid}" gradientUnits="userSpaceOnUse" '
            f'x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}">'
            f'<stop offset="0" stop-color="{ca}"/>'
            f'<stop offset=".16" stop-color="{ca}"/>'
            f'<stop offset=".84" stop-color="{cb}"/>'
            f'<stop offset="1" stop-color="{cb}"/></linearGradient>')
    for g in lay["countries"]:
        a0, a1 = g["span"]
        if math.sin(g["mid"]) < 0:        # top half reads left-to-right
            pa, pb, sweep = a0, a1, 1
        else:                             # bottom half: reverse to stay upright
            pa, pb, sweep = a1, a0, 0
        x0, y0 = _pt(cx, cy, pa, r_fam)
        x1, y1 = _pt(cx, cy, pb, r_fam)
        defs.append(
            f'<path id="carc-{_gid(g["id"])}" d="M{x0:.2f},{y0:.2f} '
            f'A{r_fam:.2f},{r_fam:.2f} 0 0 {sweep} {x1:.2f},{y1:.2f}"/>')

    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {NET_W} {NET_H}" '
        f'width="{NET_W}" height="{NET_H}" role="img" '
        f'aria-label="Football clubs arranged in a ring by country, joined by '
        f'chords for the players who played for both">')
    parts.append("<defs>" + "".join(defs) + "</defs>")
    parts.append(f'<rect x="0" y="0" width="{NET_W}" height="{NET_H}" '
                 f'fill="url(#dots)"/>')

    # --- country arcs on the rim ------------------------------------------
    for g in lay["countries"]:
        a0, a1 = g["span"]
        parts.append(
            f'<path d="{_arc(cx, cy, R, a0, a1)}" fill="none" '
            f'stroke="{g["color"]}" stroke-opacity="0.16" stroke-width="11" '
            f'stroke-linecap="round"><title>{_esc(g["label"])}: '
            f'{len(g["members"])} clubs drawn</title></path>')

    drawn: dict = {}
    # --- domestic ties reinforce their own arc -----------------------------
    for e in within:
        t = strength(e["shared"])
        radius = R - 10.0 - 4.0 * t
        d = within_d(e, radius)
        a1 = layout[e["source_id"]]["angle"]
        a2 = layout[e["target_id"]]["angle"]
        delta = (a2 - a1) % (2 * math.pi)
        if delta > math.pi:
            delta -= 2 * math.pi
        drawn[(e["source_id"], e["target_id"])] = (d, 0.7 + 2.3 * t,
                                                   ("arc", a1 + delta / 2.0,
                                                    radius))
        color = ccolor.get(e["source_country"], "#8a8278")
        parts.append(
            f'<path d="{d}" fill="none" stroke="{color}" '
            f'stroke-opacity="{0.22 + 0.55 * t ** 0.85:.3f}" '
            f'stroke-width="{0.8 + 2.6 * t ** 0.85:.2f}" stroke-linecap="round">'
            f'<title>{_esc(e["source_short"])} \u2014 {_esc(e["target_short"])}: '
            f'{_n(e["shared"])} players wore both \u00b7 same country</title>'
            f'</path>')

    # --- cross-border chords: colour blends the two countries --------------
    for e in cross:
        t = strength(e["shared"])
        d, p0, c1, c2, p1 = chord_d(e)
        width = 0.4 + 3.2 * t ** 1.35
        drawn[(e["source_id"], e["target_id"])] = (d, width,
                                                   ("cubic", p0, c1, c2, p1))
        parts.append(
            f'<path d="{d}" fill="none" '
            f'stroke="url(#{gradients[(e["source_id"], e["target_id"])]})" '
            f'stroke-opacity="{0.10 + 0.60 * t ** 1.6:.3f}" '
            f'stroke-width="{width:.2f}" stroke-linecap="round"><title>'
            f'{_esc(e["source_short"])} \u2014 {_esc(e["target_short"])}: '
            f'{_n(e["shared"])} players wore both \u00b7 '
            f'{_esc(e["source_country"])} \u2194 {_esc(e["target_country"])}'
            f'</title></path>')

    # --- strongest cross-border links: a soft orange halo (listed in panel) --
    for b in payload.get("featured_bridges", []):
        key = (b["a_id"], b["b_id"])
        if key not in drawn:
            continue
        d, width, _ctrl = drawn[key]
        parts.append(
            f'<path d="{d}" fill="none" stroke="#bf6b28" stroke-opacity="0.22" '
            f'stroke-width="{width + 7.5:.2f}" stroke-linecap="round"/>')

    # --- club dots (size is the data) --------------------------------------
    for nid in sorted(layout, key=lambda m: layout[m]["r"]):
        nd = layout[nid]
        n = nodes[nid]
        color = ccolor.get(n["country"], "#8a8278")
        parts.append(
            f'<circle cx="{nd["x"]:.1f}" cy="{nd["y"]:.1f}" r="{nd["r"]:.1f}" '
            f'fill="{color}" stroke="#f9f8f4" stroke-width="1.7"><title>'
            f'{_esc(_display_short(n))}: {_n(n["notable"])} notable players '
            f'\u00b7 {n["weight"]:,} shared-player weight \u00b7 '
            f'{_esc(n["country"])}</title></circle>')

    # --- club labels, radial so 56 never collide ---------------------------
    for nid in sorted(layout, key=lambda m: -nodes[m]["weight"]):
        nd = layout[nid]
        a = nd["angle"]
        lx, ly = _pt(cx, cy, a, R + nd["r"] + 5.0)
        deg = math.degrees(a)
        if math.cos(a) >= -1e-9:
            anchor, rot = "start", deg
        else:
            anchor, rot = "end", deg + 180.0
        parts.append(
            f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="{anchor}" '
            f'dominant-baseline="middle" class="nlabel" '
            f'transform="rotate({rot:.1f} {lx:.1f} {ly:.1f})">'
            f'{_esc(_display_short(nodes[nid]))}</text>')

    # --- country names follow their arc ------------------------------------
    for g in lay["countries"]:
        parts.append(
            f'<text class="country-label" font-size="{g["size"]}" '
            f'text-anchor="middle"><title>{_esc(g["label"])}</title>'
            f'<textPath href="#carc-{_gid(g["id"])}" startOffset="50%">'
            f'{_esc(g["short"])}</textPath></text>')

    parts.append("</svg>")
    return "".join(parts)


def _gid(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


# ---------------------------------------------------------------------------
# Card HTML
# ---------------------------------------------------------------------------

HTML_HEAD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>HumansMap — the club shared-player network</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;800&family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:#f9f8f4; --text:#1c1510;
    --chrome:#1b4d8e;          /* chrome only: wordmark, eyebrow, headline emphasis */
    --data:#bf6b28;            /* data highlight only: strongest cross-border links */
    --structure:#8a8278;       /* structure: dot grid, neutral marks */
    --surface:#ece8de; --line:rgba(28,21,16,.10);
    --display:'Playfair Display', Georgia, serif; --body:'DM Sans', system-ui, sans-serif;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:#d9d4c9; font-family:var(--body); padding:32px; }
  .card { width:1600px; height:1000px; background:var(--bg); color:var(--text);
    padding:30px 44px 16px; display:flex; flex-direction:column; position:relative;
    overflow:hidden; }
  .card header { display:flex; justify-content:space-between; align-items:baseline; }
  .eyebrow { font:700 15px/1 var(--body); letter-spacing:.2em; text-transform:uppercase;
    color:var(--chrome); }
  .brand { font:700 27px/1 var(--display); color:var(--chrome); }
  h1 { font:800 34px/1.06 var(--display); margin:9px 0 0; letter-spacing:-.012em;
    max-width:60ch; }
  h1 em { font-style:normal; color:var(--chrome); }
  .deck { font:400 13.4px/1.42 var(--body); color:var(--structure); margin:7px 0 0;
    max-width:1512px; }
  .deck b { color:var(--text); font-weight:700; }
  .stage { margin-top:6px; flex:0 0 740px; display:flex; gap:32px; }
  .chart { flex:0 0 1020px; width:1020px; height:740px; }
  .chart svg { display:block; width:1020px; height:740px; overflow:visible; }
  .nlabel { font:500 9.5px/1 var(--body); fill:var(--text); paint-order:stroke;
    stroke:#f9f8f4; stroke-width:2.6px; stroke-linejoin:round; }
  .country-label { font:700 11px/1 var(--body); letter-spacing:.15em;
    fill:var(--structure); paint-order:stroke; stroke:#f9f8f4; stroke-width:3px; }
  .badge { font:700 9.6px/1 var(--body); fill:#fff; }
  .keys { flex:1 1 auto; min-width:0; display:flex; flex-direction:column; }
  .plabel { font:700 11px/1 var(--body); letter-spacing:.16em; text-transform:uppercase;
    color:var(--structure); margin-bottom:9px; }
  .read { display:flex; flex-direction:column; gap:7px; font:400 12px/1.32 var(--body);
    color:var(--structure); }
  .read .row { display:flex; align-items:center; gap:9px; }
  .read b { color:var(--text); }
  .swatch { flex:0 0 auto; }
  .ccdot { flex:0 0 auto; width:11px; height:11px; border-radius:3px;
    display:inline-block; }
  .badge-dot { flex:0 0 auto; width:17px; height:17px; border-radius:50%;
    background:var(--data); color:#fff; font:700 9.6px/17px var(--body);
    text-align:center; }
  .cgrid { display:grid; grid-template-columns:1fr 1fr; gap:4px 16px;
    margin:10px 0 2px; }
  .cc { display:flex; align-items:center; gap:7px; font:500 11.4px/1.2 var(--body);
    color:var(--text); }
  .stats { display:flex; gap:16px; margin-top:12px; padding-top:10px;
    border-top:1px solid var(--line); }
  .stat { flex:1 1 0; }
  .stat b { display:block; font:800 18px/1 var(--display); color:var(--text); }
  .stat span { display:block; font:500 9.3px/1.25 var(--body); color:var(--structure);
    text-transform:uppercase; letter-spacing:.06em; margin-top:3px; }
  .bridges { list-style:none; margin:2px 0 0; padding:0; }
  .bridges li { padding:6px 0 5px; border-bottom:1px solid var(--line); }
  .bhead { display:flex; align-items:baseline; gap:9px; }
  .bnum { flex:0 0 auto; width:16px; height:16px; border-radius:50%;
    background:var(--data); color:#fff; font:700 9.6px/16px var(--body);
    text-align:center; }
  .bname { font:600 12.4px/1.25 var(--body); flex:1 1 auto; }
  .bname b { color:var(--text); }
  .bcount { font:800 14px/1 var(--display); color:var(--data); white-space:nowrap; }
  .bmeta { font:400 10.2px/1.3 var(--body); color:var(--structure);
    margin:3px 0 0 25px; }
  .caveat { font:400 10.8px/1.4 var(--body); color:var(--structure); margin-top:auto;
    padding-top:9px; border-top:1px solid var(--line); }
  .caveat b { color:var(--text); }
  .card footer { margin-top:8px; display:flex; justify-content:space-between;
    align-items:baseline; font:400 12.3px/1.4 var(--body); color:var(--structure); }
</style></head>
<body>"""


def render_html(payload: dict) -> str:
    if "bridges" not in payload:
        payload = analyze(payload)
    lay = build_layout(payload)
    scene = build_scene(payload, lay)

    scale = payload["scale"]
    params = payload["params"]
    shown = payload["bridges"]
    strongest = shown[0] if shown else None
    dom = payload.get("strongest_domestic")

    headline = "Football clubs, linked by the players who turned out for both."

    deck = (
        f"Two clubs are linked when <b>the same player played for both</b>, from "
        f"Wikidata&rsquo;s <b>{_n(scale['sport_affiliations'])}</b> sports-team "
        f"records. The <b>{_n(scale['clubs_drawn'])}</b> clubs drawn (the best-connected, "
        f"plus the endpoints of the strongest cross-border links) are "
        f"arranged in a ring, grouped and coloured by <b>country</b>; ties inside "
        f"a country hug the rim, and a chord crosses the middle only when two "
        f"countries share players. Dot size is the players a club shares in total, "
        f"chord thickness how many players the two clubs share, and a chord blends "
        f"the <b>two countries&rsquo; colours</b>. Links drawn: "
        f"<b>{_n(scale['edges_drawn'])}</b> of {_n(scale['edges_among_drawn'])} "
        f"pairs with {_n(params['ring_edge_min'])}+ players."
        + (f" The widest cross-border link: <b>{_n(strongest['count'])} players</b> "
           f"wore both {_esc(strongest['a'])}&rsquo;s and {_esc(strongest['b'])}&rsquo;s "
           f"shirts." if strongest else "")
    )

    def bridge_li(b: dict, idx: int) -> str:
        people = " &middot; ".join(_esc(p) for p in b["examples"][:EXAMPLE_PEOPLE])
        return (
            f'<li><div class="bhead"><span class="bnum">{idx}</span>'
            f'<span class="bname"><b>{_esc(b["a"])}</b> \u2194 {_esc(b["b"])}</span>'
            f'<span class="bcount">{_n(b["count"])}</span></div>'
            f'<div class="bmeta">{_esc(b["a_country"])} \u2194 '
            f'{_esc(b["b_country"])}'
            + (f' &middot; {people}' if people else "")
            + '</div></li>'
        )

    caveat = (
        "<b>Read with care.</b> Wikidata records <b>stints, not transfer events</b>: "
        "&ldquo;played for both&rdquo; does not prove a move between the two clubs "
        "(loans, youth squads and national-team eras count). Coverage is uneven and "
        "football-heavy (the UK, Italy and Brazil dominate the record), so this is "
        "partly a map of Wikipedia. Reserve, youth and women&rsquo;s sides are "
        "removed. For legibility the ring shows the "
        f"<b>{_n(scale['clubs_drawn'])}</b> drawn of the "
        f"<b>{_n(scale['clubs_drawn'] + scale['clubs_dropped'])}</b> clubs mined \u2014 "
        f"the other <b>{_n(scale['clubs_dropped'])}</b> are omitted. Dot size and "
        "chord thickness count only well-documented players."
    )

    # A representative gradient swatch uses the strongest cross-border pair.
    if strongest:
        ga = COUNTRY_COLOR.get(strongest["a_country"], "#8a8278")
        gb = COUNTRY_COLOR.get(strongest["b_country"], "#8a8278")
    else:
        ga, gb = COUNTRY_COLOR["Brazil"], COUNTRY_COLOR["Italy"]

    legend = (
        '<div class="read">'
        '<div class="row"><svg class="swatch" width="58" height="20">'
        '<circle cx="7" cy="10" r="3" fill="#8a8278"/>'
        '<circle cx="24" cy="10" r="5.4" fill="#8a8278"/>'
        '<circle cx="45" cy="10" r="8" fill="#8a8278"/></svg>'
        '<span>Each dot is a club; its <b>size</b> = the players it shares in '
        'total</span></div>'
        '<div class="row"><svg class="swatch" width="58" height="16">'
        '<defs><linearGradient id="lg-chord" x1="0" y1="0" x2="1" y2="0">'
        f'<stop offset="0" stop-color="{ga}"/>'
        f'<stop offset="1" stop-color="{gb}"/></linearGradient></defs>'
        '<path d="M2,12 C20,2 38,2 56,12" fill="none" stroke="url(#lg-chord)" '
        'stroke-width="3"/></svg>'
        '<span>A chord links two clubs; <b>thickness</b> = players who wore '
        'both shirts</span></div>'
        '<div class="row"><span class="ccdot" style="background:#3f7a4a"></span>'
        '<span>Colour = <b>country</b> (key below); a chord blends the two '
        'countries it crosses</span></div>'
        '<div class="row"><svg class="swatch" width="58" height="18">'
        '<line x1="2" y1="15" x2="56" y2="15" stroke="#c9c1b4" stroke-width="1.4"/>'
        '<path d="M9,15 A19,10 0 0 1 49,15" fill="none" stroke="#8a8278" '
        'stroke-width="2.2" stroke-linecap="round"/></svg>'
        '<span>A domestic tie hugs the rim; a chord across the middle links two '
        '<b>different countries</b></span></div>'
        '<div class="row"><svg class="swatch" width="58" height="14">'
        '<path d="M2,7 C20,2 38,2 56,7" fill="none" stroke="#bf6b28" '
        'stroke-opacity="0.28" stroke-width="8" stroke-linecap="round"/>'
        '<path d="M2,7 C20,2 38,2 56,7" fill="none" stroke="#bf6b28" '
        'stroke-width="2.2" stroke-linecap="round"/></svg>'
        '<span>An orange halo marks one of the <b>strongest cross-border '
        'links</b> (listed at right)</span></div>'
        '</div>'
    )

    cgrid = "".join(
        f'<div class="cc"><span class="ccdot" style="background:{c["color"]}">'
        f'</span><span>{_esc(c["label"])}</span></div>'
        for c in lay["countries"])

    stats = (
        f'<div class="stat"><b>{_n(scale["clubs_drawn"])}</b>'
        '<span>clubs drawn</span></div>'
        f'<div class="stat"><b>{_n(scale["edges_drawn"])}</b>'
        '<span>links drawn</span></div>'
        f'<div class="stat"><b>{_n(scale["countries_drawn"])}</b>'
        '<span>countries</span></div>'
        f'<div class="stat"><b>{_n(scale["max_cross"])}</b>'
        '<span>widest cross-border link</span></div>'
    )

    dom_line = ""
    if dom:
        dom_line = (
            f'<div class="bmeta" style="margin-top:6px">Densest same-country link: '
            f'<b>{_esc(dom["a"])}</b> &times; <b>{_esc(dom["b"])}</b>, '
            f'{_n(dom["count"])} players.</div>'
        )

    html = f"""<section class="card">
  <header>
    <span class="eyebrow">The club network</span>
    <span class="brand">Humans&nbsp;Map</span>
  </header>
  <h1>{headline}</h1>
  <p class="deck">{deck}</p>
  <div class="stage">
    <div class="chart">{scene}</div>
    <aside class="keys">
      <div class="plabel">How to read it</div>
      {legend}
      <div class="cgrid">{cgrid}</div>
      <div class="stats">{stats}</div>
      <div class="plabel" style="margin-top:11px">Strongest cross-border links &mdash; the same players, both shirts</div>
      <ul class="bridges">{''.join(bridge_li(b, i) for i, b in enumerate(shown, start=1))}</ul>
      {dom_line}
      <div class="caveat">{caveat}</div>
    </aside>
  </div>
  <footer>
    <span>Data: Wikidata (CC BY-SA) &middot; humansmap.com</span>
    <span>{_n(scale['clubs_drawn'])} clubs &middot; {_n(scale['edges_drawn'])} links &middot; {_n(scale['countries_drawn'])} countries</span>
  </footer>
</section>"""

    return HTML_HEAD + html + "\n</body></html>\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default="docs/marketing/clubs")
    parser.add_argument("--min-statements", type=int, default=DEFAULT_MIN_STATEMENTS)
    parser.add_argument("--min-pair", type=int, default=DEFAULT_MIN_PAIR)
    parser.add_argument("--node-floor", type=int, default=DEFAULT_NODE_FLOOR)
    parser.add_argument("--max-countries", type=int, default=DEFAULT_MAX_COUNTRIES)
    parser.add_argument("--per-country", type=int, default=DEFAULT_PER_COUNTRY)
    parser.add_argument("--max-nodes", type=int, default=DEFAULT_MAX_NODES)
    parser.add_argument("--ring-nodes", type=int, default=RING_NODES)
    parser.add_argument("--ring-edge-min", type=int, default=RING_EDGE_MIN)
    parser.add_argument("--extra-countries", default=",".join(DEFAULT_EXTRA_COUNTRIES))
    parser.add_argument("--from-json", default=None)
    parser.add_argument("--no-html", action="store_true")
    args = parser.parse_args()

    extra = [c.strip() for c in args.extra_countries.split(",") if c.strip()]

    if args.from_json:
        payload = json.loads(Path(args.from_json).read_text())
        for key in ("bridges", "featured_bridges", "hubs", "strongest_domestic",
                    "domestic_links", "top_pairs", "cross_bridges"):
            payload.pop(key, None)
    else:
        payload = collect(args.min_statements, args.min_pair, args.node_floor,
                          args.max_countries, args.per_country, args.max_nodes,
                          extra)

    analyze(payload, ring_nodes=args.ring_nodes,
            ring_edge_min=args.ring_edge_min)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not args.from_json:
        json_path = out_dir / "clubs.json"
        json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        print(f"wrote {json_path}")

    if not args.no_html:
        html_path = out_dir / "clubs.html"
        html_path.write_text(render_html(payload))
        print(f"wrote {html_path}")

    print(json.dumps({
        "scale": payload["scale"],
        "countries": payload["countries"],
        "bridges": [
            {"a": b["a"], "b": b["b"],
             "countries": f'{b["a_country"]} x {b["b_country"]}',
             "count": b["count"], "examples": b["examples"]}
            for b in payload["bridges"]
        ],
        "hubs": [
            {"club": h["short"], "country": h["country"], "notable": h["notable"],
             "degree": h["degree"], "weight": h["weight"]}
            for h in payload["hubs"][:12]
        ],
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
