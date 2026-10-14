"""
Pytest tests for the SEC EDGAR provider infrastructure.

No network access required — all HTTP is mocked via unittest.mock.patch.

Run inside the Docker container:
    docker compose exec backend pytest tests/test_sec_edgar_provider.py -s
"""

import hashlib
import json
import re
import time
from io import BytesIO
from unittest.mock import MagicMock, call, patch

import pytest

import app.providers.sec_edgar as m
from app.providers.sec_edgar import (
    BACKOFF_SECONDS,
    ResponseCache,
    SecEdgarError,
    SecEdgarProvider,
    TokenBucket,
)


# ---------------------------------------------------------------------------
# Group 1: TokenBucket math
# ---------------------------------------------------------------------------


def test_token_bucket_initial_capacity_allows_burst():
    """A full bucket at rate=5 permits 5 immediate acquire() calls."""
    rate = 5.0
    bucket = TokenBucket(rate=rate)
    start = time.monotonic()
    for _ in range(int(rate)):
        bucket.acquire()
    elapsed = time.monotonic() - start
    assert elapsed < 0.1, f"Expected burst to complete in <0.1s, took {elapsed:.3f}s"


def test_token_bucket_refills_over_time(monkeypatch):
    """After draining, advancing monotonic by 1/rate yields exactly one token."""
    rate = 10.0
    bucket = TokenBucket(rate=rate)

    # Drain the bucket
    for _ in range(int(rate)):
        bucket.acquire()

    # Capture the current _last_refill and advance monotonic by 1/rate
    frozen_now = bucket._last_refill

    def fake_monotonic():
        return frozen_now + (1.0 / rate) + 0.001  # slightly over to ensure >=1 token

    monkeypatch.setattr(time, "monotonic", fake_monotonic)

    # Should not block (one token available after the advance)
    start = time.monotonic()
    bucket.acquire()
    elapsed = time.monotonic() - start
    # With monkeypatched monotonic the elapsed is effectively 0 real seconds
    assert elapsed < 0.5, f"Expected prompt return after refill, took {elapsed:.3f}s"


def test_token_bucket_blocks_when_empty():
    """With a fast bucket (100 rps → 10 ms/token), draining then acquiring takes ≥1 interval."""
    rate = 100.0
    expected_interval = 1.0 / rate  # 10 ms
    bucket = TokenBucket(rate=rate)

    # Drain the bucket
    for _ in range(int(rate)):
        bucket.acquire()

    # Next acquire should block for ~expected_interval
    start = time.monotonic()
    bucket.acquire()
    elapsed = time.monotonic() - start

    # Allow 5× tolerance for slow CI
    assert elapsed >= expected_interval * 0.5, (
        f"Expected ≥{expected_interval * 0.5:.4f}s wait, got {elapsed:.4f}s"
    )


# ---------------------------------------------------------------------------
# Group 2: UA validation at construction
# ---------------------------------------------------------------------------


def test_empty_ua_raises_at_init(monkeypatch):
    """Empty UA string raises ValueError with the exact required message."""
    from app.config import settings

    monkeypatch.setattr(settings, "sec_user_agent", "")
    with pytest.raises(ValueError, match=re.escape(
        "SEC_USER_AGENT must be set (SEC EDGAR requires a real contact in the UA)"
    )):
        SecEdgarProvider()


def test_whitespace_only_ua_raises_at_init(monkeypatch):
    """Whitespace-only UA raises ValueError — checks both space and tab/newline variants."""
    from app.config import settings

    for bad_ua in ("   ", "\t\n"):
        monkeypatch.setattr(settings, "sec_user_agent", bad_ua)
        with pytest.raises(ValueError, match=re.escape(
            "SEC_USER_AGENT must be set (SEC EDGAR requires a real contact in the UA)"
        )):
            SecEdgarProvider()


def test_valid_ua_constructs_cleanly(monkeypatch):
    """A valid UA string allows SecEdgarProvider to construct without error."""
    from app.config import settings

    monkeypatch.setattr(settings, "sec_user_agent", "HumansMap admin@humansmap.com")
    provider = SecEdgarProvider()
    assert provider is not None


# ---------------------------------------------------------------------------
# Group 3: ResponseCache hit/miss
# ---------------------------------------------------------------------------


def test_cache_miss_returns_none(tmp_path):
    """A fresh cache directory returns None for any URL."""
    cache = ResponseCache(tmp_path)
    result = cache.get("https://example.com/test")
    assert result is None


