"""Fetch DEF 14A proxy statements from SEC EDGAR and persist to ArangoDB.

§11.1.b skeleton + §11.2.e bulk loader wiring.

What it does
------------
1. Loads all mapped org CIKs from sec_cik_map (where entity_kind == "org").
2. For each CIK, fetches https://data.sec.gov/submissions/CIK{10digit}.json
   via SecEdgarProvider (reuses throttle + on-disk cache from §10.4.a).
3. Iterates filings.recent (column-oriented arrays) AND paginated filings.files[].
4. Filters rows where form is "DEF 14A" or "DEF 14A/A" (amendment supersedes).
5. Dedups per (org, year) — most recent filing per year wins; amendment preferred.
6. Downloads the primary document HTML for each winning filing via
   SecEdgarProvider.fetch_primary_document() (§11.2.e addition to provider).
7. Calls parse_def14a(html) → {"directors": [...], "peonames": [...]}.
8. Calls write_edges() to upsert director_of / officer_of edges.
9. Checkpoints after each CIK.

DEF 14A is annual (one proxy per fiscal year per issuer), not quarterly.
The outer iteration is therefore by org CIK / year, not by quarter.

Usage (user runs these — do NOT invoke inside this script)
----------------------------------------------------------
    # Dry run (no DB writes), first 5 CIKs:
    docker compose exec backend python -m scripts.sec.fetch_def14a --dry-run --limit-ciks 5

    # Full run from 2020 onwards:
    docker compose exec backend python -m scripts.sec.fetch_def14a --since-year 2020

    # Full run (no year cutoff):
    docker compose exec backend python -m scripts.sec.fetch_def14a

    # Force re-fetch (ignore on-disk submissions cache):
    docker compose exec backend python -m scripts.sec.fetch_def14a --refresh

    # Start fresh, ignoring checkpoint:
    docker compose exec backend python -m scripts.sec.fetch_def14a --no-resume

    # Verbose debug output:
    docker compose exec backend python -m scripts.sec.fetch_def14a --verbose --dry-run

Flags
-----
--since-year YYYY  First year of DEF 14A filings to process (default: None — all years).
--limit-ciks N     Cap number of CIKs processed (0 = no limit; for smoke tests).
--dry-run          Download and parse but skip all DB writes.
--refresh          Ignore on-disk submissions cache; re-fetch from SEC EDGAR.
--no-resume        Disable checkpoint; process all CIKs even if already checkpointed.
--verbose          Set log level to DEBUG.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.config import settings  # noqa: E402
from app.database import (  # noqa: E402
    AFFILIATIONS,
    ORGANIZATIONS,
    SEC_CIK_MAP,
    ensure_collections,
    ensure_database,
    get_arango_client,
)
from app.providers.sec_edgar import SecEdgarProvider  # noqa: E402
from scripts.sec._match import global_name_fallback, last_name_match, tier1_match  # noqa: E402

logger = logging.getLogger("fetch_def14a")


def _tier1_match_natural(name: str, candidates: list[dict]):
    """Match a natural-order DEF 14A name.

    Keep compatibility with integrations that still inject the historical
    two-argument matcher while the real matcher receives ``name_format``.
    """
    try:
        return tier1_match(name, candidates, name_format="natural")
    except TypeError as exc:
        if "name_format" not in str(exc):
            raise
        return tier1_match(name, candidates)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CACHE_SUBDIR = "sparql_cache"
CHECKPOINT_FILENAME = "def14a_checkpoint.json"

# Form types we accept; amendment supersedes plain DEF 14A for same year.
_DEF14A_FORMS = frozenset({"DEF 14A", "DEF 14A/A"})

# Base URL for the paginated filings.files[] entries
_SUBMISSIONS_FILES_BASE = "https://data.sec.gov/submissions/"

# ---------------------------------------------------------------------------
# Filing dataclass
# ---------------------------------------------------------------------------


@dataclass
class Filing:
    """Lightweight representation of a single DEF 14A filing."""
    accession: str
    filing_date: str        # YYYY-MM-DD
    primary_document: str   # e.g. "proxy.htm"
    form: str               # "DEF 14A" or "DEF 14A/A"


# ---------------------------------------------------------------------------
# Parser (classification + extraction already implemented in §11.2.a/b/c)
# ---------------------------------------------------------------------------


def parse_def14a(html: str) -> dict:
    """Return both director rows (HTML layouts) and PEO name references (iXBRL).

    Returns a dict with:
        directors : list[dict]  — extracted via per-layout HTML extractors (§11.2.b)
        peonames  : list[dict]  — ecd:PeoName iXBRL tags (§11.2.c)

    Args:
        html: Raw HTML content of the primary DEF 14A document.
    """
    from .def14a_layouts import classify_layout
    from .def14a_extractors import extract_peonames, extract_rows

    layout = classify_layout(html)
    return {
        "directors": [dict(r) for r in extract_rows(html, layout)],
        "peonames":  [dict(r) for r in extract_peonames(html)],
        "layout":    layout,
    }


# ---------------------------------------------------------------------------
# Constants for edge writer
# ---------------------------------------------------------------------------

_DEF14A_UNMATCHED_CAP = 100
_PREFIX_RE = re.compile(r"^\s*(mr|mrs|ms|dr)\.?\s+", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Edge writer (§11.2.d)
# ---------------------------------------------------------------------------


def write_edges(
    org_qid: str,
    org_cik: str,
    filing_date: str,
    parsed: dict,
    db,
    *,
    dry_run: bool = False,
) -> dict:
    """Resolve director/peoname rows against sec_person_candidates and
    upsert director_of / officer_of edges into ``affiliations``. Unmatched
    names append to organizations.{org_qid}.sec_unmatched_directors[],
    deduped and capped at 100 (mirror sec_unmatched_reporters pattern).

    Returns {'matched_directors': int, 'matched_officers': int,
             'unmatched': int, 'updated_unmatched_list': bool}.
    """
    # Load candidates for this org from sec_person_candidates
    aql = f"""
    FOR doc IN sec_person_candidates
      FILTER doc.org_qid == @org_qid
      RETURN {{
        person_qid: doc.person_qid,
        person_name_en: doc.person_name_en,
        name_variants: doc.name_variants,
        source: doc.source
      }}
    """
    # Always load candidates — matching is a read-only op we want to exercise
    # under --dry-run so validators can see the real match rate. Only DB
    # writes (edge upsert + unmatched-list update) are gated below.
    try:
        cursor = db.aql.execute(aql, bind_vars={"org_qid": org_qid})
        candidates = list(cursor)
    except Exception as exc:
        logger.warning("Could not load candidates for %s: %s", org_qid, exc)
        candidates = []

    edges_to_write: list[dict] = []
    unmatched_names: set[str] = set()
    matched_directors = 0
    matched_officers = 0

    # --- Director rows -------------------------------------------------------
    for row in parsed.get("directors", []):
        name = row.get("name", "").strip()
        if not name:
            continue
        person_qid, match_method, _ = _tier1_match_natural(name, candidates)
        if not person_qid:
            # Safety-net: global name fallback after scoped miss
            person_qid, match_method, _ = global_name_fallback(
                name, db, org_qid=org_qid
            )
        if person_qid:
            start_date = row.get("start_date_hint") or filing_date
            edge_key = f"{person_qid}__{org_qid}__def14a"
            edges_to_write.append({
                "_key": edge_key,
                "_from": f"persons/{person_qid}",
                "_to": f"organizations/{org_qid}",
                "type": "director_of",
                "start_date": start_date,
                "end_date": None,
                "source": "sec_def14a",
                "match_method": match_method or "def14a_scoped",
                "raw_blob": row.get("raw_blob", ""),
            })
            matched_directors += 1
        else:
            unmatched_names.add(name)

    # --- PEO rows ------------------------------------------------------------
    for row in parsed.get("peonames", []):
        raw_name = row.get("raw_name", "").strip()
        if not raw_name:
            continue

        # Strip honorific prefix
        cleaned = _PREFIX_RE.sub("", raw_name).strip()
        tokens = cleaned.split()

        if len(tokens) >= 2:
            person_qid, match_method, _ = _tier1_match_natural(cleaned, candidates)
            if not person_qid:
                # Safety-net: global name fallback after scoped miss
                person_qid, match_method, _ = global_name_fallback(
                    cleaned, db, org_qid=org_qid
                )
        else:
            hits = last_name_match(tokens[0] if tokens else cleaned, candidates=candidates)
            person_qid = hits[0]["person_qid"] if len(hits) == 1 else None
            match_method = "def14a_scoped" if person_qid else None

        if person_qid:
            edge_key = f"{person_qid}__{org_qid}__def14a"
            edges_to_write.append({
                "_key": edge_key,
                "_from": f"persons/{person_qid}",
                "_to": f"organizations/{org_qid}",
                "type": "officer_of",
                "start_date": filing_date,
                "end_date": None,
                "source": "sec_def14a",
                "match_method": match_method or "def14a_scoped",
                "raw_blob": row.get("raw_blob", ""),
            })
            matched_officers += 1
        else:
            unmatched_names.add(raw_name)

    # --- Deduplicate edges by _key (last writer wins) -----------------------
    seen_keys: dict[str, dict] = {}
    for e in edges_to_write:
        seen_keys[e["_key"]] = e
    deduped_edges = list(seen_keys.values())

    # --- Unmatched accumulation ---------------------------------------------
    updated_unmatched = False
    if unmatched_names:
        if not dry_run:
            try:
                org_doc = db.collection(ORGANIZATIONS).get(org_qid)
                existing: list[str] = (
                    (org_doc or {}).get("sec_unmatched_directors") or []
                )
                combined = list(dict.fromkeys(existing + sorted(unmatched_names)))
                if len(combined) > _DEF14A_UNMATCHED_CAP:
                    combined = combined[-_DEF14A_UNMATCHED_CAP:]
                if combined != existing:
                    db.collection(ORGANIZATIONS).update(
                        {"_key": org_qid, "sec_unmatched_directors": combined}
                    )
                    updated_unmatched = True
            except Exception as exc:
                logger.warning(
                    "Could not update sec_unmatched_directors for %s: %s", org_qid, exc
                )
        else:
            updated_unmatched = bool(unmatched_names)

    # --- Write edges --------------------------------------------------------
    if not dry_run and deduped_edges:
        try:
            aql_upsert = f"""
            FOR doc IN @batch
              INSERT doc INTO {AFFILIATIONS}
              OPTIONS {{ overwriteMode: "update" }}
            """
            db.aql.execute(aql_upsert, bind_vars={"batch": deduped_edges})
        except Exception as exc:
            logger.warning("Could not upsert def14a edges for %s: %s", org_qid, exc)

    return {
        "matched_directors": matched_directors,
        "matched_officers": matched_officers,
        "unmatched": len(unmatched_names),
        "updated_unmatched_list": updated_unmatched,
    }


# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------


def checkpoint_path() -> Path:
    return Path(settings.sec_data_dir) / CACHE_SUBDIR / CHECKPOINT_FILENAME


def load_checkpoint() -> set[str]:
    """Return the set of already-processed CIK keys."""
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


def iter_mapped_org_ciks(db) -> Iterable[tuple[str, str]]:
    """Yield (cik, qid) for all org entries in sec_cik_map.

    AQL streams; ~3 500 orgs fits comfortably in memory but we keep it
    generator-shaped for parity with larger future collections.
    """
    aql = f"""
    FOR d IN {SEC_CIK_MAP}
      FILTER d.entity_kind == "org"
      FILTER d.wikidata_id != null
      RETURN {{cik: d._key, qid: d.wikidata_id}}
    """
    cursor = db.aql.execute(aql)
    for row in cursor:
        yield row["cik"], row["qid"]


# ---------------------------------------------------------------------------
# Filing iteration helpers (mirrors fetch_material_events.py pattern)
# ---------------------------------------------------------------------------


def iter_filings_rows(filings_recent: dict) -> Iterable[dict]:
    """Zip the column-oriented filings.recent dict into per-filing dicts.

    Yields one dict per row with keys: accessionNumber, filingDate,
    form, primaryDocument.  Arrays of unequal length are padded with
    empty strings so callers can unconditionally access these keys.
    """
    accessions = filings_recent.get("accessionNumber", [])
    dates = filings_recent.get("filingDate", [])
    forms = filings_recent.get("form", [])
    primary_docs = filings_recent.get("primaryDocument", [])
    report_dates = filings_recent.get("reportDate", [])
    period_dates = filings_recent.get("periodOfReport", [])

    max_len = max(
        [len(accessions), len(dates), len(forms), len(primary_docs),
         len(report_dates), len(period_dates)],
        default=0,
    )

    def _pad(lst: list, length: int) -> list:
        return lst + [""] * (length - len(lst))

    accessions = _pad(list(accessions), max_len)
    dates = _pad(list(dates), max_len)
    forms = _pad(list(forms), max_len)
    primary_docs = _pad(list(primary_docs), max_len)
    report_dates = _pad(list(report_dates), max_len)
    period_dates = _pad(list(period_dates), max_len)

    for acc, date, form, doc, rdate, pdate in zip(
        accessions, dates, forms, primary_docs, report_dates, period_dates, strict=True
    ):
        yield {
            "accessionNumber": acc,
            "filingDate": date,
            "form": form,
            "primaryDocument": doc,
            "reportDate": rdate,
            "periodOfReport": pdate,
        }


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
    files_list: list[dict] = submissions.get("filings", {}).get("files") or []
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
        yield from iter_filings_rows(page)


def iter_def14a_filings(
    submissions_json: dict[str, Any],
    since_year: int | None,
    provider: SecEdgarProvider | None = None,
) -> Iterable[Filing]:
    """Yield the most recent DEF 14A (or DEF 14A/A) per calendar year.

    Iterates both ``filings.recent`` arrays and paginated ``filings.files[]``
    pages (via *provider*).  Accepts both "DEF 14A" and "DEF 14A/A" rows.
    Within a given year, the most recent filing date wins; when dates tie,
    an amendment ("DEF 14A/A") is preferred over the plain form.

    Args:
        submissions_json: Parsed submissions JSON from SEC EDGAR.
        since_year:       If set, skip filings whose year < since_year.
        provider:         SecEdgarProvider instance (needed to fetch pagination
                          pages).  May be None if submissions_json has no
                          ``filings.files`` pages (e.g. in unit tests).

    Yields:
        :class:`Filing` objects, one per calendar year, in arbitrary order.
    """
    # Collect raw rows from filings.recent
    filings_recent = submissions_json.get("filings", {}).get("recent", {})
    all_rows: list[dict] = list(iter_filings_rows(filings_recent))

    # Collect rows from paginated pages if provider is available
    if provider is not None:
        all_rows.extend(iter_extra_filings(submissions_json, provider))

    # Filter to DEF 14A / DEF 14A/A only
    def14a_rows = [r for r in all_rows if r.get("form", "") in _DEF14A_FORMS]

    # Dedup per year: keep most-recent filing; prefer amendment on tie
    # best[year] = (filing_date_str, is_amendment, row_dict)
    best: dict[int, tuple[str, bool, dict]] = {}
    for row in def14a_rows:
        date_str = row.get("filingDate", "")
        if not date_str or len(date_str) < 4:
            continue
        try:
            year = int(date_str[:4])
        except ValueError:
            continue
        if since_year is not None and year < since_year:
            continue

        is_amendment = row.get("form", "") == "DEF 14A/A"
        current = best.get(year)
        if current is None:
            best[year] = (date_str, is_amendment, row)
        else:
            cur_date, cur_amend, _ = current
            # Prefer later date; on equal dates prefer amendment
            if date_str > cur_date or (date_str == cur_date and is_amendment and not cur_amend):
                best[year] = (date_str, is_amendment, row)

    for year, (date_str, _is_amend, row) in sorted(best.items()):
        accession = row.get("accessionNumber", "")
        primary_doc = row.get("primaryDocument", "")
        form = row.get("form", "")
        if not accession or not primary_doc:
            logger.debug("Skipping year %d — missing accession or primary doc", year)
            continue
        yield Filing(
            accession=accession,
            filing_date=date_str,
            primary_document=primary_doc,
            form=form,
        )


# ---------------------------------------------------------------------------
# URL builder
# ---------------------------------------------------------------------------


def build_filing_url(cik: str, accession: str, primary_document: str) -> str:
    """Build the EDGAR filing URL for a given CIK, accession, and document.

    >>> build_filing_url("0000320193", "0001193125-24-198765", "proxy.htm")
    'https://www.sec.gov/Archives/edgar/data/320193/000119312524198765/proxy.htm'
    """
    cik_int = int(cik)
    accession_nodash = accession.replace("-", "")
    return (
        f"https://www.sec.gov/Archives/edgar/data/{cik_int}"
        f"/{accession_nodash}/{primary_document}"
    )


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------


def run(args: argparse.Namespace) -> int:
    """Execute the fetch_def14a pipeline. Returns exit code (0 = success)."""

    # --- Connect to DB -------------------------------------------------------
    client = get_arango_client()
    db = ensure_database(client)
    ensure_collections(db)

    # --- Instantiate provider once ------------------------------------------
    provider = SecEdgarProvider()

    # --- Load CIK → QID mapping ---------------------------------------------
    logger.info("Loading org CIKs from %s …", SEC_CIK_MAP)
    cik_qid_pairs: list[tuple[str, str]] = list(iter_mapped_org_ciks(db))
    logger.info("Found %d org CIK mappings in %s", len(cik_qid_pairs), SEC_CIK_MAP)

    if not cik_qid_pairs:
        logger.error(
            "No org CIKs found in %s. Run build_cik_map.py first.", SEC_CIK_MAP
        )
        return 1

    # --- Optionally cap the list --------------------------------------------
    if args.cik:
        wanted = {str(c).lstrip("0").zfill(10) for c in args.cik}
        cik_qid_pairs = [
            (cik, qid) for (cik, qid) in cik_qid_pairs
            if str(cik).lstrip("0").zfill(10) in wanted
        ]
        logger.info("Filtering to %d CIK(s) via --cik", len(cik_qid_pairs))

    if args.limit_ciks:
        cik_qid_pairs = cik_qid_pairs[: args.limit_ciks]
        logger.info("Limiting to %d CIKs (--limit-ciks)", args.limit_ciks)

    # --- Load checkpoint ----------------------------------------------------
    processed_ciks: set[str] = set()
    if not args.no_resume:
        processed_ciks = load_checkpoint()
        if processed_ciks:
            logger.info(
                "Checkpoint: %d CIKs already processed (will skip)",
                len(processed_ciks),
            )

    # --- Totals counters ----------------------------------------------------
    totals: Counter = Counter()

    # --- Per-CIK fetch loop -------------------------------------------------
    for idx, (cik, org_qid) in enumerate(cik_qid_pairs, start=1):
        if cik in processed_ciks:
            logger.debug("CIK %s (%s): skipping (checkpoint)", cik, org_qid)
            totals["skipped_checkpoint"] += 1
            continue

        logger.info(
            "[%d/%d] DEF 14A for CIK %s (org=%s) …",
            idx, len(cik_qid_pairs), cik, org_qid,
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
            logger.warning("CIK %s (%s): HTTP/IO error — %s", cik, org_qid, exc)
            totals["http_errors"] += 1
            continue

        # --- Iterate filings: deduplicate per year --------------------------
        filings = list(
            iter_def14a_filings(submissions, since_year=args.since_year, provider=provider)
        )
        totals["orgs_processed"] += 1

        cik_matched_dirs = 0
        cik_matched_offs = 0
        cik_unmatched = 0
        cik_filings = 0

        for filing in filings:
            # Bust primary-doc cache if --refresh
            if args.refresh:
                provider.invalidate_primary_document(cik, filing.accession, filing.primary_document)

            # Fetch primary document HTML
            try:
                html = provider.fetch_primary_document(cik, filing.accession, filing.primary_document)
            except Exception as exc:
                logger.warning(
                    "CIK %s: could not fetch HTML for %s (%s) — %s",
                    cik, filing.accession, filing.filing_date, exc,
                )
                totals["html_errors"] += 1
                continue

            # Parse: layout-based directors + iXBRL peonames
            parsed = parse_def14a(html)
            layout = parsed.get("layout", "unknown")
            n_directors = len(parsed.get("directors", []))
            n_peonames = len(parsed.get("peonames", []))

            # Write edges (or dry-run)
            stats = write_edges(
                org_qid, cik, filing.filing_date, parsed, db, dry_run=args.dry_run
            )
            cik_matched_dirs += stats["matched_directors"]
            cik_matched_offs += stats["matched_officers"]
            cik_unmatched += stats["unmatched"]
            cik_filings += 1

            logger.info(
                "[DEF14A] %s %s layout=%s directors=%d/%d officers=%d unmatched=%d",
                cik,
                filing.filing_date,
                layout,
                stats["matched_directors"],
                n_directors,
                stats["matched_officers"],
                stats["unmatched"],
            )

        totals["filings_processed"] += cik_filings
        totals["matched_directors"] += cik_matched_dirs
        totals["matched_officers"] += cik_matched_offs
        totals["unmatched"] += cik_unmatched

        logger.info(
            "CIK %s done: %d filings, directors_matched=%d, officers=%d, unmatched=%d",
            cik, cik_filings, cik_matched_dirs, cik_matched_offs, cik_unmatched,
        )

        # --- Mark as processed and save checkpoint --------------------------
        # Dry runs must not persist state: a --dry-run that poisons the
        # checkpoint would make subsequent real runs skip the same CIKs.
        processed_ciks.add(cik)
        if not args.no_resume and not args.dry_run:
            save_checkpoint(processed_ciks)

    # --- Final totals -------------------------------------------------------
    logger.info(
        "fetch_def14a DONE — orgs=%d filings=%d directors_matched=%d "
        "officers=%d unmatched=%d errors(http=%d html=%d) skipped=%d",
        totals["orgs_processed"],
        totals["filings_processed"],
        totals["matched_directors"],
        totals["matched_officers"],
        totals["unmatched"],
        totals["http_errors"],
        totals["html_errors"],
        totals["skipped_checkpoint"],
    )

    print("")
    print("=" * 60)
    print("  fetch_def14a SUMMARY")
    print("=" * 60)
    print(f"  Orgs processed           : {totals['orgs_processed']:>8,}")
    print(f"  Skipped (checkpoint)     : {totals['skipped_checkpoint']:>8,}")
    print(f"  Filings processed        : {totals['filings_processed']:>8,}")
    print(f"  Directors matched        : {totals['matched_directors']:>8,}")
    print(f"  Officers matched         : {totals['matched_officers']:>8,}")
    print(f"  Unmatched names          : {totals['unmatched']:>8,}")
    print(f"  HTTP/IO errors           : {totals['http_errors']:>8,}")
    print(f"  HTML fetch errors        : {totals['html_errors']:>8,}")
    if args.dry_run:
        print("  MODE: DRY RUN (no DB writes)")
    print("=" * 60)

    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Fetch DEF 14A proxy statements from SEC EDGAR and persist "
            "officer_of/director_of edges to the affiliations collection."
        )
    )
    p.add_argument(
        "--since-year",
        type=int,
        default=None,
        metavar="YYYY",
        help=(
            "First year of DEF 14A filings to process (default: all years). "
            "Recommend --since-year 2020 for initial runs to avoid pre-iXBRL "
            "filings that parse poorly."
        ),
    )
    p.add_argument(
        "--limit-ciks",
        type=int,
        default=0,
        metavar="N",
        help="Process only the first N CIKs (0 = no limit; for smoke tests).",
    )
    p.add_argument(
        "--cik",
        action="append",
        default=None,
        metavar="CIK",
        help=(
            "Restrict the run to specific CIK(s). Repeatable. Accepts "
            "zero-padded (0000320193) or bare (320193) form; padded to "
            "10 digits internally. Useful for targeted smoke tests."
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Download and parse but skip all DB writes.",
    )
    p.add_argument(
        "--refresh",
        action="store_true",
        help="Ignore on-disk submissions + primary-doc cache; re-fetch from SEC EDGAR.",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Set log level to DEBUG.",
    )
    p.add_argument(
        "--no-resume",
        dest="no_resume",
        action="store_true",
        default=False,
        help="Disable checkpoint; process all CIKs even if already processed.",
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
