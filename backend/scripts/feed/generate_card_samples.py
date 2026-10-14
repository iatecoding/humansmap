"""generate_card_samples.py — Produce ~200 sample feed cards (SVG + gallery HTML).

Usage (inside the backend container):
    python -m scripts.feed.generate_card_samples [<out_dir>]

Defaults to /tmp/feed_out if no argument is given.

Output layout:
    <out_dir>/cards/*.svg   — one SVG per card
    <out_dir>/gallery.html  — single-page vertical feed gallery

DB access is READ-ONLY throughout.
"""

from __future__ import annotations

import base64
import collections
import os
import sys
from pathlib import Path
from typing import Any

from app.database import get_db, get_arango_client
from scripts.feed.domains import domain_of, is_cross_domain, pretty_occupation
from scripts.feed import card_svg

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IMAGES_DIR: str = "/app/images"

REL_MIN_STATEMENTS: int = 120
REL_TARGET: int = 130
DYN_MIN_STATEMENTS: int = 120
DYN_MIN_COMPONENT_SIZE: int = 3
DYN_TARGET: int = 70
DYN_MAX_DISPLAYED: int = 6

RELATIONSHIP_TYPES: list[str] = [
    "spouse",
    "unmarried_partner",
    "professional_collaborator",
    "relative",
]

FAMILY_TYPES: list[str] = [
    "parent",
    "child",
    "sibling",
    "father",
    "mother",
]

RELATION_LABEL_MAP: dict[str, str] = {
    "spouse": "married",
    "unmarried_partner": "together",
    "professional_collaborator": "collaborators",
    "relative": "relatives",
}


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------


def load_image_data_uri(wikidata_id: str) -> str | None:
    """Return a base64 data URI for the person's cached JPEG, or None."""
    path = os.path.join(IMAGES_DIR, f"{wikidata_id}.jpg")
    try:
        with open(path, "rb") as fh:
            data = fh.read()
        encoded = base64.b64encode(data).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"
    except FileNotFoundError:
        return None


# ---------------------------------------------------------------------------
# Date / lifespan formatting
# ---------------------------------------------------------------------------


def _year(date_str: str | None) -> str | None:
    """Extract 4-digit year from an ISO date string, or return None."""
    if not date_str:
        return None
    # Dates may be "1685-03-21", "1685", or "+1685-03-21T00:00:00Z"
    cleaned = date_str.lstrip("+")
    if len(cleaned) >= 4 and cleaned[:4].isdigit():
        return cleaned[:4]
    return None


def format_dates(start_date: str | None, end_date: str | None) -> str | None:
    """Format relationship dates as 'since YYYY', 'YYYY–YYYY', or None."""
    start = _year(start_date)
    end = _year(end_date)
    if start and end:
        return f"{start}–{end}"
    if start:
        return f"since {start}"
    return None


def format_lifespan(birth_date: str | None, death_date: str | None) -> str:
    """Format lifespan as 'b. YYYY', 'YYYY–YYYY', or '' (empty)."""
    birth = _year(birth_date)
    death = _year(death_date)
    if birth and death:
        return f"{birth}–{death}"
    if birth:
        return f"b. {birth}"
    return ""


# ---------------------------------------------------------------------------
# Union-find for connected components
# ---------------------------------------------------------------------------


class UnionFind:
    """Weighted quick-union with path compression."""

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}
        self._rank: dict[str, int] = {}

    def find(self, x: str) -> str:
        if x not in self._parent:
            self._parent[x] = x
            self._rank[x] = 0
        if self._parent[x] != x:
            self._parent[x] = self.find(self._parent[x])
        return self._parent[x]

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self._rank[ra] < self._rank[rb]:
            ra, rb = rb, ra
        self._parent[rb] = ra
        if self._rank[ra] == self._rank[rb]:
            self._rank[ra] += 1

    def components(self) -> dict[str, list[str]]:
        """Return {root: [member, ...]} mapping."""
        result: dict[str, list[str]] = collections.defaultdict(list)
        for node in self._parent:
            result[self.find(node)].append(node)
        return dict(result)


