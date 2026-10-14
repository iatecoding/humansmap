"""Fetch per-CIK submission metadata from SEC EDGAR and persist to ArangoDB.

§10.4.a of the project design notes.

What it does
------------
1. Reads all org CIKs from sec_cik_map where entity_kind == "org".
2. For each CIK, fetches https://data.sec.gov/submissions/CIK{10digit}.json
   via SECEdgarProvider (reuses on-disk cache + throttle).
3. Extracts top-level metadata fields only — filings.recent is NOT parsed
   (§10.0.b: can be thousands of entries per CIK).
4. Persists a `sec_metadata` sub-doc onto the matching organizations document
   using AQL UPDATE with mergeObjects: false (atomic replacement, idempotent).
5. Supports resumable checkpoint so interrupted runs can continue.

Usage (user runs these — do NOT invoke inside this script)
----------------------------------------------------------
    # Dry run (no DB writes):
    docker compose exec backend python -m scripts.sec.fetch_submissions --dry-run --limit 5

    # Full run:
    docker compose exec backend python -m scripts.sec.fetch_submissions

    # Force re-fetch (ignore on-disk response cache):
    docker compose exec backend python -m scripts.sec.fetch_submissions --refresh

    # Start fresh, ignoring checkpoint:
    docker compose exec backend python -m scripts.sec.fetch_submissions --no-resume

Flags
-----
--limit N      Cap number of CIKs processed (0 = no limit).
--dry-run      Parse metadata but skip all DB writes.
--verbose      Set log level to DEBUG.
--refresh      Ignore on-disk cache; re-fetch from SEC EDGAR.
--resume       Skip already-processed CIKs via checkpoint (default ON).
--no-resume    Disable checkpoint; process all CIKs.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.config import settings  # noqa: E402
from app.database import (  # noqa: E402
    ORGANIZATIONS,
    SEC_CIK_MAP,
    ensure_collections,
    ensure_database,
    get_arango_client,
)
from app.providers.sec_edgar import SecEdgarProvider  # noqa: E402

logger = logging.getLogger("fetch_submissions")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CACHE_SUBDIR = "sparql_cache"
CHECKPOINT_FILENAME = "submissions_checkpoint.json"

# ---------------------------------------------------------------------------
# Pure helpers — easy to unit-test
# ---------------------------------------------------------------------------


def extract_metadata(submissions: dict) -> dict:
    """Extract top-level metadata fields from a submissions JSON dict.

    Only includes keys that are present and non-None in the source dict.
    Never touches ``filings.recent``.

    Returns a plain dict ready to store as ``sec_metadata`` (without
    ``last_synced`` — that is added by :func:`build_update_doc`).

    >>> md = extract_metadata({"sic": "3571", "fiscalYearEnd": "0926"})
    >>> md["sic"]
    '3571'
    >>> md["fiscal_year_end"]
    '0926'
    >>> "filings" not in md
    True
    """
    _FIELD_MAP: list[tuple[str, str]] = [
        ("fiscal_year_end", "fiscalYearEnd"),
        ("sic", "sic"),
        ("sic_description", "sicDescription"),
        ("state_of_incorporation", "stateOfIncorporation"),
        ("state_of_incorporation_description", "stateOfIncorporationDescription"),
        ("ein", "ein"),
        ("lei", "lei"),
        ("entity_type", "entityType"),
        ("category", "category"),
        ("exchanges", "exchanges"),
        ("tickers", "tickers"),
        ("insider_transaction_for_issuer_exists", "insiderTransactionForIssuerExists"),
        ("addresses", "addresses"),
    ]

    result: dict[str, Any] = {}

    for dest_key, src_key in _FIELD_MAP:
        if src_key in submissions and submissions[src_key] is not None:
            result[dest_key] = submissions[src_key]

    # former_names: transform list[{name, from, to}] keeping ISO strings as-is
    former_names_raw = submissions.get("formerNames")
    if former_names_raw is not None:
        # Always include the key; empty list is valid
        result["former_names"] = [
            {
                "name": entry.get("name"),
                "from": entry.get("from"),
                "to": entry.get("to"),
            }
            for entry in (former_names_raw or [])
        ]

    return result


def build_update_doc(metadata: dict) -> dict:
    """Combine extracted metadata with a UTC last_synced timestamp.

    This is the dict stored under ``sec_metadata`` on the org document.

    >>> doc = build_update_doc({"sic": "3571"})
    >>> isinstance(doc["last_synced"], str)
    True
    >>> doc["sic"]
    '3571'
    """
    doc = dict(metadata)
    doc["last_synced"] = datetime.now(timezone.utc).isoformat()
    return doc


# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------


def checkpoint_path() -> Path:
    return Path(settings.sec_data_dir) / CACHE_SUBDIR / CHECKPOINT_FILENAME


def load_checkpoint() -> set[str]:
    """Return the set of already-processed CIKs."""
    p = checkpoint_path()
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_bytes())
        return set(data.get("processed_ciks", []))
    except Exception as exc:
        logger.warning("Could not read checkpoint file (%s); starting fresh", exc)
        return set()


def save_checkpoint(processed: set[str]) -> None:
    p = checkpoint_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "processed_ciks": sorted(processed),
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# ArangoDB helpers
# ---------------------------------------------------------------------------


def load_org_ciks(db) -> list[tuple[str, str]]:
    """Return list of (cik, wikidata_id) for all org entries in sec_cik_map."""
    aql = f"""
    FOR doc IN {SEC_CIK_MAP}
      FILTER doc.entity_kind == "org"
      FILTER doc.wikidata_id != null
      RETURN [doc._key, doc.wikidata_id]
    """
    cursor = db.aql.execute(aql)
    return [tuple(row) for row in cursor]


def persist_metadata(db, wikidata_id: str, update_doc: dict, dry_run: bool) -> bool:
    """Write sec_metadata onto the organizations document for wikidata_id.

    Uses UPDATE ... WITH { sec_metadata: @doc } ... OPTIONS { mergeObjects: false }
    so the entire sec_metadata sub-doc is replaced atomically on each run.

    Returns True if the org document was found and updated, False otherwise.
    """
    if dry_run:
        logger.debug(
            "DRY RUN: would update %s/%s sec_metadata", ORGANIZATIONS, wikidata_id
        )
        return True

    aql = f"""
    UPDATE @key
      WITH {{ sec_metadata: @doc }}
      IN {ORGANIZATIONS}
      OPTIONS {{ mergeObjects: false }}
      RETURN NEW._key
    """
    cursor = db.aql.execute(aql, bind_vars={"key": wikidata_id, "doc": update_doc})
    result = list(cursor)
    return len(result) > 0


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------


def run(args: argparse.Namespace) -> int:
    """Execute the fetch_submissions pipeline. Returns exit code (0 = success)."""

    # --- Connect to DB -------------------------------------------------------
    client = get_arango_client()
    db = ensure_database(client)
    ensure_collections(db)

    # --- Load CIK → QID mapping from sec_cik_map ----------------------------
    logger.info("Loading org CIKs from %s …", SEC_CIK_MAP)
    cik_qid_pairs: list[tuple[str, str]] = load_org_ciks(db)
    logger.info("Found %d org CIK mappings in %s", len(cik_qid_pairs), SEC_CIK_MAP)

    if not cik_qid_pairs:
        logger.error(
            "No org CIKs found in %s. Run build_cik_map.py first.", SEC_CIK_MAP
        )
        return 1

    # --- Optionally cap the list --------------------------------------------
    if args.limit:
        cik_qid_pairs = cik_qid_pairs[: args.limit]
        logger.info("Limiting to %d CIKs (--limit)", args.limit)

    # --- Load checkpoint ----------------------------------------------------
    processed_ciks: set[str] = set()
    if args.resume:
        processed_ciks = load_checkpoint()
        if processed_ciks:
            logger.info(
                "Checkpoint: %d CIKs already processed (will skip)",
                len(processed_ciks),
            )

    # --- Instantiate provider once ------------------------------------------
    provider = SecEdgarProvider()

    # --- Stats counters -----------------------------------------------------
    stats = {
        "total": len(cik_qid_pairs),
        "skipped_checkpoint": 0,
        "http_errors": 0,
        "org_not_found": 0,
        "updated": 0,
        "dry_run_would_update": 0,
    }
    # Track how many docs have each metadata field populated
    field_counts: dict[str, int] = {}

    # --- Per-CIK fetch loop -------------------------------------------------
    for idx, (cik, wikidata_id) in enumerate(cik_qid_pairs, start=1):
        if args.resume and cik in processed_ciks:
            stats["skipped_checkpoint"] += 1
            logger.debug(
                "CIK %s (%s): skipping (checkpoint)", cik, wikidata_id
            )
            continue

        logger.info(
            "[%d/%d] Fetching submissions for CIK %s (org=%s) …",
            idx, len(cik_qid_pairs), cik, wikidata_id,
        )

        # Fetch — provider handles rate limiting, caching, and backoff
        try:
            if args.refresh:
                # Bust the on-disk cache by temporarily patching the cache get
                # to always return None for this URL, then re-fetch.
                # Since ResponseCache.get() is checked first in _get(), the
                # cleanest approach is to just delete the cache entry if it
                # exists, so the provider re-fetches.
                import hashlib as _hl
                url = f"https://data.sec.gov/submissions/CIK{str(cik).zfill(10)}.json"
                cache_key = _hl.sha256(url.encode()).hexdigest()
                cache_entry = (
                    Path(settings.sec_data_dir) / "cache" / cache_key
                )
                if cache_entry.exists():
                    cache_entry.unlink()
                    logger.debug("Cleared cache for CIK %s", cik)

            raw = provider.fetch_submissions_for_cik(cik)
        except Exception as exc:
            logger.warning(
                "CIK %s (%s): HTTP/IO error — %s", cik, wikidata_id, exc
            )
            stats["http_errors"] += 1
            continue

        # Extract metadata (pure, never touches filings.recent)
        metadata = extract_metadata(raw)
        update_doc = build_update_doc(metadata)

        if args.verbose:
            populated = [k for k in metadata if k != "last_synced"]
            logger.debug(
                "CIK %s: extracted fields: %s", cik, populated
            )

        # Accumulate field-population stats
        for k in metadata:
            field_counts[k] = field_counts.get(k, 0) + 1

        # Persist to DB
        found = persist_metadata(db, wikidata_id, update_doc, args.dry_run)
        if found:
            if args.dry_run:
                stats["dry_run_would_update"] += 1
            else:
                stats["updated"] += 1
                logger.debug(
                    "CIK %s: updated organizations/%s sec_metadata", cik, wikidata_id
                )
        else:
            stats["org_not_found"] += 1
            logger.warning(
                "CIK %s: organizations/%s not found in DB — skipping",
                cik, wikidata_id,
            )

        # Mark as processed
        processed_ciks.add(cik)
        if not args.dry_run:
            save_checkpoint(processed_ciks)

    # --- Summary ------------------------------------------------------------
    print("")
    print("=" * 60)
    print("  fetch_submissions SUMMARY")
    print("=" * 60)
    print(f"  Total CIKs in mapping    : {stats['total']:>8,}")
    print(f"  Skipped (checkpoint)     : {stats['skipped_checkpoint']:>8,}")
    print(f"  HTTP/IO errors           : {stats['http_errors']:>8,}")
    print(f"  Org not in DB            : {stats['org_not_found']:>8,}")
    if args.dry_run:
        print(f"  Would update (dry run)   : {stats['dry_run_would_update']:>8,}")
        print("  MODE: DRY RUN (no DB writes)")
    else:
        print(f"  Updated                  : {stats['updated']:>8,}")
    print("")
    print("  Field population (across processed orgs):")
    for field, count in sorted(field_counts.items()):
        print(f"    {field:<44}: {count:>5,}")
    print("=" * 60)

    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Fetch per-CIK submissions metadata from SEC EDGAR and persist "
            "sec_metadata onto organizations documents. §10.4.a."
        )
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        metavar="N",
        help="Process only the first N CIKs (0 = no limit).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Extract metadata but skip all DB writes.",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Set log level to DEBUG.",
    )
    p.add_argument(
        "--refresh",
        action="store_true",
        help="Ignore on-disk response cache; re-fetch from SEC EDGAR.",
    )
    resume_group = p.add_mutually_exclusive_group()
    resume_group.add_argument(
        "--resume",
        dest="resume",
        action="store_true",
        default=True,
        help="Skip already-processed CIKs via checkpoint (default ON).",
    )
    resume_group.add_argument(
        "--no-resume",
        dest="resume",
        action="store_false",
        help="Disable checkpoint; process all CIKs.",
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
