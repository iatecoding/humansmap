"""Image download, optimization, and caching service.

Uses Wikimedia Commons thumbnail API (thumb.php) instead of Special:FilePath
to avoid throttling — thumbnails are served from CDN cache.
"""

import gzip
import html
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from urllib.error import HTTPError
from datetime import datetime, timezone
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path

from PIL import Image

from app.config import settings

logger = logging.getLogger(__name__)

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
COMMONS_METADATA_BATCH_SIZE = 20
_ALLOWED_LICENSE_RE = re.compile(
    r"^(?:cc[ -]?(?:by(?:[ -]?sa)?|zero|0)(?:[ -]?\d(?:\.\d)?)?|public domain|pd(?:m)?)",
    re.IGNORECASE,
)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _plain_text(value: str | None) -> str | None:
    if not value:
        return None
    parser = _TextExtractor()
    parser.feed(value)
    text = " ".join(html.unescape("".join(parser.parts)).split())
    return text or None


def _is_license_allowed(license_str: str | None) -> bool:
    """Allow only explicit CC or public-domain licenses."""
    if not license_str:
        return False
    normalized = " ".join(license_str.strip().replace("_", "-").split())
    return bool(_ALLOWED_LICENSE_RE.fullmatch(normalized))


def commons_filename(image_url: str) -> str:
    """Extract and URL-decode the Commons filename from a Wikidata image URL."""
    if "Special:FilePath/" in image_url:
        raw = image_url.split("Special:FilePath/", 1)[1]
    else:
        raw = image_url.rsplit("/", 1)[-1]
    return urllib.parse.unquote(raw).replace("_", " ")


def _metadata_from_imageinfo(filename: str, info: dict) -> dict:
    """Normalize the small Commons metadata subset required for attribution."""
    ext = info.get("extmetadata") or {}

    def value(key: str) -> str | None:
        item = ext.get(key) or {}
        return item.get("value") or None

    return {
        "image_filename": filename,
        "image_source_url": info.get("descriptionurl") or (
            "https://commons.wikimedia.org/wiki/File:"
            + urllib.parse.quote(filename.replace(" ", "_"))
        ),
        "image_license": _plain_text(value("LicenseShortName")),
        "image_license_url": value("LicenseUrl"),
        "image_author": _plain_text(value("Artist")),
        "image_credit": _plain_text(value("Credit")),
        "image_attribution_required": (
            str(value("AttributionRequired") or "").lower() == "true"
        ),
        "image_metadata_retrieved_at": datetime.now(timezone.utc).isoformat(),
        "_image_download_url": info.get("thumburl"),
    }


def _read_json_response(response) -> dict:
    raw = response.read()
    headers = getattr(response, "headers", None)
    if headers is not None and headers.get("Content-Encoding") == "gzip":
        raw = gzip.decompress(raw)
    return json.loads(raw)


def fetch_commons_metadata_batch(image_urls: dict[str, str]) -> dict[str, dict | None]:
    """Fetch attribution data and thumbnail URLs for a small file batch."""
    if not image_urls:
        return {}
    if len(image_urls) > COMMONS_METADATA_BATCH_SIZE:
        raise ValueError(
            f"Commons metadata batch exceeds {COMMONS_METADATA_BATCH_SIZE} files"
        )

    title_to_qid = {
        f"File:{commons_filename(url)}".replace("_", " ").casefold(): qid
        for qid, url in image_urls.items()
    }
    params = urllib.parse.urlencode({
        "action": "query",
        "format": "json",
        "formatversion": "2",
        "prop": "imageinfo",
        "iiprop": "url|extmetadata",
        "iiurlwidth": str(settings.image_max_dimension_px),
        "iiextmetadatafilter": (
            "LicenseShortName|LicenseUrl|Artist|Credit|AttributionRequired"
        ),
        "titles": "|".join(
            f"File:{commons_filename(url)}" for url in image_urls.values()
        ),
        "redirects": "1",
        "maxlag": "5",
    })
    request = urllib.request.Request(
        f"{COMMONS_API}?{params}",
        headers={
            "User-Agent": settings.sparql_user_agent,
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        },
    )

    payload = None
    for attempt in range(5):
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                payload = _read_json_response(response)
            if "error" not in payload:
                break
            if payload["error"].get("code") != "maxlag":
                raise RuntimeError(payload["error"].get("info", "Commons API error"))
            wait = min(60, 5 * (2**attempt))
            logger.info("Commons is busy (maxlag); retrying batch in %ds", wait)
            time.sleep(wait)
        except HTTPError as exc:
            if exc.code not in (429, 503) or attempt == 4:
                raise
            retry_after = int(exc.headers.get("Retry-After", 5 * (2**attempt)))
            time.sleep(min(120, retry_after))
    else:
        payload = None

    results: dict[str, dict | None] = {qid: None for qid in image_urls}
    if not payload or "error" in payload:
        return results

    aliases = {}
    query = payload.get("query", {})
    for item in (query.get("normalized") or []) + (query.get("redirects") or []):
        aliases[item["to"].replace("_", " ").casefold()] = (
            item["from"].replace("_", " ").casefold()
        )

    for page in query.get("pages") or []:
        page_title = (page.get("title") or "").replace("_", " ").casefold()
        requested_title = aliases.get(page_title, page_title)
        qid = title_to_qid.get(requested_title) or title_to_qid.get(page_title)
        if not qid:
            continue
        info = (page.get("imageinfo") or [{}])[0]
        if info:
            filename = (page.get("title") or "File:").split(":", 1)[-1]
            results[qid] = _metadata_from_imageinfo(filename, info)
    return results