# ---------------------------------------------------------------------------
# Generation depth inference
# ---------------------------------------------------------------------------


def _assign_depths(
    members: list[str],
    edges: list[tuple[str, str, str]],
) -> dict[str, int]:
    """Derive per-member generation depth within a family component.

    *edges* is a list of (from_qid, to_qid, edge_type) tuples restricted to
    this component.  'parent'/'father'/'mother' edges imply from→to is
    parent→child (+1 depth).  'child' edges are the reverse.

    Falls back to depth 0 for all members if inference fails.
    """
    # Build directed parent→child adjacency
    children: dict[str, list[str]] = collections.defaultdict(list)
    parents: dict[str, list[str]] = collections.defaultdict(list)

    for from_q, to_q, etype in edges:
        if etype in ("parent", "father", "mother"):
            children[from_q].append(to_q)
            parents[to_q].append(from_q)
        elif etype == "child":
            children[to_q].append(from_q)
            parents[from_q].append(to_q)
        # 'sibling' edges don't imply depth difference

    member_set = set(members)
    # Restrict to edges within this component
    children_c = {p: [c for c in cs if c in member_set] for p, cs in children.items()}
    parents_c = {c: [p for p in ps if p in member_set] for c, ps in parents.items()}

    # Roots = members with no parent inside the component
    roots = [m for m in members if not parents_c.get(m)]
    if not roots:
        # Cycle-safe fallback: pick the node with fewest parents
        roots = [min(members, key=lambda m: len(parents_c.get(m, [])))]

    depths: dict[str, int] = {}
    queue: collections.deque[tuple[str, int]] = collections.deque()
    for root in roots:
        if root not in depths:
            depths[root] = 0
            queue.append((root, 0))

    while queue:
        node, depth = queue.popleft()
        for child in children_c.get(node, []):
            if child not in depths:
                depths[child] = depth + 1
                queue.append((child, depth + 1))

    # Fill in any unvisited members (siblings/orphans in the component)
    for m in members:
        if m not in depths:
            depths[m] = 0

    return depths


# ---------------------------------------------------------------------------
# Relationship card pipeline
# ---------------------------------------------------------------------------


def _fetch_relationship_edges(db: Any) -> list[dict]:
    """Return raw relationship edge rows for the target relation types."""
    aql = """
    FOR e IN relationships
        FILTER e.type IN @types
        LET a = DOCUMENT(e._from)
        LET b = DOCUMENT(e._to)
        FILTER a != null AND b != null
        FILTER (a.wikidata_statements_count >= @min_stmts)
           AND (b.wikidata_statements_count >= @min_stmts)
        FILTER LENGTH(a.occupations) > 0 AND LENGTH(b.occupations) > 0
        RETURN {
            type:       e.type,
            start_date: e.start_date,
            end_date:   e.end_date,
            a: {
                qid:        a.wikidata_id,
                name:       a.name_en,
                occupations: a.occupations,
                birth_date: a.birth_date,
                death_date: a.death_date,
                statements: a.wikidata_statements_count
            },
            b: {
                qid:        b.wikidata_id,
                name:       b.name_en,
                occupations: b.occupations,
                birth_date: b.birth_date,
                death_date: b.death_date,
                statements: b.wikidata_statements_count
            }
        }
    """
    cursor = db.aql.execute(
        aql,
        bind_vars={"types": RELATIONSHIP_TYPES, "min_stmts": REL_MIN_STATEMENTS},
        ttl=120,
    )
    return list(cursor)


