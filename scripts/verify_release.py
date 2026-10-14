#!/usr/bin/env python3
"""Fail unless the public frontend and backend serve the expected release."""

import json
import os
import sys
import urllib.request

BASE_URL = os.getenv("HUMANSMAP_BASE_URL", "https://humansmap.com").rstrip("/")
EXPECTED = os.environ.get("APP_BUILD_SHA", "").strip()


def _json(path: str) -> dict:
    request = urllib.request.Request(
        f"{BASE_URL}{path}", headers={
            "Cache-Control": "no-cache",
            "User-Agent": "HumansMap-Release-Verifier/1.0",
        }
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def main() -> int:
    if not EXPECTED:
        print("APP_BUILD_SHA is required", file=sys.stderr)
        return 2
    try:
        backend = str(_json("/api/health").get("build", ""))
        frontend = str(_json("/release.json").get("build", ""))
    except Exception as exc:
        print(f"release verification request failed: {exc}", file=sys.stderr)
        return 1
    if backend != EXPECTED or frontend != EXPECTED:
        print(
            f"release mismatch: expected={EXPECTED} backend={backend} "
            f"frontend={frontend}",
            file=sys.stderr,
        )
        return 1
    print(f"release verified: {EXPECTED}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
