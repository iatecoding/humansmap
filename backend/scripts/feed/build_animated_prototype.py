"""build_animated_prototype.py — Scroll-driven animated feed prototype for HumansMap.

Produces a single self-contained HTML file at /tmp/animated_prototype.html.
All CSS, JS, and portrait images (base64) are inlined — no external assets
except a Google Fonts <link>.

Card archetypes:
  - relationship : two people connected by a labelled edge (original)
  - dynasty      : family members on a vertical timeline spine (original)
  - org_founded  : organisation with hub-and-spoke spokes to its founders (new)

Usage (inside the backend container):
    python -m scripts.feed.build_animated_prototype

DB access is READ-ONLY throughout.
"""

from __future__ import annotations

import base64
import os
import textwrap
from pathlib import Path
from typing import Any

from app.database import get_db, get_arango_client

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IMAGES_DIR: str = "/app/images"
OUTPUT_PATH: str = "/tmp/animated_prototype.html"

# Curated relationship pairs: (name_a, qid_a, name_b, qid_b, relation_label, dates)
# QIDs are looked up by name; hardcoded here as a stable fallback.
CURATED_PAIRS: list[tuple[str, str, str, str, str, str]] = [
    ("John F. Kennedy",  "Q9696",  "Marilyn Monroe",     "Q4616",  "together",  "1954–1962"),
    ("Frédéric Chopin",  "Q1268",  "George Sand",        "Q3816",  "together",  "1837–1847"),
    ("Grace Kelly",      "Q3925",  "Rainier III, Prince of Monaco", "Q185025", "married", "1956–1982"),
    ("Jean-Paul Sartre", "Q9364",  "Simone de Beauvoir", "Q7197",  "together",  "1929–1980"),
    ("Arthur Miller",    "Q80596", "Marilyn Monroe",     "Q4616",  "married",   "1956–1961"),
    ("Salvador Dalí",    "Q5577",  "Gala Dalí",          "Q464056","married",   "1958–1982"),
]

# Curated org-founded entries: org name to look up, fallback QID, display type label.
# Founders are resolved via affiliations edges at runtime ordered by wikidata_statements_count DESC.
CURATED_ORG_FOUNDED: list[tuple[str, str, str]] = [
    ("Apple Inc.",  "Q312",  "Technology company"),
    ("Microsoft",   "Q2283", "Technology company"),
]

# Fallback org QIDs (≥2 notable founders) used when a primary org yields < 2 founders with images.
ORG_FOUNDED_FALLBACK_QIDS: list[str] = ["Q7184", "Q188507", "Q125977", "Q49108"]

# Curated dynasty families: surname → list of QIDs (oldest→newest by birth year)
# We filter to those with images; first 2 qualifying families used.
CURATED_DYNASTY_QIDS: dict[str, list[str]] = {
    "Bach": [
        "Q1339",    # Johann Sebastian Bach  1685
        "Q107277",  # Wilhelm Friedemann Bach 1710
        "Q57225",   # Johann Christoph Friedrich Bach 1732
        "Q106641",  # Johann Christian Bach  1735
        "Q76428",   # Carl Philipp Emanuel Bach 1714 (reordered below by birth)
    ],
    "Grimm": [
        "Q6701",   # Jacob Grimm  1785
        "Q6714",   # Wilhelm Grimm 1786
        "Q558766", # Ludwig Emil Grimm 1790
        "Q65292",  # Herman Grimm 1828
        "Q1712868",# Julius Otto Grimm 1827
    ],
}

# Org type → human-readable label used on org-founded cards.
# Fallback: capitalise each word of the raw type string.
ORG_TYPE_LABELS: dict[str, str] = {
    "company":             "Company",
    "nonprofit":           "Nonprofit",
    "government_agency":   "Government agency",
    "university":          "University",
    "secondary_school":    "School",
    "foundation":          "Foundation",
    "sports_club":         "Sports club",
    "publisher":           "Publisher",
    "organization":        "Organisation",
}

# Domain mapping from primary_occupation → editorial bucket label
DOMAIN_LABELS: dict[str, str] = {
    "singer": "Music", "musician": "Music", "composer": "Music",
    "pianist": "Music", "guitarist": "Music", "conductor": "Music",
    "songwriter": "Music", "rapper": "Music", "dj": "Music",
    "actor": "Film", "film_actor": "Film", "screenwriter": "Film",
    "film_director": "Film", "television_actor": "Film",
    "television_presenter": "Film", "film_producer": "Film",
    "stage_actor": "Film", "comedian": "Film",
    "writer": "Literature", "poet": "Literature", "essayist": "Literature",
    "novelist": "Literature", "playwright": "Literature",
    "philosopher": "Ideas", "sociologist": "Ideas", "historian": "Ideas",
    "psychologist": "Ideas",
    "scientist": "Science", "mathematician": "Science", "physicist": "Science",
    "chemist": "Science", "biologist": "Science", "astronomer": "Science",
    "university_teacher": "Science", "researcher": "Science",
    "politician": "Politics", "diplomat": "Politics", "judge": "Politics",
    "head_of_state": "Politics", "monarch": "Politics",
    "businessperson": "Business", "entrepreneur": "Business",
    "banker": "Business", "financier": "Business",
    "painter": "Art", "sculptor": "Art", "photographer": "Art",
    "architect": "Art", "designer": "Art",
    "journalist": "Media", "publisher": "Media",
    "model": "Modelling",
    "military_person": "Military", "military_officer": "Military",
    "lawyer": "Law",
    "athlete": "Sport", "football_player": "Sport",
}


# ---------------------------------------------------------------------------
# DB helpers (read-only)
# ---------------------------------------------------------------------------


def _fetch_person(db: Any, qid: str) -> dict | None:
    """Fetch a single person by Wikidata QID. Returns None if not found."""
    aql = """
    FOR p IN persons
        FILTER p.wikidata_id == @qid
        LIMIT 1
        RETURN {
            qid:          p.wikidata_id,
            name:         p.name_en,
            description:  p.description_en,
            occ:          p.primary_occupation,
            occupations:  p.occupations,
            birth:        p.birth_date,
            death:        p.death_date,
            notable:      p.notable_works,
            stmts:        p.wikidata_statements_count,
            family_name:  p.family_name
        }
    """
    cursor = db.aql.execute(aql, bind_vars={"qid": qid}, ttl=60)
    rows = list(cursor)
    return rows[0] if rows else None


def _fetch_persons_batch(db: Any, qids: list[str]) -> dict[str, dict]:
    """Fetch multiple persons by QID list. Returns {qid: person_dict}."""
    if not qids:
        return {}
    aql = """
    FOR p IN persons
        FILTER p.wikidata_id IN @qids
        RETURN {
            qid:         p.wikidata_id,
            name:        p.name_en,
            description: p.description_en,
            occ:         p.primary_occupation,
            birth:       p.birth_date,
            death:       p.death_date,
            notable:     p.notable_works,
            stmts:       p.wikidata_statements_count,
            family_name: p.family_name
        }
    """
    cursor = db.aql.execute(aql, bind_vars={"qids": qids}, ttl=60)
    return {row["qid"]: row for row in cursor}


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------


