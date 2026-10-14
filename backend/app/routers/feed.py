"""feed.py — GET /api/feed endpoint for the HumansMap connections scroll feed.

v2 layout (Phase 13Q): card bodies stored in feed:cardmap:v2 HASH; ID order
arrays in feed:order:all:v2 and feed:order:cat:<cat>:v2.  Per-request cost is
one ID-array read + HMGET for only the page window (~20 bodies).

Query params:
    cursor      int    Offset into the (shuffled) ID list.
    limit       int    Cards per page (max 50).
    lang        str    Language code (forward-compat; currently EN only).
    categories  str    Comma-separated taxonomy ids to boost
                       (politics, business, sport, science_education, arts).
                       Unknown ids silently ignored.
    nationality str    Single ISO-3166 alpha-2 code (e.g. "IT", "FR").
                       Soft-boosts cards involving that nationality using a
                       2-home : 1-world interleave blend.  Ignored when the
                       code is not present in feed:meta:v2 "countries".
                       Omit (or send no value) for pure worldwide order.

Returns:
    200  {"cards": [...], "next_cursor": int|null, "total": int}
    503  {"detail": "feed not generated"}  — if feed:meta:v2 is absent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import random
import threading
from datetime import date
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request

from app.domain.feed_contract import FEED_CONTENT_VERSION
from app.rate_limit import limiter
from app.services.cache import get_redis

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FEED_CARDMAP_KEY: str = "feed:cardmap:v2"
FEED_ORDER_ALL_KEY: str = "feed:order:all:v2"
FEED_ORDER_CAT_PREFIX: str = "feed:order:cat:"
FEED_ORDER_NAT_PREFIX: str = "feed:order:nat:"
FEED_ORDER_TYPE_PREFIX: str = "feed:order:type:"
FEED_META_KEY: str = "feed:meta:v2"

# Taxonomy ids — unknown values from the query param are silently ignored.
VALID_CATEGORIES: frozenset[str] = frozenset({
    "politics", "business", "sport", "science_education", "arts",
})

# Card archetypes selectable via the `types` filter ("Tailor your feed").
# "People" in the UI maps to the relationship ("Did you know") cards.
VALID_TYPES: frozenset[str] = frozenset({
    "relationship", "dynasty", "organization",
})

# Number of equal-sized score bands for the day-seeded shuffle.
SHUFFLE_BANDS: int = 5

# Serendipity injection interval when category filter is active.
# Every SERENDIPITY_INTERVAL positions in the front-loaded list, one card
# from the non-selected remainder is spliced in.
SERENDIPITY_INTERVAL: int = 6

# Maximum cards returnable per request (safety guard).
MAX_LIMIT: int = 50

# Nationality blend ratio: for every NAT_BLEND_WORLD "world" card, emit
# NAT_BLEND_HOME "home" (nationality-matching) cards.  4:1 → ~80% home, so the
# selected nationality clearly dominates while a worldwide trickle keeps the
# feed from running dry (esp. for small home pools). When the home pool is
# exhausted the blend simply drains the remaining world cards.
NAT_BLEND_HOME: int = 4
NAT_BLEND_WORLD: int = 1

# Regex for a valid ISO-3166 alpha-2 country code (2 ASCII letters).
_NAT_CODE_RE = re.compile(r"^[A-Za-z]{2}$")

# Regex for a valid feed card id (must match the pattern used in share.py).
_CARD_ID_RE = re.compile(r"^[a-f0-9]{6,16}$")

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["feed"])

# In-process cache: maps generated_at string → parsed ID list.
# Avoids re-parsing the JSON array on every request within the same process.
_id_array_cache: dict[str, list[str]] = {}

# The generated feed is durable application data, not an ordinary request
# cache. It can still disappear after a Redis flush/restart, so recovery is
# guarded by both a process lock and a Redis lock: a burst of visitors or
# workers must never launch duplicate expensive ArangoDB feed builds.
FEED_REBUILD_LOCK_KEY = "feed:rebuild-lock:v1"
FEED_REBUILD_LOCK_TIMEOUT_SECONDS = 15 * 60
_feed_rebuild_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Day-seeded shuffle helpers
# ---------------------------------------------------------------------------


def _today_seed() -> int:
    """Return an integer seed based on today's date (deterministic within a day)."""
    d = date.today()
    return d.year * 10000 + d.month * 100 + d.day


