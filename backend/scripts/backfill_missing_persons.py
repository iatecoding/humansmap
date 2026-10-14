"""Backfill missing persons from Wikidata API.

Identifies persons that should be in the DB but are missing — due to either:
  1. Dump-processing gaps (entities skipped during local latest-all processing)
  2. Historical-state P27 values (Newton → Kingdom of England, not UK)

Fetches via the Wikidata wbgetentities API. Uses the same parse_json_entity →
build_person_doc → build_edge_docs pipeline as load_from_dump.py so documents
are schema-identical to dump-loaded ones.

The --include-historical flag extends discovery to catch persons whose P27
(citizenship) points to a dissolved historical state (e.g. Kingdom of France,
Soviet Union, Duchy of Milan) rather than the modern successor state. These
are mapped to the modern ISO code for the nationalities field.

Usage (inside backend container):
  # Dry run — just identify and count missing persons:
  python /app/scripts/backfill_missing_persons.py --dry-run

  # Backfill persons with ≥150 statements (high-profile):
  python /app/scripts/backfill_missing_persons.py --min-statements 150

  # Include historical states (Newton, Napoleon, Voltaire…):
  python /app/scripts/backfill_missing_persons.py --min-statements 150 --include-historical

  # Backfill everything ≥30 (full gap closure):
  python /app/scripts/backfill_missing_persons.py --min-statements 30

  # Backfill specific QIDs only:
  python /app/scripts/backfill_missing_persons.py --qids Q76,Q935,Q312

Env vars:
  SPARQL_DELAY    seconds between SPARQL queries (default: 2.0)
  API_DELAY       seconds between wbgetentities calls (default: 1.0)
  API_BATCH_SIZE  entities per wbgetentities call (default: 50, max 50)
"""

import argparse
import json
import logging
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from urllib.error import HTTPError