def _load_image_b64(qid: str) -> str | None:
    """Return a base64 JPEG data URI for *qid*, or None if not on disk."""
    path = os.path.join(IMAGES_DIR, f"{qid}.jpg")
    try:
        with open(path, "rb") as fh:
            data = fh.read()
        encoded = base64.b64encode(data).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"
    except FileNotFoundError:
        return None


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _year(date_str: str | None) -> str | None:
    """Extract 4-digit year from an ISO date string like '1685-03-21'."""
    if not date_str:
        return None
    cleaned = date_str.lstrip("+")
    if len(cleaned) >= 4 and cleaned[:4].isdigit():
        return cleaned[:4]
    return None


def _lifespan(birth: str | None, death: str | None) -> str:
    """Format lifespan as 'YYYY–YYYY', 'b. YYYY', or ''."""
    b = _year(birth)
    d = _year(death)
    if b and d:
        return f"{b}–{d}"
    if b:
        return f"b. {b}"
    return ""


def _domain_label(occ: str | None) -> str:
    """Map primary occupation to editorial domain label."""
    if not occ:
        return "Unknown"
    return DOMAIN_LABELS.get(occ.lower().replace(" ", "_"), occ.replace("_", " ").title())


def _pretty_occ(occ: str | None) -> str:
    """Human-readable occupation label."""
    if not occ:
        return ""
    return occ.replace("_", " ").capitalize()


def _known_for(notable: list | None, *, max_items: int = 2) -> str:
    """Return a 'Known for: X, Y' string if notable works exist."""
    if not notable:
        return ""
    works = [w for w in (notable or []) if w][:max_items]
    if not works:
        return ""
    return "Known for: " + ", ".join(works)


def _initials(name: str) -> str:
    """Return up to 2 initials from a name string."""
    parts = name.strip().split()
    if len(parts) >= 2:
        return (parts[0][0] + parts[-1][0]).upper()
    return name[:2].upper() if name else "?"


def _prettify_org_type(raw_type: str | None, *, override: str | None = None) -> str:
    """Map a raw org type string to an editorial label.

    *override* is used when the caller knows a better label (e.g. the curated
    constant ``"Technology company"`` for Apple/Microsoft).
    """
    if override:
        return override
    if not raw_type:
        return "Organisation"
    return ORG_TYPE_LABELS.get(raw_type.lower(), raw_type.replace("_", " ").title())


# ---------------------------------------------------------------------------
# Card data builders
# ---------------------------------------------------------------------------


def _build_rel_card(
    db: Any,
    name_a: str,
    qid_a: str,
    name_b: str,
    qid_b: str,
    relation_label: str,
    dates: str,
) -> dict | None:
    """Build a relationship card dict. Returns None if either person lacks description."""
    p_a = _fetch_person(db, qid_a)
    p_b = _fetch_person(db, qid_b)
    if p_a is None or p_b is None:
        print(f"  [rel] SKIP {qid_a}/{qid_b}: not in DB")
        return None
    if not p_a.get("description") or not p_b.get("description"):
        print(f"  [rel] SKIP {name_a}/{name_b}: missing description")
        return None

    img_a = _load_image_b64(qid_a)
    img_b = _load_image_b64(qid_b)

    # Both must have portraits — we embed base64; initials fallback is also ok here
    # but the brief asks to keep only pairs where BOTH have images
    if img_a is None or img_b is None:
        print(f"  [rel] SKIP {name_a}/{name_b}: missing portrait")
        return None

    domain_a = _domain_label(p_a.get("occ"))
    domain_b = _domain_label(p_b.get("occ"))

    return {
        "type": "relationship",
        "eyebrow": "DID YOU KNOW?",
        "domain_tag": f"{domain_a} ↔ {domain_b}",
        "relation_label": relation_label,
        "dates": dates,
        "person_a": {
            "qid": qid_a,
            "name": p_a["name"],
            "description": p_a["description"],
            "occ": _pretty_occ(p_a.get("occ")),
            "lifespan": _lifespan(p_a.get("birth"), p_a.get("death")),
            "known_for": _known_for(p_a.get("notable")),
            "image": img_a,
            "initials": _initials(p_a["name"]),
        },
        "person_b": {
            "qid": qid_b,
            "name": p_b["name"],
            "description": p_b["description"],
            "occ": _pretty_occ(p_b.get("occ")),
            "lifespan": _lifespan(p_b.get("birth"), p_b.get("death")),
            "known_for": _known_for(p_b.get("notable")),
            "image": img_b,
            "initials": _initials(p_b["name"]),
        },
    }


def _build_dynasty_card(db: Any, surname: str, qids: list[str]) -> dict | None:
    """Build a dynasty card dict for a named family."""
    persons = _fetch_persons_batch(db, qids)
    if len(persons) < 2:
        print(f"  [dynasty] SKIP {surname}: only {len(persons)} persons found")
        return None

    # Sort oldest → newest by birth year; filter to known persons
    members_raw = [persons[q] for q in qids if q in persons]
    members_raw.sort(key=lambda p: int(_year(p.get("birth")) or "9999"))

    # Attach image and compute portrait scale (most notable = largest)
    max_stmts = max(p["stmts"] for p in members_raw) if members_raw else 1
    members: list[dict] = []
    for p in members_raw:
        img = _load_image_b64(p["qid"])
        # Scale portrait between 0.75 and 1.0 based on statement count
        scale = 0.75 + 0.25 * (p["stmts"] / max(max_stmts, 1))
        label = (p.get("description") or _pretty_occ(p.get("occ")) or "").rstrip(".")
        if len(label) > 80:
            label = label[:77] + "…"
        members.append({
            "qid": p["qid"],
            "name": p["name"],
            "label": label,
            "lifespan": _lifespan(p.get("birth"), p.get("death")),
            "stmts": p["stmts"],
            "scale": round(scale, 3),
            "image": img,
            "initials": _initials(p["name"]),
        })

    return {
        "type": "dynasty",
        "family_label": f"The {surname} Family",
        "surname": surname,
        "subtitle": f"{len(members)} members",
        "members": members,
    }


