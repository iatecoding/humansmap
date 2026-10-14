"""OG image generation endpoint — produces 1200x630 PNG cards for social sharing."""

import asyncio
import io
import json
import logging
import os
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from PIL import Image, ImageDraw, ImageFont

from app.config import settings
from app.database import ORGANIZATIONS, PERSONS, get_arango_client, get_db
from app.rate_limit import limiter
from app.validators import valid_qid

_FEED_CARD_ID_RE = re.compile(r"^[a-f0-9]{6,16}$")

logger = logging.getLogger(__name__)

router = APIRouter(tags=["og"])

# Design tokens
BG_COLOR = "#f9f8f4"
SURFACE_COLOR = "#e4ddd0"
TEXT_COLOR = "#1c1510"
ACCENT_PERSON = "#bf6b28"
ACCENT_ORG = "#8a8278"
ACCENT_BLUE = "#1b4d8e"
TEXT_MUTED = "#8a8278"

OG_CACHE_DIR = "/app/og_cache"
CARD_W, CARD_H = 1200, 630

_FONT_DIR = "/app/fonts"
_FONT_REGULAR = os.path.join(_FONT_DIR, "Inter-Regular.ttf")
_FONT_BOLD = os.path.join(_FONT_DIR, "Inter-Bold.ttf")


