"""SEC EDGAR provider — throttled urllib session with on-disk response cache.

Public surface (§5.1):
  get_ticker_map()               -> dict[str, dict]
  fetch_submissions_for_cik()    -> dict
  iter_form4_for_cik()           -> NotImplementedError (§10.3)
  iter_material_events_for_cik() -> NotImplementedError (§10.5.a)
  iter_beneficial_ownership()    -> NotImplementedError (§10.5.b)

Infrastructure (tested in §10.1.c):
  TokenBucket       — thread-safe token-bucket rate limiter
  ResponseCache     — SHA256-keyed on-disk HTTP response cache
  SecEdgarProvider  — wires the above, UA-validates at construction
"""

import gzip
import hashlib
import json
import logging
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError

from app.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# Retry delays on HTTP 429 / 403, in seconds.  Tests may monkeypatch this list.
BACKOFF_SECONDS: list[int] = [2, 8, 30, 120]

_TICKER_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------

class SecEdgarError(Exception):
    """Raised when the SEC EDGAR provider encounters an unrecoverable error."""


# ---------------------------------------------------------------------------
# Token-bucket rate limiter
# ---------------------------------------------------------------------------

class TokenBucket:
    """Thread-safe token-bucket limiter.

    Capacity and refill rate are both ``rate`` tokens per second, which gives a
    burst ceiling of 1 second's worth of requests — appropriate for SEC's
    10 req/s limit with a 2-req/s headroom margin baked into config.

    Usage::

        bucket = TokenBucket(rate=8.0)
        bucket.acquire()   # blocks until a token is available
    """

    def __init__(self, rate: float) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self._rate = rate          # tokens added per second
        self._capacity = rate      # max tokens in the bucket (1-second burst)
        self._tokens: float = rate # start full
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Block until one token is available, then consume it."""
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last_refill
                self._tokens = min(
                    self._capacity,
                    self._tokens + elapsed * self._rate,
                )
                self._last_refill = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                # How long until the next token arrives
                wait = (1.0 - self._tokens) / self._rate
            # Release lock while sleeping so other threads can check too
            time.sleep(wait)


# ---------------------------------------------------------------------------
# On-disk response cache
# ---------------------------------------------------------------------------

class ResponseCache:
    """SHA256-keyed on-disk HTTP response cache.

    Layout::

        {cache_dir}/
            <sha256hex>           — raw response body bytes
            <sha256hex>.meta.json — {"url", "status", "etag",
                                     "last_modified", "fetched_at"}

    Cache directory is created on first use.

    Usage::

        cache = ResponseCache(Path("/data/sec/cache"))
        body = cache.get(url)           # None on miss
        cache.put(url, body, headers)   # headers is a http.client.HTTPMessage
    """

    def __init__(self, cache_dir: Path) -> None:
        self._dir = cache_dir

    def _ensure_dir(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)

    def _key(self, url: str) -> str:
        return hashlib.sha256(url.encode()).hexdigest()

    def get(self, url: str) -> bytes | None:
        """Return cached body bytes, or None on cache miss."""
        key = self._key(url)
        body_path = self._dir / key
        if not body_path.exists():
            logger.debug("Cache miss: %s", url)
            return None
        logger.debug("Cache hit: %s", url)
        return body_path.read_bytes()

    def put(self, url: str, body: bytes, headers) -> None:
        """Write body and sidecar metadata to the cache directory."""
        self._ensure_dir()
        key = self._key(url)
        body_path = self._dir / key
        meta_path = self._dir / f"{key}.meta.json"

        body_path.write_bytes(body)

        # headers is an http.client.HTTPMessage; .get() is safe on None
        etag = headers.get("ETag") if headers else None
        last_modified = headers.get("Last-Modified") if headers else None
        status = headers.get("X-Cached-Status", "200") if headers else "200"
        meta = {
            "url": url,
            "status": status,
            "etag": etag,
            "last_modified": last_modified,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Main provider class
# ---------------------------------------------------------------------------

class SecEdgarProvider:
    """SEC EDGAR data provider.

    Validates UA at construction so misconfiguration fails at startup rather than
    on the first network call.  All HTTP goes through ``_get()``, which enforces
    rate limiting, transparent gzip decompression, exponential backoff on 429/403,
    and on-disk caching.
    """

    def __init__(self) -> None:
        ua = settings.sec_user_agent
        if not ua or not ua.strip():
            raise ValueError(
                "SEC_USER_AGENT must be set (SEC EDGAR requires a real contact in the UA)"
            )
        self._ua = ua.strip()
        self._bucket = TokenBucket(rate=settings.sec_requests_per_second)
        cache_dir = Path(settings.sec_data_dir) / "cache"
        self._cache = ResponseCache(cache_dir)

    # ------------------------------------------------------------------
    # Private HTTP helper
    # ------------------------------------------------------------------

    def _get(self, url: str) -> bytes:
        """Fetch *url* with caching, rate limiting, and backoff.

        Returns raw (decompressed) response body bytes.

        Raises:
            SecEdgarError: after exhausting all backoff retries on 429/403.
            urllib.error.URLError / urllib.error.HTTPError: for any other
                non-retried HTTP error (5xx, 404, network timeout, etc.).
        """
        # 1. Cache check
        cached = self._cache.get(url)
        if cached is not None:
            return cached

        # 2. Rate-limit then fetch, with retry on 429/403
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": self._ua,
                "Accept-Encoding": "gzip",
            },
        )

        last_exc: Exception | None = None
        for attempt, backoff in enumerate([0] + BACKOFF_SECONDS):
            if attempt > 0:
                logger.warning(
                    "SEC EDGAR backoff: attempt %d, sleeping %ds (url=%s)",
                    attempt,
                    backoff,
                    url,
                )
                time.sleep(backoff)

            self._bucket.acquire()

            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    body = resp.read()
                    headers = resp.headers

                    # Transparent gzip decompression
                    if headers.get("Content-Encoding") == "gzip":
                        body = gzip.decompress(body)

                    # 3. Store in cache
                    self._cache.put(url, body, headers)
                    return body

            except HTTPError as exc:
                if exc.code in (429, 403):
                    logger.warning(
                        "SEC EDGAR HTTP %d on attempt %d (url=%s)",
                        exc.code,
                        attempt,
                        url,
                    )
                    last_exc = exc
                    continue  # retry with next backoff slot
                raise  # non-retryable HTTP error — let caller handle

        # Exhausted all retries
        raise SecEdgarError(
            f"SEC EDGAR request failed after {len(BACKOFF_SECONDS) + 1} attempts "
            f"(url={url})"
        ) from last_exc

    # ------------------------------------------------------------------
    # Public API (§5.1)
    # ------------------------------------------------------------------

    def get_ticker_map(self) -> dict[str, dict]:
        """Fetch the SEC company-tickers file and return a CIK-keyed dict.

        Returns::

            {
                "0000320193": {"ticker": "AAPL", "title": "Apple Inc."},
                ...
            }

        The CIK is zero-padded to 10 digits to match the submissions URL
        convention used throughout this provider.
        """
        body = self._get(_TICKER_URL)
        raw: dict = json.loads(body)
        # The file is keyed by an integer ordinal; each value has cik_str, ticker, title.
        result: dict[str, dict] = {}
        for entry in raw.values():
            cik = str(entry["cik_str"]).zfill(10)
            result[cik] = {
                "ticker": entry.get("ticker"),
                "title": entry.get("title"),
            }
        return result

    def fetch_submissions_for_cik(self, cik: str) -> dict:
        """Fetch the submissions JSON for a single CIK and return it as-is.

        The ``filings.recent`` arrays can be thousands of entries long (e.g. RBC:
        4,338 rows — see §10.0.b notes).  This method deliberately returns the raw
        parsed dict WITHOUT materialising or filtering ``filings.recent`` — consumers
        MUST iterate lazily.  The streaming/ijson parser for bulk scripts lands
        in §10.4.

        Args:
            cik: CIK as a string; zero-padded to 10 digits if needed.
        """
        cik = str(cik).zfill(10)
        url = _SUBMISSIONS_URL.format(cik=cik)
        body = self._get(url)
        return json.loads(body)

    def fetch_primary_document(
        self, cik: str, accession: str, primary_document: str
    ) -> str:
        """Fetch the primary HTML document for a single filing and return it decoded.

        Uses a dedicated on-disk cache at ``sec_cache_dir/def14a/{cik}_{accession_nodash}.htm``
        so that DEF 14A proxy HTML is not evicted by the general SHA256 cache for
        submissions JSON (which has different churn characteristics).

        URL form:
            https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_nodash}/{primary_document}

        Args:
            cik:              CIK as a string (zero-padded to 10 digits if needed).
            accession:        Accession number with or without dashes.
            primary_document: Filename of the primary document (e.g. "proxy.htm").

        Returns:
            Decoded HTML text (UTF-8 with error replacement).
        """
        cik = str(cik).zfill(10)
        cik_int = int(cik)
        accession_nodash = accession.replace("-", "")

        # Dedicated cache file for this filing
        cache_dir = Path(settings.sec_data_dir) / "def14a"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"{cik}_{accession_nodash}.htm"

        if cache_file.exists():
            logger.debug("Primary-doc cache hit: %s", cache_file)
            return cache_file.read_text(encoding="utf-8", errors="replace")

        url = (
            f"https://www.sec.gov/Archives/edgar/data/{cik_int}"
            f"/{accession_nodash}/{primary_document}"
        )
        body = self._get(url)
        html = body.decode("utf-8", errors="replace")
        cache_file.write_text(html, encoding="utf-8")
        logger.debug("Primary-doc cached: %s", cache_file)
        return html

    def invalidate_primary_document(
        self, cik: str, accession: str, primary_document: str  # noqa: ARG002
    ) -> None:
        """Remove the on-disk cache entry for a primary document (used with --refresh).

        Args:
            cik:              CIK string.
            accession:        Accession number with or without dashes.
            primary_document: Unused; kept for signature parity with fetch_primary_document.
        """
        cik = str(cik).zfill(10)
        accession_nodash = accession.replace("-", "")
        cache_file = Path(settings.sec_data_dir) / "def14a" / f"{cik}_{accession_nodash}.htm"
        if cache_file.exists():
            cache_file.unlink()
            logger.debug("Invalidated primary-doc cache: %s", cache_file)

    def iter_form4_for_cik(self, cik: str, since=None):
        """Iterate Form 4 insider transactions for a CIK.

        Not implemented — bulk TSV loader lands in §10.3.
        """
        raise NotImplementedError("Form 4 bulk loader — see §10.3")

    def iter_material_events_for_cik(self, cik: str, since=None):
        """Iterate material events (8-K) for a CIK.

        Not implemented — 8-K loader lands in §10.5.a.
        """
        raise NotImplementedError("8-K loader — see §10.5.a")

    def iter_beneficial_ownership(self, cik: str, since=None):
        """Iterate SC 13D/G beneficial ownership filings for a CIK.

        Not implemented — SC 13D/G loader lands in §10.5.b.
        """
        raise NotImplementedError("SC 13D/G loader — see §10.5.b")
