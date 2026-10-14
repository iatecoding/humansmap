"""Server-rendered organization pages for search and link previews.

The interactive Vue graph still takes over for visitors, but this route gives
search engines a stable organization URL with useful ownership and people
relationships in ordinary HTML.
"""

import asyncio
import hashlib
import html
import json
import logging
import re

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.database import AFFILIATIONS, ORGANIZATIONS, ORG_STRUCTURE, PERSONS, get_arango_client, get_db
from app.routers.person_page import _render_page, get_shell
from app.services.cache import get_cached_text, set_cached_text
from app.release import cache_namespace
from app.rate_limit import limiter
from app.services.ssr_guard import overloaded_response, release as release_ssr_slot, try_acquire
from app.utils.seo import is_thin_organization
from app.utils.slug import slugify

logger = logging.getLogger(__name__)

router = APIRouter(tags=["organization_page"])

_SITE_URL = "https://humansmap.com"
_RENDER_VERSION = "3"
_QID_SLUG_RE = re.compile(r"^(Q\d+)(?:-(.*))?$")


def _fast_page_cache_key(qid_slug: str, shell_hash: str) -> str:
    """Return a cheap cache key for this exact canonical URL."""
    qid = qid_slug.split("-", 1)[0]
    slug_hash = hashlib.sha256(qid_slug.encode("utf-8")).hexdigest()[:16]
    return (
        f"organization_page:{cache_namespace()}:{_RENDER_VERSION}:"
        f"{qid}:fast:{slug_hash}:{shell_hash}"
    )


def _display_name(doc: dict) -> str:
    return doc.get("name_en") or doc.get("name_it") or doc.get("_key") or "Organization"


def _organization_path(qid: str, name: str) -> str:
    slug = slugify(name)
    return f"/organization/{qid}-{slug}" if slug else f"/organization/{qid}"


def _person_path(qid: str, name: str) -> str:
    slug = slugify(name)
    return f"/person/{qid}-{slug}" if slug else f"/person/{qid}"


def _format_number(value: object) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def _meta_description(org: dict, people_count: int, structure_count: int) -> str:
    name = _display_name(org)
    facts: list[str] = []
    if people_count:
        facts.append(f"{people_count} associated people")
    if structure_count:
        facts.append(f"{structure_count} organization relationships")
    country = org.get("country")
    if country:
        facts.append(str(country))
    suffix = f" — {', '.join(facts)}." if facts else "."
    raw = f"Explore {name}'s ownership, people, and organization network on Humans Map{suffix}"
    return html.escape(raw[:157] + "…" if len(raw) > 160 else raw)


def _relationship_label(edge_type: str, outgoing: bool) -> str:
    labels = {
        ("parent_of", True): "Parent organization",
        ("parent_of", False): "Subsidiary",
        ("subsidiary_of", True): "Subsidiary",
        ("subsidiary_of", False): "Parent organization",
        ("owned_by", True): "Owner",
        ("owned_by", False): "Owned organization",
        ("owns", True): "Owned organization",
        ("owns", False): "Owner",
        ("beneficial_owner_of", True): "Beneficial ownership",
        ("beneficial_owner_of", False): "Beneficial owner",
        ("holds_shares_of", True): "Shareholding",
        ("holds_shares_of", False): "Shareholder",
    }
    return labels.get((edge_type, outgoing), edge_type.replace("_", " ").title())


