"""Serve prebuilt XML sitemap shards without request-time database work."""

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from app.services.sitemap_snapshot import snapshot_response

router = APIRouter(tags=["sitemap"])


def _parse_lastmod(raw: object) -> str | None:
    """Return a YYYY-MM-DD string from a last_updated field value, or None on failure."""
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw.date().isoformat()
    s = str(raw).strip()
    if not s:
        return None
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    try:
        return datetime.fromisoformat(s).date().isoformat()
    except ValueError:
        return None


def _priority(statements: int) -> str:
    """Map wikidata_statements_count to a sitemap priority bucket."""
    if statements >= 300:
        return "0.9"
    if statements >= 150:
        return "0.8"
    if statements >= 100:
        return "0.7"
    return "0.5"


def _changefreq(lastmod: str | None, now_date: str) -> str:
    """Assign changefreq based on how recently the record was updated."""
    if not lastmod:
        return "monthly"
    try:
        delta = (
            datetime.strptime(now_date, "%Y-%m-%d")
            - datetime.strptime(lastmod, "%Y-%m-%d")
        ).days
        if delta <= 7:
            return "weekly"
        if delta <= 60:
            return "monthly"
        return "yearly"
    except Exception:
        return "monthly"


@router.get("/sitemap.xml", include_in_schema=False)
async def sitemap_index():
    """Return the prebuilt index for person sitemap shards."""
    return snapshot_response("sitemap.xml")


def _url_block(
    loc: str,
    lastmod: str | None,
    priority: str | None = None,
    changefreq: str | None = None,
) -> str:
    """Render a single <url> block."""
    parts = [f"    <loc>{loc}</loc>"]
    if lastmod is not None:
        parts.append(f"    <lastmod>{lastmod}</lastmod>")
    if changefreq is not None:
        parts.append(f"    <changefreq>{changefreq}</changefreq>")
    if priority is not None:
        parts.append(f"    <priority>{priority}</priority>")
    inner = "\n".join(parts)
    return f"  <url>\n{inner}\n  </url>"



@router.get("/sitemap-org-{page}.xml", include_in_schema=False)
async def organization_sitemap_page(page: int):
    """Return one prebuilt organization sitemap page."""
    if page < 1:
        raise HTTPException(404, "Invalid sitemap page")
    return snapshot_response(
        f"sitemap-org-{page}.xml", page=page, page_key="organization_pages"
    )


@router.get("/sitemap-{page}.xml", include_in_schema=False)
async def sitemap_page(page: int):
    """Return one prebuilt sitemap page without querying the database."""
    if page < 1:
        raise HTTPException(404, "Invalid sitemap page")
    return snapshot_response(f"sitemap-{page}.xml", page=page)