def _build_org_founded_card(
    db: Any,
    org_name: str,
    org_qid: str,
    type_label_override: str | None = None,
) -> dict | None:
    """Build an org-founded card dict.

    Looks up the org by QID, then resolves its founders via ``affiliations``
    edges where ``type == 'founder'``, ordered by ``wikidata_statements_count``
    DESC.  Returns None when the org is missing or fewer than 2 founders can be
    resolved.

    Args:
        db:                  ArangoDB handle (read-only).
        org_name:            Display name (used only for logging; the DB value
                             is always used on the card).
        org_qid:             Wikidata QID of the organisation (e.g. ``"Q312"``).
        type_label_override: Human-readable type label to show on the card,
                             e.g. ``"Technology company"``.  When *None* the
                             raw ``type`` field is prettified automatically.
    """
    # --- Fetch org ---
    org_aql = """
    FOR o IN organizations
        FILTER o.wikidata_id == @qid
        LIMIT 1
        RETURN {
            qid:   o.wikidata_id,
            name:  o.name_en,
            type:  o.type,
            founded: o.founded,
            image: o.image_cached_path
        }
    """
    org_cursor = db.aql.execute(org_aql, bind_vars={"qid": org_qid}, ttl=60)
    org_rows = list(org_cursor)
    if not org_rows:
        print(f"  [org] SKIP {org_name}: org not found in DB")
        return None
    org = org_rows[0]

    # --- Fetch founders ordered by notability ---
    founders_aql = """
    FOR e IN affiliations
        FILTER e._to == @org_id AND e.type == 'founder'
        FOR p IN persons
            FILTER p._id == e._from
            SORT p.wikidata_statements_count DESC
            LIMIT 4
            RETURN {
                qid:         p.wikidata_id,
                name:        p.name_en,
                description: p.description_en,
                occ:         p.primary_occupation,
                stmts:       p.wikidata_statements_count
            }
    """
    founders_cursor = db.aql.execute(
        founders_aql,
        bind_vars={"org_id": f"organizations/{org_qid}"},
        ttl=60,
    )
    founders_raw = list(founders_cursor)

    if len(founders_raw) < 2:
        print(f"  [org] SKIP {org_name}: only {len(founders_raw)} founders found")
        return None

    # --- Attach images ---
    founders: list[dict] = []
    for f in founders_raw:
        img = _load_image_b64(f["qid"])
        descriptor = (f.get("description") or _pretty_occ(f.get("occ")) or "").rstrip(".")
        if len(descriptor) > 80:
            descriptor = descriptor[:77] + "…"
        founders.append({
            "qid":        f["qid"],
            "name":       f["name"],
            "descriptor": descriptor,
            "image":      img,
            "initials":   _initials(f["name"]),
        })

    # --- Org image ---
    org_image = _load_image_b64(org["qid"]) if org.get("image") else None

    return {
        "type":        "org_founded",
        "org_qid":     org["qid"],
        "org_name":    org["name"],
        "org_type":    _prettify_org_type(org.get("type"), override=type_label_override),
        "org_founded": _year(org.get("founded")),
        "org_image":   org_image,
        "org_initials": _initials(org["name"]),
        "founders":    founders,
    }


# ---------------------------------------------------------------------------
# HTML rendering helpers
# ---------------------------------------------------------------------------


def _render_portrait_html(person: dict, css_class: str = "portrait") -> str:
    """Return HTML for a circular portrait — image or initials fallback."""
    if person.get("image"):
        return (
            f'<div class="{css_class}" style="will-change:transform,opacity;">'
            f'<img src="{person["image"]}" alt="{person["name"]}" loading="eager">'
            f'</div>'
        )
    # Initials fallback
    return (
        f'<div class="{css_class} portrait--initials" style="will-change:transform,opacity;">'
        f'<span>{person["initials"]}</span>'
        f'</div>'
    )


def _render_dynasty_portrait(member: dict, size_px: int) -> str:
    """Return HTML for a dynasty member portrait at a specific pixel size."""
    style = f"width:{size_px}px;height:{size_px}px;will-change:transform,opacity;"
    if member.get("image"):
        return (
            f'<div class="portrait portrait--dynasty" style="{style}">'
            f'<img src="{member["image"]}" alt="{member["name"]}" loading="eager">'
            f'</div>'
        )
    return (
        f'<div class="portrait portrait--dynasty portrait--initials" style="{style}">'
        f'<span>{member["initials"]}</span>'
        f'</div>'
    )


def _render_rel_card_html(card: dict, idx: int) -> str:
    """Render a relationship card as an HTML section."""
    pa = card["person_a"]
    pb = card["person_b"]

    known_a_html = (
        f'<p class="known-for">{pa["known_for"]}</p>'
        if pa.get("known_for") else ""
    )
    known_b_html = (
        f'<p class="known-for">{pb["known_for"]}</p>'
        if pb.get("known_for") else ""
    )

    lifespan_a = (
        f'<span class="lifespan">{pa["lifespan"]}</span>' if pa.get("lifespan") else ""
    )
    lifespan_b = (
        f'<span class="lifespan">{pb["lifespan"]}</span>' if pb.get("lifespan") else ""
    )

    portrait_a = _render_portrait_html(pa, "portrait portrait--a")
    portrait_b = _render_portrait_html(pb, "portrait portrait--b")

    return f"""
<section class="snap-section" data-card-idx="{idx}">
  <div class="card card--relationship" aria-label="Connection between {pa['name']} and {pb['name']}">

    <!-- Card header -->
    <div class="card__header" style="will-change:transform,opacity;">
      <span class="eyebrow">{card['eyebrow']}</span>
      <span class="domain-tag">{card['domain_tag']}</span>
    </div>

    <!-- Portraits + connector -->
    <div class="card__connector-area">

      <!-- Person A -->
      <div class="person-col person-col--a">
        {portrait_a}
        <div class="person-info" style="will-change:transform,opacity;">
          <h2 class="person-name">{pa['name']}</h2>
          {lifespan_a}
          <p class="person-desc">{pa['description']}</p>
          {known_a_html}
        </div>
      </div>

      <!-- Animated connecting line + relation pill -->
      <div class="connector">
        <div class="connector__line" style="will-change:transform,opacity;">
          <div class="connector__fill"></div>
        </div>
        <div class="relation-pill" style="will-change:transform,opacity;">
          <span class="relation-label">{card['relation_label']}</span>
          <span class="relation-dates">{card['dates']}</span>
        </div>
      </div>

      <!-- Person B -->
      <div class="person-col person-col--b">
        {portrait_b}
        <div class="person-info" style="will-change:transform,opacity;">
          <h2 class="person-name">{pb['name']}</h2>
          {lifespan_b}
          <p class="person-desc">{pb['description']}</p>
          {known_b_html}
        </div>
      </div>

    </div><!-- /.card__connector-area -->

    <!-- Footer -->
    <div class="card__footer" style="will-change:transform,opacity;">
      <span class="footer-brand">humansmap.com</span>
    </div>

  </div><!-- /.card -->
</section>
"""