sys.path.insert(0, "/app")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.database import (
    init_db, PERSONS, ORGANIZATIONS, EVENTS,
    RELATIONSHIPS, AFFILIATIONS, INVOLVEMENTS, ORG_STRUCTURE,
)
from app.config import settings
from scripts.load_from_dump import (
    TARGET_COUNTRY_QIDS,
    NATIONALITY_MAP,
    parse_json_entity,
    classify_entity,
    build_person_doc,
    build_org_doc,
    build_event_doc,
    build_edge_docs,
    build_reverse_edge_docs,
    build_org_structure_edge_docs,
    ORG_REVERSE_PROPERTIES,
    EVENT_REVERSE_PROPERTIES,
    batch_upsert,
    merge_occupations,
    sanitize_key,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("backfill")

SPARQL_ENDPOINT = settings.sparql_endpoint
USER_AGENT = settings.sparql_user_agent
SPARQL_DELAY = float(os.getenv("SPARQL_DELAY", "2.0"))
API_DELAY = float(os.getenv("API_DELAY", "1.0"))
API_BATCH_SIZE = int(os.getenv("API_BATCH_SIZE", "50"))
WIKIDATA_API = "https://www.wikidata.org/w/api.php"

# Historical / dissolved states → modern ISO code. These entities appear as P27
# values on historical figures (Newton, Napoleon, Voltaire, Caravaggio…) whose
# modern-state P27 claims are deprecated or absent on Wikidata.
HISTORICAL_STATE_MAP = {
    # British Isles
    "Q179876":  "GB",   # Kingdom of England
    "Q161885":  "GB",   # Kingdom of Great Britain
    "Q174193":  "GB",   # Kingdom of Ireland (pre-1801)
    "Q12548":   "GB",   # Holy Roman Empire (many have GB as successor)
    "Q157367":  "GB",   # Kingdom of Hanover (personal union)
    # France
    "Q145640":  "FR",   # Kingdom of France
    "Q70802":   "FR",   # French First Republic
    "Q71084":   "FR",   # First French Empire
    "Q2669117": "FR",   # French Second Republic
    "Q71092":   "FR",   # Second French Empire
    "Q217498":  "FR",   # Ancien Régime
    # German-speaking
    "Q43287":   "DE",   # German Empire
    "Q41304":   "DE",   # Weimar Republic
    "Q7318":    "DE",   # Nazi Germany
    "Q55164":   "DE",   # East Germany (GDR)
    "Q713750":  "DE",   # Kingdom of Prussia
    "Q154741":  "AT",   # Austria-Hungary
    "Q131964":  "AT",   # Austrian Empire
    # Italian states
    "Q16957":   "IT",   # Kingdom of Italy (1861-1946)
    "Q172579":  "IT",   # Kingdom of Sardinia
    "Q148540":  "IT",   # Duchy of Milan
    "Q16959":   "IT",   # Grand Duchy of Tuscany
    "Q9903":    "IT",   # Papal States
    "Q13580004": "IT",  # Kingdom of the Two Sicilies
    "Q42585":   "IT",   # Republic of Venice
    "Q221338":  "IT",   # Republic of Genoa
    # Russia
    "Q34266":   "RU",   # Russian Empire
    "Q15180":   "RU",   # Soviet Union
    # Iberia
    "Q180684":  "ES",   # Kingdom of Castile
    "Q185682":  "ES",   # Crown of Aragon
    "Q203070":  "ES",   # Crown of Castile
    # Low Countries
    "Q188553":  "NL",   # United Kingdom of the Netherlands
    "Q170072":  "BE",   # Southern Netherlands
    # Scandinavia
    "Q108480":  "SE",   # Swedish Empire
    "Q756617":  "DK",   # Denmark-Norway
    # Poland
    "Q227":     "PL",   # Polish-Lithuanian Commonwealth (Q227 is Azerbaijan, fixing)
    "Q35672":   "PL",   # Polish-Lithuanian Commonwealth
    "Q36279":   "PL",   # Kingdom of Poland
    # Ottoman
    "Q12560":   "TR",   # Ottoman Empire
    # Americas
    "Q161178":  "US",   # Confederate States (historical)
}

EXTENDED_NATIONALITY_MAP = {**NATIONALITY_MAP, **HISTORICAL_STATE_MAP}


# ── SPARQL helpers ──────────────────────────────────────────────────────────

def sparql_query(query: str, retries: int = 3) -> list[dict]:
    params = urllib.parse.urlencode({"query": query, "format": "json"})
    url = f"{SPARQL_ENDPOINT}?{params}"
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"},
    )
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
            return data["results"]["bindings"]
        except HTTPError as e:
            if e.code == 429 or e.code >= 500:
                wait = (attempt + 1) * 30
                logger.warning("SPARQL %d — retrying in %ds", e.code, wait)
                time.sleep(wait)
                continue
            raise
        except Exception:
            if attempt < retries - 1:
                time.sleep(10)
                continue
            raise
    return []


# ── Wikidata API helpers ────────────────────────────────────────────────────

def fetch_entities(qids: list[str]) -> dict[str, dict]:
    """Fetch entities via wbgetentities API. Returns {qid: entity_json}."""
    if not qids:
        return {}
    params = urllib.parse.urlencode({
        "action": "wbgetentities",
        "ids": "|".join(qids),
        "format": "json",
        "props": "labels|descriptions|claims|aliases|sitelinks",
        "languages": "en|it",
        "sitefilter": "enwiki|itwiki",
    })
    url = f"{WIKIDATA_API}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
            return data.get("entities", {})
        except Exception as e:
            if attempt < 2:
                logger.warning("API error (attempt %d): %s", attempt + 1, e)
                time.sleep(5 * (attempt + 1))
                continue
            raise
    return {}


# ── Insert-only upsert (never overwrites existing docs) ────────────────────

def batch_insert_only(db, collection_name: str, docs: list[dict], label: str = "") -> int:
    """Insert new documents only. Existing documents are left untouched."""
    if not docs:
        return 0
    col = db.collection(collection_name)
    try:
        result = col.import_bulk(docs, on_duplicate="ignore")
        created = result.get("created", 0)
        ignored = result.get("ignored", 0)
        errors = result.get("errors", 0)
        if errors:
            logger.warning("Batch %s: %d created, %d ignored, %d errors", label, created, ignored, errors)
        return created
    except Exception as e:
        logger.error("Batch insert failed for %s: %s", label, e)
        return 0


