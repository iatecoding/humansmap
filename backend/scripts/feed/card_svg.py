"""
card_svg.py — Pure-SVG renderers for the HumansMap connections feed.

Two card types:
  • render_relationship_card  — two people connected by a relation label.
  • render_dynasty_card       — an ordered list of family members with
                                notability-weighted portraits.

No external dependencies.  All text in SVG output is XML-escaped.
Images arrive pre-encoded as data URIs; no file I/O is performed.
"""

from __future__ import annotations

import itertools
import xml.sax.saxutils as _saxutils
from typing import Any

# ---------------------------------------------------------------------------
# Design tokens  (load-bearing brand colours — do not repurpose)
# ---------------------------------------------------------------------------

CARD_WIDTH: int = 1080

BG: str = "#f9f8f4"          # warm off-white ground
CARD_BORDER: str = "#e0d8c9"
TEXT: str = "#1c1510"        # near-black primary
TEXT_MUTED: str = "#6b6256"  # muted subtitle / occupation text
ACCENT: str = "#1b4d8e"      # editorial blue: rules, relation label, CTA
PERSON: str = "#bf6b28"      # person ring / initials fill (semantic)
ORG: str = "#8a8278"
HAIRLINE: str = "#d8cfc0"
FONT_HEAD: str = "'Playfair Display', Georgia, serif"
FONT_BODY: str = "'Inter', system-ui, sans-serif"

# Fixed card dimensions
_REL_HEIGHT: int = 1350

# Portrait sizing bounds for dynasty cards
_DYN_R_MIN: float = 28.0
_DYN_R_MAX: float = 64.0
_DYN_NAME_MIN: float = 26.0
_DYN_NAME_MAX: float = 40.0

# Unique-id counter shared across calls within a single process session;
# collision-safe for any realistic feed batch.
_id_counter: itertools.count[int] = itertools.count(1)


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------


def _esc(text: Any) -> str:
    """XML-escape *text* for safe embedding in SVG element content."""
    return _saxutils.escape(str(text))


def _attr(text: Any) -> str:
    """XML-escape *text* for safe embedding in SVG attribute values."""
    return _saxutils.quoteattr(str(text), {'"': "&quot;", "'": "&apos;"})


def _initials(name: str) -> str:
    """Return up to two initials (first + last token) from *name*, uppercased."""
    tokens = name.strip().split()
    if not tokens:
        return "?"
    if len(tokens) == 1:
        return tokens[0][0].upper()
    return (tokens[0][0] + tokens[-1][0]).upper()


def _uid() -> str:
    """Return a unique string id for clip-path elements."""
    return f"cp{next(_id_counter)}"


def _portrait(
    cx: float,
    cy: float,
    r: float,
    image_data_uri: str | None,
    ring_color: str,
    name: str,
) -> str:
    """Render a circular portrait centred at (cx, cy) with radius r.

    If *image_data_uri* is provided the image is clipped to the circle and
    given a coloured stroke ring.  Otherwise a filled circle is drawn with
    white initials centred inside.

    Returns an SVG fragment (no enclosing element).
    """
    clip_id = _uid()
    parts: list[str] = []

    # Clip-path definition — always generated so the image branch can use it.
    parts.append(
        f'<defs>'
        f'<clipPath id={_attr(clip_id)}>'
        f'<circle cx={_attr(cx)} cy={_attr(cy)} r={_attr(r)} />'
        f'</clipPath>'
        f'</defs>'
    )

    if image_data_uri:
        # White backing disc (visible if image has transparency)
        parts.append(
            f'<circle cx={_attr(cx)} cy={_attr(cy)} r={_attr(r)} fill="#ffffff" />'
        )
        # Image clipped to circle
        x_img = cx - r
        y_img = cy - r
        d = r * 2
        parts.append(
            f'<image '
            f'x={_attr(x_img)} y={_attr(y_img)} '
            f'width={_attr(d)} height={_attr(d)} '
            f'href={_attr(image_data_uri)} '
            f'clip-path={_attr(f"url(#{clip_id})")} '
            f'preserveAspectRatio="xMidYMid slice" />'
        )
        # Coloured ring drawn on top
        parts.append(
            f'<circle cx={_attr(cx)} cy={_attr(cy)} r={_attr(r)} '
            f'fill="none" stroke={_attr(ring_color)} stroke-width="4" />'
        )
    else:
        # Filled circle with initials
        parts.append(
            f'<circle cx={_attr(cx)} cy={_attr(cy)} r={_attr(r)} '
            f'fill={_attr(ring_color)} />'
        )
        font_size = max(14.0, r * 0.55)
        initials = _esc(_initials(name))
        parts.append(
            f'<text '
            f'x={_attr(cx)} y={_attr(cy)} '
            f'font-family={_attr(FONT_BODY)} '
            f'font-size={_attr(font_size)} '
            f'font-weight="600" '
            f'fill="#ffffff" '
            f'text-anchor="middle" '
            f'dominant-baseline="central">'
            f'{initials}'
            f'</text>'
        )

    return "".join(parts)