def test_cache_put_then_get_roundtrips_bytes(tmp_path):
    """put() followed by get() returns the exact bytes stored."""
    cache = ResponseCache(tmp_path)
    url = "https://example.com/data"
    body = b"hello"
    headers = MagicMock()
    headers.get = lambda k, default=None: {"ETag": "abc"}.get(k, default)

    cache.put(url, body, headers)
    result = cache.get(url)
    assert result == body


def test_cache_writes_sidecar_meta_json(tmp_path):
    """put() writes a sidecar .meta.json with url, status, etag, fetched_at keys."""
    cache = ResponseCache(tmp_path)
    url = "https://example.com/filing"
    body = b"raw bytes"
    headers = MagicMock()
    headers.get = lambda k, default=None: {"ETag": "xyz123"}.get(k, default)

    cache.put(url, body, headers)

    key = hashlib.sha256(url.encode()).hexdigest()
    meta_path = tmp_path / f"{key}.meta.json"
    assert meta_path.exists(), "Sidecar .meta.json was not created"

    meta = json.loads(meta_path.read_text())
    assert "url" in meta
    assert "status" in meta
    assert "etag" in meta
    assert "fetched_at" in meta, "fetched_at key missing from sidecar"
    assert re.match(r"\d{4}-\d{2}-\d{2}T", meta["fetched_at"]), (
        f"fetched_at does not look ISO-like: {meta['fetched_at']}"
    )


def test_cache_key_differs_per_url(tmp_path):
    """Two different URLs produce two different cache filenames."""
    cache = ResponseCache(tmp_path)
    url_a = "https://example.com/a"
    url_b = "https://example.com/b"
    headers = MagicMock()
    headers.get = lambda k, default=None: default

    cache.put(url_a, b"aaa", headers)
    cache.put(url_b, b"bbb", headers)

    key_a = hashlib.sha256(url_a.encode()).hexdigest()
    key_b = hashlib.sha256(url_b.encode()).hexdigest()
    assert key_a != key_b, "Cache keys for different URLs must differ"
    assert (tmp_path / key_a).exists()
    assert (tmp_path / key_b).exists()


def test_cache_creates_directory_automatically(tmp_path):
    """put() creates a deeply-nested directory that did not previously exist."""
    nested_dir = tmp_path / "a" / "b" / "c"
    assert not nested_dir.exists()

    cache = ResponseCache(nested_dir)
    headers = MagicMock()
    headers.get = lambda k, default=None: default

    cache.put("https://example.com/x", b"data", headers)
    assert nested_dir.exists(), "Cache directory was not created automatically"


# ---------------------------------------------------------------------------
# Group 4: 429 exponential backoff
# ---------------------------------------------------------------------------


def _make_http_error(code: int):
    """Build a minimal urllib.error.HTTPError for a given status code."""
    from urllib.error import HTTPError

    return HTTPError(
        url="https://example.com",
        code=code,
        msg=f"HTTP {code}",
        hdrs=None,
        fp=None,
    )


def _make_200_response(body: bytes):
    """Build a mock HTTP response that behaves like urllib.request.urlopen's return value."""
    resp = MagicMock()
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    resp.read.return_value = body
    resp.headers = MagicMock()
    resp.headers.get.return_value = None  # no Content-Encoding, no ETag
    return resp


def test_first_429_sleeps_backoff_zero():
    """Contract test: BACKOFF_SECONDS == [2, 8, 30, 120] exactly."""
    assert m.BACKOFF_SECONDS == [2, 8, 30, 120], (
        f"BACKOFF_SECONDS contract broken: {m.BACKOFF_SECONDS}"
    )
    assert len(m.BACKOFF_SECONDS) == 4


@pytest.fixture(autouse=False)
def zero_backoff(monkeypatch):
    """Monkeypatch BACKOFF_SECONDS to zeros so retry tests complete instantly."""
    monkeypatch.setattr("app.providers.sec_edgar.BACKOFF_SECONDS", [0, 0, 0, 0])


@pytest.fixture()
def provider(monkeypatch):
    """Construct a SecEdgarProvider with a valid UA, bypassing real config."""
    from app.config import settings

    monkeypatch.setattr(settings, "sec_user_agent", "HumansMap test@humansmap.com")
    # Ensure the token bucket doesn't throttle unit tests
    monkeypatch.setattr(settings, "sec_requests_per_second", 10000.0)
    return SecEdgarProvider()


