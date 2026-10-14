"""Person page endpoint — returns a full SPA shell whose <body> contains the
person's real content as static HTML for SEO crawlers.  The Vue SPA mounts on
top and replaces the SEO content with the interactive graph.

URL shape: /person/{QID}-{slug}
  - QID is load-bearing; slug is cosmetic.
  - Requests with a missing or wrong slug receive a 301 to the canonical form.
  - Requests with the correct slug receive the full SPA shell with injected SEO
    content (no redirect).

Phase 2 changes vs Phase 1:
  - Loads the built index.html from /srv/frontend/index.html at startup (or on
    each request in dev mode) and mutates it with per-person SEO content.
  - Returns the mutated shell directly; removes the JS-redirect stub.
  - Caches the rendered HTML in Redis with a content-hash key (auto-invalidates
    when person data changes).

Phase 3 changes vs Phase 2:
  - Fetches top-25 person-to-person connections and renders them as real
    <a href="/person/{qid}-{slug}"> links inside a <nav> block.
  - Connection hash included in the Redis cache key so edge changes bust cache.

Phase 4 changes vs Phase 3:
  - Adds server-rendered organization affiliations sourced only from Wikidata,
    grouped into factual explanations with canonical organization links.
  - Organization relationship data participates in cache invalidation.
"""

import asyncio
import hashlib
import html
import json
import logging
import os
import re
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.database import AFFILIATIONS, GRAPH_NAME, ORGANIZATIONS, PERSONS, get_arango_client, get_db
from app.domain.organization_roles import (
    PUBLIC_ORGANIZATION_ROLE_PHRASES,
    PUBLIC_ORGANIZATION_ROLE_PRIORITY,
    PUBLIC_ORGANIZATION_ROLE_TYPES,
    SPECIFIC_PUBLIC_ORGANIZATION_ROLES,
    public_role_phrase,
    relationship_has_ended,
)
from app.services.cache import get_cached_text, set_cached_text
from app.release import cache_namespace
from app.rate_limit import limiter
from app.services.ssr_guard import overloaded_response, release as release_ssr_slot, try_acquire
from app.utils.seo import is_thin_stub
from app.utils.slug import slugify

logger = logging.getLogger(__name__)

router = APIRouter(tags=["person_page"])

_SITE_URL = "https://humansmap.com"
_FRONTEND_SHELL_PATH = "/srv/frontend/index.html"
_PERSON_PAGE_RENDER_VERSION = "6"  # Bump when server-rendered SEO markup changes

# Matches the full path segment after /person/ — e.g. "Q1339" or "Q1339-wrong-slug"
_QID_SLUG_RE = re.compile(r"^(Q\d+)(?:-(.*))?$")


def _fast_page_cache_key(qid_slug: str, shell_hash: str) -> str:
    """Return a cheap cache key for this exact canonical URL."""
    qid = qid_slug.split("-", 1)[0]
    slug_hash = hashlib.sha256(qid_slug.encode("utf-8")).hexdigest()[:16]
    return (
        f"person_page:{cache_namespace()}:{_PERSON_PAGE_RENDER_VERSION}:"
        f"{qid}:fast:{slug_hash}:{shell_hash}"
    )

# ---------------------------------------------------------------------------
# Shell loader — cached between requests and refreshed after frontend deploys
# ---------------------------------------------------------------------------

_shell_cache: str | None = None
_shell_hash: str | None = None  # SHA-256 of the shell, used in Redis cache key
_shell_signature: tuple[int, int] | None = None  # (mtime_ns, size)


def _load_shell() -> tuple[str, str]:
    """Return (shell_html, shell_hash).  Reads from disk; may be called at startup."""
    try:
        with open(_FRONTEND_SHELL_PATH, "r", encoding="utf-8") as fh:
            content = fh.read()
        h = hashlib.sha256(content.encode()).hexdigest()[:16]
        return content, h
    except FileNotFoundError:
        logger.warning("Frontend shell not found at %s — falling back to minimal HTML", _FRONTEND_SHELL_PATH)
        return "", ""


def _shell_file_signature() -> tuple[int, int] | None:
    try:
        stat = os.stat(_FRONTEND_SHELL_PATH)
        return stat.st_mtime_ns, stat.st_size
    except FileNotFoundError:
        return None


def get_shell() -> tuple[str, str]:
    """Return the current frontend shell, reloading it after a volume update."""
    global _shell_cache, _shell_hash, _shell_signature
    signature = _shell_file_signature()
    dev_mode = os.environ.get("APP_ENV", "").lower() in ("dev", "development", "local")
    if dev_mode or _shell_cache is None or signature != _shell_signature:
        _shell_cache, _shell_hash = _load_shell()
        _shell_signature = signature
    return _shell_cache, _shell_hash


