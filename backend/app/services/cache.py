"""Redis cache-aside helpers.

Redis is treated as optional infrastructure — if it is down, every
function here logs a warning and returns a cache-miss so the caller
falls through to ArangoDB.  The app must never crash because Redis
is unreachable.
"""

import base64
import json
import logging
import zlib

import redis

from app.config import settings

logger = logging.getLogger(__name__)

_COMPRESSED_PREFIX = "z1:"
_COMPRESSION_THRESHOLD = 1024

# ---------------------------------------------------------------------------
# Connection pool (singleton per process / uvicorn worker)
# ---------------------------------------------------------------------------

_pool: redis.ConnectionPool | None = None


def _get_pool() -> redis.ConnectionPool:
    global _pool
    if _pool is None:
        _pool = redis.ConnectionPool(
            host=settings.redis_host,
            port=settings.redis_port,
            max_connections=20,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
    return _pool


def get_redis() -> redis.Redis:
    """Return a Redis client backed by the shared connection pool."""
    return redis.Redis(connection_pool=_get_pool())


# ---------------------------------------------------------------------------
# Cache-aside helpers
# ---------------------------------------------------------------------------


def _encode_value(raw: str) -> str:
    """Compress larger values while keeping Redis in decode-responses mode."""
    if len(raw) < _COMPRESSION_THRESHOLD:
        return raw
    compressed = zlib.compress(raw.encode("utf-8"), level=3)
    encoded = base64.b64encode(compressed).decode("ascii")
    # Small or incompressible values are cheaper to store without a marker.
    packed = _COMPRESSED_PREFIX + encoded
    return packed if len(packed) < len(raw) else raw


def _decode_value(raw: str) -> str:
    if not raw.startswith(_COMPRESSED_PREFIX):
        return raw
    compressed = base64.b64decode(raw[len(_COMPRESSED_PREFIX):], validate=True)
    return zlib.decompress(compressed).decode("utf-8")


def get_cached(key: str) -> dict | list | None:
    """Return a cached JSON value, transparently reading old uncompressed keys."""
    try:
        raw = get_redis().get(key)
        if raw is None:
            return None
        return json.loads(_decode_value(raw))
    except Exception:
        logger.warning("Redis GET failed for key=%s", key, exc_info=True)
        return None


def set_cached(key: str, data: dict | list, ttl_seconds: int | None = None) -> None:
    """Write a compact, compressed JSON value to Redis (best effort)."""
    if ttl_seconds is None:
        ttl_seconds = settings.redis_default_ttl
    try:
        raw = json.dumps(data, default=str, separators=(",", ":"))
        get_redis().set(key, _encode_value(raw), ex=ttl_seconds)
    except Exception:
        logger.warning("Redis SET failed for key=%s", key, exc_info=True)


def get_cached_text(key: str) -> str | None:
    """Return cached text with backward-compatible decompression."""
    try:
        raw = get_redis().get(key)
        return None if raw is None else _decode_value(raw)
    except Exception:
        logger.warning("Redis text GET failed for key=%s", key, exc_info=True)
        return None


def set_cached_text(key: str, value: str, ttl_seconds: int | None = None) -> None:
    """Write compressed text to Redis (best effort)."""
    if ttl_seconds is None:
        ttl_seconds = settings.redis_default_ttl
    try:
        get_redis().set(key, _encode_value(value), ex=ttl_seconds)
    except Exception:
        logger.warning("Redis text SET failed for key=%s", key, exc_info=True)


def flush_pattern(pattern: str = "*") -> int:
    """Delete keys matching *pattern*.

    For the simple "*" case we use FLUSHDB which is O(1) and avoids
    scanning.  For a targeted pattern we use SCAN + DELETE in batches.

    Returns the number of keys deleted (best-effort count).
    """
    try:
        r = get_redis()
        if pattern == "*":
            r.flushdb()
            logger.info("Redis FLUSHDB executed")
            return -1  # unknown count
        deleted = 0
        cursor = 0
        while True:
            cursor, keys = r.scan(cursor=cursor, match=pattern, count=500)
            if keys:
                deleted += r.delete(*keys)
            if cursor == 0:
                break
        logger.info("Redis flush pattern=%s deleted=%d", pattern, deleted)
        return deleted
    except Exception:
        logger.warning("Redis flush failed for pattern=%s", pattern, exc_info=True)
        return 0