def _render_dynasty_card_html(card: dict, idx: int) -> str:
    """Render a dynasty card as an HTML section."""
    members = card["members"]

    # Portrait sizes: scale between 36px and 52px based on computed scale factor
    MIN_PX, MAX_PX = 36, 52

    rows_html_parts: list[str] = []
    for i, m in enumerate(members):
        size_px = round(MIN_PX + (MAX_PX - MIN_PX) * (m["scale"] - 0.75) / 0.25)
        portrait_html = _render_dynasty_portrait(m, size_px)
        rows_html_parts.append(
            f'<div class="dynasty-row" data-row-idx="{i}" style="will-change:transform,opacity;">'
            f'  {portrait_html}'
            f'  <div class="dynasty-row__info">'
            f'    <span class="dynasty-name">{m["name"]}</span>'
            f'    <span class="dynasty-label">{m["label"]}</span>'
            f'    <span class="dynasty-lifespan">{m["lifespan"]}</span>'
            f'  </div>'
            f'</div>'
        )

    rows_html = "\n".join(rows_html_parts)

    return f"""
<section class="snap-section" data-card-idx="{idx}">
  <div class="card card--dynasty" aria-label="The {card['surname']} family dynasty">

    <!-- Card header -->
    <div class="card__header" style="will-change:transform,opacity;">
      <span class="eyebrow">FAMILY DYNASTY</span>
    </div>

    <!-- Dynasty title -->
    <div class="dynasty-title" style="will-change:transform,opacity;">
      <h2 class="dynasty-family-label">{card['family_label']}</h2>
      <p class="dynasty-subtitle">{card['subtitle']}</p>
    </div>

    <!-- Timeline spine + member rows -->
    <div class="dynasty-timeline">
      <div class="dynasty-spine" style="will-change:height,opacity;">
        <div class="dynasty-spine__fill"></div>
      </div>
      <div class="dynasty-rows">
{rows_html}
      </div>
    </div>

    <!-- Footer -->
    <div class="card__footer" style="will-change:transform,opacity;">
      <span class="footer-brand">humansmap.com</span>
    </div>

  </div><!-- /.card -->
</section>
"""


def _render_org_portrait_html(entity: dict, css_class: str, color_var: str) -> str:
    """Return HTML for a portrait node — image clipped inside a circle/square,
    or an initials monogram.  *color_var* is the CSS custom property that
    supplies the border/background colour (``--person`` or ``--org``).
    """
    if entity.get("image"):
        return (
            f'<div class="{css_class}" '
            f'style="border-color:var({color_var});will-change:transform,opacity;">'
            f'<img src="{entity["image"]}" alt="{entity["name"]}" loading="eager">'
            f'</div>'
        )
    return (
        f'<div class="{css_class} portrait--initials" '
        f'style="background:var({color_var});will-change:transform,opacity;">'
        f'<span>{entity["initials"]}</span>'
        f'</div>'
    )


def _render_org_founded_card_html(card: dict, idx: int) -> str:
    """Render an org-founded card as an HTML section.

    Layout (top → bottom inside the card):
      1. Card header — eyebrow ``"FOUNDED BY"`` (right-aligned domain tag omitted
         for org cards to keep chrome minimal).
      2. Org node — rounded-square logo/monogram at top-centre.
      3. Org name (Playfair) + muted ``"Founded YEAR · TYPE"`` line.
      4. Hub-and-spoke SVG area — one spoke per founder, drawn with a stagger.
      5. Founder portraits in a row — circular, person-orange ring.
      6. Founder name + descriptor labels.
      7. Standard footer.

    The spoke draw uses ``stroke-dashoffset`` animation via a per-founder CSS
    class carrying a staggered ``transition-delay`` set inline so that the
    existing ``card.in-view`` toggle drives everything without extra JS.
    """
    founders = card["founders"]
    n = len(founders)

    # --- Founder portrait + label columns ---
    founder_cols_parts: list[str] = []
    for i, f in enumerate(founders):
        portrait_html = _render_org_portrait_html(
            {"image": f.get("image"), "name": f["name"], "initials": f["initials"]},
            css_class="portrait portrait--founder",
            color_var="--person",
        )
        founder_cols_parts.append(
            f'<div class="founder-col" data-founder-idx="{i}"'
            f' style="will-change:transform,opacity;">'
            f'  {portrait_html}'
            f'  <div class="founder-info" style="will-change:transform,opacity;">'
            f'    <span class="founder-name">{f["name"]}</span>'
            f'    <span class="founder-desc">{f["descriptor"]}</span>'
            f'  </div>'
            f'</div>'
        )
    founder_cols_html = "\n".join(founder_cols_parts)

    # --- SVG spokes (drawn via stroke-dashoffset) ---
    # We use a fixed viewBox height; spokes fan downward from the org node.
    # The org node sits at the top-centre of the SVG; each spoke points toward
    # the centre of its founder column.
    spoke_stagger_ms = 90  # delay between consecutive spokes
    org_cx = 50            # % x-centre (SVG units 0–100)
    org_cy = 8             # % y-origin  (top of SVG, near the org node foot)
    founder_y = 92         # % y-destination (bottom of SVG, above portraits)

    svg_parts: list[str] = []
    for i in range(n):
        # Distribute founder anchors evenly across the SVG width.
        # For 1 founder: 50 %. For 2: 25 % and 75 %. For 3: 17, 50, 83. Etc.
        fx = (i + 1) * 100 / (n + 1)
        delay_ms = 480 + i * spoke_stagger_ms   # org node appears at ~400ms
        svg_parts.append(
            f'<line class="org-spoke" data-spoke-idx="{i}"'
            f' x1="{org_cx}" y1="{org_cy}" x2="{fx:.1f}" y2="{founder_y}"'
            f' style="--spoke-delay:{delay_ms}ms;" />'
        )
    spokes_svg_html = "\n    ".join(svg_parts)

    # --- Org meta line ---
    meta_parts: list[str] = []
    if card.get("org_founded"):
        meta_parts.append(f"Founded {card['org_founded']}")
    meta_parts.append(card["org_type"])
    meta_line = " · ".join(meta_parts)

    # --- Org node HTML ---
    org_node_html = _render_org_portrait_html(
        {"image": card.get("org_image"), "name": card["org_name"], "initials": card["org_initials"]},
        css_class="portrait portrait--org-node",
        color_var="--org",
    )

    return f"""
<section class="snap-section" data-card-idx="{idx}">
  <div class="card card--org-founded" aria-label="{card['org_name']} — founded by">

    <!-- Card header -->
    <div class="card__header" style="will-change:transform,opacity;">
      <span class="eyebrow">FOUNDED BY</span>
    </div>

    <!-- Org node -->
    <div class="org-node-area" style="will-change:transform,opacity;">
      {org_node_html}
      <h2 class="org-name">{card['org_name']}</h2>
      <p class="org-meta">{meta_line}</p>
    </div>

    <!-- Hub-and-spoke SVG -->
    <div class="org-spokes-wrap" aria-hidden="true">
      <svg class="org-spokes-svg" viewBox="0 0 100 100" preserveAspectRatio="none"
           xmlns="http://www.w3.org/2000/svg">
    {spokes_svg_html}
      </svg>
    </div>

    <!-- Founder portraits row -->
    <div class="founders-row" style="will-change:transform,opacity;">
{founder_cols_html}
    </div>

    <!-- Footer -->
    <div class="card__footer" style="will-change:transform,opacity;">
      <span class="footer-brand">humansmap.com</span>
    </div>

  </div><!-- /.card -->
</section>
"""


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