def _build_relationship_cards(
    raw_edges: list[dict],
) -> list[dict]:
    """Dedup, filter cross-domain, rank, and cap at REL_TARGET."""
    seen: set[tuple[str, str, str]] = set()
    deduped: list[dict] = []

    for edge in raw_edges:
        a, b = edge["a"], edge["b"]
        qa, qb = a["qid"], b["qid"]
        if qa is None or qb is None:
            continue
        # Canonical pair key: sorted QIDs + type
        pair_key = (min(qa, qb), max(qa, qb), edge["type"])
        if pair_key in seen:
            continue
        seen.add(pair_key)
        deduped.append(edge)

    # Keep only cross-domain pairs
    cross = [
        e for e in deduped
        if is_cross_domain(e["a"]["occupations"], e["b"]["occupations"])
    ]

    # Rank by min(statements) desc
    cross.sort(key=lambda e: min(e["a"]["statements"], e["b"]["statements"]), reverse=True)

    return cross[:REL_TARGET]


def _edge_to_relationship_card(edge: dict) -> tuple[dict, bool, bool]:
    """Convert a raw edge row to a relationship card dict.

    Returns (card_dict, used_image_a, used_image_b).
    """
    a, b = edge["a"], edge["b"]

    img_a = load_image_data_uri(a["qid"])
    img_b = load_image_data_uri(b["qid"])

    card: dict = {
        "type": "relationship",
        "eyebrow": "DID YOU KNOW?",
        "relation_label": RELATION_LABEL_MAP.get(edge["type"], edge["type"]),
        "dates": format_dates(edge.get("start_date"), edge.get("end_date")),
        "person_a": {
            "name": a["name"],
            "occupation": pretty_occupation(a["occupations"][0] if a["occupations"] else None),
            "lifespan": format_lifespan(a.get("birth_date"), a.get("death_date")),
            "statements": a["statements"],
            "image_data_uri": img_a,
        },
        "person_b": {
            "name": b["name"],
            "occupation": pretty_occupation(b["occupations"][0] if b["occupations"] else None),
            "lifespan": format_lifespan(b.get("birth_date"), b.get("death_date")),
            "statements": b["statements"],
            "image_data_uri": img_b,
        },
    }
    return card, img_a is not None, img_b is not None


# ---------------------------------------------------------------------------
# Dynasty card pipeline
# ---------------------------------------------------------------------------


def _fetch_family_edges(db: Any) -> list[dict]:
    """Return raw family relationship edge rows."""
    aql = """
    FOR e IN relationships
        FILTER e.type IN @types
        LET a = DOCUMENT(e._from)
        LET b = DOCUMENT(e._to)
        FILTER a != null AND b != null
        FILTER (a.wikidata_statements_count >= @min_stmts)
           AND (b.wikidata_statements_count >= @min_stmts)
        RETURN {
            type:     e.type,
            from_qid: a.wikidata_id,
            to_qid:   b.wikidata_id
        }
    """
    cursor = db.aql.execute(
        aql,
        bind_vars={"types": FAMILY_TYPES, "min_stmts": DYN_MIN_STATEMENTS},
        ttl=120,
    )
    return list(cursor)


def _fetch_persons_by_qids(db: Any, qids: list[str]) -> dict[str, dict]:
    """Fetch person details for a list of QIDs.  Returns {qid: person_dict}."""
    if not qids:
        return {}
    aql = """
    FOR p IN persons
        FILTER p.wikidata_id IN @qids
        RETURN {
            qid:        p.wikidata_id,
            name:       p.name_en,
            occupations: p.occupations,
            birth_date: p.birth_date,
            death_date: p.death_date,
            statements: p.wikidata_statements_count,
            family_name: p.family_name
        }
    """
    cursor = db.aql.execute(aql, bind_vars={"qids": qids}, ttl=60)
    return {row["qid"]: row for row in cursor}


def _modal_family_name(members: list[dict]) -> str | None:
    """Return the most common non-null family_name among members."""
    counts: dict[str, int] = collections.Counter(
        m["family_name"]
        for m in members
        if m.get("family_name")
    )
    if not counts:
        return None
    return counts.most_common(1)[0][0]


def _fallback_surname(member: dict) -> str:
    """Extract last space-separated token from the member's name."""
    name = member.get("name") or ""
    tokens = name.strip().split()
    return tokens[-1] if tokens else "Unknown"


