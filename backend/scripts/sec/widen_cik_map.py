"""Widen sec_cik_map by seeding institutional-manager CIKs from SEC form-index files.

Pulls the last N quarters of SEC full-index form.idx files, extracts 13F-HR
filer CIKs, and joins them to existing Wikidata orgs by exact normalised name
match. Idempotent upsert into sec_cik_map with source="sec_13f_form_index".
Rejects ambiguous matches (>1 Wikidata org) rather than guessing.

Intended cron usage: weekly run with default --last-quarters 4 picks up new
13F filers as they appear. Feeds fetch_13f.py on the next run.

Usage (user runs these — do NOT invoke inside this script)
----------------------------------------------------------
    # Dry run, last 4 quarters:
    docker compose exec backend python -m scripts.sec.widen_cik_map --dry-run

    # Full run:
    docker compose exec backend python -m scripts.sec.widen_cik_map

    # Historical catch-up from 2020:
    docker compose exec backend python -m scripts.sec.widen_cik_map --since-year 2020

    # Force re-download of form.idx files:
    docker compose exec backend python -m scripts.sec.widen_cik_map --refresh --dry-run

Flags
-----
--last-quarters N    Download the last N completed quarters (default 4).
--since-year YYYY    One-time catch-up: process every quarter since YYYY.
                     Mutually exclusive with --last-quarters.
--refresh            Ignore idx cache and re-download.
--dry-run            Parse and match but skip all DB writes (still caches idx).
--verbose            Set log level to DEBUG.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.config import settings  # noqa: E402
from app.database import (  # noqa: E402
    ORGANIZATIONS,
    SEC_CIK_MAP,
    ensure_collections,
    ensure_database,
    get_arango_client,
)
from scripts.sec.resolve_cusip import normalize_issuer_name  # noqa: E402

logger = logging.getLogger("widen_cik_map")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FORM_INDEX_URL = (
    "https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{quarter}/form.idx"
)
FORM_INDEX_SUBDIR = "form_index"

_TARGET_FORMS = frozenset({"13F-HR", "13F-HR/A"})

BATCH_SIZE = 500

# Conservative list of corporate suffixes for manager name normalisation.
# Note: we reuse normalize_issuer_name() from resolve_cusip which already
# handles these, but this comment documents the design intent.
_SOURCE_TAG = "sec_13f_form_index"

# ---------------------------------------------------------------------------
# Quarter helpers
# ---------------------------------------------------------------------------


def _completed_quarters_since(since_year: int, today: datetime) -> list[tuple[int, int]]:
    """Return all (year, quarter) pairs from since_year up to the last completed quarter."""
    quarters = []
    year = since_year
    q = 1
    while True:
        # Quarter Q of year Y is complete when today is past the last day of that quarter.
        # Q1 ends 31 Mar, Q2 ends 30 Jun, Q3 ends 30 Sep, Q4 ends 31 Dec.
        q_end_month = q * 3
        # Last day of q_end_month
        if q_end_month == 12:
            next_month_start = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            next_month_start = datetime(year, q_end_month + 1, 1, tzinfo=timezone.utc)
        if next_month_start > today.replace(tzinfo=timezone.utc) if today.tzinfo is None else today:
            break
        quarters.append((year, q))
        q += 1
        if q > 4:
            q = 1
            year += 1
    return quarters


def last_n_completed_quarters(n: int, today: datetime | None = None) -> list[tuple[int, int]]:
    """Return the last N completed (year, quarter) pairs, most-recent last.

    >>> last_n_completed_quarters(4, datetime(2026, 4, 18))
    [(2025, 2), (2025, 3), (2025, 4), (2026, 1)]
    """
    if today is None:
        today = datetime.now(timezone.utc)
    # Walk backwards from the most recent completed quarter
    result = []
    # Find current year/q
    month = today.month
    cur_q = (month - 1) // 3 + 1
    cur_year = today.year

    # Step back one quarter if current quarter is still in progress
    # (quarter Q of year Y is completed when we are strictly past its end)
    q_end_month = cur_q * 3
    if q_end_month == 12:
        next_after = datetime(cur_year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        next_after = datetime(cur_year, q_end_month + 1, 1, tzinfo=timezone.utc)

    today_utc = today if today.tzinfo else today.replace(tzinfo=timezone.utc)
    if today_utc < next_after:
        # Still in cur_q — step back
        cur_q -= 1
        if cur_q == 0:
            cur_q = 4
            cur_year -= 1

    year, q = cur_year, cur_q
    for _ in range(n):
        result.append((year, q))
        q -= 1
        if q == 0:
            q = 4
            year -= 1

    result.reverse()
    return result


def quarters_since_year(since_year: int, today: datetime | None = None) -> list[tuple[int, int]]:
    """Return all completed quarters from since_year up to today."""
    if today is None:
        today = datetime.now(timezone.utc)
    return _completed_quarters_since(since_year, today)


# ---------------------------------------------------------------------------
# form.idx cache helpers
# ---------------------------------------------------------------------------


def _idx_cache_path(year: int, quarter: int) -> Path:
    return Path(settings.sec_data_dir) / FORM_INDEX_SUBDIR / f"{year}q{quarter}.idx"


def _fetch_form_idx(year: int, quarter: int, *, refresh: bool = False) -> bytes:
    """Download form.idx for the given quarter; cache on disk. Returns raw bytes."""
    cache_path = _idx_cache_path(year, quarter)
    if cache_path.exists() and not refresh:
        logger.debug("form.idx cache hit: %s", cache_path)
        return cache_path.read_bytes()

    url = FORM_INDEX_URL.format(year=year, quarter=quarter)
    logger.info("Downloading %s …", url)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": settings.sec_user_agent},
    )
    delay = 1.0 / max(settings.sec_requests_per_second, 1.0)
    time.sleep(delay)
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = resp.read()

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(body)
    logger.info("Cached form.idx → %s (%d bytes)", cache_path, len(body))
    return body


# ---------------------------------------------------------------------------
# form.idx parser
# ---------------------------------------------------------------------------


def parse_form_idx(raw: bytes) -> Iterator[tuple[str, str, str]]:
    """Parse a SEC full-index form.idx file.

    Yields (form_type, company_name, cik) tuples for data rows.

    The file is fixed-width with a header block (first ~10 lines) ending with
    a separator line of dashes. Data rows look like:

        13F-HR          VANGUARD GROUP INC              102909     2025-01-...

    The separator row immediately above the data defines the field starts.
    Deriving the offsets from that row keeps this compatible with the SEC's
    fixed-width layout while also handling the slightly different widths used
    by archived index files and test fixtures.
    """
    text = raw.decode("latin-1", errors="replace")
    lines = text.splitlines()

    # Find the separator line (all dashes) to skip the header
    in_data = False
    field_starts: list[int] | None = None
    for line in lines:
        stripped = line.strip()
        if not in_data:
            if re.fullmatch(r"[-\s]+", stripped) and stripped.count("-") >= 10:
                dash_spans = [
                    match.span() for match in re.finditer(r"-+", line)
                ]
                if len(dash_spans) >= 4:
                    field_starts = [span[0] for span in dash_spans]
                in_data = True
            continue
        if not stripped:
            continue
        # Fixed-width fields: form, company, CIK, date, filename.  The first
        # four starts are enough for this parser; the filename is ignored.
        if field_starts is None or len(field_starts) < 4:
            continue
        form_start, company_start, cik_start, date_start = field_starts[:4]
        if len(line) < date_start:
            continue
        form_type = line[form_start:company_start].strip()
        company_name = line[company_start:cik_start].strip()
        cik_raw = line[cik_start:date_start].strip()
        if not cik_raw.isdigit():
            # A few archived rows are shifted by one column when the form
            # type includes a suffix such as ``/A``.  Their two-space field
            # separators are still unambiguous, so recover those rows without
            # weakening the fixed-width path for normal SEC data.
            fields = re.split(r"\s{2,}", stripped, maxsplit=4)
            if len(fields) < 3 or not fields[2].isdigit():
                continue
            form_type, company_name, cik_raw = fields[:3]
        yield form_type, company_name, cik_raw


# ---------------------------------------------------------------------------
# CIK extraction
# ---------------------------------------------------------------------------


def extract_13f_ciks(raw: bytes) -> dict[str, str]:
    """Parse form.idx bytes and return {cik10: company_name} for 13F-HR filers.

    Deduplicates on CIK; for duplicates the last seen row wins (most recent
    filing appears latest in time-sorted index).
    """
    result: dict[str, str] = {}
    for form_type, company_name, cik_raw in parse_form_idx(raw):
        if form_type not in _TARGET_FORMS:
            continue
        cik10 = cik_raw.zfill(10)
        result[cik10] = company_name
    return result


# ---------------------------------------------------------------------------
# ArangoDB helpers
# ---------------------------------------------------------------------------


def already_mapped_ciks(db) -> set[str]:
    """Return the set of CIK _keys already in sec_cik_map."""
    aql = f"FOR doc IN {SEC_CIK_MAP} RETURN doc._key"
    cursor = db.aql.execute(aql)
    return set(cursor)


def already_mapped_qids(db) -> set[str]:
    """Return the set of wikidata_id values already in sec_cik_map.

    sec_cik_map has a unique index on wikidata_id — each QID can map to
    at most one CIK. We pre-filter candidates whose QID is already taken
    to avoid AQL unique-constraint failures mid-batch.
    """
    aql = (
        f"FOR doc IN {SEC_CIK_MAP} "
        f"FILTER doc.wikidata_id != null "
        f"RETURN doc.wikidata_id"
    )
    cursor = db.aql.execute(aql)
    return set(cursor)


# Org types that cannot plausibly be 13F filers. Filters out false-positive
# name matches like "Aberdeen Group plc" → Aberdeen F.C. (football club),
# "FIDUCIARY TRUST CO" → trustee (the generic concept Q2047357), etc.
_DENIED_ORG_TYPES = frozenset({
    "sports_club",
    "stadium",
    "sports_facility",
    "position",
    "primary_school",
    "secondary_school",
    "political_party",
    "museum",
    "newspaper",
    "magazine",
    "publisher",
    "television_station",
    "radio_station",
    "broadcaster",
    "news_agency",
    "art_gallery",
})


def match_org_by_name(db, company_name: str) -> tuple[str | None, str]:
    """Match company_name against organizations.name_en / aliases_en.

    Returns (qid, reason) where qid is None on miss or ambiguous match.
    reason is one of: "matched", "unmatched", "ambiguous", "type_denied".
    """
    target = normalize_issuer_name(company_name)
    if not target:
        return None, "unmatched"

    first_token = target.split(" ", 1)[0]
    aql = """
    FOR o IN organizations
      FILTER CONTAINS(UPPER(o.name_en), @first)
          OR LENGTH(
               FOR a IN (o.aliases_en != null ? o.aliases_en : [])
                 FILTER CONTAINS(UPPER(a), @first)
                 LIMIT 1
                 RETURN 1
             ) > 0
      LIMIT 500
      RETURN {qid: o._key, name_en: o.name_en, aliases_en: o.aliases_en, type: o.type}
    """
    try:
        cursor = db.aql.execute(aql, bind_vars={"first": first_token})
        candidates = list(cursor)
    except Exception as exc:
        logger.warning("AQL failed for %r: %s", company_name, exc)
        return None, "unmatched"

    hits: list[str] = []
    denied: list[str] = []
    for c in candidates:
        if (c.get("type") or "") in _DENIED_ORG_TYPES:
            # Track separately so we can surface type-denied matches in logs.
            matched_here = False
            names = [c.get("name_en") or ""] + list(c.get("aliases_en") or [])
            for n in names:
                if normalize_issuer_name(n) == target:
                    matched_here = True
                    break
            if matched_here:
                denied.append(c["qid"])
            continue
        names = [c.get("name_en") or ""] + list(c.get("aliases_en") or [])
        for n in names:
            if normalize_issuer_name(n) == target:
                hits.append(c["qid"])
                break

    unique = set(hits)
    if len(unique) == 1:
        return next(iter(unique)), "matched"
    if len(unique) > 1:
        return None, "ambiguous"
    if denied:
        # Every candidate that name-matched was of a denied type (e.g. sports
        # club, political party). Report separately so the summary can surface
        # these without silently treating them as "unmatched".
        return None, "type_denied"
    return None, "unmatched"


def upsert_cik_map_batch(db, batch: list[dict], dry_run: bool) -> None:
    """Upsert a batch of sec_cik_map documents using INSERT … OPTIONS {overwrite: true}."""
    if dry_run:
        logger.debug("DRY RUN: would upsert %d rows into %s", len(batch), SEC_CIK_MAP)
        return
    aql = f"""
    FOR doc IN @batch
      INSERT doc INTO {SEC_CIK_MAP}
      OPTIONS {{ overwrite: true }}
    """
    db.aql.execute(aql, bind_vars={"batch": batch})


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------


def run(args: argparse.Namespace) -> int:
    today = datetime.now(timezone.utc)

    # --- Determine quarters to process ---------------------------------------
    if args.since_year:
        quarters = quarters_since_year(args.since_year, today)
        logger.info(
            "--since-year %d: processing %d quarters", args.since_year, len(quarters)
        )
    else:
        quarters = last_n_completed_quarters(args.last_quarters, today)
        logger.info(
            "--last-quarters %d: processing %s",
            args.last_quarters,
            [f"{y}Q{q}" for y, q in quarters],
        )

    if not quarters:
        logger.error("No completed quarters to process.")
        return 1

    # --- Download / load form.idx files --------------------------------------
    all_ciks: dict[str, str] = {}  # cik10 -> company_name (last seen wins)
    total_rows_seen = 0

    for year, quarter in quarters:
        try:
            raw = _fetch_form_idx(year, quarter, refresh=args.refresh)
        except Exception as exc:
            logger.warning("Failed to fetch %dQ%d form.idx: %s", year, quarter, exc)
            continue

        quarter_ciks = extract_13f_ciks(raw)
        rows_this = sum(
            1
            for form_type, _, _ in parse_form_idx(raw)
            if form_type in _TARGET_FORMS
        )
        total_rows_seen += rows_this
        all_ciks.update(quarter_ciks)
        logger.info(
            "%dQ%d: %d 13F rows → %d unique CIKs this quarter",
            year, quarter, rows_this, len(quarter_ciks),
        )

    unique_ciks_total = len(all_ciks)
    logger.info("Total unique 13F CIKs across all quarters: %d", unique_ciks_total)

    # --- Connect to DB -------------------------------------------------------
    client = get_arango_client()
    db = ensure_database(client)
    ensure_collections(db)

    # --- Filter already-mapped CIKs ------------------------------------------
    existing = already_mapped_ciks(db)
    logger.info("sec_cik_map already contains %d CIKs", len(existing))

    candidates: dict[str, str] = {
        cik: name for cik, name in all_ciks.items() if cik not in existing
    }
    already_mapped_count = unique_ciks_total - len(candidates)
    logger.info(
        "%d CIKs already mapped (skipped), %d candidates to match",
        already_mapped_count,
        len(candidates),
    )

    # Pre-fetch QIDs already mapped: sec_cik_map has a unique index on
    # wikidata_id. We skip candidates whose QID is already taken under a
    # different CIK to avoid a mid-batch AQL failure. These skips are
    # counted under qid_already_mapped (not "ambiguous", which means the
    # *company name* matched multiple orgs).
    existing_qids = already_mapped_qids(db)
    logger.info("sec_cik_map already uses %d distinct wikidata_ids", len(existing_qids))

    # --- Match to Wikidata orgs ----------------------------------------------
    now_iso = datetime.now(timezone.utc).isoformat()
    docs_to_upsert: list[dict] = []
    unmatched_names: list[str] = []
    ambiguous_count = 0
    matched_count = 0
    unmatched_count = 0
    qid_already_mapped_count = 0
    type_denied_count = 0

    for cik10, company_name in candidates.items():
        qid, reason = match_org_by_name(db, company_name)
        if reason == "matched":
            if qid in existing_qids:
                qid_already_mapped_count += 1
                logger.debug(
                    "CIK %s → %s (%r): QID already mapped under a different CIK — skipped",
                    cik10, qid, company_name,
                )
                continue
            docs_to_upsert.append({
                "_key": cik10,
                "wikidata_id": qid,
                "entity_kind": "org",
                "ticker": None,
                "last_synced": now_iso,
                "filings_count": 0,
                "source": _SOURCE_TAG,
                "company_name_sec": company_name,
            })
            existing_qids.add(qid)  # guard against dup QIDs within this batch
            matched_count += 1
            logger.debug("Matched CIK %s → %s (%r)", cik10, qid, company_name)
        elif reason == "ambiguous":
            ambiguous_count += 1
            logger.debug("Ambiguous match for CIK %s (%r) — rejected", cik10, company_name)
        elif reason == "type_denied":
            type_denied_count += 1
            logger.debug(
                "Type-denied match for CIK %s (%r) — only non-financial types matched",
                cik10, company_name,
            )
        else:
            unmatched_count += 1
            unmatched_names.append(company_name)

    # --- Upsert matched docs -------------------------------------------------
    logger.info(
        "Upserting %d new CIK→org matches into %s …", len(docs_to_upsert), SEC_CIK_MAP
    )
    for i in range(0, len(docs_to_upsert), BATCH_SIZE):
        chunk = docs_to_upsert[i : i + BATCH_SIZE]
        upsert_cik_map_batch(db, chunk, args.dry_run)
        logger.debug("  upserted batch %d–%d", i, i + len(chunk))

    # --- Summary -------------------------------------------------------------
    quarters_processed = len([
        (y, q) for y, q in quarters
        if _idx_cache_path(y, q).exists()
    ])

    print("")
    print("=" * 60)
    print("  widen_cik_map SUMMARY")
    print("=" * 60)
    print(f"  Quarters processed      : {quarters_processed:>8,}")
    print(f"  13F rows seen           : {total_rows_seen:>8,}")
    print(f"  Unique CIKs             : {unique_ciks_total:>8,}")
    print(f"  Already mapped (skip)   : {already_mapped_count:>8,}")
    print(f"  QID already mapped      : {qid_already_mapped_count:>8,}")
    print(f"  Matched (new additions) : {matched_count:>8,}")
    print(f"  Ambiguous (rejected)    : {ambiguous_count:>8,}")
    print(f"  Type-denied (rejected)  : {type_denied_count:>8,}")
    print(f"  Unmatched (0 Wikidata)  : {unmatched_count:>8,}")
    if args.dry_run:
        print("  MODE: DRY RUN (no DB writes)")
    print("=" * 60)

    if unmatched_names:
        top30 = sorted(unmatched_names)[:30]
        print("")
        print("  Top 30 unmatched company names (alphabetical):")
        for name in top30:
            print(f"    {name}")

    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Widen sec_cik_map by seeding institutional-manager CIKs from SEC "
            "quarterly form-index files, joined to existing Wikidata orgs by "
            "exact normalised name match."
        )
    )
    group = p.add_mutually_exclusive_group()
    group.add_argument(
        "--last-quarters",
        type=int,
        default=4,
        metavar="N",
        help="Process the last N completed quarters (default 4).",
    )
    group.add_argument(
        "--since-year",
        type=int,
        default=None,
        metavar="YYYY",
        help=(
            "One-time historical catch-up: process every quarter since YYYY. "
            "Mutually exclusive with --last-quarters."
        ),
    )
    p.add_argument(
        "--refresh",
        action="store_true",
        help="Ignore on-disk form.idx cache and re-download.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and match but skip all DB writes. Still writes idx cache.",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Set log level to DEBUG.",
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