def _scale(value: float, lo: float, hi: float, out_lo: float, out_hi: float) -> float:
    """Linearly map *value* from [lo, hi] to [out_lo, out_hi], clamped."""
    if hi == lo:
        return (out_lo + out_hi) / 2.0
    t = (value - lo) / (hi - lo)
    t = max(0.0, min(1.0, t))
    return out_lo + t * (out_hi - out_lo)


def _svg_wrap(width: int, height: int, body: str) -> str:
    """Wrap *body* in a top-level SVG element with standard attributes."""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'width={_attr(width)} height={_attr(height)} '
        f'viewBox={_attr(f"0 0 {width} {height}")}>'
        f'{body}'
        f'</svg>'
    )


def _accent_rule(x: float, y: float, width: float, thickness: float = 3.0) -> str:
    """A solid ACCENT-coloured horizontal rule."""
    return (
        f'<rect x={_attr(x)} y={_attr(y)} '
        f'width={_attr(width)} height={_attr(thickness)} '
        f'fill={_attr(ACCENT)} rx="1" />'
    )


def _hairline_rule(x: float, y: float, width: float) -> str:
    """A 1px HAIRLINE-coloured horizontal rule."""
    return (
        f'<line x1={_attr(x)} y1={_attr(y)} x2={_attr(x + width)} y2={_attr(y)} '
        f'stroke={_attr(HAIRLINE)} stroke-width="1" />'
    )


def _text_line(
    x: float,
    y: float,
    content: str,
    font_family: str,
    font_size: float,
    fill: str,
    anchor: str = "middle",
    weight: str = "400",
    letter_spacing: str = "0",
) -> str:
    """Single-line SVG text element."""
    return (
        f'<text '
        f'x={_attr(x)} y={_attr(y)} '
        f'font-family={_attr(font_family)} '
        f'font-size={_attr(font_size)} '
        f'font-weight={_attr(weight)} '
        f'fill={_attr(fill)} '
        f'text-anchor={_attr(anchor)} '
        f'dominant-baseline="auto" '
        f'letter-spacing={_attr(letter_spacing)}>'
        f'{_esc(content)}'
        f'</text>'
    )


def _wordmark(y: float, card_width: int = CARD_WIDTH) -> str:
    """Render 'humansmap.com' footer wordmark left-aligned."""
    x = 48.0
    return _text_line(
        x, y, "humansmap.com",
        font_family=FONT_BODY,
        font_size=24,
        fill=ACCENT,
        anchor="start",
        weight="600",
    )


# ---------------------------------------------------------------------------
# Relationship card
# ---------------------------------------------------------------------------


