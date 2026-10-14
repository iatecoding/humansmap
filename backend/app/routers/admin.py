"""Admin endpoints — cache management, internal tooling."""

import hmac
import json
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException

from app.config import settings
from app.services.cache import flush_pattern

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _verify_admin_token(authorization: str | None = Header(None)) -> None:
    if not settings.admin_token:
        raise HTTPException(403, "Admin token not configured")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing Bearer token")
    if not hmac.compare_digest(authorization[7:], settings.admin_token):
        raise HTTPException(403, "Invalid token")


@router.post("/flush-cache")
async def flush_cache(authorization: str | None = Header(None)):
    """Flush all Redis cache keys. Requires Bearer token."""
    _verify_admin_token(authorization)
    flush_pattern("*")
    return {"status": "flushed"}


@router.get("/reports")
async def get_reports(authorization: str | None = Header(None), last: int = 50):
    """Return the last N user reports from reports.log. Requires Bearer token."""
    _verify_admin_token(authorization)
    from app.routers.report import REPORTS_FILE
    if not REPORTS_FILE.exists():
        return {"total": 0, "reports": []}
    lines = REPORTS_FILE.read_text(encoding="utf-8").splitlines()
    parsed = []
    for line in lines:
        line = line.strip()
        if line:
            try:
                parsed.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return {"total": len(parsed), "reports": parsed[-last:]}
