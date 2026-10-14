"""SPARQL qualifier backfill — Phase 9 of the project design notes.

Why this exists: the truthy Wikidata dump strips statement reification, so
P580 (start time) and P582 (end time) qualifiers never land on any edge
during the bulk load. This script fetches them over SPARQL and UPDATEs
the existing edges in place. Idempotent: re-running is safe.

Pilot scope (v1):
- mode=person: Tier-1 properties P39, P108, P69, P102 with P580/P582
- updates affiliations edges by their deterministic _key
- sequential batches of --batch-size QIDs, --delay seconds between requests
- checkpoint to backend/logs/backfill_qualifiers.state.json; --resume to continue

Tier-2 (org history) will be added in a follow-up commit once the person
pilot proves the SPARQL endpoint doesn't throttle us.

Usage:
    python -m scripts.backfill_qualifiers --limit 100 --dry-run
    python -m scripts.backfill_qualifiers --limit 100
    python -m scripts.backfill_qualifiers --limit 5000 --resume
    python -m scripts.backfill_qualifiers --limit 0           # no limit; whole DB
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.database import ensure_collections, ensure_database, get_arango_client  # noqa: E402

logger = logging.getLogger("backfill_qualifiers")

SPARQL_ENDPOINT = settings.sparql_endpoint
USER_AGENT = settings.sparql_user_agent

CHECKPOINT_PATH = Path(__file__).resolve().parents[1] / "logs" / "backfill_qualifiers.state.json"

# Wikidata property → edge type (must match load_from_dump.EDGE_PROPERTIES)
PERSON_PROPERTIES = {
    "P39":  "position_held",
    "P108": "employee",
    "P69":  "educated_at",
    "P102": "member_of",
}

# Reverse: edge_type → prop_id — used to reconstruct _key.
# Loader emits _key = sanitize("{qid}_{target}_{edge_type}_{prop_id}[_{count}]")
# (load_from_dump.py:1179). Truthy-loaded edges never have a count suffix.


def build_person_sparql(qids: list[str]) -> str:
    """One query for a batch of persons across all tier-1 properties."""
    values = " ".join(f"wd:{q}" for q in qids)
    props = " ".join(f"wdt:{p}" for p in PERSON_PROPERTIES)  # unused; kept for reference
    # Reified pattern: follow p: → ps: → pq:
    p_values = " ".join(f"p:{p}" for p in PERSON_PROPERTIES)
    ps_values = " ".join(f"ps:{p}" for p in PERSON_PROPERTIES)
    return f"""
    SELECT ?person ?p ?value ?start ?end WHERE {{
      VALUES ?person {{ {values} }}
      VALUES ?p      {{ {p_values} }}
      ?person ?p ?stmt .
      ?stmt ?ps ?value .
      FILTER(?ps IN ({ps_values}))
      FILTER(isIRI(?value))
      OPTIONAL {{ ?stmt pq:P580 ?start . }}
      OPTIONAL {{ ?stmt pq:P582 ?end . }}
      FILTER(BOUND(?start) || BOUND(?end))
    }}
    """


def sparql_get(sparql: str, retries: int = 5) -> list[dict[str, Any]]:
    """GET the endpoint with retry/backoff. Raises on terminal failure."""
    params = urllib.parse.urlencode({"query": sparql, "format": "json"})
    url = f"{SPARQL_ENDPOINT}?{params}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"},
    )
    backoff = 2.0
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
            return data["results"]["bindings"]
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                wait = float(e.headers.get("Retry-After", backoff))
                logger.warning("HTTP %d; sleeping %.1fs (attempt %d/%d)",
                               e.code, wait, attempt + 1, retries)
                time.sleep(wait)
                backoff *= 2
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            logger.warning("Network err: %s; sleeping %.1fs", e, backoff)
            time.sleep(backoff)
            backoff *= 2
    raise RuntimeError(f"SPARQL retries exhausted for batch of {sparql.count('wd:')} QIDs")


def qid_of(uri: str) -> str | None:
    if not uri or "/entity/" not in uri:
        return None
    q = uri.rsplit("/", 1)[-1]
    return q if q.startswith("Q") else None


def prop_of(uri: str) -> str | None:
    """Parse http://www.wikidata.org/prop/P39 → 'P39'."""
    if not uri:
        return None
    tail = uri.rsplit("/", 1)[-1]
    return tail if tail.startswith("P") and tail[1:].isdigit() else None