# Pre-load at import time so the first request isn't cold
try:
    _shell_cache, _shell_hash = _load_shell()
    _shell_signature = _shell_file_signature()
except Exception:
    pass  # will retry on first request

# ---------------------------------------------------------------------------
# HTML building helpers
# ---------------------------------------------------------------------------

_FALLBACK_DESC = "Explore connections on humansmap.com"


def _meta_description(person: dict) -> str:
    """Build a ≤160-char meta description from the person's real data."""
    raw = person.get("description_en") or person.get("description_it") or ""
    if not raw:
        parts: list[str] = []
        occs = person.get("occupations") or []
        if occs:
            parts.append(", ".join(o.replace("_", " ").title() for o in occs[:3]))
        nats = person.get("nationalities") or []
        if nats:
            parts.append(", ".join(n.replace("_", " ").title() for n in nats[:2]))
        birth = (person.get("birth_date") or "")[:4]
        death = (person.get("death_date") or "")[:4]
        if birth:
            parts.append(f"{birth}–{death}" if death else f"b. {birth}")
        raw = " · ".join(parts) if parts else _FALLBACK_DESC
    if len(raw) > 160:
        raw = raw[:157] + "…"
    return html.escape(raw)

def _org_meta_description(org: dict) -> str:
    """Build a ≤160-char meta description for an organization."""
    raw = org.get("description_en") or org.get("description_it") or ""
    if not raw:
        parts: list[str] = []
        org_type = org.get("type") or ""
        if org_type:
            parts.append(org_type.replace("_", " ").title())
        founded = org.get("founded") or ""
        if founded:
            parts.append(f"Founded {str(founded)[:4]}")
        country = org.get("country") or ""
        if country:
            parts.append(country)
        raw = " · ".join(parts) if parts else _FALLBACK_DESC
    if len(raw) > 160:
        raw = raw[:157] + "…"
    return html.escape(raw)


def _build_org_head_fragment(org: dict, name_esc: str, desc_esc: str,
                             canonical_url: str, og_image_url: str) -> str:
    """Build OG/Twitter/title head tags for an organization page."""
    return f"""  <title>{name_esc} | humansmap.com</title>
  <meta name="description" content="{desc_esc}">
  <link rel="canonical" href="{html.escape(canonical_url)}">

  <!-- Open Graph -->
  <meta property="og:type"         content="website">
  <meta property="og:site_name"    content="humansmap.com">
  <meta property="og:url"          content="{html.escape(canonical_url)}">
  <meta property="og:title"        content="{name_esc}">
  <meta property="og:description"  content="{desc_esc}">
  <meta property="og:image"        content="{html.escape(og_image_url)}">
  <meta property="og:image:width"  content="1200">
  <meta property="og:image:height" content="630">

  <!-- Twitter / X -->
  <meta name="twitter:card"        content="summary_large_image">
  <meta name="twitter:title"       content="{name_esc}">
  <meta name="twitter:description" content="{desc_esc}">
  <meta name="twitter:image"       content="{html.escape(og_image_url)}">"""


def _build_jsonld(person: dict, name: str, canonical_url: str, qid: str, has_image: bool) -> str:
    """Build a JSON-LD Person schema dict and return it as a safe <script> string."""
    data: dict = {
        "@context": "https://schema.org",
        "@type": "Person",
        "name": html.unescape(name),
        "url": canonical_url,
        "sameAs": [f"https://www.wikidata.org/wiki/{qid}"],
    }

    desc = person.get("description_en") or person.get("description_it") or ""
    if desc:
        data["description"] = desc

    if has_image:
        image_data: dict = {
            "@type": "ImageObject",
            "contentUrl": f"{_SITE_URL}/api/images/{qid}.jpg",
        }
        if person.get("image_source_url"):
            image_data["acquireLicensePage"] = person["image_source_url"]
        if person.get("image_license_url"):
            image_data["license"] = person["image_license_url"]
        if person.get("image_credit") or person.get("image_author"):
            image_data["creditText"] = person.get("image_credit") or person.get("image_author")
        if person.get("image_author"):
            image_data["creator"] = {"@type": "Person", "name": person["image_author"]}
        data["image"] = image_data

    awards = person.get("awards_top5") or []
    if awards:
        data["award"] = [str(award) for award in awards]
    occupations = person.get("occupations") or []
    if occupations:
        data["knowsAbout"] = occupations
    if person.get("cause_of_death"):
        data["deathCause"] = person["cause_of_death"]

    birth_date = (person.get("birth_date") or "")[:10]
    if birth_date:
        data["birthDate"] = birth_date

    death_date = (person.get("death_date") or "")[:10]
    if death_date:
        data["deathDate"] = death_date

    birth_place = person.get("birth_place") or ""
    if birth_place:
        data["birthPlace"] = {"@type": "Place", "name": birth_place}

    death_place = person.get("death_place") or ""
    if death_place:
        data["deathPlace"] = {"@type": "Place", "name": death_place}

    # Escape </script> injection: replace </ with <\/
    json_str = json.dumps(data, ensure_ascii=False).replace("</", r"<\/")
    return f'<script type="application/ld+json">\n{json_str}\n</script>'