def _person_block_rel(
    cx: float,
    top_y: float,
    person: dict,
    portrait_r: float = 110.0,
) -> tuple[str, float]:
    """Render one person block for the relationship card.

    Returns (svg_fragment, bottom_y).
    """
    parts: list[str] = []
    portrait_cy = top_y + portrait_r

    parts.append(
        _portrait(
            cx, portrait_cy, portrait_r,
            image_data_uri=person.get("image_data_uri"),
            ring_color=PERSON,
            name=person["name"],
        )
    )

    name_y = portrait_cy + portrait_r + 52
    parts.append(
        _text_line(
            cx, name_y,
            content=person["name"],
            font_family=FONT_HEAD,
            font_size=52,
            fill=TEXT,
            weight="700",
        )
    )

    sub_parts: list[str] = []
    if person.get("occupation"):
        sub_parts.append(person["occupation"])
    if person.get("lifespan"):
        sub_parts.append(person["lifespan"])
    sub_text = "  ·  ".join(sub_parts) if sub_parts else ""

    sub_y = name_y + 42
    if sub_text:
        parts.append(
            _text_line(
                cx, sub_y,
                content=sub_text,
                font_family=FONT_BODY,
                font_size=28,
                fill=TEXT_MUTED,
            )
        )

    bottom_y = sub_y + 20
    return "".join(parts), bottom_y


def render_relationship_card(card: dict) -> str:
    """Render a relationship card to a standalone SVG string.

    *card* must match the relationship card input schema documented in
    the module docstring.  Returns a complete ``<svg>`` element.
    """
    width = CARD_WIDTH
    height = _REL_HEIGHT
    cx = width / 2.0
    parts: list[str] = []

    # Background
    parts.append(f'<rect width={_attr(width)} height={_attr(height)} fill={_attr(BG)} />')
    # Border
    parts.append(
        f'<rect x="1" y="1" width={_attr(width - 2)} height={_attr(height - 2)} '
        f'fill="none" stroke={_attr(CARD_BORDER)} stroke-width="1" rx="4" />'
    )

    # Top accent rule
    rule_y = 48.0
    parts.append(_accent_rule(48, rule_y, width - 96, thickness=3))

    # Eyebrow
    eyebrow_y = rule_y + 44
    parts.append(
        _text_line(
            cx, eyebrow_y,
            content=card.get("eyebrow", ""),
            font_family=FONT_BODY,
            font_size=22,
            fill=ACCENT,
            weight="600",
            letter_spacing="3",
        )
    )

    # Person A
    person_a_top = eyebrow_y + 52
    person_a_svg, person_a_bottom = _person_block_rel(cx, person_a_top, card["person_a"])
    parts.append(person_a_svg)

    # Relation pill
    pill_top = person_a_bottom + 40
    relation_label = card.get("relation_label", "")
    dates = card.get("dates")
    pill_text = relation_label + (f"  {dates}" if dates else "")

    pill_font_size = 26
    # Approximate text width (inter is ~0.52× font_size per char at normal weight)
    pill_text_w = len(pill_text) * pill_font_size * 0.52
    pill_pad_h = 28.0
    pill_pad_v = 16.0
    pill_w = pill_text_w + pill_pad_h * 2
    pill_h = pill_font_size + pill_pad_v * 2
    pill_x = cx - pill_w / 2
    pill_y = pill_top

    # Flanking hairlines
    hairline_margin = 48.0
    gap = 24.0
    hairline_end_left = pill_x - gap
    hairline_start_right = pill_x + pill_w + gap
    hairline_mid_y = pill_y + pill_h / 2

    if hairline_end_left > hairline_margin:
        parts.append(_hairline_rule(hairline_margin, hairline_mid_y, hairline_end_left - hairline_margin))
    if hairline_start_right < width - hairline_margin:
        parts.append(_hairline_rule(hairline_start_right, hairline_mid_y, width - hairline_margin - hairline_start_right))

    # Pill rect
    parts.append(
        f'<rect x={_attr(pill_x)} y={_attr(pill_y)} '
        f'width={_attr(pill_w)} height={_attr(pill_h)} '
        f'fill={_attr(BG)} stroke={_attr(HAIRLINE)} stroke-width="1" rx={_attr(pill_h / 2)} />'
    )
    # Pill text
    parts.append(
        _text_line(
            cx, pill_y + pill_h / 2 + pill_font_size * 0.36,
            content=pill_text,
            font_family=FONT_BODY,
            font_size=pill_font_size,
            fill=ACCENT,
            weight="500",
        )
    )

    # Person B
    person_b_top = pill_y + pill_h + 40
    person_b_svg, person_b_bottom = _person_block_rel(cx, person_b_top, card["person_b"])
    parts.append(person_b_svg)

    # Footer hairline
    footer_y = height - 72.0
    parts.append(_hairline_rule(48, footer_y, width - 96))
    parts.append(_wordmark(footer_y + 38, width))

    body = "".join(parts)
    return _svg_wrap(width, height, body)