def parse_date(xsd_date: str | None) -> str | None:
    """Wikidata dates are xsd:dateTime like '2009-01-20T00:00:00Z'.
    Return YYYY-MM-DD, or the year-only form for low-precision dates."""
    if not xsd_date:
        return None
    d = xsd_date.rstrip("Z")
    if "T" in d:
        d = d.split("T", 1)[0]
    # Strip leading '+' which Wikidata uses for post-0 dates.
    d = d.lstrip("+")
    return d or None


def top_n_persons_by_notability(db, limit: int) -> list[str]:
    """Return QIDs of the top-N persons by wikidata_statements_count DESC."""
    aql = f"""
    FOR p IN persons
      FILTER p.wikidata_statements_count != null
      SORT p.wikidata_statements_count DESC
      { "LIMIT @limit" if limit > 0 else "" }
      RETURN p._key
    """
    cursor = db.aql.execute(aql, bind_vars={"limit": limit} if limit > 0 else {})
    return list(cursor)


def load_checkpoint() -> dict:
    if CHECKPOINT_PATH.exists():
        try:
            return json.loads(CHECKPOINT_PATH.read_text())
        except Exception:
            logger.warning("Corrupt checkpoint; starting fresh")
    return {"processed_qids": [], "batches": 0, "edges_updated": 0, "errors": []}


def save_checkpoint(state: dict) -> None:
    CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_PATH.write_text(json.dumps(state, indent=2))


def sanitize_key_part(s: str) -> str:
    """Mirror load_from_dump.sanitize_key for the fragments we construct."""
    # The loader uses Arango-key-safe chars; QIDs and our prop/type tokens already are.
    return s


def apply_updates(db, updates: dict[tuple[str, str, str, str], dict], dry_run: bool) -> int:
    """updates key: (person_qid, target_qid, edge_type, prop_id) -> {start_date, end_date}.
    Returns count of edges actually updated."""
    if not updates:
        return 0

    # Group by (edge_type, prop_id) to update each collection once; only
    # affiliations is touched in the person pilot.
    updated = 0
    coll = db.collection("affiliations")
    for (person, target, etype, pid), dates in updates.items():
        edge_key = sanitize_key_part(f"{person}_{target}_{etype}_{pid}")
        if dry_run:
            logger.debug("DRY-RUN would update %s with %s", edge_key, dates)
            updated += 1
            continue
        try:
            if coll.has(edge_key):
                patch = {}
                if dates.get("start_date"):
                    patch["start_date"] = dates["start_date"]
                if dates.get("end_date"):
                    patch["end_date"] = dates["end_date"]
                if patch:
                    coll.update({"_key": edge_key, **patch}, keep_none=False)
                    updated += 1
            else:
                logger.debug("Edge %s not in DB (likely filtered org); skipping", edge_key)
        except Exception as e:
            logger.warning("Failed to update %s: %s", edge_key, e)
    return updated