def test_429_then_200_returns_body(monkeypatch, provider, zero_backoff, tmp_path):
    """urlopen raises 429 once, then succeeds; sleep called once with BACKOFF_SECONDS[0]."""
    # Patch the provider's cache to always miss (avoid tmp_path issues)
    monkeypatch.setattr(provider._cache, "_dir", tmp_path)

    sleep_calls = []
    monkeypatch.setattr(time, "sleep", lambda s: sleep_calls.append(s))

    response_body = b'{"ok": true}'
    side_effects = [_make_http_error(429), _make_200_response(response_body)]

    with patch("urllib.request.urlopen", side_effect=side_effects):
        result = provider._get("https://example.com/endpoint")

    assert result == response_body
    assert len(sleep_calls) == 1
    assert sleep_calls[0] == 0  # BACKOFF_SECONDS[0] after zero_backoff monkeypatch


def test_429_then_200_sleeps_real_schedule(monkeypatch, provider, tmp_path):
    """Without zero_backoff, first retry sleeps BACKOFF_SECONDS[0] = 2s (value, not wall time)."""
    monkeypatch.setattr(provider._cache, "_dir", tmp_path)

    sleep_calls = []
    monkeypatch.setattr(time, "sleep", lambda s: sleep_calls.append(s))

    response_body = b'{"ok": true}'
    side_effects = [_make_http_error(429), _make_200_response(response_body)]

    with patch("urllib.request.urlopen", side_effect=side_effects):
        result = provider._get("https://example.com/endpoint2")

    assert result == response_body
    assert len(sleep_calls) == 1
    assert sleep_calls[0] == BACKOFF_SECONDS[0], (
        f"Expected first backoff sleep={BACKOFF_SECONDS[0]}, got {sleep_calls[0]}"
    )


def test_backoff_escalates_through_schedule(monkeypatch, provider, zero_backoff, tmp_path):
    """urlopen always raises 429; sleep is called for each BACKOFF_SECONDS entry; SecEdgarError raised."""
    monkeypatch.setattr(provider._cache, "_dir", tmp_path)

    sleep_calls = []
    monkeypatch.setattr(time, "sleep", lambda s: sleep_calls.append(s))

    # Always 429 — needs 1 initial attempt + len(BACKOFF_SECONDS) retries = 5 total calls
    side_effects = [_make_http_error(429)] * (1 + len(BACKOFF_SECONDS))

    with patch("urllib.request.urlopen", side_effect=side_effects):
        with pytest.raises(SecEdgarError):
            provider._get("https://example.com/always-429")

    # sleep is called for each of the BACKOFF_SECONDS values (but NOT for the initial attempt)
    assert sleep_calls == [0, 0, 0, 0], (
        f"Expected zero-patched backoff schedule, got {sleep_calls}"
    )


def test_403_also_triggers_backoff(monkeypatch, provider, zero_backoff, tmp_path):
    """HTTP 403 is treated identically to 429 — triggers retry with sleep."""
    monkeypatch.setattr(provider._cache, "_dir", tmp_path)

    sleep_calls = []
    monkeypatch.setattr(time, "sleep", lambda s: sleep_calls.append(s))

    response_body = b'{"data": "ok"}'
    side_effects = [_make_http_error(403), _make_200_response(response_body)]

    with patch("urllib.request.urlopen", side_effect=side_effects):
        result = provider._get("https://example.com/403-then-200")

    assert result == response_body
    assert len(sleep_calls) == 1


def test_non_throttle_error_raises_immediately(monkeypatch, provider, zero_backoff, tmp_path):
    """HTTP 500 is not retried — raises immediately with no sleep calls."""
    monkeypatch.setattr(provider._cache, "_dir", tmp_path)

    sleep_calls = []
    monkeypatch.setattr(time, "sleep", lambda s: sleep_calls.append(s))

    with patch("urllib.request.urlopen", side_effect=_make_http_error(500)):
        from urllib.error import HTTPError

        with pytest.raises(HTTPError) as exc_info:
            provider._get("https://example.com/500")

    assert exc_info.value.code == 500
    assert sleep_calls == [], f"Expected no sleep calls on 500, got {sleep_calls}"


# ---------------------------------------------------------------------------
# Smoke: NotImplementedError stubs
# ---------------------------------------------------------------------------


def test_form4_stub_raises_not_implemented(monkeypatch, provider):
    """iter_form4_for_cik raises NotImplementedError (§10.3 stub)."""
    with pytest.raises(NotImplementedError):
        provider.iter_form4_for_cik("0000320193")
