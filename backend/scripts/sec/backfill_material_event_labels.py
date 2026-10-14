"""One-time backfill: populate name_en / name_it on existing material_event docs.

Every doc in the `events` collection where type == "material_event" and
name_en or name_it is missing/null gets a human-readable label derived from
its subtype, filing org name, filing_date, and is_amendment flag.

The label templates are shared with fetch_material_events via the helper
:func:`build_material_event_labels` imported from this module.

Usage (run inside the already-running backend container)
---------------------------------------------------------
    # Preview ~10 samples without writing:
    docker compose exec -T -u appuser backend \\
        python -m scripts.sec.backfill_material_event_labels --dry-run

    # Full backfill (writes to DB):
    docker compose exec -T -u appuser backend \\
        python -m scripts.sec.backfill_material_event_labels
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.database import (  # noqa: E402
    EVENTS,
    ORGANIZATIONS,
    ensure_collections,
    ensure_database,
    get_arango_client,
)

logger = logging.getLogger("backfill_material_event_labels")

# ---------------------------------------------------------------------------
# Shared label helper — single source of truth used here and by
# fetch_material_events.build_event_doc()
# ---------------------------------------------------------------------------

_LABEL_TEMPLATES: dict[str, tuple[str, str]] = {
    "exec_departure": (
        "Executive departure — {org} ({filing_date})",
        "Uscita di dirigente — {org} ({filing_date})",
    ),
    "bankruptcy_filed": (
        "Bankruptcy filing — {org} ({filing_date})",
        "Istanza di fallimento — {org} ({filing_date})",
    ),
    "acquisition_announced": (
        "Acquisition announced — {org} ({filing_date})",
        "Acquisizione annunciata — {org} ({filing_date})",
    ),
}

_FALLBACK_TEMPLATES: tuple[str, str] = (
    "8-K filing — {org} ({filing_date})",
    "Deposito 8-K — {org} ({filing_date})",
)

_AMENDMENT_PREFIX_EN = "[Amendment] "
_AMENDMENT_PREFIX_IT = "[Emendamento] "


def build_material_event_labels(
    subtype: str,
    org_name_en: str | None,
    org_name_it: str | None,
    filing_date: str,
    is_amendment: bool,
) -> tuple[str, str]:
    """Return (name_en, name_it) for a material_event document.

    Looks up the label template for *subtype* (falls back to a generic 8-K
    label for unknown subtypes).  Uses *org_name_en* / *org_name_it* as the
    org placeholder; either may be None in which case the other is used, and
    if both are None an empty string is substituted.

    Prepends amendment prefixes when *is_amendment* is True.

    >>> build_material_event_labels("exec_departure", "Acme Corp", "Acme Corp", "2024-03-15", False)
    ('Executive departure — Acme Corp (2024-03-15)', 'Uscita di dirigente — Acme Corp (2024-03-15)')
    >>> build_material_event_labels("bankruptcy_filed", "Beta Inc", None, "2023-07-01", True)
    ('[Amendment] Bankruptcy filing — Beta Inc (2023-07-01)', '[Emendamento] Istanza di fallimento — Beta Inc (2023-07-01)')
    >>> build_material_event_labels("unknown_type", None, None, "2022-01-01", False)
    ('8-K filing —  (2022-01-01)', 'Deposito 8-K —  (2022-01-01)')
    """
    tmpl_en, tmpl_it = _LABEL_TEMPLATES.get(subtype, _FALLBACK_TEMPLATES)

    org_en = org_name_en or org_name_it or ""
    org_it = org_name_it or org_name_en or ""

    name_en = tmpl_en.format(org=org_en, filing_date=filing_date)
    name_it = tmpl_it.format(org=org_it, filing_date=filing_date)

    if is_amendment:
        name_en = _AMENDMENT_PREFIX_EN + name_en
        name_it = _AMENDMENT_PREFIX_IT + name_it

    return name_en, name_it


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

BATCH_SIZE = 500


def load_org_name_cache(db) -> dict[str, tuple[str | None, str | None]]:
    """Return {qid: (name_en, name_it)} for all orgs in one AQL query."""
    aql = f"""
    FOR org IN {ORGANIZATIONS}
      FILTER org._key != null
      RETURN [org._key, org.name_en, org.name_it]
    """
    cursor = db.aql.execute(aql, ttl=300)
    cache: dict[str, tuple[str | None, str | None]] = {}
    for row in cursor:
        qid, name_en, name_it = row[0], row[1], row[2]
        cache[qid] = (name_en or None, name_it or None)
    return cache


def fetch_unlabelled_events(db) -> list[dict]:
    """Return all material_event docs missing name_en or name_it."""
    aql = f"""
    FOR doc IN {EVENTS}
      FILTER doc.type == "material_event"
      FILTER doc.name_en == null OR doc.name_en == ""
         OR doc.name_it == null OR doc.name_it == ""
      RETURN doc
    """
    cursor = db.aql.execute(aql, ttl=600)
    return list(cursor)


def bulk_update_labels(
    db,
    updates: list[dict],
    dry_run: bool,
) -> None:
    """Write a batch of {_key, name_en, name_it} patches to the events collection."""
    if dry_run or not updates:
        return

    aql = f"""
    FOR patch IN @updates
      UPDATE patch._key WITH {{ name_en: patch.name_en, name_it: patch.name_it }}
        IN {EVENTS}
        OPTIONS {{ keepNull: false }}
    """
    db.aql.execute(aql, bind_vars={"updates": updates})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run(args: argparse.Namespace) -> int:
    client = get_arango_client()
    db = ensure_database(client)
    ensure_collections(db)

    logger.info("Loading org name cache from %s …", ORGANIZATIONS)
    org_cache = load_org_name_cache(db)
    logger.info("Cached %d org names", len(org_cache))

    logger.info("Fetching unlabelled material_event docs from %s …", EVENTS)
    docs = fetch_unlabelled_events(db)
    logger.info("Found %d docs to process", len(docs))

    stats = {"updated": 0, "skipped": 0, "org_missing": 0}

    batch: list[dict] = []
    sample_shown = 0

    for i, doc in enumerate(docs):
        key = doc.get("_key", "")
        name_en_existing = doc.get("name_en") or ""
        name_it_existing = doc.get("name_it") or ""

        if name_en_existing and name_it_existing:
            stats["skipped"] += 1
            continue

        subtype = doc.get("subtype", "")
        filing_date = doc.get("filing_date", "")
        is_amendment = bool(doc.get("is_amendment", False))
        org_qid = doc.get("org_qid") or ""
        org_cik = doc.get("org_cik") or ""

        if org_qid in org_cache:
            org_name_en, org_name_it = org_cache[org_qid]
        else:
            # Fall back to CIK as display label
            org_name_en = org_cik or org_qid or None
            org_name_it = org_cik or org_qid or None
            if org_qid:
                stats["org_missing"] += 1

        name_en, name_it = build_material_event_labels(
            subtype, org_name_en, org_name_it, filing_date, is_amendment
        )

        if args.dry_run and sample_shown < 10:
            print(f"  key={key!r:>22s}  EN={name_en!r}")
            print(f"  {'':>22s}  IT={name_it!r}")
            sample_shown += 1

        batch.append({"_key": key, "name_en": name_en, "name_it": name_it})
        stats["updated"] += 1

        if len(batch) >= BATCH_SIZE:
            bulk_update_labels(db, batch, args.dry_run)
            batch.clear()
            logger.info(
                "Progress: %d / %d processed …", i + 1, len(docs)
            )

    # Flush remainder
    bulk_update_labels(db, batch, args.dry_run)

    print("")
    print("=" * 60)
    print("  backfill_material_event_labels SUMMARY")
    print("=" * 60)
    print(f"  Docs examined            : {len(docs):>8,}")
    print(f"  Updated (or would)       : {stats['updated']:>8,}")
    print(f"  Skipped (already labelled): {stats['skipped']:>7,}")
    print(f"  Org doc missing          : {stats['org_missing']:>8,}")
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
            "Backfill name_en / name_it on material_event docs that are "
            "missing human-readable labels."
        )
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print ~10 sample labels without writing to the DB.",
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