CSS: str = textwrap.dedent("""
  /* =========================================================
     Design tokens
     ========================================================= */
  :root {
    --bg-page:  #f2ede3;
    --bg-card:  #f9f8f4;
    --border:   #e0d8c9;
    --text:     #1c1510;
    --muted:    #6b6256;
    --accent:   #1b4d8e;
    --person:   #bf6b28;

    /* Typography scale */
    --font-serif: 'Playfair Display', Georgia, serif;
    --font-sans:  'Inter', system-ui, -apple-system, sans-serif;

    /* Animation timing */
    --ease-out-quart: cubic-bezier(.22,.61,.36,1);
    --ease-spring:    cubic-bezier(.34,1.4,.64,1);
  }

  /* =========================================================
     Reset & base
     ========================================================= */
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  html {
    scroll-snap-type: y mandatory;
    overflow-y: scroll;
    height: 100%;
  }

  body {
    background: var(--bg-page);
    font-family: var(--font-sans);
    color: var(--text);
    min-height: 100%;
  }

  /* =========================================================
     Feed layout — each section snaps to viewport
     ========================================================= */
  .snap-section {
    scroll-snap-align: start;
    min-height: 100dvh;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 24px 16px;
  }

  /* =========================================================
     Card shell
     ========================================================= */
  .card {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 16px;
    width: 100%;
    max-width: 440px;
    padding: 28px 24px 20px;
    box-shadow:
      0 2px 8px rgba(28,21,16,.06),
      0 12px 36px rgba(28,21,16,.09);

    /* Card entrance animation target */
    will-change: transform, opacity;
    transform: translateY(18px);
    opacity: 0;
    transition:
      transform 400ms var(--ease-out-quart),
      opacity   400ms var(--ease-out-quart);
  }

  .card.in-view {
    transform: translateY(0);
    opacity: 1;
  }

  /* =========================================================
     Card header — eyebrow + domain tag
     ========================================================= */
  .card__header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 20px;
    transform: translateY(8px);
    opacity: 0;
    transition:
      transform 350ms var(--ease-out-quart) 80ms,
      opacity   350ms var(--ease-out-quart) 80ms;
  }

  .card.in-view .card__header {
    transform: translateY(0);
    opacity: 1;
  }

  .eyebrow {
    font-size: 10px;
    font-weight: 700;
    letter-spacing: .12em;
    text-transform: uppercase;
    color: var(--accent);
  }

  .domain-tag {
    font-size: 10px;
    font-weight: 600;
    letter-spacing: .06em;
    text-transform: uppercase;
    color: var(--muted);
    background: var(--border);
    padding: 3px 8px;
    border-radius: 20px;
  }

  /* =========================================================
     Relationship card — portraits + connector area
     ========================================================= */
  .card__connector-area {
    display: grid;
    grid-template-columns: 1fr auto 1fr;
    gap: 0;
    align-items: start;
  }

  /* --- Circular portrait --- */
  .portrait {
    width: 76px;
    height: 76px;
    border-radius: 50%;
    overflow: hidden;
    border: 2.5px solid var(--person);
    background: var(--surface, #ece8de);
    flex-shrink: 0;
  }

  .portrait img {
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
  }

  /* Initials fallback */
  .portrait--initials {
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--person);
  }
  .portrait--initials span {
    color: #fff;
    font-size: 22px;
    font-weight: 700;
    font-family: var(--font-serif);
    line-height: 1;
  }

  /* Portrait entrance animations */
  .portrait--a {
    transform: scale(.9);
    opacity: 0;
    transition:
      transform 420ms var(--ease-out-quart) 150ms,
      opacity   420ms var(--ease-out-quart) 150ms;
  }
  .portrait--b {
    transform: scale(.9);
    opacity: 0;
    transition:
      transform 420ms var(--ease-out-quart) 290ms,
      opacity   420ms var(--ease-out-quart) 290ms;
  }
  .card.in-view .portrait--a,
  .card.in-view .portrait--b {
    transform: scale(1);
    opacity: 1;
  }

  /* --- Person columns --- */
  .person-col {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 10px;
    text-align: center;
    min-width: 0;
  }

  .person-info {
    transform: translateY(8px);
    opacity: 0;
    transition:
      transform 320ms var(--ease-out-quart) 560ms,
      opacity   320ms var(--ease-out-quart) 560ms;
  }
  .person-col--b .person-info {
    transition-delay: 640ms, 640ms;
  }
  .card.in-view .person-info {
    transform: translateY(0);
    opacity: 1;
  }

  .person-name {
    font-family: var(--font-serif);
    font-size: 14px;
    font-weight: 700;
    color: var(--text);
    line-height: 1.2;
    margin-bottom: 3px;
  }

  .lifespan {
    display: block;
    font-size: 10px;
    font-weight: 500;
    color: var(--muted);
    letter-spacing: .04em;
    margin-bottom: 4px;
  }

  .person-desc {
    font-size: 11px;
    color: var(--muted);
    line-height: 1.45;
    margin-bottom: 4px;
  }

  .known-for {
    font-size: 10px;
    font-weight: 500;
    font-style: italic;
    color: var(--accent);
    line-height: 1.4;
  }

  /* --- Connector column (line + pill) --- */
  .connector {
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 0 8px;
    position: relative;
    min-height: 140px;
  }

  .connector__line {
    width: 2px;
    flex: 1;
    background: var(--border);
    border-radius: 1px;
    position: relative;
    overflow: hidden;
    margin: 8px 0;
    min-height: 60px;
  }

  /* The fill element draws itself top-to-bottom */
  .connector__fill {
    position: absolute;
    top: 0; left: 0; right: 0;
    background: var(--accent);
    height: 0;
    border-radius: 1px;
    transition: height 500ms var(--ease-out-quart) 400ms;
  }

  .card.in-view .connector__fill {
    height: 100%;
  }

  /* Relation pill */
  .relation-pill {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 2px;
    background: var(--accent);
    color: #fff;
    border-radius: 20px;
    padding: 6px 10px;
    white-space: nowrap;

    transform: scale(.8);
    opacity: 0;
    /* Spring overshoot for the pop-in */
    transition:
      transform 380ms var(--ease-spring) 780ms,
      opacity   280ms var(--ease-out-quart) 780ms;
  }

  .card.in-view .relation-pill {
    transform: scale(1);
    opacity: 1;
  }

  .relation-label {
    font-size: 10px;
    font-weight: 700;
    letter-spacing: .08em;
    text-transform: uppercase;
  }
  .relation-dates {
    font-size: 9px;
    font-weight: 500;
    opacity: .85;
  }

  /* =========================================================
     Dynasty card
     ========================================================= */
  .card--dynasty .card__header {
    margin-bottom: 12px;
  }

  .dynasty-title {
    margin-bottom: 20px;
    transform: translateY(8px);
    opacity: 0;
    transition:
      transform 380ms var(--ease-out-quart) 120ms,
      opacity   380ms var(--ease-out-quart) 120ms;
  }
  .card.in-view .dynasty-title {
    transform: translateY(0);
    opacity: 1;
  }

  .dynasty-family-label {
    font-family: var(--font-serif);
    font-size: 22px;
    font-weight: 700;
    color: var(--text);
    line-height: 1.15;
  }

  .dynasty-subtitle {
    font-size: 12px;
    font-weight: 500;
    color: var(--muted);
    margin-top: 3px;
  }

  /* Timeline container */
  .dynasty-timeline {
    display: grid;
    grid-template-columns: 20px 1fr;
    gap: 0 12px;
    position: relative;
  }

  /* Vertical spine */
  .dynasty-spine {
    position: relative;
    /* Full height of the rows container */
  }

  .dynasty-spine__fill {
    position: absolute;
    top: 0; left: 9px;
    width: 2px;
    background: var(--accent);
    height: 0;
    border-radius: 1px;
    transition: height 600ms var(--ease-out-quart) 200ms;
  }

  .card.in-view .dynasty-spine__fill {
    height: 100%;
  }

  /* Dynasty member rows */
  .dynasty-rows {
    display: flex;
    flex-direction: column;
    gap: 14px;
  }

  .dynasty-row {
    display: flex;
    align-items: center;
    gap: 12px;
    position: relative;

    /* Each row slides in from the left */
    transform: translateX(-12px);
    opacity: 0;
    /* Stagger set via JS (data-row-idx) */
    transition:
      transform 320ms var(--ease-out-quart),
      opacity   280ms var(--ease-out-quart);
  }

  /* Dot on the spine */
  .dynasty-row::before {
    content: '';
    position: absolute;
    left: -16px;
    top: 50%;
    transform: translateY(-50%);
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--accent);
    border: 2px solid var(--bg-card);
    flex-shrink: 0;
  }

  .card.in-view .dynasty-row {
    transform: translateX(0);
    opacity: 1;
  }

  /* Dynasty portrait sizing (set inline via JS/Python) */
  .portrait--dynasty {
    border-radius: 50%;
    overflow: hidden;
    border: 2px solid var(--person);
    background: var(--surface, #ece8de);
    flex-shrink: 0;
  }
  .portrait--dynasty img {
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
  }
  .portrait--dynasty.portrait--initials {
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--person);
  }
  .portrait--dynasty.portrait--initials span {
    color: #fff;
    font-size: 13px;
    font-weight: 700;
    font-family: var(--font-serif);
  }

  .dynasty-row__info {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }

  .dynasty-name {
    font-family: var(--font-serif);
    font-size: 13px;
    font-weight: 700;
    color: var(--text);
    line-height: 1.2;
  }

  .dynasty-label {
    font-size: 11px;
    color: var(--muted);
    line-height: 1.35;
    /* Truncate long descriptions */
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }

  .dynasty-lifespan {
    font-size: 10px;
    font-weight: 500;
    color: var(--accent);
    letter-spacing: .03em;
  }

  /* =========================================================
     Org-founded card
     ========================================================= */

  /* Semantic colour token for organisations (muted warm gray) */
  :root { --org: #8a8278; }

  /* Org node — rounded square, larger than a founder portrait */
  .portrait--org-node {
    width: 72px;
    height: 72px;
    border-radius: 14px;      /* rounded square, not circle */
    overflow: hidden;
    border: 2.5px solid var(--org);
    background: var(--surface, #ece8de);
    flex-shrink: 0;
    display: flex;
    align-items: center;
    justify-content: center;

    /* Entrance: rises + scales in first */
    transform: translateY(12px) scale(.88);
    opacity: 0;
    transition:
      transform 400ms var(--ease-spring)   160ms,
      opacity   360ms var(--ease-out-quart) 160ms;
  }
  .portrait--org-node img {
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
  }
  .portrait--org-node.portrait--initials span {
    color: #fff;
    font-size: 22px;
    font-weight: 700;
    font-family: var(--font-serif);
    line-height: 1;
  }
  .card.in-view .portrait--org-node {
    transform: translateY(0) scale(1);
    opacity: 1;
  }

  /* Org node area — centred stack */
  .org-node-area {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 6px;
    text-align: center;
    margin-bottom: 4px;

    transform: translateY(8px);
    opacity: 0;
    transition:
      transform 380ms var(--ease-out-quart) 260ms,
      opacity   380ms var(--ease-out-quart) 260ms;
  }
  .card.in-view .org-node-area {
    transform: translateY(0);
    opacity: 1;
  }

  .org-name {
    font-family: var(--font-serif);
    font-size: 18px;
    font-weight: 700;
    color: var(--text);
    line-height: 1.15;
  }

  .org-meta {
    font-size: 11px;
    color: var(--muted);
    line-height: 1.4;
  }

  /* SVG spoke container — fixed height, full width */
  .org-spokes-wrap {
    width: 100%;
    height: 56px;
    margin: 0 -4px;   /* bleed slightly past card padding so spokes reach edges */
    overflow: visible;
  }

  .org-spokes-svg {
    width: calc(100% + 8px);
    height: 100%;
    overflow: visible;
  }

  /* Individual spokes — drawn via stroke-dashoffset */
  .org-spoke {
    stroke: var(--accent);
    stroke-width: 1.5;
    stroke-linecap: round;
    fill: none;
    /* Use pathLength=1 trick via stroke-dasharray/dashoffset for a clean 0→1 draw */
    stroke-dasharray: 1;
    stroke-dashoffset: 1;
    /* vector-effect keeps stroke width screen-space regardless of SVG scaling */
    vector-effect: non-scaling-stroke;

    /* Delay injected inline via --spoke-delay custom property */
    transition: stroke-dashoffset 420ms var(--ease-out-quart) var(--spoke-delay, 480ms);
  }

  .card.in-view .org-spoke {
    stroke-dashoffset: 0;
  }

  /* Founders row — flex, centred, even spacing */
  .founders-row {
    display: flex;
    justify-content: center;
    gap: 12px;
    flex-wrap: nowrap;
  }

  /* Individual founder column */
  .founder-col {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 6px;
    text-align: center;
    min-width: 0;
    flex: 1;
    max-width: 90px;

    /* Stagger set in JS via data-founder-idx */
    transform: scale(.9);
    opacity: 0;
    transition:
      transform 360ms var(--ease-spring),
      opacity   320ms var(--ease-out-quart);
  }

  .card.in-view .founder-col {
    transform: scale(1);
    opacity: 1;
  }

  /* Founder portrait — same circular style as relationship cards */
  .portrait--founder {
    width: 52px;
    height: 52px;
    border-radius: 50%;
    overflow: hidden;
    border: 2px solid var(--person);
    background: var(--surface, #ece8de);
    flex-shrink: 0;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .portrait--founder img {
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
  }
  .portrait--founder.portrait--initials span {
    color: #fff;
    font-size: 15px;
    font-weight: 700;
    font-family: var(--font-serif);
    line-height: 1;
  }

  /* Founder text labels */
  .founder-info {
    display: flex;
    flex-direction: column;
    gap: 2px;

    transform: translateY(6px);
    opacity: 0;
    /* Delay added via JS (data-founder-idx) */
    transition:
      transform 280ms var(--ease-out-quart),
      opacity   260ms var(--ease-out-quart);
  }

  .card.in-view .founder-info {
    transform: translateY(0);
    opacity: 1;
  }

  .founder-name {
    font-family: var(--font-serif);
    font-size: 11px;
    font-weight: 700;
    color: var(--text);
    line-height: 1.2;
    /* Truncate very long names */
    overflow: hidden;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
  }

  .founder-desc {
    font-size: 10px;
    color: var(--muted);
    line-height: 1.35;
    overflow: hidden;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
  }

  /* =========================================================
     Card footer
     ========================================================= */
  .card__footer {
    margin-top: 20px;
    padding-top: 14px;
    border-top: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: center;
    transform: translateY(6px);
    opacity: 0;
    transition:
      transform 300ms var(--ease-out-quart) 900ms,
      opacity   300ms var(--ease-out-quart) 900ms;
  }

  .card.in-view .card__footer {
    transform: translateY(0);
    opacity: 1;
  }

  .footer-brand {
    font-size: 10px;
    font-weight: 600;
    letter-spacing: .1em;
    text-transform: uppercase;
    color: var(--accent);
    opacity: .6;
  }

  /* =========================================================
     Page chrome — intro + outro sections
     ========================================================= */
  .intro-section {
    scroll-snap-align: start;
    min-height: 100dvh;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 32px 24px;
  }

  .intro-card {
    max-width: 400px;
    text-align: center;
  }

  .intro-wordmark {
    font-family: var(--font-serif);
    font-size: 28px;
    font-weight: 700;
    color: var(--accent);
    letter-spacing: -.01em;
    margin-bottom: 12px;
  }

  .intro-tagline {
    font-size: 16px;
    color: var(--muted);
    line-height: 1.55;
    margin-bottom: 28px;
  }

  .intro-scroll-hint {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 6px;
    animation: bounce 1.8s ease-in-out infinite;
  }
  .intro-scroll-hint span {
    font-size: 11px;
    font-weight: 500;
    letter-spacing: .08em;
    text-transform: uppercase;
    color: var(--muted);
  }
  .intro-scroll-hint svg {
    opacity: .5;
  }

  @keyframes bounce {
    0%, 100% { transform: translateY(0); }
    50% { transform: translateY(6px); }
  }

  /* =========================================================
     Reduced motion — everything just appears
     ========================================================= */
  @media (prefers-reduced-motion: reduce) {
    .card,
    .card__header,
    .card__footer,
    .portrait--a, .portrait--b,
    .person-info,
    .connector__fill,
    .relation-pill,
    .dynasty-title,
    .dynasty-spine__fill,
    .dynasty-row,
    .portrait--org-node,
    .org-node-area,
    .org-spoke,
    .founder-col,
    .founder-info {
      transition: none !important;
      animation: none !important;
      transform: none !important;
      opacity: 1 !important;
    }
    .connector__fill {
      height: 100% !important;
    }
    .dynasty-spine__fill {
      height: 100% !important;
    }
    .org-spoke {
      stroke-dashoffset: 0 !important;
    }
  }

  /* =========================================================
     Responsive — mobile full-width
     ========================================================= */
  @media (max-width: 480px) {
    .snap-section {
      padding: 16px 12px;
      align-items: flex-start;
      padding-top: 32px;
    }
    .card {
      padding: 22px 18px 16px;
    }
    .portrait {
      width: 60px;
      height: 60px;
    }
    .portrait--initials span {
      font-size: 18px;
    }
    .person-name {
      font-size: 13px;
    }
  }
""")