def _birth_year_sort_key(member: dict) -> int:
    """Return integer birth year for sorting; 9999 if unknown."""
    y = _year(member.get("birth_date"))
    return int(y) if y else 9999


def _build_dynasty_components(
    raw_edges: list[dict],
) -> list[tuple[list[str], list[tuple[str, str, str]]]]:
    """Build connected components and return only those with >= DYN_MIN_COMPONENT_SIZE members.

    Returns list of (member_qids, edges_within_component).
    """
    uf = UnionFind()
    all_edges: list[tuple[str, str, str]] = []

    for e in raw_edges:
        fq, tq = e["from_qid"], e["to_qid"]
        if fq and tq:
            uf.union(fq, tq)
            all_edges.append((fq, tq, e["type"]))

    components = uf.components()
    result: list[tuple[list[str], list[tuple[str, str, str]]]] = []

    for _root, members in components.items():
        if len(members) < DYN_MIN_COMPONENT_SIZE:
            continue
        member_set = set(members)
        comp_edges = [(f, t, et) for f, t, et in all_edges if f in member_set and t in member_set]
        result.append((members, comp_edges))

    return result


def _component_to_dynasty_card(
    members: list[str],
    comp_edges: list[tuple[str, str, str]],
    persons: dict[str, dict],
) -> dict | None:
    """Build a dynasty card dict from a connected component.

    Returns None if fewer than DYN_MIN_COMPONENT_SIZE member details found.
    """
    # Resolve members and filter to those we have data for
    known = [persons[q] for q in members if q in persons]
    if len(known) < DYN_MIN_COMPONENT_SIZE:
        return None

    # Assign depths
    depths = _assign_depths(members, comp_edges)

    # Cap at DYN_MAX_DISPLAYED (pick by highest statements count)
    sorted_by_notability = sorted(known, key=lambda m: m["statements"], reverse=True)
    displayed = sorted_by_notability[:DYN_MAX_DISPLAYED]
    overflow_count = len(known) - len(displayed)

    # Sort displayed oldest→newest
    displayed.sort(key=_birth_year_sort_key)

    # Count distinct generation depths
    display_qids = {m["qid"] for m in displayed}
    gen_depths = {depths.get(q, 0) for q in display_qids}
    generation_count = max(1, len(gen_depths))

    # Family label
    surname = _modal_family_name(known) or _fallback_surname(sorted_by_notability[0])
    family_label = f"The {surname} Family"

    subtitle = f"{len(known)} notable members · {generation_count} generation{'s' if generation_count != 1 else ''}"

    # Build member dicts
    member_dicts: list[dict] = []
    for m in displayed:
        img = load_image_data_uri(m["qid"])
        member_dicts.append({
            "name": m["name"],
            "occupation": pretty_occupation(
                m["occupations"][0] if m.get("occupations") else None
            ),
            "lifespan": format_lifespan(m.get("birth_date"), m.get("death_date")),
            "statements": m["statements"],
            "image_data_uri": img,
            "depth": depths.get(m["qid"], 0),
            "is_overflow": False,
        })

    if overflow_count > 0:
        member_dicts.append({
            "name": f"+{overflow_count} more",
            "occupation": "",
            "lifespan": None,
            "statements": 0,
            "image_data_uri": None,
            "depth": 0,
            "is_overflow": True,
        })

    return {
        "type": "dynasty",
        "family_label": family_label,
        "subtitle": subtitle,
        "members": member_dicts,
        "_member_count": len(known),
        "_max_statements": max(m["statements"] for m in known),
    }


# ---------------------------------------------------------------------------
# Gallery HTML
# ---------------------------------------------------------------------------


