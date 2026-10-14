"""Offline sitemap materialization and constant-time snapshot serving."""

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse

from app.database import get_arango_client, get_db
from app.utils.slug import slugify


SITE_URL = "https://humansmap.com"
MIN_STATEMENTS = 50
MIN_ORG_STATEMENTS = 25
MAX_URLS_PER_SITEMAP = 45_000
SNAPSHOT_DIR = Path(os.getenv("SITEMAP_DIR", "/app/reports/sitemaps"))
MANIFEST_NAME = "manifest.json"

PersonRow = tuple[str, str | None, str | None, str | None, int]
OrgRow = tuple[str, str | None, str | None, str | None, int]


def _parse_lastmod(raw: object) -> str | None:
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw.date().isoformat()
    value = str(raw).strip()
    if len(value) >= 10 and value[4] == "-" and value[7] == "-":
        return value[:10]
    try:
        return datetime.fromisoformat(value).date().isoformat()
    except ValueError:
        return None


def _priority(statements: int) -> str:
    if statements >= 300:
        return "0.9"
    if statements >= 150:
        return "0.8"
    if statements >= 100:
        return "0.7"
    return "0.5"


def _changefreq(lastmod: str | None, now_date: str) -> str:
    if not lastmod:
        return "monthly"
    try:
        age = (
            datetime.strptime(now_date, "%Y-%m-%d")
            - datetime.strptime(lastmod, "%Y-%m-%d")
        ).days
    except (TypeError, ValueError):
        return "monthly"
    if age <= 7:
        return "weekly"
    if age <= 60:
        return "monthly"
    return "yearly"


def _url_block(
    loc: str,
    lastmod: str | None,
    priority: str | None = None,
    changefreq: str | None = None,
) -> str:
    parts = [f"    <loc>{loc}</loc>"]
    if lastmod is not None:
        parts.append(f"    <lastmod>{lastmod}</lastmod>")
    if changefreq is not None:
        parts.append(f"    <changefreq>{changefreq}</changefreq>")
    if priority is not None:
        parts.append(f"    <priority>{priority}</priority>")
    inner = "\n".join(parts)
    return f"  <url>\n{inner}\n  </url>"


def _person_url(qid: str, name_en: str | None, name_it: str | None) -> str:
    slug = slugify(name_en or name_it or "")
    path = f"/person/{qid}-{slug}" if slug else f"/person/{qid}"
    return f"{SITE_URL}{path}"


def _organization_url(qid: str, name_en: str | None, name_it: str | None) -> str:
    slug = slugify(name_en or name_it or "")
    path = f"/organization/{qid}-{slug}" if slug else f"/organization/{qid}"
    return f"{SITE_URL}{path}"


def _fetch_rows() -> tuple[list[PersonRow], int]:
    """Fetch eligible rows; only contentless people require an edge lookup."""
    db = get_db(get_arango_client(request_timeout=300))
    query = """
        FOR p IN persons
            FILTER p.wikidata_statements_count >= @min_statements
            LET has_content = (
                (p.image_cached_path != null AND p.image_cached_path != "") OR
                (p.description_en != null AND p.description_en != "") OR
                (p.description_it != null AND p.description_it != "")
            )
            FILTER has_content OR LENGTH(
                FOR v IN 1..1 ANY p relationships
                    FILTER IS_SAME_COLLECTION("persons", v)
                    LIMIT 2
                    RETURN 1
            ) >= 2
            SORT p.wikidata_statements_count DESC, p._key
            RETURN [
                p._key,
                p.last_updated,
                p.name_en,
                p.name_it,
                p.wikidata_statements_count
            ]
    """
    raw_rows = list(db.aql.execute(
        query,
        bind_vars={"min_statements": MIN_STATEMENTS},
    ))
    rows: list[PersonRow] = [
        (
            str(qid),
            _parse_lastmod(lastmod),
            name_en,
            name_it,
            int(statements or MIN_STATEMENTS),
        )
        for qid, lastmod, name_en, name_it, statements in raw_rows
    ]
    total_query = """
        FOR p IN persons
            FILTER p.wikidata_statements_count >= @min_statements
            COLLECT WITH COUNT INTO total
            RETURN total
    """
    total = next(db.aql.execute(
        total_query,
        bind_vars={"min_statements": MIN_STATEMENTS},
    ))
    return rows, int(total)