def _get_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    """Load a font at the given size. Falls back to system fonts if Inter not found."""
    preferred = _FONT_BOLD if bold else _FONT_REGULAR
    if os.path.isfile(preferred):
        try:
            return ImageFont.truetype(preferred, size)
        except Exception:
            pass
    # Check common system locations for DejaVu (decent fallback)
    system_fonts = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in system_fonts:
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _wrap_text(text: str, font: ImageFont.ImageFont, max_width: int, draw: ImageDraw.ImageDraw) -> list[str]:
    """Wrap text to fit within max_width pixels."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if bbox[2] - bbox[0] <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _circle_crop(img: Image.Image, size: int) -> Image.Image:
    """Resize image to size×size and apply a circular mask."""
    img = img.convert("RGBA").resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.ellipse((0, 0, size, size), fill=255)
    result = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    result.paste(img, (0, 0), mask)
    return result


def _make_initial_avatar(initial: str, size: int) -> Image.Image:
    """Generate a circular avatar with the person's initial letter."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Draw filled circle background
    draw.ellipse((0, 0, size, size), fill=_hex_to_rgb(ACCENT_PERSON))
    font = _get_font(size // 2, bold=True)
    letter = (initial or "?")[0].upper()
    bbox = draw.textbbox((0, 0), letter, font=font)
    lw = bbox[2] - bbox[0]
    lh = bbox[3] - bbox[1]
    x = (size - lw) // 2 - bbox[0]
    y = (size - lh) // 2 - bbox[1]
    draw.text((x, y), letter, fill=(255, 255, 255), font=font)
    return img


def _generate_og_png(person: dict) -> bytes:
    """Render a 1200×630 OG card for the given person dict. Returns PNG bytes."""
    img = Image.new("RGB", (CARD_W, CARD_H), _hex_to_rgb(BG_COLOR))
    draw = ImageDraw.Draw(img)

    PADDING = 50
    PHOTO_SIZE = 320
    PHOTO_X = PADDING
    PHOTO_Y = (CARD_H - PHOTO_SIZE) // 2

    # --- Photo ---
    qid = person.get("_key") or person.get("id", "")
    photo_path = os.path.join(settings.image_cache_dir, f"{qid}.jpg")
    name = person.get("name_en") or person.get("name_it") or ""

    if os.path.isfile(photo_path):
        try:
            photo = Image.open(photo_path)
            photo_circle = _circle_crop(photo, PHOTO_SIZE)
        except Exception:
            photo_circle = _make_initial_avatar(name, PHOTO_SIZE)
    else:
        photo_circle = _make_initial_avatar(name, PHOTO_SIZE)

    # Paste circle photo (RGBA) onto RGB background
    if photo_circle.mode == "RGBA":
        img.paste(photo_circle, (PHOTO_X, PHOTO_Y), photo_circle.split()[3])
    else:
        img.paste(photo_circle, (PHOTO_X, PHOTO_Y))

    # --- Text area ---
    TEXT_X = PHOTO_X + PHOTO_SIZE + 44
    TEXT_MAX_W = CARD_W - TEXT_X - PADDING
    text_y = PHOTO_Y + 10

    # Name (large, bold)
    font_name = _get_font(64, bold=True)
    name_lines = _wrap_text(name, font_name, TEXT_MAX_W, draw)
    for line in name_lines[:2]:  # cap at 2 lines
        draw.text((TEXT_X, text_y), line, fill=_hex_to_rgb(TEXT_COLOR), font=font_name)
        bbox = draw.textbbox((TEXT_X, text_y), line, font=font_name)
        text_y += (bbox[3] - bbox[1]) + 8
    text_y += 12

    # Occupations (accent orange)
    occupations = person.get("occupations") or []
    if occupations:
        font_occ = _get_font(34)
        occ_str = " \u00b7 ".join(o.replace("_", " ").title() for o in occupations[:3])
        draw.text((TEXT_X, text_y), occ_str, fill=_hex_to_rgb(ACCENT_PERSON), font=font_occ)
        bbox = draw.textbbox((TEXT_X, text_y), occ_str, font=font_occ)
        text_y += (bbox[3] - bbox[1]) + 14

    # Nationality + years (muted)
    font_meta = _get_font(30)
    meta_parts: list[str] = []
    nats = person.get("nationalities") or []
    if nats:
        meta_parts.append(", ".join(n.replace("_", " ").title() for n in nats[:2]))
    birth = (person.get("birth_date") or "")[:4]
    death = (person.get("death_date") or "")[:4]
    if birth:
        # Use ASCII hyphen-minus which all fonts render correctly
        lifespan = f"{birth} - {death}" if death else f"b. {birth}"
        meta_parts.append(lifespan)
    if meta_parts:
        meta_str = "  \u00b7  ".join(meta_parts)
        draw.text((TEXT_X, text_y), meta_str, fill=_hex_to_rgb(TEXT_MUTED), font=font_meta)
        bbox = draw.textbbox((TEXT_X, text_y), meta_str, font=font_meta)
        text_y += (bbox[3] - bbox[1]) + 18

    # Description snippet
    desc = (person.get("description_en") or person.get("description_it") or "").strip()
    if desc:
        if len(desc) > 160:
            desc = desc[:157].rsplit(" ", 1)[0] + "..."
        font_desc = _get_font(30)
        desc_lines = _wrap_text(desc, font_desc, TEXT_MAX_W, draw)
        for line in desc_lines[:3]:
            draw.text((TEXT_X, text_y), line, fill=_hex_to_rgb(TEXT_MUTED), font=font_desc)
            bbox = draw.textbbox((TEXT_X, text_y), line, font=font_desc)
            text_y += (bbox[3] - bbox[1]) + 6

    # --- Divider line ---
    draw.rectangle(
        [(PADDING, CARD_H - 72), (CARD_W - PADDING, CARD_H - 71)],
        fill=_hex_to_rgb(SURFACE_COLOR),
    )

    # --- Wordmark bottom-right ---
    font_wm = _get_font(30, bold=True)
    wordmark = "humansmap.com"
    wm_bbox = draw.textbbox((0, 0), wordmark, font=font_wm)
    wm_w = wm_bbox[2] - wm_bbox[0]
    draw.text(
        (CARD_W - PADDING - wm_w, CARD_H - 56),
        wordmark,
        fill=_hex_to_rgb(ACCENT_BLUE),
        font=font_wm,
    )

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _make_org_avatar(initial: str, size: int) -> Image.Image:
    """Generate a circular avatar with the org's initial letter in org color."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((0, 0, size, size), fill=_hex_to_rgb(ACCENT_ORG))
    font = _get_font(size // 2, bold=True)
    letter = (initial or "?")[0].upper()
    bbox = draw.textbbox((0, 0), letter, font=font)
    lw = bbox[2] - bbox[0]
    lh = bbox[3] - bbox[1]
    x = (size - lw) // 2 - bbox[0]
    y = (size - lh) // 2 - bbox[1]
    draw.text((x, y), letter, fill=(255, 255, 255), font=font)
    return img


def _generate_org_og_png(org: dict) -> bytes:
    """Render a 1200×630 OG card for the given organization dict. Returns PNG bytes."""
    img = Image.new("RGB", (CARD_W, CARD_H), _hex_to_rgb(BG_COLOR))
    draw = ImageDraw.Draw(img)

    PADDING = 50
    PHOTO_SIZE = 320
    PHOTO_X = PADDING
    PHOTO_Y = (CARD_H - PHOTO_SIZE) // 2

    qid = org.get("_key") or org.get("id", "")
    name = org.get("name_en") or org.get("name_it") or ""

    # Logo/avatar — try cached image, fall back to letter avatar
    photo_path = os.path.join(settings.image_cache_dir, f"{qid}.jpg")
    if os.path.isfile(photo_path):
        try:
            photo = Image.open(photo_path)
            photo_circle = _circle_crop(photo, PHOTO_SIZE)
        except Exception:
            photo_circle = _make_org_avatar(name, PHOTO_SIZE)
    else:
        photo_circle = _make_org_avatar(name, PHOTO_SIZE)

    if photo_circle.mode == "RGBA":
        img.paste(photo_circle, (PHOTO_X, PHOTO_Y), photo_circle.split()[3])
    else:
        img.paste(photo_circle, (PHOTO_X, PHOTO_Y))

    # Text area
    TEXT_X = PHOTO_X + PHOTO_SIZE + 44
    TEXT_MAX_W = CARD_W - TEXT_X - PADDING
    text_y = PHOTO_Y + 10

    # Name
    font_name = _get_font(64, bold=True)
    name_lines = _wrap_text(name, font_name, TEXT_MAX_W, draw)
    for line in name_lines[:2]:
        draw.text((TEXT_X, text_y), line, fill=_hex_to_rgb(TEXT_COLOR), font=font_name)
        bbox = draw.textbbox((TEXT_X, text_y), line, font=font_name)
        text_y += (bbox[3] - bbox[1]) + 8
    text_y += 12

    # Type
    org_type = org.get("org_type") or org.get("type") or ""
    if org_type:
        font_type = _get_font(34)
        type_str = org_type.replace("_", " ").title()
        draw.text((TEXT_X, text_y), type_str, fill=_hex_to_rgb(ACCENT_ORG), font=font_type)
        bbox = draw.textbbox((TEXT_X, text_y), type_str, font=font_type)
        text_y += (bbox[3] - bbox[1]) + 14

    # Founded + HQ (muted)
    font_meta = _get_font(30)
    meta_parts: list[str] = []
    founded = org.get("founded") or ""
    if founded:
        meta_parts.append(f"Founded {str(founded)[:4]}")
    hq = org.get("headquarters") or ""
    if hq:
        meta_parts.append(hq)
    country = org.get("country") or ""
    if country and not hq:
        meta_parts.append(country)
    if meta_parts:
        meta_str = "  ·  ".join(meta_parts)
        draw.text((TEXT_X, text_y), meta_str, fill=_hex_to_rgb(TEXT_MUTED), font=font_meta)
        bbox = draw.textbbox((TEXT_X, text_y), meta_str, font=font_meta)
        text_y += (bbox[3] - bbox[1]) + 18

    # Description
    desc = (org.get("description_en") or org.get("description_it") or "").strip()
    if desc:
        if len(desc) > 160:
            desc = desc[:157].rsplit(" ", 1)[0] + "..."
        font_desc = _get_font(30)
        desc_lines = _wrap_text(desc, font_desc, TEXT_MAX_W, draw)
        for line in desc_lines[:3]:
            draw.text((TEXT_X, text_y), line, fill=_hex_to_rgb(TEXT_MUTED), font=font_desc)
            bbox = draw.textbbox((TEXT_X, text_y), line, font=font_desc)
            text_y += (bbox[3] - bbox[1]) + 6

    # Divider line
    draw.rectangle(
        [(PADDING, CARD_H - 72), (CARD_W - PADDING, CARD_H - 71)],
        fill=_hex_to_rgb(SURFACE_COLOR),
    )

    # Wordmark
    font_wm = _get_font(30, bold=True)
    wordmark = "humansmap.com"
    wm_bbox = draw.textbbox((0, 0), wordmark, font=font_wm)
    wm_w = wm_bbox[2] - wm_bbox[0]
    draw.text(
        (CARD_W - PADDING - wm_w, CARD_H - 56),
        wordmark,
        fill=_hex_to_rgb(ACCENT_BLUE),
        font=font_wm,
    )

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Feed card OG image helpers
# ---------------------------------------------------------------------------

def _load_portrait(qid: str, size: int, is_org: bool = False) -> Image.Image:
    """Load a circular portrait from the image cache, falling back to an initial avatar."""
    photo_path = os.path.join(settings.image_cache_dir, f"{qid}.jpg")
    name = qid  # worst-case fallback initial
    if os.path.isfile(photo_path):
        try:
            photo = Image.open(photo_path)
            return _circle_crop(photo, size)
        except Exception:
            pass
    if is_org:
        return _make_org_avatar(name, size)
    return _make_initial_avatar(name, size)


def _load_portrait_named(qid: str, name: str, size: int, is_org: bool = False) -> Image.Image:
    """Like _load_portrait but uses the entity name for avatar initials."""
    photo_path = os.path.join(settings.image_cache_dir, f"{qid}.jpg")
    if os.path.isfile(photo_path):
        try:
            photo = Image.open(photo_path)
            return _circle_crop(photo, size)
        except Exception:
            pass
    if is_org:
        return _make_org_avatar(name, size)
    return _make_initial_avatar(name, size)


def _paste_circle(img: Image.Image, circle: Image.Image, x: int, y: int) -> None:
    """Paste an RGBA circle onto an RGB image using its alpha channel."""
    if circle.mode == "RGBA":
        img.paste(circle, (x, y), circle.split()[3])
    else:
        img.paste(circle, (x, y))


def _draw_wordmark(draw: ImageDraw.ImageDraw) -> None:
    """Draw the humansmap.com wordmark in the bottom-right corner."""
    PADDING = 50
    font_wm = _get_font(30, bold=True)
    wordmark = "humansmap.com"
    wm_bbox = draw.textbbox((0, 0), wordmark, font=font_wm)
    wm_w = wm_bbox[2] - wm_bbox[0]
    draw.text(
        (CARD_W - PADDING - wm_w, CARD_H - 56),
        wordmark,
        fill=_hex_to_rgb(ACCENT_BLUE),
        font=font_wm,
    )


def _draw_divider(draw: ImageDraw.ImageDraw) -> None:
    """Draw the horizontal divider line above the footer."""
    PADDING = 50
    draw.rectangle(
        [(PADDING, CARD_H - 72), (CARD_W - PADDING, CARD_H - 71)],
        fill=_hex_to_rgb(SURFACE_COLOR),
    )


def _render_dynasty_feed_og(card: dict) -> bytes:
    """1200×630 OG card for a dynasty card."""
    img = Image.new("RGB", (CARD_W, CARD_H), _hex_to_rgb(BG_COLOR))
    draw = ImageDraw.Draw(img)
    PADDING = 50

    # --- Eyebrow ---
    font_eyebrow = _get_font(26, bold=True)
    draw.text(
        (PADDING, PADDING),
        "FAMILY DYNASTY",
        fill=_hex_to_rgb(ACCENT_BLUE),
        font=font_eyebrow,
    )

    # --- Title ---
    title = card.get("title", "")
    font_title = _get_font(64, bold=True)
    title_lines = _wrap_text(title, font_title, CARD_W - 2 * PADDING, draw)
    title_y = PADDING + 42
    for line in title_lines[:2]:
        draw.text((PADDING, title_y), line, fill=_hex_to_rgb(TEXT_COLOR), font=font_title)
        bbox = draw.textbbox((PADDING, title_y), line, font=font_title)
        title_y += (bbox[3] - bbox[1]) + 6

    # --- Subtitle (muted) ---
    subtitle = card.get("subtitle", "")
    if subtitle:
        font_sub = _get_font(30)
        draw.text((PADDING, title_y + 8), subtitle, fill=_hex_to_rgb(TEXT_MUTED), font=font_sub)
        title_y += _get_text_height(draw, subtitle, font_sub) + 16

    # --- Member portraits row ---
    members = card.get("members", [])[:5]
    n = len(members)
    if n:
        AVATAR_SIZE = 110
        CAPTION_GAP = 10
        CAPTION_LINE_H = 22
        CAPTIONS_H = CAPTION_LINE_H * 2 + 4  # name + life

        # Row is centred below the title block
        portrait_y = title_y + 28
        # Make sure it fits above the footer divider
        MAX_Y = CARD_H - 72 - AVATAR_SIZE - CAPTIONS_H - CAPTION_GAP - 10
        portrait_y = min(portrait_y, MAX_Y)

        total_w = n * AVATAR_SIZE + (n - 1) * 24
        start_x = (CARD_W - total_w) // 2

        font_caption = _get_font(22)
        font_life = _get_font(19)
        for i, member in enumerate(members):
            qid = member.get("qid", "")
            name = member.get("name", "")
            life = member.get("life", "")
            ax = start_x + i * (AVATAR_SIZE + 24)
            portrait = _load_portrait_named(qid, name, AVATAR_SIZE)
            _paste_circle(img, portrait, ax, portrait_y)
            # First name only for caption
            first_name = name.split()[0] if name else ""
            cap_bbox = draw.textbbox((0, 0), first_name, font=font_caption)
            cap_w = cap_bbox[2] - cap_bbox[0]
            cap_x = ax + (AVATAR_SIZE - cap_w) // 2
            cap_y = portrait_y + AVATAR_SIZE + CAPTION_GAP
            draw.text((cap_x, cap_y), first_name, fill=_hex_to_rgb(TEXT_COLOR), font=font_caption)
            if life:
                life_bbox = draw.textbbox((0, 0), life, font=font_life)
                life_w = life_bbox[2] - life_bbox[0]
                life_x = ax + (AVATAR_SIZE - life_w) // 2
                draw.text(
                    (life_x, cap_y + CAPTION_LINE_H + 4),
                    life,
                    fill=_hex_to_rgb(TEXT_MUTED),
                    font=font_life,
                )

    _draw_divider(draw)
    _draw_wordmark(draw)

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _get_text_height(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[3] - bbox[1]


def _render_relationship_feed_og(card: dict) -> bytes:
    """1200×630 OG card for a relationship card."""
    img = Image.new("RGB", (CARD_W, CARD_H), _hex_to_rgb(BG_COLOR))
    draw = ImageDraw.Draw(img)
    PADDING = 50

    a = card.get("a", {})
    b = card.get("b", {})
    relation = card.get("relation", "")
    dates = card.get("dates") or ""
    domains = card.get("domains") or []

    # --- Eyebrow: domain contrast ---
    if len(domains) >= 2:
        eyebrow = f"{domains[0].upper()}  ↔  {domains[1].upper()}"
    elif len(domains) == 1:
        eyebrow = domains[0].upper()
    else:
        eyebrow = ""
    if eyebrow:
        font_eyebrow = _get_font(26, bold=True)
        draw.text((PADDING, PADDING), eyebrow, fill=_hex_to_rgb(ACCENT_BLUE), font=font_eyebrow)

    # Portrait layout: centred, two large circles with connector in between
    AVATAR_SIZE = 200
    GAP = 120  # space between the two portraits for the connector pill
    total_portraits_w = 2 * AVATAR_SIZE + GAP
    start_x = (CARD_W - total_portraits_w) // 2

    PORTRAIT_Y = 130
    ax = start_x
    bx = start_x + AVATAR_SIZE + GAP

    portrait_a = _load_portrait_named(a.get("qid", ""), a.get("name", ""), AVATAR_SIZE)
    portrait_b = _load_portrait_named(b.get("qid", ""), b.get("name", ""), AVATAR_SIZE)
    _paste_circle(img, portrait_a, ax, PORTRAIT_Y)
    _paste_circle(img, portrait_b, bx, PORTRAIT_Y)

    # --- Connector line ---
    centre_y = PORTRAIT_Y + AVATAR_SIZE // 2
    line_x1 = ax + AVATAR_SIZE + 10
    line_x2 = bx - 10
    draw.line([(line_x1, centre_y), (line_x2, centre_y)], fill=_hex_to_rgb(SURFACE_COLOR), width=3)

    # --- Relation pill centred between the two portraits ---
    relation_text = relation.upper().replace("_", " ") if relation else ""
    font_pill = _get_font(26, bold=True)
    pill_bbox = draw.textbbox((0, 0), relation_text, font=font_pill)
    pill_text_w = pill_bbox[2] - pill_bbox[0]
    pill_text_h = pill_bbox[3] - pill_bbox[1]
    pill_pad_x, pill_pad_y = 18, 10
    pill_w = pill_text_w + 2 * pill_pad_x
    pill_h = pill_text_h + 2 * pill_pad_y
    pill_cx = ax + AVATAR_SIZE + GAP // 2  # centre of the gap
    pill_x = pill_cx - pill_w // 2
    pill_y = centre_y - pill_h // 2
    # Draw pill background
    draw.rounded_rectangle(
        [(pill_x, pill_y), (pill_x + pill_w, pill_y + pill_h)],
        radius=pill_h // 2,
        fill=_hex_to_rgb(SURFACE_COLOR),
    )
    draw.text(
        (pill_x + pill_pad_x, pill_y + pill_pad_y - pill_bbox[1]),
        relation_text,
        fill=_hex_to_rgb(ACCENT_BLUE),
        font=font_pill,
    )

    # --- Names under portraits ---
    font_name = _get_font(32, bold=True)
    name_y = PORTRAIT_Y + AVATAR_SIZE + 14

    name_a = a.get("name", "")
    name_b = b.get("name", "")

    # Center-align name under each portrait
    for name_str, px, p_size in [(name_a, ax, AVATAR_SIZE), (name_b, bx, AVATAR_SIZE)]:
        name_lines = _wrap_text(name_str, font_name, p_size + 60, draw)
        ny = name_y
        for line in name_lines[:2]:
            lb = draw.textbbox((0, 0), line, font=font_name)
            lw = lb[2] - lb[0]
            lx = px + (p_size - lw) // 2
            draw.text((lx, ny), line, fill=_hex_to_rgb(TEXT_COLOR), font=font_name)
            ny += (lb[3] - lb[1]) + 4

    # --- Dates (if present) — muted, centred ---
    if dates:
        font_dates = _get_font(26)
        db_ = draw.textbbox((0, 0), dates, font=font_dates)
        dw = db_[2] - db_[0]
        draw.text(
            ((CARD_W - dw) // 2, name_y + 80),
            dates,
            fill=_hex_to_rgb(TEXT_MUTED),
            font=font_dates,
        )

    _draw_divider(draw)
    _draw_wordmark(draw)

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _render_organization_feed_og(card: dict) -> bytes:
    """1200×630 OG card for an organization card."""
    img = Image.new("RGB", (CARD_W, CARD_H), _hex_to_rgb(BG_COLOR))
    draw = ImageDraw.Draw(img)
    PADDING = 50

    org = card.get("org", {})
    org_qid = org.get("qid", "")
    org_name = org.get("name", "")
    headline = card.get("headline", "")
    people = card.get("people", [])[:5]

    # --- Org logo + name row ---
    LOGO_SIZE = 100
    LOGO_Y = PADDING
    logo = _load_portrait_named(org_qid, org_name, LOGO_SIZE, is_org=True)
    _paste_circle(img, logo, PADDING, LOGO_Y)

    # Org name to the right of logo
    font_org_name = _get_font(48, bold=True)
    org_name_x = PADDING + LOGO_SIZE + 20
    org_name_max_w = CARD_W - org_name_x - PADDING
    org_name_lines = _wrap_text(org_name, font_org_name, org_name_max_w, draw)
    ony = LOGO_Y + (LOGO_SIZE - _get_text_height(draw, org_name, font_org_name)) // 2
    for line in org_name_lines[:2]:
        draw.text((org_name_x, ony), line, fill=_hex_to_rgb(TEXT_COLOR), font=font_org_name)
        bbox = draw.textbbox((org_name_x, ony), line, font=font_org_name)
        ony += (bbox[3] - bbox[1]) + 6

    # --- Headline eyebrow ---
    if headline:
        font_headline = _get_font(30, bold=True)
        hl_y = LOGO_Y + LOGO_SIZE + 18
        draw.text(
            (PADDING, hl_y),
            headline.upper(),
            fill=_hex_to_rgb(ACCENT_BLUE),
            font=font_headline,
        )
    else:
        hl_y = LOGO_Y + LOGO_SIZE + 4

    # --- People row ---
    if people:
        AVATAR_SIZE = 110
        CAPTION_GAP = 10
        CAPTION_LINE_H = 22

        # Horizontal centred row below the headline
        n = len(people)
        portrait_y = hl_y + 60
        # Clamp so we don't overflow into the footer
        MAX_Y = CARD_H - 72 - AVATAR_SIZE - CAPTION_LINE_H * 2 - CAPTION_GAP - 14
        portrait_y = min(portrait_y, MAX_Y)

        total_w = n * AVATAR_SIZE + (n - 1) * 28
        start_x = (CARD_W - total_w) // 2

        font_caption = _get_font(22)
        font_role = _get_font(19)
        for i, person in enumerate(people):
            qid = person.get("qid", "")
            name = person.get("name", "")
            role = person.get("role", "")
            ax = start_x + i * (AVATAR_SIZE + 28)
            portrait = _load_portrait_named(qid, name, AVATAR_SIZE)
            _paste_circle(img, portrait, ax, portrait_y)
            # Name caption
            first_name = name.split()[0] if name else ""
            cap_bbox = draw.textbbox((0, 0), first_name, font=font_caption)
            cap_w = cap_bbox[2] - cap_bbox[0]
            cap_x = ax + (AVATAR_SIZE - cap_w) // 2
            cap_y = portrait_y + AVATAR_SIZE + CAPTION_GAP
            draw.text((cap_x, cap_y), first_name, fill=_hex_to_rgb(TEXT_COLOR), font=font_caption)
            # Role caption (muted)
            if role:
                role_short = role[:20] if len(role) > 20 else role
                role_bbox = draw.textbbox((0, 0), role_short, font=font_role)
                role_w = role_bbox[2] - role_bbox[0]
                role_x = ax + (AVATAR_SIZE - role_w) // 2
                draw.text(
                    (role_x, cap_y + CAPTION_LINE_H + 4),
                    role_short,
                    fill=_hex_to_rgb(TEXT_MUTED),
                    font=font_role,
                )

    _draw_divider(draw)
    _draw_wordmark(draw)

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _generate_feed_og_png(card: dict) -> bytes:
    """Dispatch to the correct layout renderer based on card type."""
    card_type = card.get("type", "")
    if card_type == "dynasty":
        return _render_dynasty_feed_og(card)
    elif card_type == "relationship":
        return _render_relationship_feed_og(card)
    elif card_type == "organization":
        return _render_organization_feed_og(card)
    else:
        raise ValueError(f"Unknown feed card type: {card_type!r}")


@router.get("/og/feed/{card_id}.png", include_in_schema=False)
@limiter.limit("30/minute")
async def og_feed_image(request: Request, card_id: str):
    """Generate (or serve cached) OG card PNG for a feed card id."""
    if not _FEED_CARD_ID_RE.match(card_id):
        raise HTTPException(400, "Invalid card id")

    cache_dir = Path(OG_CACHE_DIR)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"feed_{card_id}.png"

    if cache_path.is_file():
        return Response(
            content=cache_path.read_bytes(),
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=604800"},
        )

    from app.services.cache import get_redis

    raw = get_redis().hget("feed:cardmap:v2", card_id)
    if not raw:
        raise HTTPException(404, f"Feed card {card_id} not found")

    card = json.loads(raw)
    png_bytes = await asyncio.to_thread(_generate_feed_og_png, card)

    try:
        cache_path.write_bytes(png_bytes)
    except Exception as e:
        logger.warning("Could not write OG cache for feed card %s: %s", card_id, e)

    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=604800"},
    )


@router.get("/og/{qid}.png", include_in_schema=False)
@limiter.limit("30/minute")
async def og_image(request: Request, qid: str = Depends(valid_qid)):
    """Generate (or serve cached) OG card PNG for a person or org QID."""
    cache_dir = Path(OG_CACHE_DIR)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{qid}.png"

    if cache_path.is_file():
        return Response(
            content=cache_path.read_bytes(),
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=604800"},
        )

    client = get_arango_client()
    db = get_db(client)

    import asyncio

    person = db.collection(PERSONS).get(qid)
    if person:
        png_bytes = await asyncio.to_thread(_generate_og_png, person)
    else:
        org = db.collection(ORGANIZATIONS).get(qid)
        if not org:
            raise HTTPException(404, f"Entity {qid} not found")
        png_bytes = await asyncio.to_thread(_generate_org_og_png, org)

    try:
        cache_path.write_bytes(png_bytes)
    except Exception as e:
        logger.warning("Could not write OG cache for %s: %s", qid, e)

    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=604800"},
    )