def _gallery_html(cards_meta: list[dict]) -> str:
    """Generate a self-contained gallery HTML page.

    *cards_meta* is a list of dicts with keys:
        filename, card_type, label
    """
    total = len(cards_meta)
    rel_count = sum(1 for c in cards_meta if c["card_type"] == "relationship")
    dyn_count = total - rel_count

    items_html_parts: list[str] = []
    for meta in cards_meta:
        caption = meta["label"]
        fname = meta["filename"]
        items_html_parts.append(
            f'<div class="card-wrap">'
            f'<p class="caption">{caption}</p>'
            f'<img src="cards/{fname}" alt="{caption}" loading="lazy">'
            f'</div>'
        )

    items_html = "\n".join(items_html_parts)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>HumansMap — Feed Samples</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Playfair+Display:wght@700&display=swap" rel="stylesheet">
  <style>
    *, *::before, *::after {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: #f2ede3;
      font-family: 'Inter', system-ui, sans-serif;
      color: #1c1510;
    }}
    .header {{
      text-align: center;
      padding: 48px 24px 32px;
    }}
    .header h1 {{
      font-family: 'Playfair Display', Georgia, serif;
      font-size: 32px;
      font-weight: 700;
      margin: 0 0 8px;
      color: #1b4d8e;
    }}
    .header p {{
      font-size: 15px;
      color: #6b6256;
      margin: 0;
    }}
    .feed {{
      max-width: 440px;
      margin: 0 auto;
      padding: 0 0 64px;
      display: flex;
      flex-direction: column;
      gap: 28px;
    }}
    .card-wrap img {{
      width: 100%;
      display: block;
      border-radius: 4px;
      box-shadow: 0 4px 20px rgba(28,21,16,0.12);
    }}
    .caption {{
      font-size: 11px;
      font-weight: 500;
      color: #8a8278;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      margin: 0 0 8px 2px;
    }}
  </style>
