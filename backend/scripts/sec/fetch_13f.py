"""Bulk loader for SEC 13F-HR institutional holdings (§11.4.d).

Wires parse_13f (11.4.a) + CusipResolver (11.4.b) + build_edge_docs (11.4.c)
into a CIK-iterating pipeline that mirrors fetch_def14a's shape.

13F-HR is quarterly (due 45 days after each calendar-quarter end) so the outer
iteration is by (manager_cik, quarterly_filing) rather than by (org, year).
Edge `_key` includes `period_of_report`, so re-running a quarter is a no-op.

Usage (user runs these — do NOT invoke inside this script)
----------------------------------------------------------
    # Dry run on first 5 managers
    docker compose exec backend python -m scripts.sec.fetch_13f --dry-run --limit-ciks 5

    # Full run from 2023 onwards
    docker compose exec backend python -m scripts.sec.fetch_13f --since-year 2023

    # Full run
    docker compose exec backend python -m scripts.sec.fetch_13f

Flags
-----
--since-year YYYY  Skip filings whose year < YYYY (default: no cutoff).
--limit-ciks N     Cap number of manager CIKs processed (0 = no limit).
--cik CIK          Restrict to specific CIK(s); repeatable.
--dry-run          Fetch + parse but skip all DB writes and checkpoint updates.
--refresh          Ignore on-disk caches (submissions JSON + info-table XML).
--no-resume        Disable checkpoint; reprocess all CIKs.
--verbose          DEBUG logging.
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
    ensure_collections,
    ensure_database,
    get_arango_client,
)
from app.providers.sec_edgar import SecEdgarProvider  # noqa: E402
from scripts.sec.fetch_def14a import (  # noqa: E402
    iter_extra_filings,
    iter_filings_rows,
    iter_mapped_org_ciks,
)
from scripts.sec.parse_13f import aggregate_by_cusip, parse_13f_xml  # noqa: E402
from scripts.sec.resolve_cusip import CusipResolver  # noqa: E402
from scripts.sec.write_13f_edges import (  # noqa: E402
    build_edge_docs,
    merge_unmatched_holdings,
    upsert_edges,
)

logger = logging.getLogger("fetch_13f")

CHECKPOINT_FILENAME = "13f_checkpoint.json"
CACHE_SUBDIR = "sparql_cache"

_FORMS = frozenset({"13F-HR", "13F-HR/A"})

# Info-table XML filename hint — filers use varying names
_INFOTABLE_RE = re.compile(r"(infotable|informationtable|form13f)", re.IGNORECASE)


@dataclass
class Filing:
    accession: str
    filing_date: str        # YYYY-MM-DD
    period_of_report: str   # YYYY-MM-DD (quarter end)
    primary_document: str
    form: str
    is_amendment: bool


# ── Checkpoint ────────────────────────────────────────────────────────────────

def _checkpoint_path() -> Path:
    return Path(settings.sec_data_dir) / CACHE_SUBDIR / CHECKPOINT_FILENAME


def load_checkpoint() -> set[str]:
    p = _checkpoint_path()
    if not p.exists():
        return set()
    try:
        return set(json.loads(p.read_bytes()).get("processed_ciks", []))
    except Exception as exc:
        logger.warning("bad checkpoint (%s); starting fresh", exc)
        return set()


def save_checkpoint(processed: set[str]) -> None:
    p = _checkpoint_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "processed_ciks": sorted(processed),
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }, indent=2), encoding="utf-8")


# ── Filing iteration ─────────────────────────────────────────────────────────

def iter_13f_filings(
    submissions: dict[str, Any],
    since_year: int | None,
    provider: SecEdgarProvider | None,
) -> Iterable[Filing]:
    rows: list[dict] = list(iter_filings_rows(submissions.get("filings", {}).get("recent", {})))
    if provider is not None:
        rows.extend(iter_extra_filings(submissions, provider))

    for row in rows:
        form = row.get("form", "")
        if form not in _FORMS:
            continue
        filing_date = row.get("filingDate", "")
        if not filing_date or len(filing_date) < 4:
            continue
        try:
            year = int(filing_date[:4])
        except ValueError:
            continue
        if since_year is not None and year < since_year:
            continue

        accession = row.get("accessionNumber", "")
        primary = row.get("primaryDocument", "")
        period = row.get("reportDate") or row.get("periodOfReport") or ""
        if not accession or not period:
            continue

        yield Filing(
            accession=accession,
            filing_date=filing_date,
            period_of_report=period,
            primary_document=primary,
            form=form,
            is_amendment=form.endswith("/A"),
        )


# ── Info-table XML fetch ─────────────────────────────────────────────────────

def _xml_cache_path(cik: str, accession: str) -> Path:
    cik10 = str(cik).zfill(10)
    acc_nd = accession.replace("-", "")
    return Path(settings.sec_data_dir) / "13f" / f"{cik10}_{acc_nd}.xml"


def fetch_infotable_xml(
    provider: SecEdgarProvider,
    cik: str,
    accession: str,
    *,
    refresh: bool = False,
) -> bytes | None:
    """Fetch the informationTable XML for a 13F filing.

    Uses the accession's index.json to locate the actual XML filename (varies
    by filer) and caches the raw bytes under {sec_data_dir}/13f/.
    Returns None if no info-table XML is present (e.g. 13F-NT notices).
    """
    path = _xml_cache_path(cik, accession)
    if path.exists() and not refresh:
        return path.read_bytes()

    cik_int = int(cik)
    acc_nd = accession.replace("-", "")
    index_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nd}/index.json"

    try:
        index = json.loads(provider._get(index_url))
    except Exception as exc:
        logger.warning("index.json fetch failed for %s/%s: %s", cik, accession, exc)
        return None

    items = index.get("directory", {}).get("item", [])
    xml_name = None
    for it in items:
        name = it.get("name", "")
        if name.lower().endswith(".xml") and _INFOTABLE_RE.search(name):
            xml_name = name
            break
    if not xml_name:
        logger.debug("no info-table XML in %s/%s (likely 13F-NT)", cik, accession)
        return None

    xml_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nd}/{xml_name}"
    try:
        body = provider._get(xml_url)
    except Exception as exc:
        logger.warning("XML fetch failed for %s: %s", xml_url, exc)
        return None

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return body


# ── Main run ─────────────────────────────────────────────────────────────────

def run(args: argparse.Namespace) -> int:
    client = get_arango_client()
    db = ensure_database(client)
    ensure_collections(db)

    provider = SecEdgarProvider()
    resolver = CusipResolver(db, cache_path=Path(settings.sec_data_dir) / "cusip_to_qid.json")

    pairs: list[tuple[str, str]] = list(iter_mapped_org_ciks(db))
    logger.info("loaded %d org CIK mappings", len(pairs))
    if not pairs:
        logger.error("no org CIKs — run build_cik_map.py first")
        return 1

    if args.cik:
        wanted = {str(c).lstrip("0").zfill(10) for c in args.cik}
        pairs = [(c, q) for (c, q) in pairs if str(c).lstrip("0").zfill(10) in wanted]
    if args.limit_ciks:
        pairs = pairs[: args.limit_ciks]

    processed: set[str] = set() if args.no_resume else load_checkpoint()
    totals: Counter = Counter()

    for idx, (cik, manager_qid) in enumerate(pairs, start=1):
        if cik in processed:
            totals["skipped_checkpoint"] += 1
            continue

        logger.info("[%d/%d] 13F for CIK %s (org=%s)", idx, len(pairs), cik, manager_qid)

        try:
            submissions = provider.fetch_submissions_for_cik(cik)
        except Exception as exc:
            logger.warning("CIK %s: submissions fetch failed — %s", cik, exc)
            totals["http_errors"] += 1
            continue

        filings = list(iter_13f_filings(submissions, since_year=args.since_year, provider=provider))
        if not filings:
            processed.add(cik)
            if not args.no_resume and not args.dry_run:
                save_checkpoint(processed)
            continue

        totals["managers_processed"] += 1

        for f in filings:
            xml = fetch_infotable_xml(provider, cik, f.accession, refresh=args.refresh)
            if not xml:
                totals["no_infotable"] += 1
                continue

            try:
                rows = parse_13f_xml(xml)
            except Exception as exc:
                logger.warning("parse failed %s/%s: %s", cik, f.accession, exc)
                totals["parse_errors"] += 1
                continue

            if not rows:
                totals["empty_filings"] += 1
                continue

            agg = aggregate_by_cusip(rows)
            edges, unmatched = build_edge_docs(
                manager_qid=manager_qid,
                period_of_report=f.period_of_report,
                filing_date=f.filing_date,
                accession=f.accession,
                holdings_by_cusip=agg,
                resolver=resolver,
                is_amendment=f.is_amendment,
            )

            totals["filings_processed"] += 1
            totals["rows_parsed"] += len(rows)
            totals["issuers_aggregated"] += len(agg)
            totals["edges_built"] += len(edges)
            totals["unmatched_cusips"] += len(unmatched)

            if not args.dry_run:
                try:
                    upsert_edges(db, AFFILIATIONS, edges)
                except Exception as exc:
                    logger.warning("edge upsert failed %s/%s: %s", cik, f.accession, exc)
                    totals["upsert_errors"] += 1
                if unmatched:
                    try:
                        merge_unmatched_holdings(db, ORGANIZATIONS, manager_qid, unmatched)
                    except Exception as exc:
                        logger.warning("unmatched merge failed %s: %s", cik, exc)

            logger.info(
                "[13F] %s %s period=%s issuers=%d edges=%d unmatched=%d",
                cik, f.filing_date, f.period_of_report,
                len(agg), len(edges), len(unmatched),
            )

        processed.add(cik)
        if not args.no_resume and not args.dry_run:
            save_checkpoint(processed)
            resolver.save()

    resolver.save()

    logger.info(
        "fetch_13f DONE — managers=%d filings=%d edges=%d unmatched_cusips=%d "
        "errors(http=%d parse=%d upsert=%d) skipped=%d",
        totals["managers_processed"], totals["filings_processed"],
        totals["edges_built"], totals["unmatched_cusips"],
        totals["http_errors"], totals["parse_errors"], totals["upsert_errors"],
        totals["skipped_checkpoint"],
    )
    print("")
    print("=" * 60)
    print("  fetch_13f SUMMARY")
    print("=" * 60)
    for k in sorted(totals):
        print(f"  {k:28s} {totals[k]}")
    cache_stats = resolver.stats()
    print(f"  cusip_cache_total           {cache_stats['total']}")
    print(f"  cusip_cache_matched         {cache_stats['matched']}")
    print("=" * 60)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Bulk-load SEC 13F-HR institutional holdings.")
    p.add_argument("--since-year", type=int, default=None)
    p.add_argument("--limit-ciks", type=int, default=0)
    p.add_argument("--cik", action="append", default=[], help="Restrict to specific CIK(s); repeatable.")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--no-resume", action="store_true")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
