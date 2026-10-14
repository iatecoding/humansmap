#!/usr/bin/env python3
"""Materialize production sitemap XML files outside the request path."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json
import logging

from app.services.sitemap_snapshot import build_sitemap_snapshot


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


if __name__ == "__main__":
    print(json.dumps(build_sitemap_snapshot(), sort_keys=True))