# ---------------------------------------------------------------------------
# JavaScript
# ---------------------------------------------------------------------------

JS: str = textwrap.dedent("""
  /* =========================================================
     Animated feed — IntersectionObserver + parallax
     ========================================================= */
  (function () {
    'use strict';

    // -------------------------------------------------------
    // Respect reduced motion
    // -------------------------------------------------------
    const prefersReducedMotion =
      window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    // -------------------------------------------------------
    // IntersectionObserver: toggle .in-view on .card elements
    // Each .snap-section observed; when ≥50% visible, its
    // child .card gets .in-view added (and removed on exit so
    // scrolling back replays the animation).
    // -------------------------------------------------------
    const sections = document.querySelectorAll('.snap-section');

    function activateCard(section) {
      const card = section.querySelector('.card');
      if (!card) return;

      // Dynasty rows get staggered transition-delay via style
      if (card.classList.contains('card--dynasty')) {
        const rows = card.querySelectorAll('.dynasty-row');
        rows.forEach((row, i) => {
          const delay = 300 + i * 90;   // spine starts at 200ms; rows follow
          row.style.transitionDelay = `${delay}ms, ${delay}ms`;
        });
      }

      // Org-founded cards: stagger founder columns and their info labels.
      // Spokes already carry --spoke-delay inline; portraits pop in after the
      // spoke reaches them (spoke delay + ~420ms draw duration).
      if (card.classList.contains('card--org-founded')) {
        const spokeDelayBase = 480;  // matches CSS --spoke-delay default
        const spokeDuration  = 420;  // matches CSS spoke transition duration
        const spokePad       = 60;   // extra gap before portrait appears
        const spokePitch     = 90;   // ms between consecutive spokes (= spoke_stagger_ms in Python)

        const cols  = card.querySelectorAll('.founder-col');
        const infos = card.querySelectorAll('.founder-info');
        cols.forEach((col, i) => {
          const portraitDelay = spokeDelayBase + i * spokePitch + spokeDuration + spokePad;
          col.style.transitionDelay  = `${portraitDelay}ms, ${portraitDelay}ms`;
        });
        infos.forEach((info, i) => {
          const infoDelay = spokeDelayBase + i * spokePitch + spokeDuration + spokePad + 80;
          info.style.transitionDelay = `${infoDelay}ms, ${infoDelay}ms`;
        });
      }

      card.classList.add('in-view');
    }

    function deactivateCard(section) {
      const card = section.querySelector('.card');
      if (card) card.classList.remove('in-view');
    }

    if (!prefersReducedMotion) {
      const observer = new IntersectionObserver(
        (entries) => {
          entries.forEach((entry) => {
            if (entry.isIntersecting) {
              activateCard(entry.target);
            } else {
              deactivateCard(entry.target);
            }
          });
        },
        { threshold: 0.45 }
      );

      sections.forEach((section) => observer.observe(section));
    } else {
      // Reduced motion: show all cards immediately, no transitions
      sections.forEach((section) => {
        const card = section.querySelector('.card');
        if (card) card.classList.add('in-view');
      });
    }

    // -------------------------------------------------------
    // Subtle parallax — portraits move a few px slower than
    // text as the card drifts through the viewport.
    // Runs on scroll; only for non-reduced-motion users.
    // Capped at ±8px so it never feels gimmicky.
    // -------------------------------------------------------
    if (!prefersReducedMotion) {
      let rafPending = false;

      function applyParallax() {
        rafPending = false;
        const vh = window.innerHeight;

        sections.forEach((section) => {
          const card = section.querySelector('.card');
          if (!card || !card.classList.contains('in-view')) return;

          const rect = section.getBoundingClientRect();
          // progress: 0 when section top is at bottom of viewport,
          //           1 when section top is at top of viewport
          const progress = 1 - rect.top / vh;
          // Shift: –8px at bottom entrance, +8px at top exit
          const shift = (progress - 0.5) * 16;  // ±8px
          const clampedShift = Math.max(-8, Math.min(8, shift));

          // Apply inverse shift to portraits (they lag behind)
          const portraits = card.querySelectorAll('.portrait');
          portraits.forEach((p) => {
            // Don't override transform during entrance animation —
            // only apply once the card is fully visible (opacity ~1)
            p.style.transform = `translateY(${clampedShift * -0.5}px)`;
          });
        });
      }

      window.addEventListener('scroll', () => {
        if (!rafPending) {
          rafPending = true;
          requestAnimationFrame(applyParallax);
        }
      }, { passive: true });
    }

    // -------------------------------------------------------
    // Optional idle float on the currently-centred card
    // Very faint vertical oscillation, only when card is in-view
    // -------------------------------------------------------
    if (!prefersReducedMotion) {
      const style = document.createElement('style');
      style.textContent = `
        @keyframes idle-float {
          0%, 100% { box-shadow: 0 2px 8px rgba(28,21,16,.06), 0 12px 36px rgba(28,21,16,.09); }
          50%        { box-shadow: 0 6px 18px rgba(28,21,16,.09), 0 20px 48px rgba(28,21,16,.13); }
        }
        .card.in-view {
          animation: idle-float 4s ease-in-out infinite;
          animation-delay: 1.2s;  /* only starts after entrance completes */
        }
      `;
      document.head.appendChild(style);
    }

  })();
""")


