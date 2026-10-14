"""Admission control for database-backed server-rendered pages.

SSR pages are requested heavily by crawlers, but each uncached page requires
multiple synchronous ArangoDB queries.  Keep that work bounded per Uvicorn
worker so a burst cannot consume every socket or thread in the process.
"""

import asyncio
import os

from fastapi.responses import HTMLResponse


def _max_in_flight() -> int:
    raw = os.getenv("SSR_MAX_IN_FLIGHT", "4")
    try:
        return max(1, min(32, int(raw)))
    except ValueError:
        return 4


_SSR_MAX_IN_FLIGHT = _max_in_flight()
_admission_lock = asyncio.Lock()
_in_flight = 0


async def try_acquire() -> bool:
    """Claim an SSR slot without waiting behind other requests."""
    global _in_flight
    async with _admission_lock:
        if _in_flight >= _SSR_MAX_IN_FLIGHT:
            return False
        _in_flight += 1
        return True


async def release() -> None:
    """Release a previously claimed SSR slot."""
    global _in_flight
    async with _admission_lock:
        _in_flight = max(0, _in_flight - 1)


def overloaded_response() -> HTMLResponse:
    """Return a retryable response without starting another database query."""
    return HTMLResponse(
        content="<h1>Temporarily busy</h1><p>Please retry shortly.</p>",
        status_code=503,
        headers={
            "Cache-Control": "no-store",
            "Retry-After": "5",
        },
    )