def _fetch_relationships(db, qid: str) -> tuple[list[dict], list[dict], int, int]:
    """Fetch direct organization links and associated people for an org."""
    org_id = f"{ORGANIZATIONS}/{qid}"
    structure_aql = f"""
        FOR e IN {ORG_STRUCTURE}
          FILTER e._from == @org_id OR e._to == @org_id
          LET outgoing = e._from == @org_id
          LET other_id = outgoing ? e._to : e._from
          LET org = DOCUMENT(other_id)
          FILTER org != null AND IS_SAME_COLLECTION('{ORGANIZATIONS}', other_id)
          SORT org.wikidata_statements_count DESC, org.name_en ASC
          LIMIT 30
          RETURN {{
            qid: org._key,
            name_en: org.name_en,
            name_it: org.name_it,
            edge_type: e.type,
            outgoing: outgoing
          }}
    """
    people_aql = f"""
        FOR e IN {AFFILIATIONS}
          FILTER e._to == @org_id
          COLLECT person_id = e._from INTO grouped = e
          LET person = DOCUMENT(person_id)
          FILTER person != null AND IS_SAME_COLLECTION('{PERSONS}', person_id)
          LET best_edge = FIRST(
            FOR edge IN grouped
              SORT edge.type IN ['founder', 'chief_executive', 'chairperson', 'owner', 'board_member'] DESC
              RETURN edge
          )
          SORT person.wikidata_statements_count DESC, person.name_en ASC
          LIMIT 20
          RETURN {{
            qid: person._key,
            name_en: person.name_en,
            name_it: person.name_it,
            edge_type: best_edge.type,
            role: best_edge.role
          }}
    """
    counts_aql = f"""
        LET people_count = LENGTH(
          FOR e IN {AFFILIATIONS}
            FILTER e._to == @org_id
            COLLECT person_id = e._from
            RETURN person_id
        )
        LET structure_count = LENGTH(
          FOR e IN {ORG_STRUCTURE}
            FILTER e._from == @org_id OR e._to == @org_id
            RETURN 1
        )
        RETURN {{people_count: people_count, structure_count: structure_count}}
    """
    try:
        structure = list(db.aql.execute(structure_aql, bind_vars={"org_id": org_id}))
        people = list(db.aql.execute(people_aql, bind_vars={"org_id": org_id}))
        counts = next(iter(db.aql.execute(counts_aql, bind_vars={"org_id": org_id})), {})
        return structure, people, counts.get("people_count", 0), counts.get("structure_count", 0)
    except Exception:
        logger.warning("Failed to fetch organization SEO relationships for %s", qid, exc_info=True)
        return [], [], 0, 0


def _build_jsonld(org: dict, canonical_url: str, structure: list[dict]) -> str:
    name = _display_name(org)
    data: dict = {
        "@context": "https://schema.org",
        "@type": "Organization",
        "name": name,
        "url": canonical_url,
        "sameAs": [f"https://www.wikidata.org/wiki/{org['_key']}"],
    }
    desc = org.get("description_en") or org.get("description_it")
    if desc:
        data["description"] = desc
    if org.get("founded"):
        data["foundingDate"] = str(org["founded"])[:10]
    if org.get("dissolved"):
        data["dissolutionDate"] = str(org["dissolved"])[:10]
    if org.get("employees_count"):
        data["numberOfEmployees"] = org["employees_count"]
    if org.get("website"):
        data["sameAs"].append(org["website"])
    if org.get("headquarters"):
        data["location"] = {"@type": "Place", "name": org["headquarters"]}
    if org.get("image_cached_path"):
        image_data: dict = {
            "@type": "ImageObject",
            "contentUrl": f"{_SITE_URL}/api/images/{org['_key']}.jpg",
        }
        if org.get("image_source_url"):
            image_data["acquireLicensePage"] = org["image_source_url"]
        if org.get("image_license_url"):
            image_data["license"] = org["image_license_url"]
        if org.get("image_credit") or org.get("image_author"):
            image_data["creditText"] = org.get("image_credit") or org.get("image_author")
        if org.get("image_author"):
            image_data["creator"] = {"@type": "Person", "name": org["image_author"]}
        data["logo"] = image_data

    parents = []
    subsidiaries = []
    for item in structure:
        label = _relationship_label(item.get("edge_type", ""), bool(item.get("outgoing")))
        entry = {
            "@type": "Organization",
            "name": item.get("name_en") or item.get("name_it") or item.get("qid"),
            "url": f"{_SITE_URL}{_organization_path(item['qid'], item.get('name_en') or item.get('name_it') or item['qid'])}",
        }
        if label == "Parent organization":
            parents.append(entry)
        elif label in {"Subsidiary", "Owned organization"}:
            subsidiaries.append(entry)
    if parents:
        data["parentOrganization"] = parents[0]
    if subsidiaries:
        data["subOrganization"] = subsidiaries

    json_str = json.dumps(data, ensure_ascii=False).replace("</", r"<\/")
    return f'<script type="application/ld+json">\n{json_str}\n</script>'