def _fetch_organization_rows() -> tuple[list[OrgRow], int]:
    """Fetch a bounded set of substantive, non-position organizations."""
    db = get_db(get_arango_client(request_timeout=300))
    query = """
        FOR o IN organizations
            FILTER o.type != "position"
              AND o.wikidata_statements_count >= @min_statements
              AND (
                  (o.description_en != null AND o.description_en != "") OR
                  (o.description_it != null AND o.description_it != "") OR
                  (o.image_cached_path != null AND o.image_cached_path != "") OR
                  (o.website != null AND o.website != "") OR
                  o.affiliation_count > 0
              )
            SORT o.wikidata_statements_count DESC, o._key
            RETURN [
                o._key, o.last_updated, o.name_en, o.name_it,
                o.wikidata_statements_count
            ]
    """
    raw_rows = list(db.aql.execute(
        query, bind_vars={"min_statements": MIN_ORG_STATEMENTS}
    ))
    rows: list[OrgRow] = [
        (str(qid), _parse_lastmod(lastmod), name_en, name_it, int(statements or 0))
        for qid, lastmod, name_en, name_it, statements in raw_rows
    ]
    return rows, len(rows)


def _index_xml(pages: int, organization_pages: int) -> str:
    locations = [f"{SITE_URL}/sitemap-{page}.xml" for page in range(1, pages + 1)]
    locations.extend(
        f"{SITE_URL}/sitemap-org-{page}.xml"
        for page in range(1, organization_pages + 1)
    )
    entries = "\n".join(
        "  <sitemap>\n" f"    <loc>{location}</loc>\n" "  </sitemap>"
        for location in locations
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{entries}
</sitemapindex>"""


def _page_xml(rows: list[PersonRow], page: int, today: str) -> str:
    start = (page - 1) * MAX_URLS_PER_SITEMAP
    chunk = rows[start : start + MAX_URLS_PER_SITEMAP]
    blocks: list[str] = []
    if page == 1:
        blocks.append(_url_block(
            f"{SITE_URL}/", None, priority="1.0", changefreq="weekly"
        ))
    blocks.extend(
        _url_block(
            _person_url(qid, name_en, name_it),
            lastmod,
            priority=_priority(statements),
            changefreq=_changefreq(lastmod, today),
        )
        for qid, lastmod, name_en, name_it, statements in chunk
    )
    urls = "\n".join(blocks)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{urls}
</urlset>"""


def _organization_page_xml(rows: list[OrgRow], page: int, today: str) -> str:
    start = (page - 1) * MAX_URLS_PER_SITEMAP
    chunk = rows[start : start + MAX_URLS_PER_SITEMAP]
    urls = "\n".join(
        _url_block(
            _organization_url(qid, name_en, name_it),
            lastmod,
            priority=_priority(statements),
            changefreq=_changefreq(lastmod, today),
        )
        for qid, lastmod, name_en, name_it, statements in chunk
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{urls}
</urlset>"""


def _publish(directory: Path, name: str, content: str) -> None:
    temporary = directory / f".{name}.tmp"
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, directory / name)


def build_sitemap_snapshot(directory: Path = SNAPSHOT_DIR) -> dict[str, int | str]:
    """Materialize all shards atomically and publish the index last."""
    rows, total = _fetch_rows()
    organization_rows, organization_total = _fetch_organization_rows()
    pages = max(1, math.ceil(len(rows) / MAX_URLS_PER_SITEMAP))
    organization_pages = math.ceil(len(organization_rows) / MAX_URLS_PER_SITEMAP)
    generated_at = datetime.now(timezone.utc).isoformat()
    today = generated_at[:10]
    directory.mkdir(parents=True, exist_ok=True)

    for page in range(1, pages + 1):
        _publish(directory, f"sitemap-{page}.xml", _page_xml(rows, page, today))

    for page in range(1, organization_pages + 1):
        _publish(
            directory,
            f"sitemap-org-{page}.xml",
            _organization_page_xml(organization_rows, page, today),
        )

    manifest: dict[str, int | str] = {
        "included": len(rows),
        "excluded": total - len(rows),
        "pages": pages,
        "organizations": organization_total,
        "organization_pages": organization_pages,
        "generated_at": generated_at,
    }
    _publish(directory, MANIFEST_NAME, json.dumps(manifest, sort_keys=True))
    _publish(directory, "sitemap.xml", _index_xml(pages, organization_pages))
    return manifest


def snapshot_response(
    filename: str, *, page: int | None = None, page_key: str = "pages"
) -> FileResponse:
    """Serve a completed snapshot without database or Redis work."""
    if page is not None:
        manifest_path = SNAPSHOT_DIR / MANIFEST_NAME
        if manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if page > int(manifest.get(page_key, 0)):
                raise HTTPException(404, "Sitemap page not found")

    path = SNAPSHOT_DIR / filename
    if not path.is_file():
        raise HTTPException(
            status_code=503,
            detail="Sitemap snapshot is being generated",
            headers={"Retry-After": "300"},
        )
    return FileResponse(
        path,
        media_type="application/xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )
