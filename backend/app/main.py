"""FastAPI application — HumansMap API."""

import json
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.middleware.base import BaseHTTPMiddleware

from app.rate_limit import limiter
from app.release import APP_BUILD_SHA


class UTF8JSONResponse(JSONResponse):
    """JSONResponse that serialises non-ASCII characters as-is instead of \\uXXXX escapes."""

    def render(self, content: Any) -> bytes:
        return json.dumps(content, ensure_ascii=False).encode("utf-8")


from app.database import close_arango_clients, get_arango_client, get_db, init_db
from app.config import settings
from app.routers import persons, organizations, events, graph, search, edge_types, stats, images, game, admin, featured, report, share, leaderboard
from app.routers import trivia_common
from app.routers import og_image, person_page, organization_page, sitemap
from app.routers import parliament
from app.routers import feed

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Max request body size: 1 MB (prevents abuse via large POST bodies)
MAX_REQUEST_SIZE = 1_048_576


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests with bodies larger than MAX_REQUEST_SIZE."""

    async def dispatch(self, request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > MAX_REQUEST_SIZE:
            return JSONResponse(
                status_code=413,
                content={"detail": "Request body too large"},
            )
        return await call_next(request)


class ReleaseIdentityMiddleware(BaseHTTPMiddleware):
    """Expose the exact backend build serving every response."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-HumansMap-Build"] = APP_BUILD_SHA
        return response


# --- Cloudflare CDN Cache-Control headers ---
# Maps route prefixes to (max-age in seconds, public or private).
# Cloudflare free tier respects Cache-Control on "Cache Everything" rules.
# Trivia/admin/game-challenge are private (randomized or authenticated).
_CACHE_RULES: list[tuple[str, int | None]] = [
    ("/api/stats",                3600),    # 1h  — rarely changes
    ("/api/edge-types",           86400),   # 24h — almost never changes
    # /api/featured is intentionally excluded — it shuffles on every call, must not be CDN-cached
    ("/api/persons/",             86400),   # 24h — person detail is very stable (sync weekly)
    ("/api/graph/neighborhood/",  3600),    # 1h  — graph data stable between weekly syncs
    ("/api/graph/path/",          3600),    # 1h  — paths don't change
    ("/api/graph/org-members/",   3600),    # 1h  — sorted by notability, stable between syncs
    ("/api/graph/cluster/",       3600),    # 1h  — stable between syncs
    ("/api/search",               900),     # 15min — popular queries repeat
    ("/api/images/",              604800),  # 7 days — images are immutable once cached
    ("/api/game/nationalities",   86400),   # 24h
    ("/api/leaderboard/",         86400),   # 24h — stable between weekly syncs
    ("/api/parliament/hemicycle/", 300),    # 5min — parliament membership stable short-term
    ("/api/feed",                  300),    # 5min — feed cards change daily; short CDN TTL
    # Everything else (/api/trivia, /api/admin, /api/game/challenge, /share, /api/health)
    # gets no cache header → Cloudflare won't cache it.
]


class CDNCacheMiddleware(BaseHTTPMiddleware):
    """Set Cache-Control headers for successful GET responses so Cloudflare caches them."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        # Only cache successful GET responses
        if request.method != "GET" or response.status_code >= 400:
            return response

        path = request.url.path

        for prefix, max_age in _CACHE_RULES:
            if path.startswith(prefix):
                response.headers["Cache-Control"] = f"public, max-age={max_age}, s-maxage={max_age}"
                return response

        # No rule matched — tell Cloudflare not to cache
        if path.startswith("/api/"):
            response.headers["Cache-Control"] = "private, no-store"

        return response


def _ensure_feed() -> None:
    """Ensure the durable feed pool is ready during application startup."""
    if not feed.ensure_feed_ready():
        logger.error("Feed is unavailable at startup; requests will retry the rebuild on demand.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing database...")
    init_db()
    logger.info("Database ready")
    # Feed recovery is durable background maintenance. Do not block every API
    # worker (and therefore the health endpoint) while a missing feed rebuilds.
    app.state.feed_startup_task = asyncio.create_task(asyncio.to_thread(_ensure_feed))
    try:
        yield
    finally:
        close_arango_clients()


app = FastAPI(
    title="HumansMap API",
    version="0.1.0",
    lifespan=lifespan,
    default_response_class=UTF8JSONResponse,
)

# --- Rate limiter ---
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# --- CORS ---
# Parse comma-separated origins from env var
allowed_origins = [o.strip() for o in settings.allowed_origins.split(",") if o.strip()]
logger.info("CORS allowed origins: %s", allowed_origins)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

# --- Request size limit ---
app.add_middleware(RequestSizeLimitMiddleware)

# --- Release identity (detect stale production containers) ---
app.add_middleware(ReleaseIdentityMiddleware)

# --- CDN cache headers (Cloudflare) ---
app.add_middleware(CDNCacheMiddleware)

# --- Routers ---
app.include_router(persons.router)
app.include_router(organizations.router)
app.include_router(events.router)
app.include_router(graph.router)
app.include_router(search.router)
app.include_router(edge_types.router)
app.include_router(stats.router)
app.include_router(images.router)
app.include_router(game.router)
app.include_router(trivia_common.router)
app.include_router(featured.router)
app.include_router(admin.router)
app.include_router(report.router)
app.include_router(share.router)
app.include_router(leaderboard.router)
app.include_router(og_image.router)
app.include_router(person_page.router)
app.include_router(organization_page.router)
app.include_router(sitemap.router)
app.include_router(parliament.router)
app.include_router(feed.router)


@app.get("/api/health")
async def health():
    def check_db() -> str:
        try:
            client = get_arango_client(request_timeout=2)
            db = get_db(client)
            db.properties()
            return "ok"
        except Exception:
            return "error"

    def check_cache() -> str:
        try:
            from app.services.cache import get_redis
            get_redis().ping()
            return "ok"
        except Exception:
            return "error"

    db_status, cache_status = await asyncio.gather(
        asyncio.to_thread(check_db),
        asyncio.to_thread(check_cache),
    )

    overall = "ok" if db_status == "ok" and cache_status == "ok" else "degraded"
    return {
        "status": overall,
        "db": db_status,
        "cache": cache_status,
        "build": APP_BUILD_SHA,
    }
