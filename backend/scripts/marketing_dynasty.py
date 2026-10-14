#!/usr/bin/env python3
"""Marketing dynasty miner — read-only, emits the "family lineage above the cap" card.

The insight: a family's *full* documented lineage is bigger than the feed card's
15-member cap. Starting from one seed QID, walk the ``relationships`` edge types
``parent`` / ``child`` / ``spouse`` / ``sibling`` (optionally
``unmarried_partner``) to a bounded depth, collect every reachable person with a
recorded life date, and render a Gantt of lifetimes grouped by generation.

It never writes to ArangoDB. The traversal is bounded twice:

  * ``--depth`` (default 3, values 2-3) — the number of relationship hops from
    the seed;
  * ``--max-members`` (default 1500) — a hard ceiling on visited persons, so a
    hyper-connected seed can never fan out unboundedly.

The reachable set is cached **into the emitted JSON**, together with the edges,
so the card can be regenerated without re-querying the live DB:

    # first run: traverse the DB and write JSON + HTML
    docker compose exec -T backend python - --out-dir /tmp/dynasty \\
        < backend/scripts/marketing_dynasty.py
    # later: re-render the card from the cached JSON only (no DB access)
    docker compose exec -T backend python - --from-json /tmp/dynasty/dynasty.json \\
        --out-dir /tmp/dynasty < backend/scripts/marketing_dynasty.py

Flags:
    --seed KEY|QID      candidate key (see CANDIDATE_SEEDS) or a raw QID
    --depth N           relationship hops, 2 or 3 (default 3)
    --max-members N     hard ceiling on visited persons (default 1500)
    --include-partner   also follow ``unmarried_partner`` edges
    --out-dir PATH      output directory (default: docs/marketing/dynasty)
    --from-json PATH    skip the DB; re-render HTML from a cached payload
    --no-html           emit only the JSON payload
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import OrderedDict, defaultdict, deque
from datetime import date
from pathlib import Path

# When run as a file inside the container this resolves to /app; when piped
# over stdin (``python -``) __file__ is "<stdin>" and /app is cwd, so fall back.
try:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
except (NameError, OSError):  # pragma: no cover - stdin invocation
    sys.path.insert(0, str(Path.cwd()))

from app.database import get_arango_client, get_db  # noqa: E402

# ---------------------------------------------------------------------------
# Candidate seeds
# ---------------------------------------------------------------------------

# QIDs were verified against the live graph (DOCUMENT / search) before this
# list was written: each entry's ``name`` is the graph's ``name_en`` for that
# QID. The miner re-verifies the chosen seed at run time and refuses to render
# a seed whose name does not match.
CANDIDATE_SEEDS = OrderedDict(
    [
        ("medici", {"qid": "Q177854", "name": "Lorenzo de' Medici", "family": "Medici"}),
        ("medici-cosimo", {"qid": "Q48544", "name": "Cosimo de' Medici", "family": "Medici"}),
        ("bonaparte", {"qid": "Q517", "name": "Napoleon", "family": "Bonaparte"}),
        ("bonaparte-iii", {"qid": "Q7721", "name": "Napoleon III", "family": "Bonaparte"}),
        ("habsburg", {"qid": "Q51056", "name": "Franz Joseph I of Austria", "family": "Habsburg"}),
        ("windsor", {"qid": "Q9439", "name": "Victoria", "family": "Windsor / Hanover"}),
        ("kennedy", {"qid": "Q9696", "name": "John F. Kennedy", "family": "Kennedy"}),
        ("rockefeller", {"qid": "Q160278", "name": "John D. Rockefeller", "family": "Rockefeller"}),
        ("tudor", {"qid": "Q38370", "name": "Henry VIII of England", "family": "Tudor"}),
        ("bourbon", {"qid": "Q7742", "name": "Louis XIV of France", "family": "Bourbon"}),
        ("carolingian", {"qid": "Q3044", "name": "Charlemagne", "family": "Carolingian"}),
    ]
)

DEFAULT_SEED = "medici"
DEFAULT_DEPTH = 3
DEFAULT_MAX_MEMBERS = 1500
PLOT_MAX_MEMBERS = 160  # cap plotted rows so labels stay large; extras are noted
BASE_TYPES = ["parent", "child", "spouse", "sibling"]
PARTNER_TYPE = "unmarried_partner"
NEIGHBOR_BATCH = 500
DOC_BATCH = 3000

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
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "0"


def _year(value) -> int | None:
    """Parse a Wikidata date string to a (possibly negative) year."""
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        if s.startswith("-"):
            return -int(s[1:5])
        return int(s[:4])
    except (TypeError, ValueError):
        return None


def _pretty(occ: str) -> str:
    return (occ or "").replace("_", " ").strip()


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


def _rows(db, aql: str, bind: dict | None = None) -> list:
    try:
        return list(db.aql.execute(aql, bind_vars=bind or {}))
    except Exception as exc:  # noqa: BLE001 - a marketing job must not crash
        print(f"[warn] query failed: {exc}", file=sys.stderr)
        return []


def _fetch_people(db, ids: list[str]) -> dict[str, dict]:
    """Batched ``DOCUMENT`` lookup for Arango person ids."""
    out: dict[str, dict] = {}
    ids = list(ids)
    for i in range(0, len(ids), DOC_BATCH):
        chunk = ids[i : i + DOC_BATCH]
        for r in _rows(
            db,
            "FOR d IN DOCUMENT(@ids) "
            "RETURN {id: d._id, qid: d.wikidata_id, name: d.name_en, "
            "birth: d.birth_date, death: d.death_date, "
            "occ: d.primary_occupation, n: d.wikidata_statements_count}",
            {"ids": chunk},
        ):
            if r.get("id"):
                out[r["id"]] = r
    return out


def _incident_edges(db, ids: list[str], types: list[str]) -> list[dict]:
    """All edges of ``types`` touching any of ``ids`` (edge-index friendly)."""
    rows: list[dict] = []
    for i in range(0, len(ids), NEIGHBOR_BATCH):
        chunk = ids[i : i + NEIGHBOR_BATCH]
        rows.extend(
            _rows(
                db,
                "FOR e IN relationships FILTER e.type IN @types "
                "AND (e._from IN @chunk OR e._to IN @chunk) "
                "RETURN {f: e._from, t: e._to, ty: e.type}",
                {"types": types, "chunk": chunk},
            )
        )
    return rows


def _document(db, qid: str) -> dict | None:
    rows = _rows(
        db,
        "RETURN DOCUMENT(@id)",
        {"id": f"persons/{qid}"},
    )
    return rows[0] if rows and rows[0] else None


# ---------------------------------------------------------------------------
# Traversal
# ---------------------------------------------------------------------------

# Direction rules for an incident edge ``f --ty--> t``:
#   parent : f is the child, t is the parent      (f -> t = -1 generation)
#   child  : f is the parent, t is the child      (f -> t = +1 generation)
#   spouse / sibling / unmarried_partner          (f -> t =  same generation)
_DELTA_AT_FROM = {"parent": -1, "child": 1, "spouse": 0, "sibling": 0, PARTNER_TYPE: 0}
_DELTA_AT_TO = {"parent": 1, "child": -1, "spouse": 0, "sibling": 0, PARTNER_TYPE: 0}


def traverse(db, seed_qid: str, depth: int, types: list[str], max_members: int):
    """Bounded BFS over family edges. Returns (hop, gen, edges, capped)."""
    start = f"persons/{seed_qid}"
    hop: dict[str, int] = {start: 0}
    gen: dict[str, int] = {start: 0}
    edges: dict[tuple, dict] = {}
    frontier = [start]
    capped = False
    d = 0
    while frontier and d < depth:
        nxt: list[str] = []
        seen_chunk: set[str] = set(frontier)
        for e in _incident_edges(db, frontier, types):
            f, t, ty = e["f"], e["t"], e["ty"]
            if f in seen_chunk:
                delta = _DELTA_AT_FROM.get(ty, 0)
                edges[(f, t, ty)] = {"f": f, "t": t, "ty": ty}
                if t not in hop:
                    hop[t] = d + 1
                    gen[t] = gen[f] + delta
                    nxt.append(t)
                    if len(hop) > max_members:
                        capped = True
                        frontier = []
                        break
            if t in seen_chunk:
                delta = _DELTA_AT_TO.get(ty, 0)
                edges[(f, t, ty)] = {"f": f, "t": t, "ty": ty}
                if f not in hop:
                    hop[f] = d + 1
                    gen[f] = gen[t] + delta
                    nxt.append(f)
                    if len(hop) > max_members:
                        capped = True
                        frontier = []
                        break
        frontier = nxt
        d += 1
    return hop, gen, list(edges.values()), capped


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


def collect(seed_qid: str, seed_name: str, seed_family: str, depth: int,
            types: list[str], max_members: int) -> dict:
    db = get_db(get_arango_client(request_timeout=300))
    hop, gen, edges_raw, capped = traverse(db, seed_qid, depth, types, max_members)

    people = _fetch_people(db, list(hop.keys()))

    members: list[dict] = []
    no_date = 0
    for pid, p in people.items():
        by = _year(p.get("birth"))
        dy = _year(p.get("death"))
        if by is None and dy is None:
            no_date += 1
            continue
        members.append(
            OrderedDict(
                qid=p.get("qid"),
                name=p.get("name") or p.get("qid"),
                birth=p.get("birth"),
                death=p.get("death"),
                birth_year=by,
                death_year=dy,
                occupation=_pretty(p.get("occ")) or None,
                generation=int(gen.get(pid, 0)),
                statements=int(p.get("n") or 0),
                seed=(pid == f"persons/{seed_qid}"),
            )
        )

    # Order by birth year, then name; birth-less members fall in by death year.
    def sort_key(m):
        if m["birth_year"] is not None:
            return (0, m["birth_year"], m["name"] or "")
        return (1, m["death_year"] if m["death_year"] is not None else 9999, m["name"] or "")

    members.sort(key=sort_key)

    by_gen: dict[int, int] = defaultdict(int)
    for m in members:
        by_gen[m["generation"]] += 1
    gens = sorted(by_gen)
    both = [m for m in members if m["birth_year"] is not None and m["death_year"] is not None]
    births = [m["birth_year"] for m in members if m["birth_year"] is not None]
    deaths = [m["death_year"] for m in members if m["death_year"] is not None]

    # Edges kept only where both endpoints are dated members.
    member_qids = {m["qid"] for m in members}
    by_id_qid = {pid: p.get("qid") for pid, p in people.items()}
    edges: list[dict] = []
    seen_edge: set[tuple] = set()
    for e in edges_raw:
        fq, tq = by_id_qid.get(e["f"]), by_id_qid.get(e["t"])
        if not fq or not tq or fq not in member_qids or tq not in member_qids:
            continue
        ty = e["ty"]
        if ty == "child":  # normalize the inverse duplicate to a parent edge
            fq, tq, ty = tq, fq, "parent"
        a, b = (fq, tq) if ty == "parent" else tuple(sorted((fq, tq)))
        key = (a, b, ty)
        if key in seen_edge:
            continue
        seen_edge.add(key)
        edges.append({"from": a, "to": b, "type": ty})

    seed_row = next((m for m in members if m["seed"]), None)
    payload = {
        "generated": date.today().isoformat(),
        "seed": {"qid": seed_qid, "name": seed_name, "family": seed_family},
        "params": {
            "depth": depth,
            "types": types,
            "max_members": max_members,
        },
        "stats": {
            "reachable_total": len(hop),
            "members_with_dates": len(members),
            "members_with_both_dates": len(both),
            "members_without_dates": no_date,
            "generations": len(gens),
            "generation_min": gens[0] if gens else None,
            "generation_max": gens[-1] if gens else None,
            "by_generation": {str(g): by_gen[g] for g in gens},
            "edges": len(edges),
            "earliest_birth": min(births) if births else None,
            "latest_birth": max(births) if births else None,
            "latest_death": max(deaths) if deaths else None,
            "capped": capped,
        },
        "members": members,
        "edges": edges,
    }
    if seed_row:
        payload["stats"]["seed_birth"] = seed_row["birth_year"]
        payload["stats"]["seed_death"] = seed_row["death_year"]
    return payload


# ---------------------------------------------------------------------------
# Gantt scene (inline SVG)
# ---------------------------------------------------------------------------

SVG_W = 1512
SVG_H = 690
NAME_R = 286          # right edge of the name column (names are right-aligned here)
LEAD_X = 292          # where the dotted leader leaves the name column
PLOT_X0 = 316
PLOT_X1 = SVG_W - 6
BAND_HEADER = 15.5
AXIS_H = 18
NAME_SIZE = 9.0
AGE_SIZE = 8.4

# Orange-only generation ramp (light -> dark). Blue is never used for data.
GEN_RAMP = [
    "#e6bd93", "#dfa972", "#d69355", "#c97f3d", "#b96b2b",
    "#a85a20", "#934a17", "#7d3b10", "#672d0a",
]
# Contrast-safe sibling of GEN_RAMP for the 9px name text: every step is a
# darker shade of the same bar colour, so the row reads by generation while the
# smallest label still clears 4.5:1 on the warm off-white. (The brightest
# GEN_RAMP step is only ~1.6:1 and would be illegible as text.)
TEXT_RAMP = [
    "#a4631f", "#9c5b19", "#925214", "#874a11", "#7c420e",
    "#703a0b", "#633208", "#552906", "#471f04",
]
SEED_MARK = "#1c1510"

# DM Sans advance widths as em fractions (weight 400/700), measured once in a
# browser. The miner has no browser at generation time, so it estimates label
# widths here to truncate over-long names with an ellipsis and to keep the
# end-of-bar labels on canvas.
W400 = {' ': 0.320, '!': 0.400, '"': 0.460, '#': 0.840, '%': 0.950, '&': 0.780, "'": 0.270, '(': 0.390, ')': 0.390, '*': 0.500, '+': 0.840, ',': 0.320, '-': 0.360, '.': 0.320, '/': 0.340, '0': 0.640, '1': 0.640, '2': 0.640, '3': 0.640, '4': 0.640, '5': 0.640, '6': 0.640, '7': 0.640, '8': 0.640, '9': 0.640, ':': 0.340, ';': 0.340, '<': 0.840, '=': 0.840, '>': 0.840, '?': 0.530, '@': 1.000, 'A': 0.680, 'B': 0.690, 'C': 0.700, 'D': 0.770, 'E': 0.630, 'F': 0.580, 'G': 0.770, 'H': 0.750, 'I': 0.300, 'J': 0.300, 'K': 0.660, 'L': 0.560, 'M': 0.860, 'N': 0.750, 'O': 0.790, 'P': 0.600, 'Q': 0.790, 'R': 0.690, 'S': 0.630, 'T': 0.610, 'U': 0.730, 'V': 0.680, 'W': 0.990, 'X': 0.690, 'Y': 0.610, 'Z': 0.690, '[': 0.390, ']': 0.390, 'a': 0.610, 'b': 0.630, 'c': 0.550, 'd': 0.630, 'e': 0.620, 'f': 0.350, 'g': 0.630, 'h': 0.630, 'i': 0.280, 'j': 0.280, 'k': 0.580, 'l': 0.280, 'm': 0.970, 'n': 0.630, 'o': 0.610, 'p': 0.630, 'q': 0.630, 'r': 0.410, 's': 0.520, 't': 0.390, 'u': 0.630, 'v': 0.590, 'w': 0.820, 'x': 0.590, 'y': 0.590, 'z': 0.520, '{': 0.640, '|': 0.340, '}': 0.640, '·': 0.320, 'À': 0.680, 'Á': 0.680, 'Â': 0.680, 'Ã': 0.680, 'Ä': 0.680, 'Å': 0.680, 'Æ': 0.970, 'Ç': 0.700, 'È': 0.630, 'É': 0.630, 'Ê': 0.630, 'Ë': 0.630, 'Ì': 0.300, 'Í': 0.300, 'Î': 0.300, 'Ï': 0.300, 'Ñ': 0.750, 'Ò': 0.790, 'Ó': 0.790, 'Ô': 0.790, 'Õ': 0.790, 'Ö': 0.790, 'Ø': 0.790, 'Ù': 0.730, 'Ú': 0.730, 'Û': 0.730, 'Ü': 0.730, 'Ý': 0.610, 'ß': 0.630, 'à': 0.610, 'á': 0.610, 'â': 0.610, 'ã': 0.610, 'ä': 0.610, 'å': 0.610, 'æ': 0.980, 'ç': 0.550, 'è': 0.620, 'é': 0.620, 'ê': 0.620, 'ë': 0.620, 'ì': 0.280, 'í': 0.280, 'î': 0.280, 'ï': 0.280, 'ñ': 0.630, 'ò': 0.610, 'ó': 0.610, 'ô': 0.610, 'õ': 0.610, 'ö': 0.610, 'ø': 0.610, 'ù': 0.630, 'ú': 0.630, 'û': 0.630, 'ü': 0.630, 'ý': 0.590, 'ÿ': 0.590, 'Œ': 1.070, 'œ': 1.020, '–': 0.500, '—': 1.000, '‘': 0.320, '’': 0.320, '“': 0.520, '”': 0.520, '…': 1.000}
W700 = {' ': 0.350, '!': 0.460, '"': 0.520, '#': 0.840, '%': 1.000, '&': 0.870, "'": 0.310, '(': 0.460, ')': 0.460, '*': 0.520, '+': 0.840, ',': 0.380, '-': 0.420, '.': 0.380, '/': 0.370, '0': 0.700, '1': 0.700, '2': 0.700, '3': 0.700, '4': 0.700, '5': 0.700, '6': 0.700, '7': 0.700, '8': 0.700, '9': 0.700, ':': 0.400, ';': 0.400, '<': 0.840, '=': 0.840, '>': 0.840, '?': 0.580, '@': 1.000, 'A': 0.770, 'B': 0.760, 'C': 0.730, 'D': 0.830, 'E': 0.680, 'F': 0.680, 'G': 0.820, 'H': 0.840, 'I': 0.370, 'J': 0.370, 'K': 0.770, 'L': 0.640, 'M': 1.000, 'N': 0.840, 'O': 0.850, 'P': 0.730, 'Q': 0.850, 'R': 0.770, 'S': 0.720, 'T': 0.680, 'U': 0.810, 'V': 0.770, 'W': 1.100, 'X': 0.770, 'Y': 0.720, 'Z': 0.730, '[': 0.460, ']': 0.460, 'a': 0.670, 'b': 0.720, 'c': 0.590, 'd': 0.720, 'e': 0.680, 'f': 0.440, 'g': 0.720, 'h': 0.710, 'i': 0.340, 'j': 0.340, 'k': 0.670, 'l': 0.340, 'm': 1.040, 'n': 0.710, 'o': 0.690, 'p': 0.720, 'q': 0.720, 'r': 0.490, 's': 0.600, 't': 0.480, 'u': 0.710, 'v': 0.650, 'w': 0.920, 'x': 0.650, 'y': 0.650, 'z': 0.580, '{': 0.710, '|': 0.370, '}': 0.710, '·': 0.380, 'À': 0.770, 'Á': 0.770, 'Â': 0.770, 'Ã': 0.770, 'Ä': 0.770, 'Å': 0.770, 'Æ': 1.090, 'Ç': 0.730, 'È': 0.680, 'É': 0.680, 'Ê': 0.680, 'Ë': 0.680, 'Ì': 0.370, 'Í': 0.370, 'Î': 0.370, 'Ï': 0.370, 'Ñ': 0.840, 'Ò': 0.850, 'Ó': 0.850, 'Ô': 0.850, 'Õ': 0.850, 'Ö': 0.850, 'Ø': 0.850, 'Ù': 0.810, 'Ú': 0.810, 'Û': 0.810, 'Ü': 0.810, 'Ý': 0.720, 'ß': 0.720, 'à': 0.670, 'á': 0.670, 'â': 0.670, 'ã': 0.670, 'ä': 0.670, 'å': 0.670, 'æ': 1.050, 'ç': 0.590, 'è': 0.680, 'é': 0.680, 'ê': 0.680, 'ë': 0.680, 'ì': 0.340, 'í': 0.340, 'î': 0.340, 'ï': 0.340, 'ñ': 0.710, 'ò': 0.690, 'ó': 0.690, 'ô': 0.690, 'õ': 0.690, 'ö': 0.690, 'ø': 0.690, 'ù': 0.710, 'ú': 0.710, 'û': 0.710, 'ü': 0.710, 'ý': 0.650, 'ÿ': 0.650, 'Œ': 1.170, 'œ': 1.090, '–': 0.500, '—': 1.000, '‘': 0.380, '’': 0.380, '“': 0.660, '”': 0.660, '…': 1.000}


def _ramp(i: int, n: int) -> str:
    if n <= 1:
        return GEN_RAMP[3]
    idx = round(i * (len(GEN_RAMP) - 1) / (n - 1))
    return GEN_RAMP[idx]


def _text_ramp(i: int, n: int) -> str:
    if n <= 1:
        return TEXT_RAMP[3]
    idx = round(i * (len(TEXT_RAMP) - 1) / (n - 1))
    return TEXT_RAMP[idx]


def _text_w(text: str, size: float, weight: int = 400) -> float:
    table = W700 if weight >= 600 else W400
    return sum(table.get(ch, 0.62) for ch in text) * size


def _fit(text: str, size: float, max_w: float, weight: int = 400) -> str:
    """Truncate ``text`` with a trailing ellipsis so it fits ``max_w`` pixels."""
    if _text_w(text, size, weight) <= max_w:
        return text
    ell = "\u2026"
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _text_w(text[:mid].rstrip() + ell, size, weight) <= max_w:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + ell


def build_scene(payload: dict) -> str:
    members = payload["members"]
    gens = sorted({m["generation"] for m in members})
    ramp = {g: _ramp(i, len(gens)) for i, g in enumerate(gens)}
    tramp = {g: _text_ramp(i, len(gens)) for i, g in enumerate(gens)}

    births = [m["birth_year"] for m in members if m["birth_year"] is not None]
    deaths = [m["death_year"] for m in members if m["death_year"] is not None]
    if not births:
        return "<svg></svg>"
    lo = min(births)
    hi = max(deaths) if deaths else max(births)
    lo = (lo // 25) * 25 - 5
    hi = ((hi // 25) + 1) * 25 + 5
    span = max(1, hi - lo)

    def x(year):
        return PLOT_X0 + (year - lo) / span * (PLOT_X1 - PLOT_X0)

    n_rows = len(members)
    n_bands = len(gens)
    plot_top = AXIS_H
    plot_h = SVG_H - AXIS_H
    row_h = max(4.0, (plot_h - n_bands * BAND_HEADER) / max(1, n_rows))

    # Pre-compute the row geometry so the zebra stripes can be laid down first,
    # underneath the grid and the bars.
    bands: list[tuple] = []
    rows_layout: list[tuple] = []  # (member, gen, row_top, index_in_band)
    y = plot_top
    for g in gens:
        band_rows = [m for m in members if m["generation"] == g]
        band_top = y
        y += BAND_HEADER
        for i, m in enumerate(band_rows):
            rows_layout.append((m, g, y, i))
            y += row_h
        bands.append((g, band_rows, band_top))

    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {SVG_W} {SVG_H}" '
        f'width="{SVG_W}" height="{SVG_H}" role="img" '
        f'aria-label="Lifetimes of the documented lineage, grouped by generation">'
    )

    # --- faint zebra: every other row, full width --------------------------
    for _m, _g, row_top, i in rows_layout:
        if i % 2 == 1:
            parts.append(
                f'<rect x="0" y="{row_top:.1f}" width="{SVG_W}" height="{row_h:.1f}" class="rowzebra"/>'
            )

    # --- time axis ---------------------------------------------------------
    parts.append(f'<line x1="{PLOT_X0}" y1="{AXIS_H - 1}" x2="{PLOT_X1}" y2="{AXIS_H - 1}" class="axis"/>')
    tick = (lo // 25) * 25
    while tick <= hi:
        if tick >= lo:
            xx = x(tick)
            parts.append(f'<line x1="{xx:.1f}" y1="{AXIS_H - 5}" x2="{xx:.1f}" y2="{SVG_H}" class="grid"/>')
            parts.append(
                f'<text x="{xx:.1f}" y="{AXIS_H - 7}" class="tick" text-anchor="middle">{tick}</text>'
            )
        tick += 25

    # --- generation bands --------------------------------------------------
    for g, band_rows, band_top in bands:
        label = "GEN 0" if g == 0 else ("GEN " + ("+" if g > 0 else "\u2212") + str(abs(g)))
        yrs = [m["birth_year"] for m in band_rows if m["birth_year"] is not None]
        rng = f"born {min(yrs)}\u2013{max(yrs)}" if yrs else "no birth years"
        parts.append(
            f'<rect x="0" y="{band_top:.1f}" width="{SVG_W}" height="{BAND_HEADER:.1f}" class="bandbg"/>'
        )
        parts.append(
            f'<text x="{NAME_R:.1f}" y="{band_top + BAND_HEADER - 4.0:.1f}" class="bandlabel" text-anchor="end">'
            f'{_esc(label)}</text>'
        )
        parts.append(
            f'<text x="{PLOT_X0 + 6:.1f}" y="{band_top + BAND_HEADER - 4.0:.1f}" class="bandmeta">'
            f'{len(band_rows)} members &middot; {rng}</text>'
        )

    # --- one row per lifetime ---------------------------------------------
    for m, g, row_top, _i in rows_layout:
        by, dy = m["birth_year"], m["death_year"]
        cy = row_top + row_h / 2
        bh = max(2.6, row_h - 2.6)
        by_ = row_top + (row_h - bh) / 2
        title = (
            f'{_esc(m["name"])} ({by if by is not None else "?"}'
            f'\u2013{dy if dy is not None else "?"})'
        )
        if by is not None and dy is not None:
            x0 = x(by)
            x1 = max(x(dy), x0 + 2.2)
            end_x = x1
            anchor_x = x0
            label = str(max(0, int(dy) - int(by)))
            label_kind = "age"
            parts.append(
                f'<rect x="{x0:.1f}" y="{by_:.1f}" width="{x1 - x0:.1f}" '
                f'height="{bh:.1f}" rx="1" fill="{ramp[g]}" class="life">'
                f'<title>{title}</title></rect>'
            )
        else:
            # Only one date recorded: a short hollow marker at the known edge,
            # never a bar to the plot edge (which would imply a life span).
            if by is not None:
                x0 = x(by)
                end_x = x0 + 11.0
                anchor_x = x0
                label = f"b. {by}"
            else:  # death only
                end_x = x(dy)
                x0 = end_x - 11.0
                anchor_x = x0 - 2.2
                label = f"d. {dy}"
            label_kind = "date"
            parts.append(
                f'<rect x="{x0:.1f}" y="{by_:.1f}" width="{max(4.0, end_x - x0):.1f}" '
                f'height="{bh:.1f}" rx="1" fill="#f9f8f4" stroke="{ramp[g]}" '
                f'stroke-width="0.9" stroke-dasharray="1.8 1.2" class="openmark">'
                f'<title>{title}</title></rect>'
            )

        # Dotted leader binding the name column to the birth (or known) end,
        # capped by a small filled dot in the same shade as the name.
        lead_x0 = LEAD_X + (7.6 if m["seed"] else 0.0)
        parts.append(
            f'<line x1="{lead_x0:.1f}" y1="{cy:.1f}" x2="{anchor_x:.1f}" y2="{cy:.1f}" class="leader"/>'
        )
        parts.append(
            f'<circle cx="{anchor_x:.1f}" cy="{cy:.1f}" r="1.7" fill="{tramp[g]}" class="birthdot"/>'
        )

        # Lifespan / known-date annotation at the far end of the bar.
        lw = _text_w(label, AGE_SIZE) + 2
        if end_x + 4 + lw <= PLOT_X1 - 1:
            lx, anchor = end_x + 4, "start"
        else:
            lx, anchor = end_x - 4, "end"
        cls = "agelabel" if label_kind == "age" else "datelabel"
        parts.append(
            f'<text x="{lx:.1f}" y="{cy + 3.0:.1f}" class="{cls}" text-anchor="{anchor}">{_esc(label)}</text>'
        )

        # Name: colour-coded by generation, ellipsised to fit the column.
        name = m["name"] or m["qid"]
        weight = " seed" if m["seed"] else ""
        display = _fit(name, NAME_SIZE, NAME_R - 8, 700 if m["seed"] else 400)
        parts.append(
            f'<text x="{NAME_R:.1f}" y="{cy + 3.2:.1f}" class="mlabel{weight}" '
            f'text-anchor="end" fill="{tramp[g]}">{_esc(display)}</text>'
        )
        if m["seed"]:
            parts.append(
                f'<polygon points="{LEAD_X:.1f},{cy - 3.0:.1f} {LEAD_X:.1f},{cy + 3.0:.1f} '
                f'{LEAD_X + 5.4:.1f},{cy:.1f}" fill="{SEED_MARK}"/>'
            )

    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Card HTML
# ---------------------------------------------------------------------------

HTML_HEAD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>HumansMap: a family lineage</title>
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
  .card { width:1600px; height:__CARD_H__px; background:var(--bg); color:var(--text);
    padding:36px 44px 22px; display:flex; flex-direction:column; position:relative; overflow:hidden; }
  .card header { display:flex; justify-content:space-between; align-items:baseline; }
  .eyebrow { font:700 16px/1 var(--body); letter-spacing:.2em; text-transform:uppercase; color:var(--accent); }
  .brand { font:700 30px/1 var(--display); color:var(--accent); }
  h1 { font:800 47px/1.04 var(--display); margin:14px 0 0; letter-spacing:-.012em; max-width:40ch; }
  h1 em { font-style:normal; color:var(--person); }
  .deck { font:400 17.5px/1.45 var(--body); color:var(--muted); margin:9px 0 0; max-width:1500px; }
  .deck b { color:var(--text); font-weight:700; }
  .topright { text-align:right; }
  .stats { display:flex; gap:26px; justify-content:flex-end; margin-top:2px; }
  .stat .v { font:800 27px/1 var(--display); }
  .stat .k { font:500 11px/1.25 var(--body); color:var(--muted); text-transform:uppercase; letter-spacing:.08em; margin-top:3px; }
  .chart { margin-top:8px; position:relative; }
  .chart > svg { display:block; width:1512px; height:__CHART_H__px; overflow:visible; }
  .axis { stroke:rgba(28,21,16,.34); stroke-width:1.4; }
  .grid { stroke:rgba(28,21,16,.075); stroke-width:1; }
  .tick { font:500 12.5px/1 var(--body); fill:var(--muted); }
  .rowzebra { fill:rgba(28,21,16,.028); }
  .bandbg { fill:rgba(138,130,120,.16); }
  .bandlabel { font:700 12.5px/1 var(--body); letter-spacing:.14em; fill:var(--text); }
  .bandmeta { font:400 11px/1 var(--body); fill:var(--muted); }
  .mlabel { font:400 9px/1 var(--body); }
  .mlabel.seed { font-weight:700; }
  .leader { stroke:rgba(138,130,120,.62); stroke-width:.6; stroke-dasharray:.6 2.6; stroke-linecap:round; }
  .birthdot { stroke:#f9f8f4; stroke-width:.7; }
  .agelabel { font:500 8.4px/1 var(--body); fill:#6f6a63; }
  .datelabel { font:400 8.4px/1 var(--body); fill:#6f6a63; }
  .life { stroke:rgba(28,21,16,.22); stroke-width:.5px; }
  .legend { display:flex; flex-wrap:wrap; gap:8px 16px; align-items:center; margin-top:8px; }
  .legend .item { display:flex; align-items:center; gap:7px; font:400 12px/1 var(--body); color:var(--muted); white-space:nowrap; }
  .legend i { display:inline-block; width:24px; height:9px; border-radius:2px; }
  .legend svg { display:block; }
  .legend .agemark { font:500 12px/1 var(--body); color:#6f6a63; }
  .card footer { margin-top:auto; padding-top:10px; display:flex; justify-content:space-between;
    align-items:baseline; font:400 13px/1.4 var(--body); color:var(--muted); }
  .card footer b { color:var(--text); font-weight:700; }
</style></head>
<body>"""


