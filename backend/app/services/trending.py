"""Trending people service — Wikipedia pageview-based suggestions per language."""

import json
import logging
import random
import urllib.request
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.database import PERSONS
from app.services.cache import get_cached, set_cached

logger = logging.getLogger(__name__)

# ISO country codes per language — used as fallback when Wikipedia API fails
_NATIONALITY_MAP = {
    "it": ["IT"],
    "en": ["GB", "US", "CA"],
    "fr": ["FR"],
    "de": ["DE"],
    "es": ["ES"],
    "pt": ["PT", "BR"],
    "nl": ["NL"],
    "pl": ["PL"],
    "sv": ["SE"],
}


def _yesterday_path() -> str:
    """Return yesterday's date as YYYY/MM/DD (UTC)."""
    d = datetime.now(timezone.utc) - timedelta(days=1)
    return d.strftime("%Y/%m/%d")


def fetch_wikipedia_top_articles(lang: str) -> list[str]:
    """Fetch top ~1000 article titles from Wikimedia pageview API."""
    date = _yesterday_path()
    url = (
        f"https://wikimedia.org/api/rest_v1/metrics/pageviews/top"
        f"/{lang}.wikipedia/all-access/{date}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": settings.sparql_user_agent})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        articles = data.get("items", [{}])[0].get("articles", [])
        return [a["article"].replace("_", " ") for a in articles]
    except Exception:
        logger.warning("Wikipedia pageview API failed for lang=%s", lang, exc_info=True)
        return []


def match_with_db(titles: list[str], db) -> list[dict]:
    """Batch-query ArangoDB for persons whose name matches any of the given titles."""
    if not titles:
        return []
    cursor = db.aql.execute(
        """
        FOR p IN @@persons
          FILTER p.name_en IN @titles OR p.name_it IN @titles
          FILTER p.wikidata_statements_count >= @min_stmts
          SORT p.wikidata_statements_count DESC
          LIMIT @limit
          RETURN {
            qid: p._key,
            name_it: p.name_it,
            name_en: p.name_en,
            image_cached_path: p.image_cached_path,
            statements_count: p.wikidata_statements_count
          }
        """,
        bind_vars={
            "@persons": PERSONS,
            "titles": titles[:500],  # guard against huge lists
            "min_stmts": settings.trending_min_statements,
            "limit": settings.trending_refresh_count,
        },
    )
    return list(cursor)


def _fallback(lang: str, limit: int, db) -> list[dict]:
    """Return top persons by statement count filtered by nationality."""
    nationalities = _NATIONALITY_MAP.get(lang, _NATIONALITY_MAP["it"])
    cursor = db.aql.execute(
        """
        FOR p IN @@persons
          FILTER LENGTH(INTERSECTION(p.nationalities, @nats)) > 0
          FILTER p.wikidata_statements_count >= @min_stmts
          SORT p.wikidata_statements_count DESC
          LIMIT @limit
          RETURN {
            qid: p._key,
            name_it: p.name_it,
            name_en: p.name_en,
            image_cached_path: p.image_cached_path,
            statements_count: p.wikidata_statements_count
          }
        """,
        bind_vars={
            "@persons": PERSONS,
            "nats": nationalities,
            "min_stmts": settings.trending_min_statements,
            "limit": limit,
        },
    )
    return list(cursor)


def get_trending(lang: str, limit: int, db) -> list[dict]:
    """Return trending persons for the given language, with Redis caching.

    The full pool (up to trending_refresh_count) is cached for 24h.
    Each call shuffles and slices from the pool so refreshing the page
    surfaces different people without hitting the API again.
    """
    cache_key = f"trending:{lang}"
    cached = get_cached(cache_key)
    if cached is None:
        titles = fetch_wikipedia_top_articles(lang)
        results = match_with_db(titles, db) if titles else []

        if not results:
            logger.info("Trending fallback for lang=%s (no Wikipedia matches)", lang)
            results = _fallback(lang, settings.trending_refresh_count, db)

        set_cached(cache_key, results, ttl_seconds=settings.trending_cache_ttl)
        cached = results

    pool = list(cached)
    random.shuffle(pool)
    return pool[:limit]