def _build_head(
    org: dict, canonical_url: str, description: str, structure: list[dict],
    *, noindex: bool = False,
) -> str:
    name = html.escape(_display_name(org))
    qid = org["_key"]
    og_image_url = f"{_SITE_URL}/og/{qid}.png"
    jsonld = _build_jsonld(org, canonical_url, structure)
    robots_tag = '  <meta name="robots" content="noindex, follow">\n' if noindex else ""
    return f"""{robots_tag}  <title>{name}: ownership, people and organization network | Humans Map</title>
  <meta name="description" content="{description}">
  <link rel="canonical" href="{html.escape(canonical_url)}">

  <meta property="og:type" content="website">
  <meta property="og:site_name" content="humansmap.com">
  <meta property="og:url" content="{html.escape(canonical_url)}">
  <meta property="og:title" content="{name}">
  <meta property="og:description" content="{description}">
  <meta property="og:image" content="{html.escape(og_image_url)}">
  <meta property="og:image:width" content="1200">
  <meta property="og:image:height" content="630">
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="{name}">
  <meta name="twitter:description" content="{description}">
  <meta name="twitter:image" content="{html.escape(og_image_url)}">
  {jsonld}"""


def _build_body(org: dict, structure: list[dict], people: list[dict], people_count: int, structure_count: int) -> str:
    name = _display_name(org)
    name_esc = html.escape(name)
    parts = ["<main id=\"seo-content\">", f"  <h1>{name_esc}</h1>"]

    if org.get("image_cached_path"):
        source_url = html.escape(org.get("image_source_url") or "https://commons.wikimedia.org", quote=True)
        license_url = html.escape(org.get("image_license_url") or source_url, quote=True)
        license_name = html.escape(org.get("image_license") or "license details")
        creator = html.escape(org.get("image_author") or org.get("image_credit") or "Wikimedia Commons contributor")
        parts.extend([
            "  <figure>",
            f'    <img src="/api/images/{org["_key"]}.jpg" alt="{name_esc}" width="300" height="300" loading="lazy">',
            f'    <figcaption>Image by {creator} · <a href="{source_url}">source</a> · <a href="{license_url}">{license_name}</a> · resized and converted by Humans Map</figcaption>',
            "  </figure>",
        ])

    summary_bits = []
    if people_count:
        summary_bits.append(f"{people_count} associated people")
    if structure_count:
        summary_bits.append(f"{structure_count} organization relationships")
    if summary_bits:
        parts.append(f"  <p>Explore {name_esc}'s network on Humans Map: {html.escape(', '.join(summary_bits))}.</p>")

    description_it = org.get("description_it") or ""
    description_en = org.get("description_en") or ""
    if description_it:
        parts.append(f'  <p lang="it" data-nosnippet>{html.escape(description_it)}</p>')
    if description_en:
        parts.append(f'  <p lang="en" data-nosnippet>{html.escape(description_en)}</p>')

    metadata = []
    fields = [
        ("Type", org.get("type", "").replace("_", " ").title()),
        ("Founded", str(org.get("founded") or "")[:10]),
        ("Dissolved", str(org.get("dissolved") or "")[:10]),
        ("Country", org.get("country") or ""),
        ("Headquarters", org.get("headquarters") or ""),
        ("Employees", _format_number(org["employees_count"]) if org.get("employees_count") else ""),
    ]
    for label, value in fields:
        if value:
            metadata.append(f"    <dt>{label}</dt><dd>{html.escape(value)}</dd>")
    if org.get("website"):
        href = html.escape(str(org["website"]), quote=True)
        metadata.append(f'    <dt>Website</dt><dd><a href="{href}" rel="noopener">{href}</a></dd>')
    if metadata:
        parts.extend(["  <dl>", *metadata, "  </dl>"])

    if structure:
        parts.extend(["  <section aria-labelledby=\"organization-structure\">", "    <h2 id=\"organization-structure\">Ownership and structure</h2>", "    <ul>"])
        for item in structure:
            target_name = item.get("name_en") or item.get("name_it") or item["qid"]
            href = _organization_path(item["qid"], target_name)
            label = _relationship_label(item.get("edge_type", ""), bool(item.get("outgoing")))
            parts.append(f'      <li><a href="{html.escape(href)}" rel="related">{html.escape(target_name)}</a> — {html.escape(label)}</li>')
        parts.extend(["    </ul>", "  </section>"])

    if people:
        parts.extend(["  <section aria-labelledby=\"associated-people\">", "    <h2 id=\"associated-people\">People associated with this organization</h2>", "    <ul>"])
        for item in people:
            person_name = item.get("name_en") or item.get("name_it") or item["qid"]
            href = _person_path(item["qid"], person_name)
            role = item.get("role") or item.get("edge_type", "").replace("_", " ").title()
            suffix = f" — {html.escape(str(role))}" if role else ""
            parts.append(f'      <li><a href="{html.escape(href)}" rel="related">{html.escape(person_name)}</a>{suffix}</li>')
        parts.extend(["    </ul>", "  </section>"])

    parts.extend(["  <p>Relationship data is assembled from Wikidata and public-record sources.</p>", "</main>"])
    return "\n".join(parts)


