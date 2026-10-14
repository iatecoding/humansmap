"""Share preview endpoint — returns OG-enriched HTML for social media crawlers,
then immediately redirects humans to the SPA."""

import json
import re
from html import escape
from typing import Optional
from fastapi import APIRouter, Query as Q
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["share"])

_SITE_URL = "https://humansmap.com"
_OG_IMAGE = f"{_SITE_URL}/og-image-og.png"
_FEED_CARD_ID_RE = re.compile(r"^[a-f0-9]{6,16}$")


def _build_card_og(card_id: str) -> dict | None:
    """Look up a feed card and return a dict with title, desc, og_image, and spa_url.
    Returns None if the card is not found."""
    try:
        from app.services.cache import get_redis
        raw = get_redis().hget("feed:cardmap:v2", card_id)
        if not raw:
            return None
        card = json.loads(raw)
    except Exception:
        return None

    og_image = f"{_SITE_URL}/og/feed/{card_id}.png"
    # Carry the card id so the SPA can open this specific card first
    # (card_id is already validated against _FEED_CARD_ID_RE by the caller).
    spa_url = f"{_SITE_URL}/?feed&card={card_id}"
    card_type = card.get("type", "")

    if card_type == "dynasty":
        title = escape(card.get("title", "humansmap.com")) + " | humansmap.com"
        desc = escape(card.get("subtitle", "Explore family connections"))
    elif card_type == "relationship":
        a = card.get("a", {})
        b = card.get("b", {})
        name_a = escape(a.get("name", ""))
        name_b = escape(b.get("name", ""))
        relation = escape(card.get("relation", "").replace("_", " "))
        domains = card.get("domains") or []
        title = f"{name_a} ↔ {name_b} | humansmap.com"
        domain_str = " ↔ ".join(escape(d) for d in domains[:2])
        desc = f"{relation.capitalize()}"
        if domain_str:
            desc += f" · {domain_str}"
    elif card_type == "organization":
        org = card.get("org", {})
        org_name = escape(org.get("name", ""))
        headline = escape(card.get("headline", ""))
        title = f"{headline}: {org_name} | humansmap.com" if headline else f"{org_name} | humansmap.com"
        people = card.get("people", [])
        people_names = ", ".join(escape(p.get("name", "")) for p in people[:4])
        desc = people_names if people_names else escape(headline)
    else:
        return None

    return {"title": title, "desc": desc, "og_image": og_image, "spa_url": spa_url}


@router.get("/share", response_class=HTMLResponse, include_in_schema=False)
async def share_preview(
    card: Optional[str] = Q(None, description="feed card id (12-char hex)"),
    s: Optional[int] = Q(None, description="score (correct answers)"),
    t: Optional[int] = Q(None, description="total questions"),
    nationality: Optional[str] = Q(None),
    category: Optional[str] = Q(None),
    difficulty: Optional[str] = Q(None),
    lang: Optional[str] = Q(None, description="ui language: it or en"),
):
    """Return a minimal HTML page with Open Graph meta tags so social media
    crawlers show a meaningful preview. Humans are redirected to the SPA."""

    # --- Feed card branch (takes priority) ---
    if card and _FEED_CARD_ID_RE.match(card):
        card_og = _build_card_og(card)
        if card_og:
            title = card_og["title"]
            desc = card_og["desc"]
            og_image_url = card_og["og_image"]
            spa_url = card_og["spa_url"]
        else:
            # Card not found — generic feed fallback
            title = "humansmap.com"
            desc = "Explore connections between public figures."
            og_image_url = _OG_IMAGE
            spa_url = f"{_SITE_URL}/?feed"

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{title}</title>

  <!-- Open Graph -->
  <meta property="og:type"        content="website">
  <meta property="og:site_name"   content="humansmap.com">
  <meta property="og:url"         content="{spa_url}">
  <meta property="og:title"       content="{title}">
  <meta property="og:description" content="{desc}">
  <meta property="og:image"       content="{og_image_url}">
  <meta property="og:image:width" content="1200">
  <meta property="og:image:height" content="630">

  <!-- Twitter / X -->
  <meta name="twitter:card"        content="summary_large_image">
  <meta name="twitter:title"       content="{title}">
  <meta name="twitter:description" content="{desc}">
  <meta name="twitter:image"       content="{og_image_url}">

  <!-- Redirect humans to the SPA immediately -->
  <meta http-equiv="refresh" content="0; url={spa_url}">
  <script>window.location.replace("{spa_url}");</script>
</head>
<body>
  <p><a href="{spa_url}">humansmap.com →</a></p>
</body>
</html>"""
        return HTMLResponse(content=html, headers={"Cache-Control": "public, max-age=60"})

    # --- Trivia / game branch (original behaviour unchanged) ---

    # Sanitise all string inputs before they touch the HTML
    nat  = escape(nationality[:64])  if nationality  else None
    cat  = escape(category[:64])     if category     else None
    diff = escape(difficulty[:16])   if difficulty   else None
    use_en = (lang or "").lower().startswith("en")

    # Build the SPA redirect URL
    from urllib.parse import urlencode
    spa_qs: dict = {"play": "1"}
    if nat:  spa_qs["nationality"] = nat
    if cat:  spa_qs["category"]    = cat
    if diff: spa_qs["difficulty"]  = diff
    if lang: spa_qs["lang"]        = lang
    spa_url = f"{_SITE_URL}/?{urlencode(spa_qs)}"

    # Compose title and description
    if s is not None and t is not None:
        title = f"humansmap.com | {s}/{t}"
        if use_en:
            desc = f"I scored {s} out of {t}"
            if nat:
                desc += f" on {nat} public figures"
            desc += ". Can you beat me?"
        else:
            desc = f"Ho risposto correttamente a {s} domande su {t}"
            if nat:
                desc += f" su personaggi {nat}"
            desc += ". Riesci a battermi?"
    else:
        title = "humansmap.com"
        if use_en:
            desc = "Explore connections between public figures. How many questions can you answer?"
        else:
            desc = "Esplora le connessioni tra personaggi pubblici. Quante domande riesci a rispondere?"

    html = f"""<!DOCTYPE html>
<html lang="{escape(lang or 'it')}">
<head>
  <meta charset="utf-8">
  <title>{title}</title>

  <!-- Open Graph -->
  <meta property="og:type"        content="website">
  <meta property="og:site_name"   content="humansmap.com">
  <meta property="og:url"         content="{spa_url}">
  <meta property="og:title"       content="{title}">
  <meta property="og:description" content="{desc}">
  <meta property="og:image"       content="{_OG_IMAGE}">

  <!-- Twitter / X -->
  <meta name="twitter:card"        content="summary_large_image">
  <meta name="twitter:title"       content="{title}">
  <meta name="twitter:description" content="{desc}">
  <meta name="twitter:image"       content="{_OG_IMAGE}">

  <!-- Redirect humans to the SPA immediately -->
  <meta http-equiv="refresh" content="0; url={spa_url}">
  <script>window.location.replace("{spa_url}");</script>
</head>
<body>
  <p><a href="{spa_url}">Play →</a></p>
</body>
</html>"""

    return HTMLResponse(content=html, headers={
        "Cache-Control": "public, max-age=60",
    })
