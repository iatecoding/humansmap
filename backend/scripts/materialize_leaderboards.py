#!/usr/bin/env python3
"""Rebuild materialized organization leaderboard counts.

Run after ingestion completes. Dry-run is the default; pass ``--apply`` to
perform database writes and invalidate leaderboard cache entries.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

app_root = Path(__file__).resolve().parents[1]
if str(app_root) not in sys.path:
    sys.path.insert(0, str(app_root))

from app.database import get_arango_client, get_db
from app.services.leaderboard_materialization import refresh_org_affiliation_counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the materialized counts")
    args = parser.parse_args()

    db = get_db(get_arango_client(request_timeout=600))
    if not args.apply:
        counts = {
            name: db.collection(name).count()
            for name in ("organizations", "affiliations")
        }
        print({"dry_run": True, **counts})
        print("No changes made. Re-run with --apply after ingestion is idle.")
        return

    ranked = refresh_org_affiliation_counts(db)
    print({"dry_run": False, "organizations_ranked": ranked})


if __name__ == "__main__":
    main()