def fetch_commons_metadata(image_url: str) -> dict | None:
    """Return normalized license/credit metadata for one file."""
    return fetch_commons_metadata_batch({"item": image_url}).get("item")

def image_url_to_thumb_url(image_url: str, width: int = 300) -> str:
    """Convert a Wikimedia Special:FilePath URL to a thumb.php thumbnail URL.

    Input:  http://commons.wikimedia.org/wiki/Special:FilePath/Pablo%20picasso%201.jpg
    Output: https://commons.wikimedia.org/w/thumb.php?f=Pablo%20picasso%201.jpg&w=300
    """
    filename = commons_filename(image_url)
    return f"https://commons.wikimedia.org/w/thumb.php?f={urllib.parse.quote(filename)}&w={width}"


def _encode_jpeg_with_limit(img: Image.Image, max_bytes: int) -> bytes:
    """Encode an RGB image below the configured byte ceiling when possible."""
    current = img.copy()
    while True:
        for quality in (settings.image_jpeg_quality, 72, 64, 56, 48, 42):
            output = BytesIO()
            current.save(output, "JPEG", quality=quality, optimize=True)
            data = output.getvalue()
            if len(data) <= max_bytes:
                return data
        if max(current.size) <= 120:
            return data
        current.thumbnail(
            (max(120, int(current.width * 0.85)), max(120, int(current.height * 0.85))),
            Image.Resampling.LANCZOS,
        )


def _download_and_optimize_sync(
    qid: str,
    image_url: str,
    image_license: str | None = None,
    thumbnail_url: str | None = None,
) -> str | None:
    """Sync implementation — call via asyncio.to_thread from async code."""
    if not _is_license_allowed(image_license):
        logger.debug("Skipping %s: license '%s' not allowed", qid, image_license)
        return None

    cache_dir = Path(settings.image_cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    output_path = cache_dir / f"{qid}.jpg"

    if output_path.exists():
        return str(output_path)

    # Use thumbnail API instead of full-size image
    thumb_url = thumbnail_url or image_url_to_thumb_url(
        image_url, width=settings.image_max_dimension_px
    )

    try:
        req = urllib.request.Request(
            thumb_url,
            headers={"User-Agent": settings.sparql_user_agent},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw_bytes = resp.read()
    except Exception as e:
        logger.warning("Failed to download image for %s: %s", qid, e)
        return None

    try:
        img = Image.open(BytesIO(raw_bytes))
        img = img.convert("RGB")
        max_dimension = settings.image_max_dimension_px
        if max(img.size) > max_dimension:
            img.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
        encoded = _encode_jpeg_with_limit(img, settings.image_max_size_bytes)
        output_path.write_bytes(encoded)

        logger.debug("Cached image for %s at %s (%d bytes)", qid, output_path, output_path.stat().st_size)
        return str(output_path)

    except Exception as e:
        logger.warning("Failed to optimize image for %s: %s", qid, e)
        if output_path.exists():
            os.remove(output_path)
        return None


async def download_and_optimize_image(
    qid: str,
    image_url: str,
    image_license: str | None = None,
) -> str | None:
    """Download an image from Wikimedia Commons, optimize it, and cache locally.

    Returns the cached file path, or None if download/optimization failed.
    """
    import asyncio
    return await asyncio.to_thread(_download_and_optimize_sync, qid, image_url, image_license)