def _build_head_fragment(person: dict, name_esc: str, desc_esc: str, canonical_url: str,
                         og_image_url: str, qid: str, has_image: bool,
                         noindex: bool = False) -> str:
    """Build the SEO <head> additions (title, meta, OG, Twitter, JSON-LD).

    When *noindex* is True, emits <meta name="robots" content="noindex, follow">
    so search engines skip indexing the page but still follow outbound links.
    """
    jsonld = _build_jsonld(person, name_esc, canonical_url, qid, has_image)
    robots_tag = '  <meta name="robots" content="noindex, follow">\n' if noindex else ""

    return f"""{robots_tag}  <title>{name_esc} | humansmap.com</title>
  <meta name="description" content="{desc_esc}">
  <link rel="canonical" href="{html.escape(canonical_url)}">

  <!-- Open Graph -->
  <meta property="og:type"         content="profile">
  <meta property="og:site_name"    content="humansmap.com">
  <meta property="og:url"          content="{html.escape(canonical_url)}">
  <meta property="og:title"        content="{name_esc}">
  <meta property="og:description"  content="{desc_esc}">
  <meta property="og:image"        content="{html.escape(og_image_url)}">
  <meta property="og:image:width"  content="1200">
  <meta property="og:image:height" content="630">

  <!-- Twitter / X -->
  <meta name="twitter:card"        content="summary_large_image">
  <meta name="twitter:title"       content="{name_esc}">
  <meta name="twitter:description" content="{desc_esc}">
  <meta name="twitter:image"       content="{html.escape(og_image_url)}">

  {jsonld}"""


def _format_date_display(date_str: str) -> str:
    """Return a human-readable date string from an ISO date (best-effort)."""
    if not date_str:
        return ""
    # Trim to date portion
    d = date_str[:10]
    # Simple display: keep as-is if partial, otherwise reformat YYYY-MM-DD
    parts = d.split("-")
    if len(parts) == 3 and all(p for p in parts):
        # year-month-day — try to render as "Day Month Year"
        try:
            from datetime import date as _date
            dt = _date(int(parts[0]), int(parts[1]), int(parts[2]))
            return dt.strftime("%-d %B %Y")
        except Exception:
            return d
    return d