# ── Discovery: find missing QIDs ────────────────────────────────────────────

def discover_missing_qids(db, min_statements: int, include_historical: bool = False) -> list[str]:
    """Query SPARQL per country, compare against DB, return missing QIDs."""
    persons_col = db.collection(PERSONS)

    # Modern countries — always queried
    modern_groups = [
        ["Q30"],   # US
        ["Q145"],  # UK
        ["Q142"],  # FR
        ["Q183"],  # DE
        ["Q38"],   # IT
        ["Q29"],   # ES
        ["Q159"],  # RU
        ["Q36"],   # PL
        ["Q16"],   # CA
        # Remaining European + Turkey in one batch
        [q for q in TARGET_COUNTRY_QIDS
         if q not in {"Q30", "Q145", "Q142", "Q183", "Q38", "Q29", "Q159", "Q36", "Q16"}],
    ]

    # Historical states — only when --include-historical
    historical_groups = []
    if include_historical:
        hist_qids = list(HISTORICAL_STATE_MAP.keys())
        # Batch into groups of ~15 to avoid SPARQL timeouts
        for i in range(0, len(hist_qids), 15):
            historical_groups.append(hist_qids[i:i + 15])

    all_groups = [(g, "modern") for g in modern_groups] + [(g, "historical") for g in historical_groups]

    all_missing = []
    total_checked = 0

    for group, kind in all_groups:
        group_label = group[0] if len(group) == 1 else f"{len(group)} {kind}"
        country_filter = " ".join(f"wd:{q}" for q in group)

        query = f"""
        SELECT ?person WHERE {{
          ?person wdt:P31 wd:Q5 .
          ?person wdt:P27 ?country .
          VALUES ?country {{ {country_filter} }}
          ?person wikibase:statements ?stmtCount .
          FILTER(?stmtCount >= {min_statements})
        }}
        """

        try:
            results = sparql_query(query)
            qids = set(r["person"]["value"].split("/")[-1] for r in results)
            total_checked += len(qids)

            missing_in_group = [q for q in qids if not persons_col.has(q)]
            all_missing.extend(missing_in_group)

            logger.info(
                "  %s: %d from SPARQL, %d missing",
                group_label, len(qids), len(missing_in_group),
            )
        except Exception as e:
            logger.error("  SPARQL failed for %s: %s", group_label, e)

        time.sleep(SPARQL_DELAY)

    all_missing = sorted(set(all_missing), key=lambda q: int(q[1:]))
    logger.info("Discovery complete: %d checked, %d missing", total_checked, len(all_missing))
    return all_missing


# ── Backfill: fetch + parse + upsert ───────────────────────────────────────

