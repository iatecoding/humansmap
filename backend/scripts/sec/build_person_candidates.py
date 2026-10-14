"""Populate sec_person_candidates from Wikidata per mapped org.

§10.2.d of the project design notes.

What it does
------------
1. Reads all org QIDs from sec_cik_map where entity_kind == "org".
2. Loads the persons collection _key set into memory (additive filter).
3. Chunks org QIDs into batches of --batch-size (default 50).
4. For each batch, runs a SPARQL query against Wikidata that finds persons
   linked to those orgs via:
     - wdt:P108  (employer)
     - wdt:P39 (position held) + wdt:P642 (of) = org
     - wdt:P488  (chairperson)
     - wdt:P3320 (board member — forward: person → org)
     - wdt:P169  (chief executive officer — subject is org, object is person)
     - wdt:P1037 (director / manager)
     - p:P3320/ps:P3320 (board member — reverse, all ranks: org → person)
       S&P 500 companies store board members on the org entity (§8 audit)
   Also fetches skos:altLabel for each person to build name_variants[].
5. Caches each batch response under {sec_data_dir}/sparql_cache/person_candidates/{hash}.json.
6. Supports resumable checkpoint: already-processed batch hashes are skipped on --resume.
7. Filters out candidates whose person_qid is not in our local persons collection.
8. Collapses (org, person) pairs that surface via multiple sources into one doc with
   source joined alphabetically (e.g. "P108+P39_P642").
9. Upserts into sec_person_candidates in batches of 500. Idempotent.
10. Prints a final summary.

Usage (user runs these — do NOT invoke inside this script)
----------------------------------------------------------
    # Dry run:
    docker compose exec backend python -m scripts.sec.build_person_candidates --dry-run

    # Full run:
    docker compose exec backend python -m scripts.sec.build_person_candidates

    # Process only the first 5 orgs (quick sanity check):
    docker compose exec backend python -m scripts.sec.build_person_candidates --limit 5 --dry-run

    # Force re-fetch all SPARQL batches:
    docker compose exec backend python -m scripts.sec.build_person_candidates --refresh

Flags
-----
--limit N          Cap number of org QIDs processed (0 = no limit).
--dry-run          Parse and join but skip all DB writes.
--verbose          Set log level to DEBUG.
--refresh          Ignore per-batch SPARQL cache and re-fetch.
--resume           Skip already-processed batches via checkpoint (default ON).
--no-resume        Disable checkpoint; process all batches.
--batch-size N     Org QIDs per SPARQL VALUES clause (default 50).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
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
    SEC_PERSON_CANDIDATES,
    ensure_collections,
    ensure_database,
    get_arango_client,
)

logger = logging.getLogger("build_person_candidates")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SPARQL_ENDPOINT = settings.sparql_endpoint
USER_AGENT = settings.sparql_user_agent

WD_ENTITY_PREFIX = "http://www.wikidata.org/entity/"

CACHE_SUBDIR = "sparql_cache"
CACHE_PERSON_DIR = "person_candidates"
CHECKPOINT_FILENAME = "person_candidates_checkpoint.json"

UPSERT_BATCH_SIZE = 500

# Exponential backoff delays on 429/503 (seconds)
BACKOFF_SEQUENCE = [2, 8, 30, 120]

# ---------------------------------------------------------------------------
# Pure helpers — easy to unit-test
# ---------------------------------------------------------------------------


def batch_list(items: list, size: int) -> list[list]:
    """Split *items* into successive sub-lists of at most *size* elements.

    >>> batch_list([1,2,3,4,5], 2)
    [[1, 2], [3, 4], [5]]
    >>> batch_list([], 10)
    []
    """
    if size <= 0:
        raise ValueError("batch size must be positive")
    return [items[i : i + size] for i in range(0, len(items), size)]


def batch_hash(qids: list[str]) -> str:
    """Return a deterministic SHA-256 hex digest for a list of QIDs.

    Input order does not matter — QIDs are sorted before hashing.

    >>> batch_hash(["Q380", "Q312"]) == batch_hash(["Q312", "Q380"])
    True
    >>> len(batch_hash(["Q312"])) == 64
    True
    """
    key = ",".join(sorted(qids))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def build_sparql_query(org_qids: list[str]) -> str:
    """Build the SPARQL query for a batch of org QIDs.

    Finds persons via:
      - P108  (employer)
      - P39+P642 (position held / of)
      - P488  (chairperson)
      - P3320 (board member — forward: person → org)
      - P169  (chief executive officer — subject is org, object is person)
      - P1037 (director / manager)
      - P3320_rev (board member — reverse, all ranks: org → person via p:/ps:)
        Wikidata stores board members on the org entity (wd:Q312 p:P3320 ?stmt),
        not on the person. The simple wdt:P3320 forward branch catches only cases
        where the editor put the statement on the person side (rare). The reverse
        all-rank branch (p:P3320/ps:P3320) is the authoritative path for S&P 500
        companies (Apple, Microsoft, Berkshire confirmed in §8 audit).

    All paths emit a ?source binding for traceability.
    skos:altLabel is fetched so downstream matching can try name variants.
    """
    values_clause = " ".join(f"wd:{qid}" for qid in sorted(org_qids))
    return f"""SELECT ?person ?personLabel ?personAltLabel ?org ?source WHERE {{
  VALUES ?org {{ {values_clause} }}
  {{
    ?person wdt:P108 ?org.
    BIND("P108" AS ?source)
  }} UNION {{
    ?person wdt:P39 ?pos.
    ?pos wdt:P642 ?org.
    BIND("P39_P642" AS ?source)
  }} UNION {{
    ?person wdt:P488 ?org.
    BIND("P488" AS ?source)
  }} UNION {{
    ?person wdt:P3320 ?org.
    BIND("P3320" AS ?source)
  }} UNION {{
    ?org wdt:P169 ?person.
    BIND("P169" AS ?source)
  }} UNION {{
    ?person wdt:P1037 ?org.
    BIND("P1037" AS ?source)
  }} UNION {{
    ?org p:P3320 ?_stmt .
    ?_stmt ps:P3320 ?person .
    BIND("P3320_rev" AS ?source)
  }}
  SERVICE wikibase:label {{
    bd:serviceParam wikibase:language "en".
    ?person rdfs:label ?personLabel.
    ?person skos:altLabel ?personAltLabel.
  }}
}}"""


def uri_to_qid(uri: str | None) -> str | None:
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


def collapse_source(sources: set[str]) -> str:
    """Join multiple source strings alphabetically with '+'.

    >>> collapse_source({"P108"})
    'P108'
    >>> collapse_source({"P39_P642", "P108"})
    'P108+P39_P642'
    """
    return "+".join(sorted(sources))


def parse_sparql_rows(
    bindings: list[dict],
    known_person_qids: set[str],
) -> dict[tuple[str, str], dict]:
    """Parse SPARQL result rows into (org_qid, person_qid) → candidate doc.

    Drops rows where person_qid is not in *known_person_qids* (additive filter).
    Collapses multiple rows for the same (org, person) pair by joining sources.

    Also collects skos:altLabel values into name_variants (a set of strings)
    on each pair dict. The canonical personLabel is always included in the set.
    Deduplication is case-insensitive; first-seen casing is preserved per form.

    Returns a mapping (org_qid, person_qid) → candidate document (without _key).
    """
    by_pair: dict[tuple[str, str], dict] = {}
    # Track lower-cased forms already recorded, per pair, to preserve first-seen casing
    seen_lower: dict[tuple[str, str], set[str]] = {}

    for row in bindings:
        person_uri = row.get("person", {}).get("value", "")
        org_uri = row.get("org", {}).get("value", "")
        person_qid = uri_to_qid(person_uri)
        org_qid = uri_to_qid(org_uri)

        if not person_qid or not org_qid:
            continue

        # Additive filter: only keep persons already in our graph
        if person_qid not in known_person_qids:
            continue

        source = row.get("source", {}).get("value", "")
        label = row.get("personLabel", {}).get("value") or ""
        alt_label = row.get("personAltLabel", {}).get("value") or ""

        pair = (org_qid, person_qid)
        if pair not in by_pair:
            by_pair[pair] = {
                "org_qid": org_qid,
                "person_qid": person_qid,
                "person_name_en": label,
                "sources": {source} if source else set(),
                "name_variants": set(),
            }
            seen_lower[pair] = set()
            # Seed with the canonical label
            if label:
                by_pair[pair]["name_variants"].add(label)
                seen_lower[pair].add(label.lower())
        else:
            if source:
                by_pair[pair]["sources"].add(source)
            # Update canonical label if we have a better one (non-empty)
            if label and not by_pair[pair]["person_name_en"]:
                by_pair[pair]["person_name_en"] = label
                # Also add the newly-seen canonical label to variants
                if label.lower() not in seen_lower[pair]:
                    by_pair[pair]["name_variants"].add(label)
                    seen_lower[pair].add(label.lower())

        # Accumulate alt label pieces (case-insensitive dedup, preserve first-seen casing).
        # Wikidata's wikibase:label SERVICE aggregates skos:altLabel into a single
        # comma-joined string per person (e.g. "Reid G. Hoffman, Reid Garrett Hoffman"),
        # so we split on ", " before adding each piece as a separate variant.
        if alt_label:
            for piece in alt_label.split(", "):
                piece = piece.strip()
                if piece and piece.lower() not in seen_lower[pair]:
                    by_pair[pair]["name_variants"].add(piece)
                    seen_lower[pair].add(piece.lower())

    return by_pair


def build_candidate_docs(
    by_pair: dict[tuple[str, str], dict],
    now_iso: str,
) -> list[dict]:
    """Convert internal pair dict to ArangoDB-ready documents.

    Assigns _key = "{org_qid}_{person_qid}" and collapses sources.
    Emits name_variants as a sorted list for stable, idempotent upserts.
    """
    docs = []
    for (org_qid, person_qid), data in by_pair.items():
        docs.append(
            {
                "_key": f"{org_qid}_{person_qid}",
                "org_qid": org_qid,
                "person_qid": person_qid,
                "person_name_en": data["person_name_en"],
                "source": collapse_source(data["sources"]) if data["sources"] else "",
                "name_variants": sorted(data.get("name_variants", set())),
                "last_synced": now_iso,
            }
        )
    return docs


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------


def cache_dir() -> Path:
    return Path(settings.sec_data_dir) / CACHE_SUBDIR / CACHE_PERSON_DIR


def cache_file(batch_hash_hex: str) -> Path:
    return cache_dir() / f"{batch_hash_hex}.json"


def load_batch_cache(batch_hash_hex: str) -> dict | None:
    p = cache_file(batch_hash_hex)
    if p.exists():
        logger.debug("Cache hit for batch %s", batch_hash_hex[:12])
        return json.loads(p.read_bytes())
    return None


def save_batch_cache(batch_hash_hex: str, data: dict) -> None:
    p = cache_file(batch_hash_hex)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf-8")
    logger.debug("Cached batch %s", batch_hash_hex[:12])


# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------


def checkpoint_path() -> Path:
    return Path(settings.sec_data_dir) / CACHE_SUBDIR / CHECKPOINT_FILENAME


def load_checkpoint() -> set[str]:
    """Return the set of already-processed batch hashes."""
    p = checkpoint_path()
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_bytes())
        return set(data.get("processed_batches", []))
    except Exception as exc:
        logger.warning("Could not read checkpoint file (%s); starting fresh", exc)
        return set()


def save_checkpoint(processed: set[str]) -> None:
    p = checkpoint_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "processed_batches": sorted(processed),
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# SPARQL fetch with backoff
# ---------------------------------------------------------------------------


def fetch_sparql_with_backoff(query: str) -> dict | None:
    """Run a SPARQL query. On 429/503 apply exponential backoff.

    Returns the parsed JSON dict, or None if all retries are exhausted.
    """
    params = urllib.parse.urlencode({"query": query, "format": "json"})
    url = f"{SPARQL_ENDPOINT}?{params}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/sparql-results+json",
        },
    )

    for attempt, delay in enumerate(BACKOFF_SEQUENCE, start=1):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = resp.read()
            return json.loads(body)
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 503):
                logger.warning(
                    "HTTP %d on SPARQL attempt %d/%d — backing off %ds",
                    exc.code, attempt, len(BACKOFF_SEQUENCE), delay,
                )
                time.sleep(delay)
            else:
                raise
        except Exception as exc:
            logger.error("SPARQL request failed: %s", exc)
            raise

    logger.error(
        "All %d SPARQL backoff attempts exhausted. Aborting this run. "
        "Checkpoint will allow resume.",
        len(BACKOFF_SEQUENCE),
    )
    return None


# ---------------------------------------------------------------------------
# ArangoDB helpers
# ---------------------------------------------------------------------------


def load_sec_org_qids(db) -> list[str]:
    """Return QIDs of all orgs in sec_cik_map."""
    aql = f"""
    FOR doc IN {SEC_CIK_MAP}
      FILTER doc.entity_kind == "org"
      RETURN doc.wikidata_id
    """
    cursor = db.aql.execute(aql)
    return [qid for qid in cursor if qid]


def load_person_qids(db) -> set[str]:
    """Return the set of _key values from the persons collection."""
    aql = f"FOR doc IN {PERSONS} RETURN doc._key"
    cursor = db.aql.execute(aql)
    return set(cursor)


def upsert_candidates_batch(db, batch: list[dict], dry_run: bool) -> None:
    """Upsert a batch of sec_person_candidates documents. Idempotent."""
    if dry_run:
        logger.debug("DRY RUN: would upsert %d rows into %s", len(batch), SEC_PERSON_CANDIDATES)
        return
    aql = f"""
    FOR doc IN @batch
      INSERT doc INTO {SEC_PERSON_CANDIDATES}
      OPTIONS {{ overwrite: true }}
    """
    db.aql.execute(aql, bind_vars={"batch": batch})


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------


def run(args: argparse.Namespace) -> int:
    """Execute the person-candidates build. Returns exit code (0 = success)."""

    # --- Connect to DB -------------------------------------------------------
    client = get_arango_client()
    db = ensure_database(client)
    ensure_collections(db)

    # --- Load org QIDs from sec_cik_map -------------------------------------
    logger.info("Loading mapped org QIDs from %s …", SEC_CIK_MAP)
    org_qids = load_sec_org_qids(db)
    logger.info("Found %d org QIDs in %s", len(org_qids), SEC_CIK_MAP)

    if not org_qids:
        logger.error(
            "No org QIDs found in %s. Run build_cik_map.py first.", SEC_CIK_MAP
        )
        return 1

    # --- Optionally cap the org list ----------------------------------------
    if args.limit:
        org_qids = org_qids[: args.limit]
        logger.info("Limiting to %d org QIDs (--limit)", args.limit)

    # --- Load persons set (additive filter) ---------------------------------
    logger.info("Loading person QIDs from %s …", PERSONS)
    known_person_qids = load_person_qids(db)
    logger.info("%d person QIDs loaded", len(known_person_qids))

    # --- Chunk orgs into batches --------------------------------------------
    batches = batch_list(org_qids, args.batch_size)
    logger.info(
        "%d org QIDs split into %d batches of up to %d",
        len(org_qids), len(batches), args.batch_size,
    )

    # --- Load checkpoint ----------------------------------------------------
    processed_hashes: set[str] = set()
    if args.resume:
        processed_hashes = load_checkpoint()
        if processed_hashes:
            logger.info(
                "Checkpoint: %d batches already processed (will skip)",
                len(processed_hashes),
            )

    # --- Per-batch SPARQL loop -----------------------------------------------
    now_iso = datetime.now(timezone.utc).isoformat()

    all_pairs: dict[tuple[str, str], dict] = {}
    total_raw_rows = 0
    total_dropped = 0

    for batch_idx, qid_batch in enumerate(batches, start=1):
        h = batch_hash(qid_batch)

        if args.resume and h in processed_hashes:
            logger.info("Batch %d/%d: skipping (checkpoint hit)", batch_idx, len(batches))
            continue

        # Try cache first (unless --refresh)
        raw: dict | None = None
        if not args.refresh:
            raw = load_batch_cache(h)

        if raw is None:
            logger.info(
                "Batch %d/%d: fetching SPARQL for %d orgs …",
                batch_idx, len(batches), len(qid_batch),
            )
            query = build_sparql_query(qid_batch)
            raw = fetch_sparql_with_backoff(query)
            if raw is None:
                # Backoff exhausted — save checkpoint and abort
                save_checkpoint(processed_hashes)
                return 2
            save_batch_cache(h, raw)
            # Rate limiting between HTTP calls
            if batch_idx < len(batches):
                logger.debug("Rate-limit sleep %.1fs", settings.sparql_delay_seconds)
                time.sleep(settings.sparql_delay_seconds)
        else:
            logger.info(
                "Batch %d/%d: loaded from cache (%d orgs)",
                batch_idx, len(batches), len(qid_batch),
            )

        bindings: list[dict] = raw.get("results", {}).get("bindings", [])
        batch_raw_count = len(bindings)
        total_raw_rows += batch_raw_count

        # Parse + filter
        batch_pairs = parse_sparql_rows(bindings, known_person_qids)
        batch_dropped = batch_raw_count - sum(
            # count unique (org, person) rows that were not dropped
            1 for row in bindings
            if uri_to_qid(row.get("person", {}).get("value", "")) in known_person_qids
               and uri_to_qid(row.get("org", {}).get("value", "")) is not None
        )
        total_dropped += batch_dropped

        # Merge batch pairs into global dict (collapse sources across batches too)
        for pair, data in batch_pairs.items():
            if pair not in all_pairs:
                all_pairs[pair] = data
            else:
                all_pairs[pair]["sources"].update(data["sources"])

        logger.debug(
            "Batch %d/%d: %d raw rows, %d kept pairs (cumulative %d pairs)",
            batch_idx, len(batches), batch_raw_count,
            len(batch_pairs), len(all_pairs),
        )

        processed_hashes.add(h)
        # Save checkpoint after every batch so partial progress is not lost
        if not args.dry_run:
            save_checkpoint(processed_hashes)

    # --- Build final docs ---------------------------------------------------
    candidate_docs = build_candidate_docs(all_pairs, now_iso)
    unique_pairs = len(candidate_docs)

    # --- Upsert to ArangoDB -------------------------------------------------
    logger.info(
        "Upserting %d candidate docs into %s …", unique_pairs, SEC_PERSON_CANDIDATES
    )
    for i in range(0, unique_pairs, UPSERT_BATCH_SIZE):
        chunk = candidate_docs[i : i + UPSERT_BATCH_SIZE]
        upsert_candidates_batch(db, chunk, args.dry_run)
        logger.debug("  upserted docs %d–%d", i, i + len(chunk))

    # --- Source breakdown ---------------------------------------------------
    p108_only = sum(1 for d in candidate_docs if d["source"] == "P108")
    p39_only = sum(1 for d in candidate_docs if d["source"] == "P39_P642")
    both = sum(1 for d in candidate_docs if d["source"] == "P108+P39_P642")

    # --- Summary ------------------------------------------------------------
    print("")
    print("=" * 60)
    print("  build_person_candidates SUMMARY")
    print("=" * 60)
    print(f"  Org QIDs queried        : {len(org_qids):>8,}")
    print(f"  SPARQL batches          : {len(batches):>8,}")
    print(f"  Raw candidate rows      : {total_raw_rows:>8,}")
    print(f"  Dropped (not in graph)  : {total_dropped:>8,}")
    print(f"  Unique (org, person) pairs written : {unique_pairs:>5,}")
    print(f"  Source breakdown:")
    print(f"    P108 only             : {p108_only:>8,}")
    print(f"    P39_P642 only         : {p39_only:>8,}")
    print(f"    Both (P108+P39_P642)  : {both:>8,}")
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
            "Populate sec_person_candidates from Wikidata P108/P39+P642 per "
            "mapped-CIK org. Used by downstream SEC ingestion for Tier-1 "
            "name matching (§3.2 of phase-10-sec-edgar.md)."
        )
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        metavar="N",
        help="Process only the first N org QIDs (0 = no limit; useful for testing).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and join but skip all DB writes.",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Set log level to DEBUG.",
    )
    p.add_argument(
        "--refresh",
        action="store_true",
        help="Ignore per-batch SPARQL cache and re-fetch from Wikidata.",
    )
    resume_group = p.add_mutually_exclusive_group()
    resume_group.add_argument(
        "--resume",
        dest="resume",
        action="store_true",
        default=True,
        help="Skip already-processed batches via checkpoint (default ON).",
    )
    resume_group.add_argument(
        "--no-resume",
        dest="resume",
        action="store_false",
        help="Disable checkpoint; process all batches.",
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=50,
        metavar="N",
        help="Number of org QIDs per SPARQL VALUES clause (default 50).",
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