def _build_body_fragment(
    person: dict,
    name_esc: str,
    qid: str,
    has_image: bool,
    connections: list[dict] | None = None,
    organization_connections: list[dict] | None = None,
) -> str:
    """Build the <main id="seo-content"> block."""
    parts: list[str] = []
    parts.append('<main id="seo-content">')
    parts.append(f"  <h1>{name_esc}</h1>")

    # Photo and file-specific Commons attribution.
    if has_image:
        source_url = html.escape(person.get("image_source_url") or "https://commons.wikimedia.org")
        license_url = html.escape(person.get("image_license_url") or source_url)
        license_name = html.escape(person.get("image_license") or "license details")
        creator = html.escape(person.get("image_author") or person.get("image_credit") or "Wikimedia Commons contributor")
        parts.extend([
            "  <figure>",
            f"""    <img src="/api/images/{qid}.jpg" alt="{name_esc}" width="300" height="300" loading="lazy">""",
            f"""    <figcaption>Image by {creator} · <a href="{source_url}">source</a> · <a href="{license_url}">{license_name}</a> · resized and converted by Humans Map</figcaption>""",
            "  </figure>",
        ])

    # Descriptions
    desc_it = person.get("description_it") or ""
    desc_en = person.get("description_en") or ""
    if desc_it:
        parts.append(f'  <p lang="it">{html.escape(desc_it)}</p>')
    if desc_en:
        parts.append(f'  <p lang="en">{html.escape(desc_en)}</p>')

    # Metadata <dl>
    dl_items: list[str] = []

    birth_date = person.get("birth_date") or ""
    birth_place = person.get("birth_place") or ""
    if birth_date or birth_place:
        dd_parts = []
        if birth_date:
            dd_parts.append(html.escape(_format_date_display(birth_date)))
        if birth_place:
            dd_parts.append(html.escape(birth_place))
        dl_items.append(f"    <dt>Born</dt><dd>{', '.join(dd_parts)}</dd>")

    death_date = person.get("death_date") or ""
    death_place = person.get("death_place") or ""
    death_cause = person.get("cause_of_death") or ""
    if death_date or death_place or death_cause:
        dd_parts = []
        if death_date:
            dd_parts.append(html.escape(_format_date_display(death_date)))
        if death_place:
            dd_parts.append(html.escape(death_place))
        if death_cause:
            dd_parts.append(html.escape(death_cause))
        dl_items.append(f"    <dt>Died</dt><dd>{', '.join(dd_parts)}</dd>")

    occupations = person.get("occupations") or []
    if occupations:
        occ_str = html.escape(", ".join(o.replace("_", " ").title() for o in occupations))
        dl_items.append(f"    <dt>Occupation</dt><dd>{occ_str}</dd>")

    nationalities = person.get("nationalities") or []
    if nationalities:
        nat_str = html.escape(", ".join(n.replace("_", " ").title() for n in nationalities))
        dl_items.append(f"    <dt>Nationality</dt><dd>{nat_str}</dd>")

    if dl_items:
        parts.append("  <dl>")
        parts.extend(dl_items)
        parts.append("  </dl>")

    awards = person.get("awards_top5") or []
    if awards:
        parts.append("  <section><h2>Awards</h2><ul>")
        parts.extend(f"    <li>{html.escape(str(award))}</li>" for award in awards)
        remaining = max(0, int(person.get("awards_count") or len(awards)) - len(awards))
        if remaining:
            parts.append(f"    <li>And {remaining} more</li>")
        parts.append("  </ul></section>")

    notable_works = person.get("notable_works") or []
    if notable_works:
        parts.append("  <section><h2>Notable works</h2><ul>")
        parts.extend(f"    <li>{html.escape(str(work))}</li>" for work in notable_works)
        parts.append("  </ul></section>")

    # Organization affiliations (Phase 4) — factual, crawlable graph context.
    organization_section = _build_organization_connections_section(
        name_esc,
        organization_connections or [],
    )
    if organization_section:
        parts.append(organization_section)

    # Connections block (Phase 3) — omitted when empty
    connections_nav = _build_connections_nav(connections or [])
    if connections_nav:
        parts.append(connections_nav)

    parts.append("</main>")
    return "\n".join(parts)


def _render_page(shell: str, head_fragment: str, body_fragment: str) -> str:
    """Inject head metadata and a durable profile section into the SPA shell.

    Also strips the generic homepage <title>, <meta name="description">, and
    OG/Twitter <meta> tags from the shell so person-specific tags are the only
    ones present (prevents duplicate / conflicting meta for crawlers).

    The profile section deliberately follows ``#app``. Vue owns that element,
    while the profile remains visible below the graph for people and crawlers
    after JavaScript has rendered. It must not be inserted inside ``#app``:
    mounting Vue would replace it.
    """
    # Strip generic tags that the person-specific fragment replaces
    rendered = re.sub(r"[ \t]*<title>[^<]*</title>\n?", "", shell, count=1)
    rendered = re.sub(r'[ \t]*<meta\s+name="description"[^>]*>\n?', "", rendered, count=1)
    rendered = re.sub(r'[ \t]*<link\s+rel="canonical"[^>]*/>\n?', "", rendered, count=1)
    rendered = re.sub(r'[ \t]*<meta\s+property="og:[^"]*"[^>]*/>\n?', "", rendered)
    rendered = re.sub(r'[ \t]*<meta\s+name="twitter:[^"]*"[^>]*/>\n?', "", rendered)
    rendered = re.sub(
        r'[ \t]*<script\s+type="application/ld\+json">\s*\{[^}]*"@type"\s*:\s*"WebSite"[^<]*</script>\n?',
        "", rendered, count=1,
    )

    # Entity pages must not inherit homepage fallback copy or its second H1.
    rendered = re.sub(r"\s*<noscript>.*?</noscript>", "", rendered, flags=re.DOTALL)

    # Inject person-specific head content before </head>
    rendered = rendered.replace("</head>", f"{head_fragment}\n</head>", 1)

    # Keep semantic content outside Vue's mount target, after the full-screen
    # app. This makes it part of the rendered page rather than transient SEO
    # source markup that disappears as soon as JavaScript runs.
    rendered = rendered.replace("</body>", f"\n{body_fragment}\n</body>", 1)
    return rendered