# ---------------------------------------------------------------------------
# Dynasty card
# ---------------------------------------------------------------------------


def _overflow_pill(x: float, y: float, label: str, pill_h: float = 44.0) -> tuple[str, float]:
    """Render a muted '+N more' overflow pill row.

    Returns (svg_fragment, height_consumed).
    """
    font_size = 22.0
    text_w = len(label) * font_size * 0.52
    pad = 24.0
    pill_w = text_w + pad * 2
    pill_x = x
    pill_y = y

    rect = (
        f'<rect x={_attr(pill_x)} y={_attr(pill_y)} '
        f'width={_attr(pill_w)} height={_attr(pill_h)} '
        f'fill={_attr(CARD_BORDER)} stroke="none" rx={_attr(pill_h / 2)} />'
    )
    txt = _text_line(
        pill_x + pill_w / 2,
        pill_y + pill_h / 2 + font_size * 0.36,
        content=label,
        font_family=FONT_BODY,
        font_size=font_size,
        fill=TEXT_MUTED,
        weight="500",
    )
    return rect + txt, pill_h


def _dynasty_member_row(
    member: dict,
    y: float,
    r: float,
    name_font_size: float,
    spine_x: float,
    indent: float,
) -> tuple[str, float]:
    """Render a single dynasty member row.

    The portrait sits on the spine; the text block is to its right.
    Returns (svg_fragment, row_height).
    """
    parts: list[str] = []

    # Row height gives comfortable vertical breathing room
    row_height = r * 2 + 28.0

    cx = spine_x + indent
    cy = y + r + 10.0  # vertical centering within row

    # Connector: thin line from spine to portrait left edge
    if indent > 0:
        connector_color = "rgba(191,107,40,0.18)"
        parts.append(
            f'<line x1={_attr(spine_x)} y1={_attr(cy)} '
            f'x2={_attr(cx - r)} y2={_attr(cy)} '
            f'stroke={_attr(connector_color)} stroke-width="1.5" '
            f'stroke-dasharray="4 3" />'
        )

    parts.append(
        _portrait(
            cx, cy, r,
            image_data_uri=member.get("image_data_uri"),
            ring_color=PERSON,
            name=member["name"],
        )
    )

    # Text block to the right of portrait
    text_x = cx + r + 24.0
    name_y = cy - (name_font_size * 0.6) + 2.0  # slightly above centre

    parts.append(
        _text_line(
            text_x, name_y,
            content=member["name"],
            font_family=FONT_HEAD,
            font_size=name_font_size,
            fill=TEXT,
            anchor="start",
            weight="700",
        )
    )

    sub_parts: list[str] = []
    if member.get("occupation"):
        sub_parts.append(member["occupation"])
    if member.get("lifespan"):
        sub_parts.append(member["lifespan"])
    sub_text = "  ·  ".join(sub_parts) if sub_parts else ""

    if sub_text:
        sub_font_size = max(20.0, name_font_size * 0.62)
        sub_y = name_y + name_font_size + 4.0
        parts.append(
            _text_line(
                text_x, sub_y,
                content=sub_text,
                font_family=FONT_BODY,
                font_size=sub_font_size,
                fill=TEXT_MUTED,
                anchor="start",
            )
        )

    return "".join(parts), row_height