def render_html(payload: dict) -> str:
    global SVG_H
    stats = payload["stats"]
    seed = payload["seed"]
    members = stats["members_with_dates"]
    gens = stats["generations"]
    e0, e1 = stats.get("earliest_birth"), stats.get("latest_death")
    depth = payload["params"]["depth"]
    family = seed.get("family") or seed["name"]

    # Adaptive height: larger families get a taller canvas so labels stay legible.
    all_members = payload.get("members") or []
    truncated = 0
    scene_payload = payload
    if len(all_members) > PLOT_MAX_MEMBERS:
        seed_qid = seed["qid"]
        ranked = sorted(all_members, key=lambda m: (m.get("statements") or 0), reverse=True)
        kept = ranked[:PLOT_MAX_MEMBERS]
        if not any(m["qid"] == seed_qid for m in kept):
            kept[-1] = next(m for m in all_members if m["qid"] == seed_qid)
        kept = sorted(kept, key=lambda m: (m.get("generation", 0), m.get("birth_year") or 0))
        scene_payload = {**payload, "members": kept}
        truncated = len(all_members) - len(kept)
    n_rows = len(scene_payload.get("members") or [])
    SVG_H = int(min(2400, max(690, round(n_rows * 13.2 + 150))))
    card_h = SVG_H + 348

    h1 = (
        f"The {_esc(family)} family tree: <em>{_n(members)} people</em>, "
        f"<em>{gens} generations</em>."
    )
    deck = (
        f"Every relative reachable from <b>{_esc(seed['name'])}</b> ({_esc(seed['qid'])}) within "
        f"<b>{depth} steps</b> of parent, child, spouse or sibling links: "
        f"<b>{_n(stats['reachable_total'])} people</b>, <b>{_n(members)}</b> of them with a recorded life "
        f"date, spanning <b>{e0}&ndash;{e1}</b>. Each bar is one lifetime, grouped by generation."
    )
    if truncated:
        deck += (
            f" Showing the <b>{_n(n_rows)}</b> best-documented of {_n(len(all_members))} "
            f"dated relatives so every name stays legible."
        )

    scene = build_scene(scene_payload)

    gens_sorted = sorted({m["generation"] for m in payload["members"]})
    ramp = {g: _ramp(i, len(gens_sorted)) for i, g in enumerate(gens_sorted)}
    plotted_by_gen: dict[str, int] = {}
    for m in scene_payload.get("members") or []:
        key = str(m["generation"])
        plotted_by_gen[key] = plotted_by_gen.get(key, 0) + 1
    leg_items = []
    for g in gens_sorted:
        label = "gen 0" if g == 0 else ("gen +%d" % g if g > 0 else "gen \u2212%d" % abs(g))
        count = plotted_by_gen.get(str(g), 0)
        leg_items.append(
            f'<span class="item"><i style="background:{ramp[g]}"></i>{_esc(label)} '
            f'<span style="color:#1c1510">{count}</span></span>'
        )
    leader_glyph = (
        '<svg width="34" height="10" viewBox="0 0 34 10" aria-hidden="true">'
        '<line x1="1" y1="5" x2="24" y2="5" stroke="rgba(138,130,120,.85)" '
        'stroke-width="1.1" stroke-dasharray="1 3" stroke-linecap="round"/>'
        '<circle cx="26.5" cy="5" r="2.8" fill="#a4631f" stroke="#f9f8f4" stroke-width=".8"/>'
        '</svg>'
    )

    card = f"""<section class="card">
  <header>
    <span class="eyebrow">Dynasty timeline</span>
    <span class="brand">Humans&nbsp;Map</span>
  </header>
  <h1>{h1}</h1>
  <p class="deck">{deck}</p>
  <div class="chart">
    {scene}
    <div class="legend">
      <span class="item" style="font-weight:700;color:#1c1510">Generation:</span>
      {''.join(leg_items)}
      <span class="item">{leader_glyph}leader links name to birth</span>
      <span class="item"><span class="agemark">67</span> = age at death</span>
      <span class="item"><i style="background:#f9f8f4;border:1px dashed #bf6b28"></i>hollow mark = only one date recorded</span>
    </div>
  </div>
  <footer>
    <span>Data: Wikidata (CC BY-SA) &middot; humansmap.com</span>
    <span><b>{_n(members)}</b> dated members &middot; <b>{_n(stats['edges'])}</b> family edges &middot; depth {depth} of parent/child/spouse/sibling</span>
  </footer>
</section>"""

    return (HTML_HEAD.replace("__CARD_H__", str(card_h)).replace("__CHART_H__", str(SVG_H))
            + card + "\n</body></html>\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def resolve_seed(db, arg: str) -> tuple[str, str, str]:
    """Return (qid, graph name, family label) for a candidate key or raw QID."""
    if arg in CANDIDATE_SEEDS:
        cand = CANDIDATE_SEEDS[arg]
        qid, expected, family = cand["qid"], cand["name"], cand["family"]
    else:
        qid, expected = arg.strip().upper(), None
        family = qid
    doc = _document(db, qid)
    if not doc:
        raise SystemExit(f"[error] seed {qid} not found in the graph")
    name = doc.get("name_en") or qid
    if expected and name != expected:
        print(
            f"[warn] seed {qid} expected {expected!r}, graph has {name!r}",
            file=sys.stderr,
        )
    print(f"[seed] {qid}: {name} (b {doc.get('birth_date')}, d {doc.get('death_date')})")
    return qid, name, family


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", default=DEFAULT_SEED)
    parser.add_argument("--depth", type=int, default=DEFAULT_DEPTH, choices=(2, 3))
    parser.add_argument("--max-members", type=int, default=DEFAULT_MAX_MEMBERS)
    parser.add_argument("--include-partner", action="store_true")
    parser.add_argument("--out-dir", default="docs/marketing/dynasty")
    parser.add_argument("--slug", default="dynasty",
                        help="output basename (use the family key for multi-family drops)")
    parser.add_argument("--from-json", default=None)
    parser.add_argument("--no-html", action="store_true")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.from_json:
        payload = json.loads(Path(args.from_json).read_text())
        print(f"[cache] loaded {args.from_json} (no DB access)")
    else:
        client = get_arango_client(request_timeout=300)
        db = get_db(client)
        qid, name, family = resolve_seed(db, args.seed)
        types = list(BASE_TYPES) + ([PARTNER_TYPE] if args.include_partner else [])
        print(
            f"[traverse] depth={args.depth} types={types} max_members={args.max_members}"
        )
        payload = collect(qid, name, family, args.depth, types, args.max_members)

    json_path = out_dir / f"{args.slug}.json"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {json_path}")

    if not args.no_html:
        html_path = out_dir / f"{args.slug}.html"
        html_path.write_text(render_html(payload))
        print(f"wrote {html_path}")

    print(json.dumps(payload["stats"], indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
