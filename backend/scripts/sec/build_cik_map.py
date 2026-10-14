"""Populate sec_cik_map and backfill sec_cik/sec_ticker on persons/organizations.

§10.2.c of the project design notes.

What it does
------------
1. Runs a single SPARQL query against Wikidata for P5531 (SEC Central Index Key).
2. Caches the raw JSON response under {SEC_DATA_DIR}/sparql_cache/p5531_cik_map.json.
3. Normalises each row (QID extraction, CIK zero-padding, instance_of collapse).
4. Merges manual overrides from `backend/data/sec_cik_overrides.json` for
   entities Wikidata lacks a P5531 statement on (e.g. Amazon). Wikidata wins
   on conflict — overrides only *supplement* missing data.
5. Joins to existing `persons` and `organizations` collections; drops unmatched rows.
6. Upserts into `sec_cik_map` (keyed by 10-digit CIK).
7. Backfills `sec_cik` (and `sec_ticker` for orgs) on the matched documents.
8. Prints a summary and warns loudly if fewer than 1000 rows were matched.

Usage (user runs these — do NOT invoke inside this script)
----------------------------------------------------------
    # Dry run — no DB writes:
    docker compose exec backend python -m scripts.sec.build_cik_map --dry-run

    # Full run:
    docker compose exec backend python -m scripts.sec.build_cik_map

    # Limit to 50 rows (for quick sanity checks):
    docker compose exec backend python -m scripts.sec.build_cik_map --limit 50 --dry-run

    # Force re-fetch from Wikidata even if cache exists:
    docker compose exec backend python -m scripts.sec.build_cik_map --refresh

Flags
-----
--limit N      Cap the number of SPARQL result rows processed (useful for testing).
--dry-run      Parse and join but skip all DB writes.
--refresh      Ignore the on-disk SPARQL cache and re-fetch.
--verbose      Set log level to DEBUG.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.config import settings  # noqa: E402
from app.database import (  # noqa: E402
    ORGANIZATIONS,
    PERSONS,
    SEC_CIK_MAP,
    ensure_collections,
    ensure_database,
    get_arango_client,
)

logger = logging.getLogger("build_cik_map")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SPARQL_ENDPOINT = settings.sparql_endpoint
USER_AGENT = settings.sparql_user_agent

SPARQL_QUERY = """
SELECT ?entity ?entityLabel ?cik ?ticker ?instanceOf WHERE {
  ?entity wdt:P5531 ?cik.
  OPTIONAL { ?entity wdt:P249 ?ticker. }
  OPTIONAL { ?entity wdt:P31 ?instanceOf. }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
""".strip()

WD_ENTITY_PREFIX = "http://www.wikidata.org/entity/"

CACHE_SUBDIR = "sparql_cache"
CACHE_FILENAME = "p5531_cik_map.json"

# Manual CIK overrides for entities Wikidata omits (e.g. Amazon). Shipped in-repo.
# Path is relative to the repo root; resolved from this file's location.
OVERRIDES_PATH = Path(__file__).resolve().parents[2] / "data" / "sec_cik_overrides.json"

BATCH_SIZE = 500

# ---------------------------------------------------------------------------
# Helpers — pure functions, easy to unit-test
# ---------------------------------------------------------------------------


def uri_to_qid(uri: str) -> str | None:
    """Strip Wikidata entity URI prefix and return the QID.

    >>> uri_to_qid("http://www.wikidata.org/entity/Q312")
    'Q312'
    >>> uri_to_qid("Q312")
    'Q312'
    >>> uri_to_qid(None) is None
    True
    """
    if not uri:
        return None
    if uri.startswith(WD_ENTITY_PREFIX):
        return uri[len(WD_ENTITY_PREFIX):]
    return uri


def zero_pad_cik(cik: str | int) -> str:
    """Zero-pad a CIK value to 10 digits.

    >>> zero_pad_cik("320193")
    '0000320193'
    >>> zero_pad_cik(320193)
    '0000320193'
    >>> zero_pad_cik("0000320193")
    '0000320193'
    """
    return str(cik).strip().zfill(10)


def load_overrides(path: Path) -> dict[str, str]:
    """Load the QID→CIK override map from JSON. Missing file is not an error.

    Keys beginning with `_` (e.g. `_comment`) are ignored. Values are zero-padded
    to 10 digits defensively. Only well-formed QIDs (Q + digits) are accepted.
    """
    if not path.exists():
        logger.info("No overrides file at %s — skipping", path)
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to read overrides file %s: %s", path, exc)
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or k.startswith("_"):
            continue
        if not (k.startswith("Q") and k[1:].isdigit()):
            logger.warning("Skipping non-QID override key: %r", k)
            continue
        if not isinstance(v, (str, int)):
            logger.warning("Skipping non-string CIK for %s: %r", k, v)
            continue
        out[k] = zero_pad_cik(v)
    return out


def merge_overrides(
    by_qid: dict[str, dict], overrides: dict[str, str]
) -> int:
    """Insert override rows into by_qid where Wikidata lacks a P5531 statement.

    Returns the count of rows actually added (i.e. QIDs not already in by_qid).
    Wikidata wins on conflict — we only supplement.
    """
    added = 0
    for qid, cik in overrides.items():
        if qid in by_qid:
            logger.debug("Override for %s ignored — Wikidata already has CIK %s",
                         qid, by_qid[qid]["cik"])
            continue
        by_qid[qid] = {
            "qid": qid,
            "cik": cik,
            "ticker": None,
            "label": None,
            "instance_of_list": [],
            "instance_of": None,
            "source": "override",
        }
        added += 1
    return added


def collapse_sparql_rows(rows: list[dict]) -> dict[str, dict]:
    """Collapse SPARQL cartesian-product rows (multiple instanceOf per entity) into one dict per QID.

    Returns a mapping  qid -> {cik, ticker, label, instance_of_list, instance_of}.
    """
    by_qid: dict[str, dict] = {}
    for row in rows:
        entity_uri = row.get("entity", {}).get("value", "")
        qid = uri_to_qid(entity_uri)
        if not qid:
            continue

        cik_raw = row.get("cik", {}).get("value", "")
        cik = zero_pad_cik(cik_raw) if cik_raw else None
        if not cik:
            continue

        ticker_val = row.get("ticker", {}).get("value") or None
        label_val = row.get("entityLabel", {}).get("value") or None
        instance_of_uri = row.get("instanceOf", {}).get("value") or None
        instance_of_qid = uri_to_qid(instance_of_uri) if instance_of_uri else None

        if qid not in by_qid:
            by_qid[qid] = {
                "qid": qid,
                "cik": cik,
                "ticker": ticker_val,
                "label": label_val,
                "instance_of_list": [],
                "instance_of": None,
            }

        entry = by_qid[qid]
        # Prefer first non-null ticker
        if entry["ticker"] is None and ticker_val:
            entry["ticker"] = ticker_val
        # Accumulate instanceOf values
        if instance_of_qid and instance_of_qid not in entry["instance_of_list"]:
            entry["instance_of_list"].append(instance_of_qid)
        # First non-null instanceOf is the canonical one
        if entry["instance_of"] is None and instance_of_qid:
            entry["instance_of"] = instance_of_qid

    return by_qid


# ---------------------------------------------------------------------------
# SPARQL fetch
# ---------------------------------------------------------------------------


def fetch_sparql(query: str, endpoint: str, user_agent: str) -> dict:
    """Run a SPARQL SELECT and return the parsed JSON response dict."""
    params = urllib.parse.urlencode({"query": query, "format": "json"})
    url = f"{endpoint}?{params}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "application/sparql-results+json",
        },
    )
    logger.info("Fetching SPARQL P5531 query from %s …", endpoint)
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = resp.read()
    return json.loads(body)


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------


def cache_path() -> Path:
    return Path(settings.sec_data_dir) / CACHE_SUBDIR / CACHE_FILENAME


def load_cache() -> dict | None:
    p = cache_path()
    if p.exists():
        logger.info("Loading SPARQL result from cache: %s", p)
        return json.loads(p.read_bytes())
    return None


def save_cache(data: dict) -> None:
    p = cache_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf-8")
    logger.info("Cached SPARQL response to %s", p)


# ---------------------------------------------------------------------------
# ArangoDB helpers
# ---------------------------------------------------------------------------


def qids_in_collection(db, collection_name: str) -> set[str]:
    """Return the set of `_key` values (QIDs) in a collection."""
    aql = f"FOR doc IN {collection_name} RETURN doc._key"
    cursor = db.aql.execute(aql, count=True)
    return set(cursor)


def upsert_cik_map_batch(db, batch: list[dict], dry_run: bool) -> None:
    """Upsert a batch of sec_cik_map documents using AQL INSERT … OPTIONS { overwrite: true }."""
    if dry_run:
        logger.debug("DRY RUN: would upsert %d rows into %s", len(batch), SEC_CIK_MAP)
        return
    aql = f"""
    FOR doc IN @batch
      INSERT doc INTO {SEC_CIK_MAP}
      OPTIONS {{ overwrite: true }}
    """
    db.aql.execute(aql, bind_vars={"batch": batch})


def backfill_collection(db, collection_name: str, updates: list[dict], dry_run: bool) -> None:
    """Apply sec_cik / sec_ticker backfill to an existing collection in batches.

    Each item in `updates` must have at least `_key` and `sec_cik`.
    """
    if not updates:
        return

    for i in range(0, len(updates), BATCH_SIZE):
        chunk = updates[i : i + BATCH_SIZE]
        if dry_run:
            logger.debug(
                "DRY RUN: would backfill %d docs in %s", len(chunk), collection_name
            )
            continue
        aql = f"""
        FOR upd IN @updates
          UPDATE {{ _key: upd._key, sec_cik: upd.sec_cik, sec_ticker: upd.sec_ticker }}
          IN {collection_name}
          OPTIONS {{ ignoreErrors: true }}
        """
        db.aql.execute(aql, bind_vars={"updates": chunk})
    logger.info("Backfilled %d documents in %s", len(updates), collection_name)


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------


def run(args: argparse.Namespace) -> int:
    """Execute the CIK map build. Returns exit code (0 = success)."""

    # --- Fetch or load from cache -------------------------------------------
    if not args.refresh:
        cached = load_cache()
    else:
        cached = None

    if cached is None:
        try:
            raw = fetch_sparql(SPARQL_QUERY, SPARQL_ENDPOINT, USER_AGENT)
        except Exception as exc:
            logger.error("SPARQL fetch failed: %s", exc)
            return 1
        save_cache(raw)
    else:
        raw = cached

    rows = raw.get("results", {}).get("bindings", [])
    logger.info("SPARQL returned %d raw rows (before dedup)", len(rows))

    if args.limit:
        rows = rows[: args.limit]
        logger.info("Limiting to first %d rows (--limit)", args.limit)

    # --- Collapse multi-instanceOf rows -------------------------------------
    by_qid = collapse_sparql_rows(rows)
    total_sparql = len(by_qid)
    logger.info("%d unique QIDs after row collapse", total_sparql)

    # --- Merge manual overrides (for entities Wikidata lacks P5531 on) ------
    overrides_path = Path(args.overrides_file) if args.overrides_file else OVERRIDES_PATH
    overrides = load_overrides(overrides_path)
    overrides_added = merge_overrides(by_qid, overrides)
    if overrides:
        logger.info(
            "Overrides: %d entries loaded, %d added (rest ignored — Wikidata already had the CIK)",
            len(overrides),
            overrides_added,
        )

    if not by_qid:
        logger.error("No usable QID/CIK pairs extracted from SPARQL results.")
        return 1

    # --- Connect to DB and load QID sets ------------------------------------
    if args.dry_run:
        logger.info("DRY RUN mode — connecting to DB for QID lookup only")

    client = get_arango_client()
    db = ensure_database(client)
    ensure_collections(db)

    logger.info("Loading QID sets from persons and organizations …")
    person_qids = qids_in_collection(db, PERSONS)
    org_qids = qids_in_collection(db, ORGANIZATIONS)
    logger.info(
        "persons: %d QIDs, organizations: %d QIDs", len(person_qids), len(org_qids)
    )

    # --- Join & classify -----------------------------------------------------
    now_iso = datetime.now(timezone.utc).isoformat()

    cik_map_docs: list[dict] = []
    org_backfill: list[dict] = []
    person_backfill: list[dict] = []
    dropped = 0

    for qid, entry in by_qid.items():
        cik = entry["cik"]
        ticker = entry["ticker"]

        if qid in org_qids:
            entity_kind = "org"
            org_backfill.append({"_key": qid, "sec_cik": cik, "sec_ticker": ticker})
        elif qid in person_qids:
            entity_kind = "person"
            person_backfill.append({"_key": qid, "sec_cik": cik, "sec_ticker": ticker})
        else:
            dropped += 1
            logger.debug("Dropped QID %s (not in persons or organizations)", qid)
            continue

        cik_map_docs.append(
            {
                "_key": cik,
                "wikidata_id": qid,
                "entity_kind": entity_kind,
                "ticker": ticker,
                "last_synced": now_iso,
                "filings_count": 0,
            }
        )

    org_matches = len(org_backfill)
    person_matches = len(person_backfill)
    total_matched = len(cik_map_docs)

    logger.info(
        "Join result: orgs=%d  persons=%d  dropped=%d  total_matched=%d",
        org_matches,
        person_matches,
        dropped,
        total_matched,
    )

    # --- Write sec_cik_map --------------------------------------------------
    # Full rebuild from Wikidata + overrides → truncate first to avoid
    # unique-index (wikidata_id) collisions when a QID's canonical CIK
    # shifts between runs. The CIK is the _key, so an INSERT-overwrite
    # matched by _key can't update a row whose _key changed.
    if not args.dry_run:
        logger.info("Truncating %s before rebuild …", SEC_CIK_MAP)
        db.collection(SEC_CIK_MAP).truncate()

    logger.info("Upserting %d rows into %s …", total_matched, SEC_CIK_MAP)
    for i in range(0, total_matched, BATCH_SIZE):
        chunk = cik_map_docs[i : i + BATCH_SIZE]
        upsert_cik_map_batch(db, chunk, args.dry_run)
        logger.debug("  upserted batch %d–%d", i, i + len(chunk))

    # --- Backfill organizations and persons ---------------------------------
    logger.info("Backfilling sec_cik on organizations …")
    backfill_collection(db, ORGANIZATIONS, org_backfill, args.dry_run)

    logger.info("Backfilling sec_cik on persons …")
    backfill_collection(db, PERSONS, person_backfill, args.dry_run)

    # --- Verification -------------------------------------------------------
    if not args.dry_run:
        count_cursor = db.aql.execute(
            f"RETURN LENGTH({SEC_CIK_MAP})", count=False
        )
        final_count = next(count_cursor)
        logger.info("Verification: %s contains %d documents", SEC_CIK_MAP, final_count)

        if final_count != total_matched:
            logger.warning(
                "COUNT MISMATCH: expected %d but found %d in %s — possible duplicates or prior data",
                total_matched,
                final_count,
                SEC_CIK_MAP,
            )
    else:
        final_count = total_matched  # reported, not written

    # --- Summary ------------------------------------------------------------
    print("")
    print("=" * 60)
    print("  build_cik_map SUMMARY")
    print("=" * 60)
    print(f"  SPARQL unique QIDs      : {total_sparql:>8,}")
    print(f"  Manual overrides added  : {overrides_added:>8,}")
    print(f"  Org matches             : {org_matches:>8,}")
    print(f"  Person matches          : {person_matches:>8,}")
    print(f"  Dropped (QID not found) : {dropped:>8,}")
    print(f"  sec_cik_map rows        : {final_count:>8,}")
    if args.dry_run:
        print("  MODE: DRY RUN (no DB writes)")
    print("=" * 60)

    if total_matched < 1000:
        print("")
        print("\033[1mWARNING: fewer than 1000 matched rows ({} total).\033[0m".format(total_matched))
        print("  Review the SPARQL results and the persons/organizations")
        print("  collections before running tasks 10.3+.")
        print("  Possible causes:")
        print("    - DB is not yet fully populated (pass-2 still in progress)")
        print("    - SPARQL cache is stale (re-run with --refresh)")
        print("    - --limit flag is active")

    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Populate sec_cik_map from Wikidata P5531 (SEC CIK) SPARQL query "
            "and backfill sec_cik/sec_ticker on existing persons and organizations."
        )
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        metavar="N",
        help="Process only the first N SPARQL rows (0 = no limit; useful for testing).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and join but skip all DB writes.",
    )
    p.add_argument(
        "--refresh",
        action="store_true",
        help="Ignore on-disk SPARQL cache and re-fetch from Wikidata.",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Set log level to DEBUG.",
    )
    p.add_argument(
        "--overrides-file",
        type=str,
        default="",
        metavar="PATH",
        help=(
            "Path to a JSON file mapping QID → CIK for entities Wikidata omits "
            "(e.g. Amazon). Default: backend/data/sec_cik_overrides.json."
        ),
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    sys.exit(run(args))


if __name__ == "__main__":
    main()