def _shuffled_ids(ids: list[str], seed: int | None = None) -> list[str]:
    """Return a copy of *ids* with each score-band shuffled deterministically.

    Divides into SHUFFLE_BANDS equal bands, shuffles within each band. When
    *seed* is None the day seed is used (fresh daily, stable within a day +
    CDN-cacheable). The frontend passes a per-load random seed so each page
    visit / hard-refresh reshuffles, while staying stable across that visit's
    pagination (same seed → same ordering → correct cursors).
    """
    if not ids:
        return []

    rng = random.Random(seed if seed is not None else _today_seed())

    n = len(ids)
    band_size = max(1, n // SHUFFLE_BANDS)

    result: list[str] = []
    for band_start in range(0, n, band_size):
        band = list(ids[band_start : band_start + band_size])
        rng.shuffle(band)
        result.extend(band)

    return result


# Keep the old function name for tests that import it.
def _shuffled_cards(cards: list[dict]) -> list[dict]:
    """Legacy helper: shuffle a list of card dicts (used by older tests)."""
    if not cards:
        return []

    seed = _today_seed()
    rng = random.Random(seed)

    n = len(cards)
    band_size = max(1, n // SHUFFLE_BANDS)

    result: list[dict] = []
    for band_start in range(0, n, band_size):
        band = list(cards[band_start : band_start + band_size])
        rng.shuffle(band)
        result.extend(band)

    return result


# ---------------------------------------------------------------------------
# Category soft-boost ordering (§B2)
# ---------------------------------------------------------------------------


def _build_ordered_ids(
    all_ids: list[str],
    cat_ids_map: dict[str, list[str]],
    selected_categories: list[str],
) -> list[str]:
    """Build a soft-boosted ID ordering for the given category selection.

    Algorithm:
    1. Collect the union of selected-category ID lists (ranked, deduped).
    2. Build the remainder (all_ids minus the selected set).
    3. Splice 1 serendipity card from the remainder every SERENDIPITY_INTERVAL
       positions into the front-loaded list.
    4. Append remaining all_ids (not already included) for tail coverage.
    5. Return the merged, deduplicated list.
    """
    selected_set: set[str] = set()
    front: list[str] = []
    for cat in selected_categories:
        for cid in cat_ids_map.get(cat, []):
            if cid not in selected_set:
                selected_set.add(cid)
                front.append(cid)

    # Remainder pool: all_ids not in selected set, preserving order
    remainder = [cid for cid in all_ids if cid not in selected_set]
    rem_iter = iter(remainder)

    # Splice serendipity: every SERENDIPITY_INTERVAL cards in front, inject 1
    merged: list[str] = []
    seen_merged: set[str] = set()
    pos_in_front = 0

    for cid in front:
        merged.append(cid)
        seen_merged.add(cid)
        pos_in_front += 1
        if pos_in_front % SERENDIPITY_INTERVAL == 0:
            # Inject one serendipity card
            sera = next(rem_iter, None)
            if sera is not None and sera not in seen_merged:
                merged.append(sera)
                seen_merged.add(sera)

    # Tail: remaining all_ids not yet in merged
    for cid in all_ids:
        if cid not in seen_merged:
            merged.append(cid)
            seen_merged.add(cid)

    return merged


# ---------------------------------------------------------------------------
# Nationality blend (§13P.3)
# ---------------------------------------------------------------------------


def _blend_by_nationality(
    base_ids: list[str],
    nat_id_set: set[str],
) -> list[str]:
    """Interleave *base_ids* at NAT_BLEND_HOME : NAT_BLEND_WORLD ratio.

    Partitions *base_ids* (already in score/category order) into two sublists:
      home  — IDs present in *nat_id_set* (nationality-matching), order preserved
      world — all remaining IDs, order preserved

    Then interleaves them: emit NAT_BLEND_HOME home cards, then NAT_BLEND_WORLD
    world card, repeat.  Drain whichever sublist empties first with the remainder
    of the other.  This gives roughly 2:1 home/world ratio throughout the full
    list, so the day/seed band-shuffle that follows preserves the blend within
    each band.

    Args:
        base_ids:   Ordered ID list (output of category soft-boost or all_ids).
        nat_id_set: Set of IDs that include the requested nationality.

    Returns:
        Blended ID list, same length as *base_ids*, no duplicates.
    """
    home: list[str] = []
    world: list[str] = []
    for cid in base_ids:
        if cid in nat_id_set:
            home.append(cid)
        else:
            world.append(cid)

    result: list[str] = []
    hi = 0   # home index
    wi = 0   # world index

    while hi < len(home) or wi < len(world):
        # Emit up to NAT_BLEND_HOME home cards
        for _ in range(NAT_BLEND_HOME):
            if hi < len(home):
                result.append(home[hi])
                hi += 1
        # Emit up to NAT_BLEND_WORLD world cards
        for _ in range(NAT_BLEND_WORLD):
            if wi < len(world):
                result.append(world[wi])
                wi += 1

    return result


# ---------------------------------------------------------------------------
# Redis helpers
# ---------------------------------------------------------------------------


def _get_meta(r) -> dict | None:
    """Read and parse feed:meta:v2, return None if absent."""
    raw = r.get(FEED_META_KEY)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def _get_id_array(r, key: str, cache_key: str) -> list[str] | None:
    """Read, parse and cache a JSON ID array from Redis."""
    # Include the Redis client identity so test doubles and separately-created
    # clients cannot reuse an array from another data source. In production the
    # client is shared, preserving the parsing cache for the normal hot path.
    scoped_cache_key = f"{id(r)}:{cache_key}"
    if scoped_cache_key in _id_array_cache:
        return _id_array_cache[scoped_cache_key]
    raw = r.get(key)
    if raw is None:
        return None
    try:
        ids: list[str] = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    _id_array_cache[scoped_cache_key] = ids
    return ids


def _feed_is_ready(r) -> dict | None:
    """Return current-version metadata when every required base key exists."""
    meta = _get_meta(r)
    if meta is None or meta.get("content_version") != FEED_CONTENT_VERSION:
        return None

    raw_ids = r.get(FEED_ORDER_ALL_KEY)
    try:
        if not isinstance(json.loads(raw_ids), list):
            return None
    except (json.JSONDecodeError, TypeError):
        return None

    if not r.exists(FEED_CARDMAP_KEY):
        return None
    return meta


def ensure_feed_ready() -> bool:
    """Rebuild a missing or incomplete feed once, then verify it is usable."""
    with _feed_rebuild_lock:
        try:
            r = get_redis()
            if _feed_is_ready(r) is not None:
                return True
            redis_lock = r.lock(
                FEED_REBUILD_LOCK_KEY,
                timeout=FEED_REBUILD_LOCK_TIMEOUT_SECONDS,
                blocking_timeout=FEED_REBUILD_LOCK_TIMEOUT_SECONDS,
            )
            if not redis_lock.acquire():
                return _feed_is_ready(r) is not None
        except Exception:
            logger.warning("Unable to check or lock feed recovery", exc_info=True)
            return False

        try:
            # Another worker may have rebuilt while this worker waited.
            if _feed_is_ready(r) is not None:
                return True

            logger.warning("Feed data missing or incomplete — rebuilding it now")
            from scripts.feed.generate_feed import run as generate_feed

            generate_feed()
            _id_array_cache.clear()
            return _feed_is_ready(get_redis()) is not None
        except Exception:
            logger.exception("Feed rebuild failed")
            return False
        finally:
            try:
                redis_lock.release()
            except Exception:
                logger.warning("Could not release feed rebuild lock", exc_info=True)


async def _get_ready_feed() -> tuple[object, dict] | None:
    """Return Redis + metadata, rebuilding on demand if the pool is missing."""
    try:
        r = get_redis()
        meta = _feed_is_ready(r)
    except Exception:
        logger.warning("Redis unavailable reading %s", FEED_META_KEY, exc_info=True)
        return None

    if meta is not None:
        return r, meta

    if not await asyncio.to_thread(ensure_feed_ready):
        return None

    try:
        r = get_redis()
        meta = _feed_is_ready(r)
        return (r, meta) if meta is not None else None
    except Exception:
        logger.warning("Redis unavailable after feed rebuild", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.get("/feed")
@limiter.limit("120/minute")
async def get_feed(
    request: Request,
    cursor: int = Query(default=0, ge=0, description="Offset into the shuffled feed."),
    limit: int = Query(
        default=20, ge=1, le=MAX_LIMIT,
        description="Number of cards to return.",
    ),
    lang: str = Query(
        default="en", max_length=5,
        description="Language code (forward-compat; currently only EN data).",
    ),
    categories: Optional[str] = Query(
        default=None,
        description="Comma-separated taxonomy ids to soft-boost "
                    "(politics, business, sport, science_education, arts).",
    ),
    nationality: Optional[str] = Query(
        default=None,
        description="ISO-3166 alpha-2 code to soft-boost (e.g. 'IT', 'FR'). "
                    "Blends ~2/3 home-nationality cards with ~1/3 worldwide. "
                    "Ignored if not present in feed meta's 'countries' list.",
    ),
    seed: Optional[int] = Query(
        default=None,
        description="Shuffle seed. Omit for the day-seeded (cacheable) order; "
                    "the SPA sends a per-visit random seed so each refresh "
                    "reshuffles while pagination stays stable within the visit.",
    ),
    types: Optional[str] = Query(
        default=None,
        description="Comma-separated card archetypes to restrict the feed to "
                    "(relationship, dynasty, organization). Hard filter — only "
                    "cards of the selected types are returned. Omitted or all "
                    "three selected means no filtering.",
    ),
) -> dict:
    """Return a paginated slice of the daily-shuffled connections feed (v2).

    Cards are pre-generated by ``scripts/feed/generate_feed.py`` and stored in
    Redis v2 keys.  If ``feed:meta:v2`` is absent the endpoint returns HTTP 503.

    When ``categories`` is supplied, selected-category cards are front-loaded
    with a serendipity card spliced in every 6 positions from the remainder.

    When ``nationality`` is supplied (valid ISO-2 code present in meta), the
    base ordered list is blended 2:1 home/world using ``_blend_by_nationality``.
    The blend and category boost compose: category soft-boost is applied first,
    then nationality blend is applied to the result.
    """
    # ------------------------------------------------------------------
    # 1. Ensure the generated pool is present. This self-heals a Redis
    # restart/flush instead of making the UI stay on its warming-up screen.
    # ------------------------------------------------------------------
    feed_source = await _get_ready_feed()
    if feed_source is None:
        raise HTTPException(status_code=503, detail="feed not generated")
    r, meta = feed_source

    generated_at: str = meta.get("generated_at", "")

    # ------------------------------------------------------------------
    # 2. Parse and validate categories param
    # ------------------------------------------------------------------
    selected_categories: list[str] = []
    if categories:
        for cat in categories.split(","):
            cat = cat.strip()
            if cat in VALID_CATEGORIES:
                selected_categories.append(cat)
        # Deduplicate while preserving order
        seen: set[str] = set()
        selected_categories = [
            c for c in selected_categories
            if not (c in seen or seen.add(c))  # type: ignore[func-returns-value]
        ]

    # ------------------------------------------------------------------
    # 2b. Parse and validate nationality param
    # ------------------------------------------------------------------
    nat_code: str | None = None
    if nationality and _NAT_CODE_RE.match(nationality):
        cc = nationality.upper()
        meta_countries: list[str] = meta.get("countries") or []
        if cc in meta_countries:
            nat_code = cc

    # ------------------------------------------------------------------
    # 2c. Parse and validate types param (hard filter by card archetype)
    # ------------------------------------------------------------------
    selected_types: set[str] = set()
    if types:
        for tp in types.split(","):
            tp = tp.strip()
            if tp in VALID_TYPES:
                selected_types.add(tp)

    # ------------------------------------------------------------------
    # 3. Build ID order
    # ------------------------------------------------------------------
    cache_prefix = f"{generated_at}:"
    all_ids = _get_id_array(r, FEED_ORDER_ALL_KEY, cache_prefix + "all")
    if all_ids is None:
        raise HTTPException(status_code=503, detail="feed not generated")

    if selected_categories:
        # Load per-category id arrays
        cat_ids_map: dict[str, list[str]] = {}
        for cat in selected_categories:
            key = f"{FEED_ORDER_CAT_PREFIX}{cat}:v2"
            cat_ids = _get_id_array(r, key, cache_prefix + cat)
            cat_ids_map[cat] = cat_ids or []

        ordered_ids = _build_ordered_ids(all_ids, cat_ids_map, selected_categories)
    else:
        ordered_ids = all_ids

    # Apply band shuffle FIRST (per-visit variety), then the nationality blend.
    # Blending last keeps the 2:1 home/world ratio intact — if we blended before
    # shuffling, the band-shuffle would dilute it for nationalities with few
    # cards (the home cards get spread across a wide band). The home/world
    # sublists are taken from the already-shuffled list, so per-visit variety is
    # preserved while the ratio stays exact.
    ordered_ids = _shuffled_ids(ordered_ids, seed)

    # ------------------------------------------------------------------
    # 3b. Apply card-type hard filter (order-preserving) — BEFORE the
    #     nationality blend so the home/world ratio is computed within the
    #     filtered set. If we filtered after blending, removing the other card
    #     types would distort the ratio (home cards of the kept type cluster at
    #     the top, then a long worldwide tail).
    # ------------------------------------------------------------------
    # Only filter when a strict subset is selected. Selecting all types (or
    # none) means "show everything". If the per-type id arrays are absent from
    # Redis (feed not yet regenerated with type arrays) we skip filtering rather
    # than return an empty feed — graceful degradation.
    if selected_types and not VALID_TYPES.issubset(selected_types):
        allowed: set[str] = set()
        found_any = False
        for tp in selected_types:
            tp_key = f"{FEED_ORDER_TYPE_PREFIX}{tp}:v2"
            tp_ids = _get_id_array(r, tp_key, cache_prefix + f"type:{tp}")
            if tp_ids is not None:
                found_any = True
                allowed.update(tp_ids)
        if found_any:
            ordered_ids = [cid for cid in ordered_ids if cid in allowed]

    # ------------------------------------------------------------------
    # 3c. Nationality soft boost (applied last, to the type-filtered set)
    # ------------------------------------------------------------------
    # A nationality selection means "prioritize cards involving this country",
    # not "hide the rest of the world". If the nat id array is missing from
    # Redis we skip the boost rather than returning an empty or partial feed.
    if nat_code is not None:
        nat_key = f"{FEED_ORDER_NAT_PREFIX}{nat_code}:v2"
        nat_cache_key = cache_prefix + f"nat:{nat_code}"
        nat_ids_list = _get_id_array(r, nat_key, nat_cache_key)
        if nat_ids_list:
            nat_id_set = set(nat_ids_list)
            ordered_ids = _blend_by_nationality(ordered_ids, nat_id_set)

    # ------------------------------------------------------------------
    # 4. Slice page window and hydrate card bodies
    # ------------------------------------------------------------------
    total = len(ordered_ids)
    page_ids = ordered_ids[cursor : cursor + limit]
    next_cursor: int | None = cursor + limit if cursor + limit < total else None

    if not page_ids:
        return {"cards": [], "next_cursor": next_cursor, "total": total}

    # HMGET returns values in the same order as requested fields, None for missing
    try:
        raw_bodies = r.hmget(FEED_CARDMAP_KEY, page_ids)
    except Exception:
        logger.warning("Redis HMGET failed", exc_info=True)
        raise HTTPException(status_code=503, detail="feed not generated")

    cards: list[dict] = []
    for raw in raw_bodies:
        if raw is None:
            continue
        try:
            card = json.loads(raw)
            cards.append(card)
        except (json.JSONDecodeError, TypeError):
            continue

    return {
        "cards":       cards,
        "next_cursor": next_cursor,
        "total":       total,
    }


# ---------------------------------------------------------------------------
# Single-card endpoint (deep-link / share target)
# ---------------------------------------------------------------------------


@router.get("/feed/card/{card_id}")
@limiter.limit("120/minute")
async def get_feed_card(request: Request, card_id: str) -> dict:
    """Return a single feed card body by id.

    Used by shared deep links (``/share?card=<id>`` → ``/?feed&card=<id>``) so
    the SPA can open that specific card first.  The returned shape is identical
    to one element of the ``cards`` array from ``GET /api/feed``.

    Card bodies live in the Redis HASH ``feed:cardmap:v2`` (FEED_CARDMAP_KEY),
    keyed by card id.

    Returns:
        200  the parsed card dict
        404  {"detail": "card not found"}      — id valid but absent
        503  {"detail": "feed not generated"}  — redis/meta unavailable
    """
    # Validate id against the shared hex pattern (mirrors share.py).
    if not _CARD_ID_RE.match(card_id):
        raise HTTPException(status_code=404, detail="card not found")

    # Shared links need the same self-healing behaviour as the main feed.
    feed_source = await _get_ready_feed()
    if feed_source is None:
        raise HTTPException(status_code=503, detail="feed not generated")
    r, _meta = feed_source

    try:
        raw = r.hget(FEED_CARDMAP_KEY, card_id)
    except Exception:
        logger.warning("Redis HGET failed for card %s", card_id, exc_info=True)
        raise HTTPException(status_code=503, detail="feed not generated")

    if raw is None:
        raise HTTPException(status_code=404, detail="card not found")

    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(status_code=404, detail="card not found")