def render_dynasty_card(card: dict) -> str:
    """Render a dynasty card to a standalone SVG string.

    *card* must match the dynasty card input schema documented in the module
    docstring.  Card height is computed from member count and notability
    weights.  Returns a complete ``<svg>`` element.
    """
    width = CARD_WIDTH
    members: list[dict] = card.get("members", [])

    # ------------------------------------------------------------------
    # Pre-compute notability scaling bounds
    # ------------------------------------------------------------------
    real_members = [m for m in members if not m.get("is_overflow")]
    statements_list = [m.get("statements", 0) for m in real_members] or [0]
    st_min = float(min(statements_list))
    st_max = float(max(statements_list))

    def _r_for(member: dict) -> float:
        return _scale(member.get("statements", 0), st_min, st_max, _DYN_R_MIN, _DYN_R_MAX)

    def _name_fs_for(member: dict) -> float:
        return _scale(member.get("statements", 0), st_min, st_max, _DYN_NAME_MIN, _DYN_NAME_MAX)

    # ------------------------------------------------------------------
    # Layout pass 1 — measure heights to compute total card height
    # ------------------------------------------------------------------
    HEADER_H = 200.0     # top accent rule + title + subtitle
    FOOTER_H = 96.0      # hairline + wordmark + CTA
    ROW_GAP = 18.0       # vertical gap between rows

    max_depth = max((m.get("depth", 0) for m in real_members), default=0)
    available_w = width - 96.0 * 2   # left + right margin
    spine_x = 96.0
    depth_indent = min(40.0, available_w / max(max_depth + 1, 1) * 0.18)

    total_rows_h = 0.0
    for member in members:
        if member.get("is_overflow"):
            total_rows_h += 44.0 + ROW_GAP
        else:
            r = _r_for(member)
            total_rows_h += r * 2 + 28.0 + ROW_GAP

    height = int(HEADER_H + total_rows_h + FOOTER_H + 24)

    # ------------------------------------------------------------------
    # Render
    # ------------------------------------------------------------------
    parts: list[str] = []

    # Background + border
    parts.append(f'<rect width={_attr(width)} height={_attr(height)} fill={_attr(BG)} />')
    parts.append(
        f'<rect x="1" y="1" width={_attr(width - 2)} height={_attr(height - 2)} '
        f'fill="none" stroke={_attr(CARD_BORDER)} stroke-width="1" rx="4" />'
    )

    # Accent rule
    rule_y = 48.0
    parts.append(_accent_rule(48, rule_y, width - 96, thickness=3))

    # Family label (headline)
    label_y = rule_y + 60
    parts.append(
        _text_line(
            48, label_y,
            content=card.get("family_label", ""),
            font_family=FONT_HEAD,
            font_size=48,
            fill=TEXT,
            anchor="start",
            weight="700",
        )
    )

    # Subtitle
    sub_y = label_y + 44
    parts.append(
        _text_line(
            48, sub_y,
            content=card.get("subtitle", ""),
            font_family=FONT_BODY,
            font_size=26,
            fill=TEXT_MUTED,
            anchor="start",
        )
    )

    # Vertical spine line
    spine_top = HEADER_H - 8.0
    spine_bottom = HEADER_H + total_rows_h - ROW_GAP + 8.0
    parts.append(
        f'<line x1={_attr(spine_x)} y1={_attr(spine_top)} '
        f'x2={_attr(spine_x)} y2={_attr(spine_bottom)} '
        f'stroke={_attr(HAIRLINE)} stroke-width="1.5" />'
    )

    # Member rows
    cursor_y = HEADER_H
    for member in members:
        if member.get("is_overflow"):
            pill_svg, pill_h = _overflow_pill(
                spine_x + 16,
                cursor_y + 8,
                label=member.get("name", "+more"),
            )
            parts.append(pill_svg)
            cursor_y += 44.0 + ROW_GAP
        else:
            r = _r_for(member)
            name_fs = _name_fs_for(member)
            depth = member.get("depth", 0)
            indent = depth * depth_indent

            row_svg, row_h = _dynasty_member_row(
                member=member,
                y=cursor_y,
                r=r,
                name_font_size=name_fs,
                spine_x=spine_x,
                indent=indent,
            )
            parts.append(row_svg)
            cursor_y += row_h + ROW_GAP

    # Footer
    footer_y = height - FOOTER_H + 16.0
    parts.append(_hairline_rule(48, footer_y, width - 96))
    parts.append(_wordmark(footer_y + 38, width))

    cta_x = width - 48.0
    parts.append(
        _text_line(
            cta_x, footer_y + 38,
            content="Explore this family →",
            font_family=FONT_BODY,
            font_size=22,
            fill=ACCENT,
            anchor="end",
            weight="500",
        )
    )

    body = "".join(parts)
    return _svg_wrap(width, height, body)


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import xml.dom.minidom
    import os

    # --- Relationship card ---------------------------------------------------
    rel_card: dict = {
        "type": "relationship",
        "eyebrow": "DID YOU KNOW?",
        "relation_label": "collaborators",
        "dates": "1973–1981",
        "person_a": {
            "name": "Stanley Kubrick",
            "occupation": "Film director",
            "lifespan": "1928–1999",
            "statements": 412,
            "image_data_uri": None,  # exercises initials fallback
        },
        "person_b": {
            "name": "Arthur C. Clarke",
            "occupation": "Writer",
            "lifespan": "1917–2008",
            "statements": 287,
            # Small 1×1 red-pixel PNG as a data URI — valid image
            "image_data_uri": (
                "data:image/png;base64,"
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
                "AAAADUlEQVR42mP8z8BQDwADhQGAWjR9awAAAABJRU5ErkJggg=="
            ),
        },
    }

    rel_svg = render_relationship_card(rel_card)
    rel_path = "/tmp/feed_smoke_rel.svg"
    with open(rel_path, "w", encoding="utf-8") as f:
        f.write(rel_svg)

    # Validate XML
    xml.dom.minidom.parseString(rel_svg.encode("utf-8"))
    rel_size = os.path.getsize(rel_path)
    print(f"[OK] Relationship card: {rel_path} ({rel_size} bytes)")

    # --- Dynasty card --------------------------------------------------------
    dyn_card: dict = {
        "type": "dynasty",
        "family_label": "The Bach Family",
        "subtitle": "6 notable members · 3 generations",
        "members": [
            {
                "name": "Johann Sebastian Bach",
                "occupation": "Composer",
                "lifespan": "1685–1750",
                "statements": 980,
                "image_data_uri": (
                    "data:image/png;base64,"
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
                    "AAAADUlEQVR42mP8z8BQDwADhQGAWjR9awAAAABJRU5ErkJggg=="
                ),
                "depth": 0,
                "is_overflow": False,
            },
            {
                "name": "Carl Philipp Emanuel Bach",
                "occupation": "Composer",
                "lifespan": "1714–1788",
                "statements": 320,
                "image_data_uri": None,  # initials fallback
                "depth": 1,
                "is_overflow": False,
            },
            {
                "name": "Wilhelm Friedemann Bach",
                "occupation": "Composer",
                "lifespan": "1710–1784",
                "statements": 195,
                "image_data_uri": None,
                "depth": 1,
                "is_overflow": False,
            },
            {
                "name": "+3 more",
                "occupation": "",
                "lifespan": None,
                "statements": 0,
                "image_data_uri": None,
                "depth": 0,
                "is_overflow": True,  # overflow pill
            },
        ],
    }

    dyn_svg = render_dynasty_card(dyn_card)
    dyn_path = "/tmp/feed_smoke_dyn.svg"
    with open(dyn_path, "w", encoding="utf-8") as f:
        f.write(dyn_svg)

    # Validate XML
    xml.dom.minidom.parseString(dyn_svg.encode("utf-8"))
    dyn_size = os.path.getsize(dyn_path)
    print(f"[OK] Dynasty card:       {dyn_path} ({dyn_size} bytes)")

    # Print first 15 lines of relationship card
    print("\n--- First 15 lines of feed_smoke_rel.svg ---")
    with open(rel_path, encoding="utf-8") as f:
        for i, line in enumerate(f):
            if i >= 15:
                break
            print(line, end="")