def backfill_persons(db, missing_qids: list[str], dry_run: bool = False,
                     include_historical: bool = False):
    """Fetch missing persons from Wikidata API and upsert into ArangoDB."""
    logger.info("=== Backfilling %d missing persons ===", len(missing_qids))
    if dry_run:
        logger.info("DRY RUN — no DB writes")

    # Build the accepted P27 set: modern + optionally historical states
    accepted_p27 = set(TARGET_COUNTRY_QIDS)
    if include_historical:
        accepted_p27.update(HISTORICAL_STATE_MAP.keys())

    stats = Counter()
    all_edges: list[dict] = []

    for batch_idx in range(0, len(missing_qids), API_BATCH_SIZE):
        batch = missing_qids[batch_idx:batch_idx + API_BATCH_SIZE]
        logger.info(
            "  Batch %d/%d: fetching %d entities (%s…)",
            batch_idx // API_BATCH_SIZE + 1,
            (len(missing_qids) + API_BATCH_SIZE - 1) // API_BATCH_SIZE,
            len(batch),
            batch[0],
        )

        try:
            entities_json = fetch_entities(batch)
        except Exception as e:
            logger.error("  API fetch failed: %s", e)
            stats["api_errors"] += 1
            continue

        person_docs = []

        for qid in batch:
            entity_obj = entities_json.get(qid)
            if not entity_obj or "missing" in entity_obj:
                stats["api_missing"] += 1
                continue

            parsed_qid, entity = parse_json_entity(entity_obj)
            if not parsed_qid or entity is None:
                stats["parse_failed"] += 1
                continue

            category, type_label = classify_entity(entity["p31"])

            if category != "person":
                stats["not_person"] += 1
                continue

            # Verify target country (modern or historical)
            p27_qids = entity["props"].get("P27", [])
            if not any(q in accepted_p27 for q in p27_qids):
                stats["wrong_country"] += 1
                continue

            doc = build_person_doc(
                parsed_qid, entity,
                place_labels={}, entity_labels={}, award_counts={},
            )
            if not doc:
                stats["build_failed"] += 1
                continue

            # Patch nationalities from historical states when NATIONALITY_MAP
            # produced nothing (build_person_doc only knows modern states)
            if not doc.get("nationalities"):
                patched = []
                for pq in p27_qids:
                    code = EXTENDED_NATIONALITY_MAP.get(pq)
                    if code and code not in patched:
                        patched.append(code)
                if patched:
                    doc["nationalities"] = patched

            person_docs.append(doc)
            stats["persons"] += 1

            edges = build_edge_docs(parsed_qid, entity)
            all_edges.extend(edges)
            stats["edges"] += len(edges)

        if person_docs and not dry_run:
            merge_occupations(db, person_docs)
            written = batch_upsert(db, PERSONS, person_docs, f"backfill-{batch_idx}")
            stats["persons_written"] += written

        time.sleep(API_DELAY)

    # Now fetch referenced orgs/events that don't exist yet (edge targets)
    if all_edges and not dry_run:
        logger.info("=== Resolving edge targets ===")
        _resolve_edge_targets(db, all_edges, stats)
        _write_edges(db, all_edges, stats)

    logger.info("=== Backfill Complete ===")
    for key, val in sorted(stats.items()):
        logger.info("  %s: %d", key, val)

    return stats


def _resolve_edge_targets(db, edges: list[dict], stats: Counter):
    """Fetch and upsert orgs/events referenced by edges that don't exist in DB."""
    org_qids_needed = set()
    event_qids_needed = set()

    orgs_col = db.collection(ORGANIZATIONS)
    events_col = db.collection(EVENTS)

    for edge in edges:
        to_ref = edge.get("_to", "")
        if to_ref.startswith(f"{ORGANIZATIONS}/"):
            qid = to_ref.split("/", 1)[1]
            if not orgs_col.has(qid):
                org_qids_needed.add(qid)
        elif to_ref.startswith(f"{EVENTS}/"):
            qid = to_ref.split("/", 1)[1]
            if not events_col.has(qid):
                event_qids_needed.add(qid)

    all_needed = sorted(org_qids_needed | event_qids_needed)
    if not all_needed:
        logger.info("  No missing edge targets to resolve")
        return

    logger.info("  Resolving %d missing edge targets (%d orgs, %d events)",
                len(all_needed), len(org_qids_needed), len(event_qids_needed))

    org_docs = []
    event_docs = []

    for batch_idx in range(0, len(all_needed), API_BATCH_SIZE):
        batch = all_needed[batch_idx:batch_idx + API_BATCH_SIZE]

        try:
            entities_json = fetch_entities(batch)
        except Exception as e:
            logger.error("  Target fetch failed: %s", e)
            continue

        for qid in batch:
            entity_obj = entities_json.get(qid)
            if not entity_obj or "missing" in entity_obj:
                continue

            parsed_qid, entity = parse_json_entity(entity_obj)
            if not parsed_qid or entity is None:
                continue

            category, type_label = classify_entity(entity["p31"])

            if category == "organization" and qid in org_qids_needed:
                doc = build_org_doc(parsed_qid, entity, type_label or "organization")
                if doc:
                    org_docs.append(doc)
            elif category == "event" and qid in event_qids_needed:
                doc = build_event_doc(parsed_qid, entity, type_label or "event")
                if doc:
                    event_docs.append(doc)

        time.sleep(API_DELAY)

    if org_docs:
        written = batch_insert_only(db, ORGANIZATIONS, org_docs, "backfill-orgs")
        stats["orgs_written"] = written
        logger.info("  Inserted %d new organizations (existing untouched)", written)

    if event_docs:
        written = batch_insert_only(db, EVENTS, event_docs, "backfill-events")
        stats["events_written"] = written
        logger.info("  Inserted %d new events (existing untouched)", written)


