"""Runtime release identity shared by health checks and cache namespaces."""

import os


APP_BUILD_SHA = (os.getenv("APP_BUILD_SHA") or "dev").strip()


def cache_namespace() -> str:
    """Return a bounded, Redis-safe identity for the running application image."""
    return APP_BUILD_SHA[:64]