def _content_hash(org: dict, structure: list[dict], people: list[dict]) -> str:
    fields = {
        key: org.get(key)
        for key in (
            "name_en", "name_it", "description_en", "description_it", "type",
            "founded", "dissolved", "country", "headquarters", "employees_count",
            "website", "last_updated", "image_cached_path", "image_license",
            "image_license_url", "image_author", "image_credit", "image_source_url",
        )
    }
    fields["structure"] = structure
    fields["people"] = people
    return hashlib.sha256(json.dumps(fields, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _render_organization_page(qid_slug: str):
    """Return the canonical organization page, with a server-rendered SEO body."""
    match = _QID_SLUG_RE.match(qid_slug)
    if not match:
        return HTMLResponse(status_code=404, content="Not found")
    qid, requested_slug = match.group(1), match.group(2) or ""

    db = get_db(get_arango_client())
    org = db.collection(ORGANIZATIONS).get(qid)
    if not org:
        return HTMLResponse(status_code=404, content=f"Organization {qid} not found")

    # Historical incremental imports could copy a human QID into organizations.
    # Keep one canonical URL and never index the wrong entity type.
    person = db.collection(PERSONS).get(qid)
    if person:
        person_name = person.get("name_en") or person.get("name_it") or qid
        return RedirectResponse(
            url=_person_path(qid, person_name),
            status_code=301,
            headers={"Cache-Control": "public, max-age=3600"},
        )

    name = _display_name(org)
    canonical_path = _organization_path(qid, name)
    canonical_slug = canonical_path.partition(f"/organization/{qid}")[2].lstrip("-")
    if requested_slug != canonical_slug:
        return RedirectResponse(url=canonical_path, status_code=301, headers={"Cache-Control": "public, max-age=3600"})

    structure, people, people_count, structure_count = _fetch_relationships(db, qid)
    shell, shell_hash = get_shell()
    content_hash = _content_hash(org, structure, people)
    cache_key = (
        f"organization_page:{cache_namespace()}:{_RENDER_VERSION}:"
        f"{qid}:{content_hash}:{shell_hash}"
    )
    fast_cache_key = _fast_page_cache_key(qid_slug, shell_hash)
    # HTML contains the current hashed SPA entrypoint; always revalidate it so
    # a browser cannot retain an obsolete shell across frontend deployments.
    headers = {
        # Browsers revalidate, while the CDN can serve the expensive SSR page
        # for an hour and refresh it in the background.
        "Cache-Control": "public, max-age=0, s-maxage=3600, stale-while-revalidate=60",
    }
    noindex = is_thin_organization(org, people_count + structure_count)
    headers["X-Robots-Tag"] = (
        "noindex, follow" if noindex
        else "max-image-preview:large, max-snippet:-1, max-video-preview:-1"
    )

    cached = get_cached_text(cache_key)
    if cached:
        set_cached_text(fast_cache_key, cached, ttl_seconds=86400)
        return HTMLResponse(content=cached, headers=headers)

    canonical_url = f"{_SITE_URL}{canonical_path}"
    description = _meta_description(org, people_count, structure_count)
    head = _build_head(org, canonical_url, description, structure, noindex=noindex)
    body = _build_body(org, structure, people, people_count, structure_count)
    if shell:
        rendered = _render_page(shell, head, body)
    else:
        rendered = f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">{head}</head><body>{body}<div id=\"app\"></div></body></html>"

    set_cached_text(cache_key, rendered, ttl_seconds=86400)
    set_cached_text(fast_cache_key, rendered, ttl_seconds=86400)
    return HTMLResponse(content=rendered, headers=headers)


@router.api_route("/organization/{qid_slug:path}", methods=["GET", "HEAD"], response_class=HTMLResponse, include_in_schema=False)
@limiter.limit("30/minute")
async def organization_page(request: Request, qid_slug: str):
    """Serve an SSR page without letting crawler bursts block the event loop."""
    match = _QID_SLUG_RE.match(qid_slug)
    if not match:
        return await asyncio.to_thread(_render_organization_page, qid_slug)

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
        return await asyncio.to_thread(_render_organization_page, qid_slug)
    finally:
        await release_ssr_slot()