</head>
<body>
  <div class="header">
    <h1>humansmap.com — Feed Samples</h1>
    <p>{total} cards &nbsp;·&nbsp; {rel_count} relationship &nbsp;·&nbsp; {dyn_count} dynasty</p>
  </div>
  <div class="feed">
{items_html}
  </div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def run(out_dir: Path) -> None:
    """Run the full card generation pipeline and write output to *out_dir*."""
    cards_dir = out_dir / "cards"
    cards_dir.mkdir(parents=True, exist_ok=True)

    db = get_db(get_arango_client())

    # -----------------------------------------------------------------------
    # Relationship cards
    # -----------------------------------------------------------------------
    print("[relationship] Fetching edges…")
    raw_rel_edges = _fetch_relationship_edges(db)
    print(f"[relationship] Raw edges: {len(raw_rel_edges)}")

    rel_edges = _build_relationship_cards(raw_rel_edges)
    print(f"[relationship] After dedup + cross-domain filter: {len(rel_edges)}")

    cards_meta: list[dict] = []
    rel_img_count = 0
    rel_initials_count = 0

    for i, edge in enumerate(rel_edges):
        card, img_a, img_b = _edge_to_relationship_card(edge)
        if img_a:
            rel_img_count += 1
        else:
            rel_initials_count += 1
        if img_b:
            rel_img_count += 1
        else:
            rel_initials_count += 1

        svg = card_svg.render_relationship_card(card)
        fname = f"rel_{i:04d}_{edge['a']['qid']}_{edge['b']['qid']}.svg"
        (cards_dir / fname).write_text(svg, encoding="utf-8")

        domain_a = domain_of(edge["a"]["occupations"]) or "?"
        domain_b = domain_of(edge["b"]["occupations"]) or "?"
        label = (
            f"relationship &middot; {domain_a} ↔ {domain_b} "
            f"({RELATION_LABEL_MAP.get(edge['type'], edge['type'])})"
        )
        cards_meta.append({"filename": fname, "card_type": "relationship", "label": label})

    print(f"[relationship] Written: {len(rel_edges)} cards  "
          f"(portrait images: {rel_img_count}, initials: {rel_initials_count})")

    # -----------------------------------------------------------------------
    # Dynasty cards
    # -----------------------------------------------------------------------
    print("[dynasty] Fetching family edges…")
    raw_fam_edges = _fetch_family_edges(db)
    print(f"[dynasty] Raw family edges: {len(raw_fam_edges)}")

    components = _build_dynasty_components(raw_fam_edges)
    print(f"[dynasty] Components with >= {DYN_MIN_COMPONENT_SIZE} members: {len(components)}")

    # Collect all QIDs needed
    all_qids: list[str] = list({q for members, _ in components for q in members})
    print(f"[dynasty] Fetching details for {len(all_qids)} persons…")
    persons = _fetch_persons_by_qids(db, all_qids)

    # Build candidate dynasty cards
    dyn_candidates: list[dict] = []
    for members, comp_edges in components:
        card = _component_to_dynasty_card(members, comp_edges, persons)
        if card:
            dyn_candidates.append(card)

    # Rank: (notable member count desc, max statements desc)
    dyn_candidates.sort(
        key=lambda c: (c["_member_count"], c["_max_statements"]),
        reverse=True,
    )
    dyn_selected = dyn_candidates[:DYN_TARGET]

    dyn_img_count = 0
    dyn_initials_count = 0

    # Interleave dynasty cards near the top: put some dynasty cards first
    # to make them easy to find in the gallery
    dynasty_cards_meta: list[dict] = []

    for i, dyn_card in enumerate(dyn_selected):
        member_count = len([m for m in dyn_card["members"] if not m.get("is_overflow")])

        # Count image vs initials
        for m in dyn_card["members"]:
            if m.get("is_overflow"):
                continue
            if m.get("image_data_uri"):
                dyn_img_count += 1
            else:
                dyn_initials_count += 1

        # Strip internal metadata keys (prefixed '_') before rendering
        clean_card = {k: v for k, v in dyn_card.items() if not k.startswith("_")}
        svg = card_svg.render_dynasty_card(clean_card)

        fname = f"dyn_{i:04d}.svg"
        (cards_dir / fname).write_text(svg, encoding="utf-8")

        label = (
            f"dynasty &middot; {dyn_card.get('family_label', '')} "
            f"· {member_count} members"
        )
        dynasty_cards_meta.append({"filename": fname, "card_type": "dynasty", "label": label})

    print(f"[dynasty] Written: {len(dyn_selected)} cards  "
          f"(portrait images: {dyn_img_count}, initials: {dyn_initials_count})")

    # -----------------------------------------------------------------------
    # Interleave for gallery: lead with first 3 dynasty cards, then alternate
    # -----------------------------------------------------------------------
    gallery_meta: list[dict] = []
    rel_iter = iter(cards_meta)
    dyn_iter = iter(dynasty_cards_meta)

    # First 3 dynasty cards at the top
    for _ in range(min(3, len(dynasty_cards_meta))):
        try:
            gallery_meta.append(next(dyn_iter))
        except StopIteration:
            break

    # Remaining: alternate relationship then dynasty
    remaining_dyn = list(dyn_iter)
    remaining_rel = list(rel_iter)

    ri, di = 0, 0
    while ri < len(remaining_rel) or di < len(remaining_dyn):
        # 2 relationship, then 1 dynasty
        for _ in range(2):
            if ri < len(remaining_rel):
                gallery_meta.append(remaining_rel[ri])
                ri += 1
        if di < len(remaining_dyn):
            gallery_meta.append(remaining_dyn[di])
            di += 1

    # -----------------------------------------------------------------------
    # Gallery HTML
    # -----------------------------------------------------------------------
    html = _gallery_html(gallery_meta)
    (out_dir / "gallery.html").write_text(html, encoding="utf-8")

    total = len(cards_meta) + len(dynasty_cards_meta)
    print(
        f"\n=== Summary ===\n"
        f"  Relationship cards: {len(cards_meta)}\n"
        f"  Dynasty cards:      {len(dyn_selected)}\n"
        f"  Total SVGs:         {total}\n"
        f"  Relationship portraits — image: {rel_img_count}, initials: {rel_initials_count}\n"
        f"  Dynasty portraits   — image: {dyn_img_count}, initials: {dyn_initials_count}\n"
        f"  Output: {out_dir}\n"
    )


if __name__ == "__main__":
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/feed_out")
    run(out_path)