# ---------------------------------------------------------------------------
# HTML page assembler
# ---------------------------------------------------------------------------


def _build_html(sections_html: list[str]) -> str:
    """Assemble the final self-contained HTML page."""
    body_parts = []

    # Intro snap section
    body_parts.append("""
<section class="intro-section">
  <div class="intro-card">
    <h1 class="intro-wordmark">humansmap.com</h1>
    <p class="intro-tagline">
      Unexpected connections between public figures — scroll to explore.
    </p>
    <div class="intro-scroll-hint" aria-hidden="true">
      <span>Scroll</span>
      <svg width="18" height="22" viewBox="0 0 18 22" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M9 1v20M1 14l8 7 8-7" stroke="#6b6256" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
    </div>
  </div>
</section>
""")

    for s in sections_html:
        body_parts.append(s)

    body_html = "\n".join(body_parts)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>HumansMap — Animated Feed Prototype</title>
  <meta name="description" content="Scroll-driven animated connections feed — design prototype">

  <!-- Google Fonts (the only external dependency) -->
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">

  <style>
{CSS}
  </style>
</head>
<body>

{body_html}

<script>
{JS}
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run() -> None:
    """Build the animated feed prototype HTML file."""
    print("=== build_animated_prototype ===")
    print(f"Output: {OUTPUT_PATH}")

    db = get_db(get_arango_client())

    sections: list[str] = []
    rel_count = 0
    org_count = 0
    dynasty_count = 0
    fallback_log: list[str] = []

    # -------------------------------------------------------------------
    # Relationship cards — take first 4 qualifying pairs
    # -------------------------------------------------------------------
    print("\n[relationship] Building curated pair cards…")
    for name_a, qid_a, name_b, qid_b, relation_label, dates in CURATED_PAIRS:
        if rel_count >= 4:
            break
        print(f"  Trying: {name_a} & {name_b}")
        card = _build_rel_card(db, name_a, qid_a, name_b, qid_b, relation_label, dates)
        if card is None:
            fallback_log.append(f"SKIP rel: {name_a} / {name_b}")
            continue
        idx = rel_count + org_count + dynasty_count
        sections.append(_render_rel_card_html(card, idx))
        print(f"  OK: {name_a} ({qid_a}) <-> {name_b} ({qid_b})")
        print(f"      A desc: {card['person_a']['description'][:70]}")
        print(f"      B desc: {card['person_b']['description'][:70]}")
        rel_count += 1

    # -------------------------------------------------------------------
    # Org-founded cards — Apple Inc. and Microsoft
    # These are inserted AFTER the relationship cards and BEFORE dynasties
    # to give a good rhythmic change of archetype mid-feed.
    # -------------------------------------------------------------------
    print("\n[org_founded] Building organisation-founder cards…")
    for org_name, org_qid, type_label in CURATED_ORG_FOUNDED:
        if org_count >= 2:
            break
        print(f"  Trying: {org_name} ({org_qid})")
        card = _build_org_founded_card(db, org_name, org_qid, type_label_override=type_label)
        if card is None:
            fallback_log.append(f"SKIP org: {org_name}")
            continue
        idx = rel_count + org_count + dynasty_count
        sections.append(_render_org_founded_card_html(card, idx))
        founders_summary = [f["name"] for f in card["founders"]]
        print(f"  OK: {card['org_name']} — founders: {', '.join(founders_summary)}")
        print(f"      Logo image: {'yes' if card.get('org_image') else 'no (monogram fallback)'}")
        org_count += 1

    # -------------------------------------------------------------------
    # Dynasty cards — Bach and Grimm
    # -------------------------------------------------------------------
    print("\n[dynasty] Building curated family cards…")
    for surname, qids in CURATED_DYNASTY_QIDS.items():
        if dynasty_count >= 2:
            break
        print(f"  Building dynasty: {surname} ({len(qids)} candidate QIDs)")
        card = _build_dynasty_card(db, surname, qids)
        if card is None:
            fallback_log.append(f"SKIP dynasty: {surname}")
            continue
        idx = rel_count + org_count + dynasty_count
        sections.append(_render_dynasty_card_html(card, idx))
        members_summary = [
            f"{m['name']} ({m['lifespan']})" for m in card["members"]
        ]
        print(f"  OK: {card['family_label']} — {', '.join(members_summary)}")
        dynasty_count += 1

    # -------------------------------------------------------------------
    # Assemble + write
    # -------------------------------------------------------------------
    total = rel_count + org_count + dynasty_count
    print(
        f"\n[build] {rel_count} relationship + {org_count} org-founded"
        f" + {dynasty_count} dynasty = {total} cards total"
    )

    if total == 0:
        raise RuntimeError("No cards were produced — check DB connectivity and data.")

    html = _build_html(sections)
    output_path = Path(OUTPUT_PATH)
    output_path.write_text(html, encoding="utf-8")

    size_kb = output_path.stat().st_size // 1024
    print(f"[build] Written {output_path}  ({size_kb} KB)")

    # Sanity checks
    b64_count = html.count("data:image/jpeg;base64")
    checks = {
        "<style>":                      "<style> block present",
        "IntersectionObserver":         "IntersectionObserver JS present",
        "@media (prefers-reduced-motion": "reduced-motion media query present",
        "data:image/jpeg;base64":       "at least one embedded portrait image",
        "card--org-founded":            "org-founded card class present",
        "org-spoke":                    "spoke-draw animation present",
        "--org:":                       "org colour token present",
        "stroke-dashoffset":            "spoke stroke-dashoffset animation present",
    }
    for needle, label in checks.items():
        ok = needle in html
        status = "OK" if ok else "MISSING"
        print(f"  [{status}] {label}")
    print(f"  [INFO] embedded base64 images: {b64_count}")

    if fallback_log:
        print("\n[warnings]")
        for msg in fallback_log:
            print(f"  {msg}")

    print("\nDone.")


if __name__ == "__main__":
    run()
