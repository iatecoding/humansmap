"""Fetch 8-K material events from SEC EDGAR and persist to ArangoDB.

§10.5.a of the project design notes.

What it does
------------
1. Loads all mapped org CIKs from sec_cik_map (where entity_kind == "org").
2. For each CIK, fetches https://data.sec.gov/submissions/CIK{10digit}.json
   via SECEdgarProvider (reuses throttle + on-disk cache from §10.4.a).
3. Iterates filings.recent (column-oriented arrays) AND paginated filings.files[].
4. Filters rows where form is exactly "8-K" or "8-K/A".
5. Classifies each 8-K into one of three subtypes by item number (priority order):
     - bankruptcy_filed   ← item 1.03
     - exec_departure     ← item 5.02
     - acquisition_announced ← item 1.01 OR 2.01
   Filings with no matching item are skipped.
6. Upserts event documents into the `events` collection (idempotent, overwrite: true).
7. Creates involvement edges events/{key} → organizations/{org_qid} with role "filer".

This phase is item-number classification only.  Narrative extraction (who
departed, acquisition price) is deferred per §2.4.

Usage (user runs these — do NOT invoke inside this script)
----------------------------------------------------------
    # Dry run (no DB writes):
    docker compose exec backend python -m scripts.sec.fetch_material_events --dry-run --limit 5

    # Full run:
    docker compose exec backend python -m scripts.sec.fetch_material_events

    # Force re-fetch (ignore on-disk cache):
    docker compose exec backend python -m scripts.sec.fetch_material_events --refresh

    # Start fresh, ignoring checkpoint:
    docker compose exec backend python -m scripts.sec.fetch_material_events --no-resume

Flags
-----
--limit N      Cap number of CIKs processed (0 = no limit).
--dry-run      Parse and classify but skip all DB writes.
--verbose      Set log level to DEBUG.
--refresh      Ignore on-disk submissions cache; re-fetch from SEC EDGAR.
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
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.config import settings  # noqa: E402
from app.database import (  # noqa: E402
    EVENTS,
    INVOLVEMENTS,
    ORGANIZATIONS,
    SEC_CIK_MAP,
    ensure_collections,
    ensure_database,
    get_arango_client,
)
from app.providers.sec_edgar import SecEdgarProvider  # noqa: E402
from scripts.sec.backfill_material_event_labels import (  # noqa: E402
    build_material_event_labels,
)

logger = logging.getLogger("fetch_material_events")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CACHE_SUBDIR = "sparql_cache"
CHECKPOINT_FILENAME = "material_events_checkpoint.json"

# Item number → subtype, in priority order (first match wins).
# Tuple of (item_numbers_that_trigger, subtype_name).
_ITEM_PRIORITY: list[tuple[frozenset[str], str]] = [
    (frozenset({"1.03"}), "bankruptcy_filed"),
    (frozenset({"5.02"}), "exec_departure"),
    (frozenset({"1.01", "2.01"}), "acquisition_announced"),
]

# Form types we care about
_8K_FORMS = frozenset({"8-K", "8-K/A"})

# Base URL for the paginated filings.files[] entries
_SUBMISSIONS_FILES_BASE = "https://data.sec.gov/submissions/"

# ---------------------------------------------------------------------------
# Pure helpers — easy to unit-test
# ---------------------------------------------------------------------------


def parse_items_field(items_str: str) -> list[str]:
    """Split and trim the items field from a filings.recent row.

    The items field is a comma-separated string of SEC item numbers,
    e.g. "5.02, 8.01" or "1.03" or "" (empty).

    Returns a list of stripped item-number strings.  Empty or
    whitespace-only input returns an empty list.

    >>> parse_items_field("1.01, 2.01 , 8.01")
    ['1.01', '2.01', '8.01']
    >>> parse_items_field("")
    []
    >>> parse_items_field("5.02")
    ['5.02']
    """
    if not items_str or not items_str.strip():
        return []
    return [part.strip() for part in items_str.split(",") if part.strip()]


def classify_8k_items(items_str: str) -> str | None:
    """Return the highest-priority subtype for an 8-K items field string.

    Priority order (first match wins):
      1. bankruptcy_filed    ← item 1.03
      2. exec_departure      ← item 5.02
      3. acquisition_announced ← item 1.01 OR 2.01

    Returns None if no matching item is found.

    >>> classify_8k_items("1.03")
    'bankruptcy_filed'
    >>> classify_8k_items("5.02, 8.01")
    'exec_departure'
    >>> classify_8k_items("1.01,2.01")
    'acquisition_announced'
    >>> classify_8k_items("8.01")
    >>> classify_8k_items("")
    >>> classify_8k_items("5.02, 1.03")
    'bankruptcy_filed'
    """
    items = set(parse_items_field(items_str))
    for trigger_items, subtype in _ITEM_PRIORITY:
        if items & trigger_items:
            return subtype
    return None


def iter_filings_rows(filings_recent: dict) -> Iterable[dict]:
    """Zip the column-oriented filings.recent dict into per-filing dicts.

    ``filings_recent`` is the value of ``submissions["filings"]["recent"]`` —
    a dict of parallel arrays like::

        {
            "accessionNumber": ["0001193125-24-198765", ...],
            "filingDate":      ["2024-07-15", ...],
            "form":            ["8-K", ...],
            "primaryDocument": ["d12345.htm", ...],
            "items":           ["5.02,8.01", ...],
            ...
        }

    Yields one dict per row with keys ``accessionNumber``, ``filingDate``,
    ``form``, ``primaryDocument``, ``items``.  Uses ``strict=True`` on zip
    so mismatched array lengths raise ``ValueError`` immediately rather than
    silently truncating.

    Missing columns default to empty strings so callers can unconditionally
    access these keys.
    """
    accessions = filings_recent.get("accessionNumber", [])
    dates = filings_recent.get("filingDate", [])
    forms = filings_recent.get("form", [])
    primary_docs = filings_recent.get("primaryDocument", [])
    items_list = filings_recent.get("items", [])

    # Pad shorter arrays with empty strings to match the longest — some columns
    # (especially "items") may be shorter than "accessionNumber" on sparse data.
    max_len = max(
        [len(accessions), len(dates), len(forms), len(primary_docs), len(items_list)],
        default=0,
    )

    def _pad(lst: list, length: int) -> list:
        return lst + [""] * (length - len(lst))

    accessions = _pad(list(accessions), max_len)
    dates = _pad(list(dates), max_len)
    forms = _pad(list(forms), max_len)
    primary_docs = _pad(list(primary_docs), max_len)
    items_list = _pad(list(items_list), max_len)

    for acc, date, form, doc, items in zip(
        accessions, dates, forms, primary_docs, items_list, strict=True
    ):
        yield {
            "accessionNumber": acc,
            "filingDate": date,
            "form": form,
            "primaryDocument": doc,
            "items": items,
        }


def build_filing_url(cik: str, accession: str, primary_document: str) -> str:
    """Build the EDGAR filing URL for a given CIK, accession, and document.

    Args:
        cik:              10-digit zero-padded CIK string, e.g. "0000320193".
        accession:        Raw accession number with dashes, e.g. "0001193125-24-198765".
        primary_document: Filename of the primary document, e.g. "d12345.htm".

    Returns:
        Full EDGAR URL, e.g.:
        https://www.sec.gov/Archives/edgar/data/320193/000119312524198765/d12345.htm

    >>> build_filing_url("0000320193", "0001193125-24-198765", "d12345.htm")
    'https://www.sec.gov/Archives/edgar/data/320193/000119312524198765/d12345.htm'
    """
    cik_int = int(cik)  # strips leading zeros
    accession_nodash = accession.replace("-", "")
    return (
        f"https://www.sec.gov/Archives/edgar/data/{cik_int}"
        f"/{accession_nodash}/{primary_document}"
    )


def build_event_doc(
    cik: str,
    qid: str,
    row: dict,
    now_iso: str,
    org_name_en: str | None = None,
    org_name_it: str | None = None,
) -> dict | None:
    """Build an event document dict from a single filings row.

    Returns None if the row is not a classified 8-K (wrong form type or no
    matching item).

    The returned dict has ``_key`` set to the dashless accession number and
    is ready to upsert into the ``events`` collection.

    Args:
        cik:          10-digit zero-padded CIK string, e.g. "0000320193".
        qid:          Wikidata QID for the org, e.g. "Q312".
        row:          A single dict from :func:`iter_filings_rows`.
        now_iso:      UTC ISO-8601 timestamp string for ``last_synced``.
        org_name_en:  English org name for the human-readable label (optional).
        org_name_it:  Italian org name for the human-readable label (optional).
    """
    form = row.get("form", "")
    if form not in _8K_FORMS:
        return None

    items_str = row.get("items", "") or ""
    subtype = classify_8k_items(items_str)
    if subtype is None:
        return None

    accession = row.get("accessionNumber", "")
    accession_nodash = accession.replace("-", "")

    all_items = sorted(parse_items_field(items_str))

    primary_doc = row.get("primaryDocument", "")
    filing_url = build_filing_url(cik, accession, primary_doc) if primary_doc else ""

    filing_date = row.get("filingDate", "")
    is_amendment = form == "8-K/A"

    # Fall back to CIK when no org name is available
    _org_en = org_name_en or cik or None
    _org_it = org_name_it or cik or None
    name_en, name_it = build_material_event_labels(
        subtype, _org_en, _org_it, filing_date, is_amendment
    )

    return {
        "_key": accession_nodash,
        "type": "material_event",
        "subtype": subtype,
        "filing_date": filing_date,
        "accession": accession,
        "filing_url": filing_url,
        "org_cik": cik,
        "org_qid": qid,
        "items": all_items,
        "is_amendment": is_amendment,
        "name_en": name_en,
        "name_it": name_it,
        "last_synced": now_iso,
    }


def build_involvement_doc(event_key: str, qid: str, now_iso: str) -> dict:
    """Build an involvements edge from an event node to an org node.

    Args:
        event_key: ``_key`` of the event document (dashless accession).
        qid:       Wikidata QID of the filer org.
        now_iso:   UTC ISO-8601 timestamp string for ``last_synced``.
    """
    return {
        "_key": f"{event_key}_{qid}",
        "_from": f"{EVENTS}/{event_key}",
        "_to": f"{ORGANIZATIONS}/{qid}",
        "role": "filer",
        "last_synced": now_iso,
    }


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


def upsert_event(db, event_doc: dict, dry_run: bool) -> None:
    """Upsert an event document into the events collection (overwrite: true)."""
    if dry_run:
        logger.debug(
            "DRY RUN: would upsert %s/%s (subtype=%s)",
            EVENTS, event_doc["_key"], event_doc.get("subtype"),
        )
        return

    aql = f"""
    INSERT @doc INTO {EVENTS}
      OPTIONS {{ overwrite: true }}
    """
    db.aql.execute(aql, bind_vars={"doc": event_doc})


def upsert_involvement(db, inv_doc: dict, dry_run: bool) -> None:
    """Upsert an involvement edge (overwrite: true, idempotent)."""
    if dry_run:
        logger.debug(
            "DRY RUN: would upsert %s/%s",
            INVOLVEMENTS, inv_doc["_key"],
        )
        return

    aql = f"""
    INSERT @doc INTO {INVOLVEMENTS}
      OPTIONS {{ overwrite: true }}
    """
    db.aql.execute(aql, bind_vars={"doc": inv_doc})


# ---------------------------------------------------------------------------
# Paginated filings.files[] fetch
# ---------------------------------------------------------------------------


def iter_extra_filings(
    submissions: dict[str, Any],
    provider: SecEdgarProvider,
) -> Iterable[dict]:
    """Iterate all additional filings pages referenced in filings.files[].

    Each entry in filings.files[] has a ``name`` like
    "CIK0000320193-submissions-001.json"; fetch it from
    ``https://data.sec.gov/submissions/{name}`` (throttled via the
    provider) and yield each row the same way as :func:`iter_filings_rows`.
    """
    files_list: list[dict] = (
        submissions.get("filings", {}).get("files") or []
    )
    for file_entry in files_list:
        name = file_entry.get("name", "")
        if not name:
            continue
        url = f"{_SUBMISSIONS_FILES_BASE}{name}"
        logger.debug("Fetching additional filings page: %s", url)
        try:
            body = provider._get(url)
            page: dict = json.loads(body)
        except Exception as exc:
            logger.warning("Could not fetch %s: %s", url, exc)
            continue

        # Each page has the same column-oriented structure as filings.recent
        yield from iter_filings_rows(page)


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------


def run(args: argparse.Namespace) -> int:
    """Execute the fetch_material_events pipeline. Returns exit code (0 = success)."""

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
    stats: dict[str, int] = {
        "total": len(cik_qid_pairs),
        "skipped_checkpoint": 0,
        "http_errors": 0,
        "filings_examined": 0,
        "8k_classified": 0,
        "events_upserted": 0,
        "dry_run_would_upsert": 0,
    }
    subtype_counts: dict[str, int] = {}

    # --- Per-CIK fetch loop -------------------------------------------------
    for idx, (cik, wikidata_id) in enumerate(cik_qid_pairs, start=1):
        if args.resume and cik in processed_ciks:
            stats["skipped_checkpoint"] += 1
            logger.debug("CIK %s (%s): skipping (checkpoint)", cik, wikidata_id)
            continue

        logger.info(
            "[%d/%d] Fetching 8-K events for CIK %s (org=%s) …",
            idx, len(cik_qid_pairs), cik, wikidata_id,
        )

        # --- Optionally bust the submissions JSON cache ----------------------
        if args.refresh:
            import hashlib as _hl
            url = f"https://data.sec.gov/submissions/CIK{str(cik).zfill(10)}.json"
            cache_key = _hl.sha256(url.encode()).hexdigest()
            cache_entry = Path(settings.sec_data_dir) / "cache" / cache_key
            if cache_entry.exists():
                cache_entry.unlink()
                logger.debug("Cleared submissions cache for CIK %s", cik)

        # --- Fetch submissions JSON -----------------------------------------
        try:
            submissions = provider.fetch_submissions_for_cik(cik)
        except Exception as exc:
            logger.warning("CIK %s (%s): HTTP/IO error — %s", cik, wikidata_id, exc)
            stats["http_errors"] += 1
            continue

        now_iso = datetime.now(timezone.utc).isoformat()

        # --- Iterate filings.recent -----------------------------------------
        filings_recent = submissions.get("filings", {}).get("recent", {})
        recent_rows = list(iter_filings_rows(filings_recent))

        # --- Iterate paginated filings.files[] ---------------------------------
        extra_rows = list(iter_extra_filings(submissions, provider))

        all_rows = recent_rows + extra_rows
        stats["filings_examined"] += len(all_rows)

        cik_events = 0

        for row in all_rows:
            event_doc = build_event_doc(cik, wikidata_id, row, now_iso)
            if event_doc is None:
                continue

            stats["8k_classified"] += 1
            subtype_counts[event_doc["subtype"]] = (
                subtype_counts.get(event_doc["subtype"], 0) + 1
            )
            cik_events += 1

            inv_doc = build_involvement_doc(event_doc["_key"], wikidata_id, now_iso)

            if args.dry_run:
                stats["dry_run_would_upsert"] += 1
                logger.debug(
                    "DRY RUN: %s/%s subtype=%s date=%s",
                    EVENTS, event_doc["_key"],
                    event_doc["subtype"], event_doc["filing_date"],
                )
            else:
                try:
                    upsert_event(db, event_doc, dry_run=False)
                    upsert_involvement(db, inv_doc, dry_run=False)
                    stats["events_upserted"] += 1
                except Exception as exc:
                    logger.warning(
                        "CIK %s: failed to upsert event %s — %s",
                        cik, event_doc["_key"], exc,
                    )

        logger.info(
            "CIK %s: %d filings examined, %d 8-K events classified",
            cik, len(all_rows), cik_events,
        )

        # --- Mark as processed and save checkpoint --------------------------
        processed_ciks.add(cik)
        if not args.dry_run:
            save_checkpoint(processed_ciks)

    # --- Summary ------------------------------------------------------------
    print("")
    print("=" * 60)
    print("  fetch_material_events SUMMARY")
    print("=" * 60)
    print(f"  Total CIKs in mapping    : {stats['total']:>8,}")
    print(f"  Skipped (checkpoint)     : {stats['skipped_checkpoint']:>8,}")
    print(f"  HTTP/IO errors           : {stats['http_errors']:>8,}")
    print(f"  Filings examined         : {stats['filings_examined']:>8,}")
    print(f"  8-K filings classified   : {stats['8k_classified']:>8,}")
    if args.dry_run:
        print(f"  Would upsert (dry run)   : {stats['dry_run_would_upsert']:>8,}")
        print("  MODE: DRY RUN (no DB writes)")
    else:
        print(f"  Events upserted          : {stats['events_upserted']:>8,}")
    print("")
    print("  Subtype breakdown:")
    for subtype, count in sorted(subtype_counts.items(), key=lambda x: -x[1]):
        print(f"    {subtype:<36}: {count:>5,}")
    print("=" * 60)

    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Fetch 8-K material events from SEC EDGAR and persist as event "
            "nodes + involvements edges. §10.5.a."
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
        help="Parse and classify but skip all DB writes.",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Set log level to DEBUG.",
    )
    p.add_argument(
        "--refresh",
        action="store_true",
        help="Ignore on-disk submissions cache; re-fetch from SEC EDGAR.",
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
