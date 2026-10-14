"""Rate limiting configuration using slowapi."""

from fastapi import Request
from slowapi import Limiter


def get_real_ip(request: Request) -> str:
    """Return the real visitor IP.

    Caddy extracts the real client IP from Cloudflare's X-Forwarded-For
    (after verifying the request came from a trusted Cloudflare IP) and
    forwards it as X-Real-IP. Fall back to the direct connection IP for
    local/dev environments where Caddy is not in the path.
    """
    return request.headers.get("X-Real-IP") or request.client.host


limiter = Limiter(key_func=get_real_ip)