def _is_thin_stub(person: dict, connections_count: int) -> bool:
    """Thin-stub predicate for this router — delegates to the shared SEO helper.

    # SEO thin-stub threshold: no photo, no IT/EN description, <2 connections.
    # Tune based on Search Console indexation data.
    """
    return is_thin_stub(person, connections_count)


def _content_hash(person: dict) -> str:
    """16-char hash of the fields we render, for cache key auto-invalidation."""
    fields = {
        k: person.get(k)
        for k in (
            "name_en", "name_it", "description_en", "description_it",
            "birth_date", "birth_place", "death_date", "death_place",
            "cause_of_death", "occupations", "nationalities", "awards_count",
            "awards_top5", "notable_works", "image_cached_path", "image_license",
            "image_license_url", "image_author", "image_credit", "image_source_url",
            "image_modified",
        )
    }
    return hashlib.sha256(json.dumps(fields, sort_keys=True, default=str).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Connections query (Phase 3)
# ---------------------------------------------------------------------------

_CONNECTIONS_AQL = f"""
  LET raw = (
    FOR v, e IN 1..1 ANY @source GRAPH '{GRAPH_NAME}'
      FILTER IS_SAME_COLLECTION('{PERSONS}', v)
      FILTER v._key != @source_qid
      RETURN {{v: v, e: e}}
  )
  FOR item IN raw
    COLLECT target_id = item.v._key INTO grp = item
    LET best = FIRST(
      FOR x IN grp SORT x.v.wikidata_statements_count DESC RETURN x
    )
    SORT best.v.wikidata_statements_count DESC
    LIMIT 25
    RETURN {{
      qid: target_id,
      name_en: best.v.name_en,
      name_it: best.v.name_it,
      statements: best.v.wikidata_statements_count,
      edge_type: best.e.type
    }}
"""


def _fetch_connections(db, qid: str) -> list[dict]:
    """Return up to 25 top person-to-person connections for *qid*, ranked by
    the connected person's wikidata_statements_count (notability proxy).

    Each entry: {qid, name_en, name_it, statements, edge_type}.
    Returns [] if the person has no person-to-person connections.
    """
    try:
        cursor = db.aql.execute(
            _CONNECTIONS_AQL,
            bind_vars={"source": f"{PERSONS}/{qid}", "source_qid": qid},
        )
        return list(cursor)
    except Exception:
        logger.warning("Failed to fetch connections for %s", qid, exc_info=True)
        return []


def _connections_hash(connections: list[dict]) -> str:
    """16-char hash of the connections list for cache-key inclusion."""
    # Include qid + edge_type only — enough to detect topology/label changes.
    slim = [{"qid": c.get("qid"), "edge_type": c.get("edge_type")} for c in connections]
    return hashlib.sha256(
        json.dumps(slim, sort_keys=True).encode()
    ).hexdigest()[:16]


def _build_connections_nav(connections: list[dict]) -> str:
    """Return a <nav> block of internal links, or '' if connections is empty."""
    if not connections:
        return ""

    items: list[str] = []
    for c in connections:
        target_qid = c.get("qid") or ""
        raw_name = c.get("name_en") or c.get("name_it") or target_qid
        target_slug = slugify(raw_name)
        href = f"/person/{target_qid}-{target_slug}" if target_slug else f"/person/{target_qid}"
        name_esc = html.escape(raw_name)

        edge_type = c.get("edge_type") or ""
        if edge_type:
            label_display = html.escape(edge_type.replace("_", " ").title())
            items.append(
                f'      <li><a href="{html.escape(href)}" rel="related">{name_esc}</a>'
                f" \u2014 {label_display}</li>"
            )
        else:
            items.append(
                f'      <li><a href="{html.escape(href)}" rel="related">{name_esc}</a></li>'
            )

    joined = "\n".join(items)
    return (
        '  <nav aria-label="Connected people">\n'
        "    <h2>Connections</h2>\n"
        "    <ul>\n"
        f"{joined}\n"
        "    </ul>\n"
        "  </nav>"
    )


# ---------------------------------------------------------------------------
# Organization relationships (Phase 4)
# ---------------------------------------------------------------------------

# The allowlist deliberately excludes SEC-derived relationship types such as
# insider_of, officer_of, beneficial_owner_of, director_of, and holds_shares_of.
_ORGANIZATION_EDGE_TYPES = PUBLIC_ORGANIZATION_ROLE_TYPES
_ORGANIZATION_ROLE_PRIORITY = PUBLIC_ORGANIZATION_ROLE_PRIORITY
_ORGANIZATION_ROLE_LABELS = PUBLIC_ORGANIZATION_ROLE_PHRASES
_SPECIFIC_PUBLIC_ROLES = SPECIFIC_PUBLIC_ORGANIZATION_ROLES

_ORGANIZATION_CONNECTIONS_AQL = f"""
  FOR e IN {AFFILIATIONS}
    FILTER e._from == @source
    FILTER STARTS_WITH(TO_STRING(e.source), "wikidata")
    FILTER e.type IN @edge_types
    LET organization = DOCUMENT(e._to)
    FILTER organization != null
    FILTER IS_SAME_COLLECTION('{ORGANIZATIONS}', organization)
    FILTER organization.type != "position"
    SORT organization.wikidata_statements_count DESC
    RETURN {{
      qid: organization._key,
      name_en: organization.name_en,
      name_it: organization.name_it,
      statements: organization.wikidata_statements_count,
      edge_type: e.type,
      start_date: e.start_date,
      end_date: e.end_date
    }}
"""


def _relationship_year(value: object) -> str:
    """Return a trustworthy four-digit year from full or partial graph dates."""
    match = re.match(r"^(\d{4})", str(value or ""))
    return match.group(1) if match and match.group(1) != "0000" else ""


def _relationship_period(relationship: dict) -> str:
    """Render graph dates without inventing month or day precision."""
    start_year = _relationship_year(relationship.get("start_date"))
    end_year = _relationship_year(relationship.get("end_date"))
    if start_year and end_year:
        return start_year if start_year == end_year else f"{start_year}–{end_year}"
    if start_year:
        return f"from {start_year}"
    if end_year:
        return f"until {end_year}"
    return ""


def _relationship_date_score(relationship: dict) -> tuple[int, int, int]:
    """Prefer active appointments, then the most useful date evidence."""
    has_start = bool(_relationship_year(relationship.get("start_date")))
    has_end = bool(_relationship_year(relationship.get("end_date")))
    is_active = not relationship_has_ended(relationship.get("end_date"))
    return (int(is_active), int(has_start) + int(has_end), int(has_start))


def _group_organization_connections(rows: list[dict], limit: int = 15) -> list[dict]:
    """Group raw affiliation edges into one deterministic item per organization."""
    grouped: dict[str, dict] = {}
    for row in rows:
        qid = str(row.get("qid") or "")
        edge_type = str(row.get("edge_type") or "")
        if not re.fullmatch(r"Q\d+", qid) or edge_type not in _ORGANIZATION_EDGE_TYPES:
            continue

        item = grouped.setdefault(
            qid,
            {
                "qid": qid,
                "name_en": row.get("name_en"),
                "name_it": row.get("name_it"),
                "statements": int(row.get("statements") or 0),
                "relationships_by_type": {},
            },
        )
        item["statements"] = max(item["statements"], int(row.get("statements") or 0))
        if not item.get("name_en") and row.get("name_en"):
            item["name_en"] = row.get("name_en")
        if not item.get("name_it") and row.get("name_it"):
            item["name_it"] = row.get("name_it")

        relationship = {
            "edge_type": edge_type,
            "start_date": row.get("start_date"),
            "end_date": row.get("end_date"),
        }
        existing = item["relationships_by_type"].get(edge_type)
        if existing is None or _relationship_date_score(relationship) > _relationship_date_score(existing):
            item["relationships_by_type"][edge_type] = relationship

    results: list[dict] = []
    for item in grouped.values():
        relationships_by_type = item.pop("relationships_by_type")
        if _SPECIFIC_PUBLIC_ROLES.intersection(relationships_by_type):
            relationships_by_type.pop("employee", None)
        item["relationships"] = sorted(
            relationships_by_type.values(),
            key=lambda relationship: _ORGANIZATION_ROLE_PRIORITY[relationship["edge_type"]],
        )
        item["role_priority"] = min(
            _ORGANIZATION_ROLE_PRIORITY[r["edge_type"]] for r in item["relationships"]
        )
        results.append(item)

    results.sort(
        key=lambda item: (
            item["role_priority"],
            -item["statements"],
            (item.get("name_en") or item.get("name_it") or item["qid"]).casefold(),
        )
    )
    for item in results:
        item.pop("role_priority", None)
    return results[:limit]


def _fetch_organization_connections(db, qid: str) -> list[dict]:
    """Fetch and group useful Wikidata person-to-organization relationships."""
    try:
        cursor = db.aql.execute(
            _ORGANIZATION_CONNECTIONS_AQL,
            bind_vars={
                "source": f"{PERSONS}/{qid}",
                "edge_types": list(_ORGANIZATION_EDGE_TYPES),
            },
        )
        return _group_organization_connections(list(cursor))
    except Exception:
        logger.warning("Failed to fetch organization connections for %s", qid, exc_info=True)
        return []


def _organization_connections_hash(organization_connections: list[dict]) -> str:
    """Hash rendered organization evidence for automatic cache invalidation."""
    return hashlib.sha256(
        json.dumps(organization_connections, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


def _join_phrases(phrases: list[str]) -> str:
    if len(phrases) < 2:
        return phrases[0] if phrases else ""
    if len(phrases) == 2:
        return f"{phrases[0]} and {phrases[1]}"
    return f"{', '.join(phrases[:-1])} and {phrases[-1]}"


def _build_organization_connections_section(
    person_name_esc: str,
    organization_connections: list[dict],
) -> str:
    """Build readable evidence-backed organization explanations and links."""
    if not organization_connections:
        return ""

    items: list[str] = []
    for organization in organization_connections:
        qid = str(organization.get("qid") or "")
        if not re.fullmatch(r"Q\d+", qid):
            continue
        raw_name = organization.get("name_en") or organization.get("name_it") or qid
        org_slug = slugify(raw_name)
        href = f"/organization/{qid}-{org_slug}" if org_slug else f"/organization/{qid}"
        link = f'<a href="{html.escape(href)}" rel="related">{html.escape(raw_name)}</a>'

        relationships = organization.get("relationships") or []
        education = [r for r in relationships if r.get("edge_type") == "educated_at"]
        roles = [r for r in relationships if r.get("edge_type") != "educated_at"]
        sentences: list[str] = []

        if roles:
            role_phrases: list[str] = []
            for relationship in roles:
                label = public_role_phrase(
                    relationship.get("edge_type", ""),
                    relationship.get("end_date"),
                )
                if not label:
                    continue
                period = _relationship_period(relationship)
                role_phrases.append(f"{label} ({period})" if period else label)
            if role_phrases:
                sentences.append(
                    f"The graph records {person_name_esc} as {_join_phrases(role_phrases)} of {link}."
                )

        if education:
            period = _relationship_period(education[0])
            period_text = f" ({period})" if period else ""
            sentences.append(
                f"The graph connects {person_name_esc} to {link} through education{period_text}."
            )

        if sentences:
            items.append(f"      <li>{' '.join(sentences)}</li>")

    if not items:
        return ""
    joined_items = "\n".join(items)
    return (
        '  <section aria-labelledby="organization-connections">\n'
        '    <h2 id="organization-connections">Organizations and public roles</h2>\n'
        "    <p>These connections are inferred from roles and affiliations recorded in the graph.</p>\n"
        "    <ul>\n"
        f"{joined_items}\n"
        "    </ul>\n"
        "  </section>"
    )


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------

def _render_person_page(qid_slug: str):
    """Canonical person page.  301s bad/missing slugs; 200 with full SSR content."""
    # Occupation category virtual pages (occ_architect etc.) — serve the SPA shell
    # so the Vue router handles them client-side.
    if qid_slug.startswith("occ_"):
        occ = qid_slug[4:]
        if not occ or len(occ) > 80 or not re.fullmatch(r"[a-z_]+", occ):
            return HTMLResponse(status_code=404, content="Not found")
        shell, _ = get_shell()
        if shell:
            return HTMLResponse(content=shell)
        return HTMLResponse(status_code=404, content="Not found")

    m = _QID_SLUG_RE.match(qid_slug)
    if not m:
        return HTMLResponse(status_code=404, content="Not found")

    qid = m.group(1)
    request_slug = m.group(2) or ""

    client = get_arango_client()
    db = get_db(client)
    col = db.collection(PERSONS)
    person = col.get(qid)

    if not person:
        org = db.collection(ORGANIZATIONS).get(qid)
        if not org:
            return HTMLResponse(status_code=404, content=f"Person {qid} not found")

        # Legacy person-shaped organization URLs redirect to the dedicated
        # organization URL, which has a server-rendered organization body.
        raw_org_name = org.get("name_en") or org.get("name_it") or ""
        org_slug = slugify(raw_org_name)
        org_canonical_path = f"/organization/{qid}-{org_slug}" if org_slug else f"/organization/{qid}"
        return RedirectResponse(
            url=org_canonical_path,
            status_code=301,
            headers={"Cache-Control": "public, max-age=3600"},
        )

    # Compute canonical slug
    raw_name = person.get("name_en") or person.get("name_it") or ""
    canonical_slug = slugify(raw_name)
    canonical_path = f"/person/{qid}-{canonical_slug}" if canonical_slug else f"/person/{qid}"

    # 301 if slug doesn't match
    if request_slug != canonical_slug:
        return RedirectResponse(
            url=canonical_path,
            status_code=301,
            headers={"Cache-Control": "public, max-age=3600"},
        )

    # ── Slug matches — serve the full SSR page ───────────────────────────────

    # Fetch connections first so they contribute to the cache key
    connections = _fetch_connections(db, qid)
    conn_hash = _connections_hash(connections)
    organization_connections = _fetch_organization_connections(db, qid)
    organization_conn_hash = _organization_connections_hash(organization_connections)

    # Check Redis cache first
    content_hash = _content_hash(person)
    shell_html, shell_hash = get_shell()
    cache_key = (
        f"person_page:{cache_namespace()}:{_PERSON_PAGE_RENDER_VERSION}:"
        f"{qid}:{content_hash}:"
        f"{conn_hash}:{organization_conn_hash}:{shell_hash}"
    )
    fast_cache_key = _fast_page_cache_key(qid_slug, shell_hash)

    # Build response headers (shared between cached and fresh renders)
    noindex = _is_thin_stub(person, len(connections))
    resp_headers: dict[str, str] = {
        # Browsers revalidate, while the CDN can serve the expensive SSR page
        # for an hour and refresh it in the background.
        "Cache-Control": "public, max-age=0, s-maxage=3600, stale-while-revalidate=60",
    }
    last_updated = person.get("last_updated")
    if last_updated:
        try:
            if isinstance(last_updated, datetime):
                resp_headers["Last-Modified"] = last_updated.strftime("%a, %d %b %Y %H:%M:%S GMT")
            else:
                dt = datetime.fromisoformat(str(last_updated))
                resp_headers["Last-Modified"] = dt.strftime("%a, %d %b %Y %H:%M:%S GMT")
        except (ValueError, TypeError):
            pass
    if noindex:
        resp_headers["X-Robots-Tag"] = "noindex, follow"
    else:
        resp_headers["X-Robots-Tag"] = "max-image-preview:large, max-snippet:-1, max-video-preview:-1"

    cached = get_cached_text(cache_key)
    if cached:
        set_cached_text(fast_cache_key, cached, ttl_seconds=86400)
        return HTMLResponse(content=cached, headers=resp_headers)

    # Build page
    name_esc = html.escape(raw_name or qid)
    desc_esc = _meta_description(person)
    has_image = bool(person.get("image_cached_path"))
    canonical_url = f"{_SITE_URL}{canonical_path}"
    og_image_url = f"{_SITE_URL}/og/{qid}.png"

    head_fragment = _build_head_fragment(
        person, name_esc, desc_esc, canonical_url, og_image_url, qid, has_image,
        noindex=noindex,
    )
    body_fragment = _build_body_fragment(
        person,
        name_esc,
        qid,
        has_image,
        connections,
        organization_connections,
    )

    if shell_html:
        rendered = _render_page(shell_html, head_fragment, body_fragment)
    else:
        rendered = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
{head_fragment}
</head>
<body>
{body_fragment}
<div id="app"></div>
</body>
</html>"""

    # Cache in Redis (24h TTL), compressed by the shared cache helper.
    set_cached_text(cache_key, rendered, ttl_seconds=86400)
    set_cached_text(fast_cache_key, rendered, ttl_seconds=86400)

    return HTMLResponse(content=rendered, headers=resp_headers)


@router.api_route("/person/{qid_slug:path}", methods=["GET", "HEAD"], response_class=HTMLResponse, include_in_schema=False)
@limiter.limit("30/minute")
async def person_page(request: Request, qid_slug: str):
    """Serve an SSR page without letting crawler bursts block the event loop."""
    match = _QID_SLUG_RE.match(qid_slug)
    if not match:
        return await asyncio.to_thread(_render_person_page, qid_slug)

    qid = match.group(1)
    _, shell_hash = await asyncio.to_thread(get_shell)
    fast_cache_key = _fast_page_cache_key(qid_slug, shell_hash)
    cached = await asyncio.to_thread(get_cached_text, fast_cache_key)
    if cached:
        return HTMLResponse(
            content=cached,
            headers={
                "Cache-Control": "public, max-age=0, s-maxage=3600, stale-while-revalidate=60",
                "X-SSR-Cache": "hit",
            },
        )

    if not await try_acquire():
        return overloaded_response()
    try:
        return await asyncio.to_thread(_render_person_page, qid_slug)
    finally:
        await release_ssr_slot()