def process_batch(db, qids: list[str], dry_run: bool) -> tuple[int, int]:
    """Returns (rows_received, edges_updated)."""
    sparql = build_person_sparql(qids)
    bindings = sparql_get(sparql)
    if not bindings:
        return 0, 0

    # Collapse multiple rows for the same (person, target, prop) — take
    # the first row that carries *any* date. If a later row has more
    # complete dates, prefer that.
    grouped: dict[tuple[str, str, str, str], dict] = {}
    for row in bindings:
        person = qid_of(row.get("person", {}).get("value", ""))
        target = qid_of(row.get("value", {}).get("value", ""))
        pid = prop_of(row.get("p", {}).get("value", ""))
        if not (person and target and pid and pid in PERSON_PROPERTIES):
            continue
        etype = PERSON_PROPERTIES[pid]
        start = parse_date(row.get("start", {}).get("value"))
        end = parse_date(row.get("end", {}).get("value"))
        key = (person, target, etype, pid)
        prev = grouped.get(key)
        if prev is None:
            grouped[key] = {"start_date": start, "end_date": end}
        else:
            # Merge: prefer non-null values
            if not prev.get("start_date") and start:
                prev["start_date"] = start
            if not prev.get("end_date") and end:
                prev["end_date"] = end

    rows = len(bindings)
    edges_updated = apply_updates(db, grouped, dry_run)
    return rows, edges_updated


def run(args: argparse.Namespace) -> None:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    client = get_arango_client()
    db = ensure_database(client)
    ensure_collections(db)

    logger.info("Fetching top-%s persons by wikidata_statements_count...",
                args.limit if args.limit > 0 else "all")
    all_qids = top_n_persons_by_notability(db, args.limit)
    logger.info("Got %d persons", len(all_qids))

    state = load_checkpoint() if args.resume else {
        "processed_qids": [], "batches": 0, "edges_updated": 0, "errors": []
    }
    done = set(state["processed_qids"])
    todo = [q for q in all_qids if q not in done]
    logger.info("Resume: %d already processed, %d to go", len(done), len(todo))

    t0 = time.time()
    total_rows = 0
    total_updated = state["edges_updated"]

    for i in range(0, len(todo), args.batch_size):
        batch = todo[i : i + args.batch_size]
        t_b = time.time()
        try:
            rows, updated = process_batch(db, batch, args.dry_run)
        except Exception as e:
            logger.exception("Batch failed (first QID=%s): %s", batch[0], e)
            state["errors"].append({"batch_start": batch[0], "err": str(e)})
            save_checkpoint(state)
            if len(state["errors"]) > 10:
                logger.error("Too many errors; aborting. Use --resume to continue.")
                return
            time.sleep(args.delay * 4)
            continue

        total_rows += rows
        total_updated += updated
        state["processed_qids"].extend(batch)
        state["batches"] += 1
        state["edges_updated"] = total_updated

        if state["batches"] % 10 == 0:
            save_checkpoint(state)

        elapsed = time.time() - t_b
        logger.info(
            "batch %d: %d persons → %d rows, %d edges updated (%.2fs); total %d edges, %.1fs",
            state["batches"], len(batch), rows, updated, elapsed, total_updated, time.time() - t0
        )
        time.sleep(args.delay)

    save_checkpoint(state)
    logger.info("=== Done === batches=%d rows=%d edges_updated=%d elapsed=%.1fs %s",
                state["batches"], total_rows, total_updated, time.time() - t0,
                "(DRY-RUN, no writes)" if args.dry_run else "")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    p.add_argument("--limit", type=int, default=100,
                   help="Top-N persons by notability. 0 = all. Default: 100.")
    p.add_argument("--batch-size", type=int, default=50,
                   help="QIDs per SPARQL query. Default: 50.")
    p.add_argument("--delay", type=float, default=settings.sparql_delay_seconds,
                   help="Seconds between requests. Default: sparql_delay_seconds from config.")
    p.add_argument("--mode", choices=["person"], default="person",
                   help="Pilot v1 only supports 'person'. Tier-2 orgs in a follow-up.")
    p.add_argument("--dry-run", action="store_true",
                   help="Fetch and log, don't write to ArangoDB.")
    p.add_argument("--resume", action="store_true",
                   help="Continue from checkpoint at " + str(CHECKPOINT_PATH))
    p.add_argument("--verbose", "-v", action="store_true")
    return p.parse_args(argv)


if __name__ == "__main__":
    run(parse_args())