def _write_edges(db, edges: list[dict], stats: Counter):
    """Write edges, skipping those whose endpoints don't exist in DB."""
    logger.info("=== Writing %d edges ===", len(edges))

    by_col: dict[str, list] = {
        RELATIONSHIPS: [], AFFILIATIONS: [], INVOLVEMENTS: [], ORG_STRUCTURE: [],
    }

    persons_col = db.collection(PERSONS)
    orgs_col = db.collection(ORGANIZATIONS)
    events_col = db.collection(EVENTS)

    def endpoint_exists(ref: str) -> bool:
        parts = ref.split("/", 1)
        if len(parts) != 2:
            return False
        col_name, key = parts
        if col_name == PERSONS:
            return persons_col.has(key)
        elif col_name == ORGANIZATIONS:
            return orgs_col.has(key)
        elif col_name == EVENTS:
            return events_col.has(key)
        return False

    orphans = 0
    for edge in edges:
        if not endpoint_exists(edge["_from"]) or not endpoint_exists(edge["_to"]):
            orphans += 1
            continue
        col = edge.get("edge_collection")
        if col in by_col:
            doc = {k: v for k, v in edge.items() if k != "edge_collection"}
            by_col[col].append(doc)

    stats["edges_orphaned"] = orphans
    logger.info("  %d edges valid, %d orphaned (missing endpoint)", len(edges) - orphans, orphans)

    total_written = 0
    for col, docs in by_col.items():
        if docs:
            written = batch_upsert(db, col, docs, f"edges-{col}")
            total_written += written
            logger.info("  %s: %d written", col, written)

    stats["edges_written"] = total_written


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Backfill missing persons from Wikidata API")
    parser.add_argument("--min-statements", type=int, default=150,
                        help="Minimum wikidata_statements_count to backfill (default: 150)")
    parser.add_argument("--qids", type=str, default="",
                        help="Comma-separated QIDs to backfill (skips SPARQL discovery)")
    parser.add_argument("--include-historical", action="store_true",
                        help="Also discover persons with historical-state P27 (Newton, Napoleon…)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Identify missing persons but don't write to DB")
    args = parser.parse_args()

    logger.info("=== Backfill Missing Persons ===")
    logger.info("  Min statements: %d", args.min_statements)
    logger.info("  Include historical: %s", args.include_historical)
    logger.info("  Dry run: %s", args.dry_run)

    db = init_db()

    if args.qids:
        missing_qids = [q.strip() for q in args.qids.split(",") if q.strip()]
        logger.info("  Explicit QIDs: %d", len(missing_qids))
    else:
        logger.info("  Discovering missing persons via SPARQL...")
        missing_qids = discover_missing_qids(db, args.min_statements, args.include_historical)

    if not missing_qids:
        logger.info("No missing persons found — DB is complete!")
        return

    logger.info("Found %d missing persons to backfill", len(missing_qids))

    if args.dry_run:
        logger.info("DRY RUN — would backfill these QIDs:")
        for i, qid in enumerate(missing_qids[:50]):
            logger.info("  %s", qid)
        if len(missing_qids) > 50:
            logger.info("  ... and %d more", len(missing_qids) - 50)
        return

    backfill_persons(db, missing_qids, dry_run=False, include_historical=args.include_historical)


if __name__ == "__main__":
    main()
