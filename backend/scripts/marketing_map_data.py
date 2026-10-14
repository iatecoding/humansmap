#!/usr/bin/env python3
"""Marketing map data miner — read-only country counts for the world choropleth.

The "map lane" for r/MapPorn (6.4M subscribers): a world map of where the
graph's notable people come from. This script produces the single input the
map needs — a JSON mapping of ISO-3166-1 alpha-2 country code -> count.

Definition of a "notable person" here matches the rest of the marketing
assets: a person document with a non-empty ``nationalities`` array and at
least ``--min-statements`` (default 50) Wikidata statements. People are
bucketed by the first nationality in their array, exactly as
``marketing_insights.build_countries`` does.

It never writes to ArangoDB. The one query is a read-only aggregation
(``FILTER`` + ``COLLECT``); there is no unbounded write. Run it manually
inside the backend container:

    docker compose exec -T backend python - --out-dir /tmp/map \\
        < backend/scripts/marketing_map_data.py
    docker compose exec -T backend cat /tmp/map/countries.json \\
        > docs/marketing/map/countries.json

Flags:
    --out-dir PATH       output directory (default: docs/marketing/map)
    --min-statements N   Wikidata statement floor (default: 50)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# When run as a file inside the container (``python /app/scripts/...``) this
# resolves to /app. When piped over stdin (``python -``) ``__file__`` is
# "<stdin>" and the container's cwd (/app) is already on sys.path, so the
# fallback keeps both invocation styles working.
try:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
except (NameError, OSError):  # pragma: no cover - stdin invocation
    sys.path.insert(0, str(Path.cwd()))

from app.database import get_arango_client, get_db  # noqa: E402

MIN_STATEMENTS = 50

# Read-only aggregation. COLLECT is inherently bounded by the cardinality of
# nationalities, not by the number of persons returned.
COUNTRY_COUNTS_AQL = """
FOR p IN persons
  FILTER LENGTH(p.nationalities) > 0 AND p.wikidata_statements_count >= @min_statements
  COLLECT code = p.nationalities[0] WITH COUNT INTO n
  SORT n DESC
  RETURN {code: code, value: n}
"""


def fetch_country_counts(db, min_statements: int = MIN_STATEMENTS) -> dict[str, int]:
    """Return an ordered ``{ISO2: count}`` mapping, largest count first."""
    rows = db.aql.execute(COUNTRY_COUNTS_AQL, bind_vars={"min_statements": min_statements})
    counts: dict[str, int] = {}
    for row in rows:
        code = row.get("code")
        if not isinstance(code, str):
            continue
        code = code.strip().upper()
        if len(code) == 2 and code.isalpha():
            counts[code] = int(row.get("value") or 0)
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default="docs/marketing/map")
    parser.add_argument("--min-statements", type=int, default=MIN_STATEMENTS)
    args = parser.parse_args()

    db = get_db(get_arango_client(request_timeout=180))
    counts = fetch_country_counts(db, args.min_statements)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "countries.json"
    # Insertion order is descending count, which makes the file readable.
    path.write_text(json.dumps(counts, indent=2, ensure_ascii=False) + "\n")

    total = sum(counts.values())
    top = list(counts.items())[:8]
    print(f"wrote {path} ({len(counts)} countries, {total:,} people)")
    print("top:", json.dumps(top, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
