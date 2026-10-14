#!/usr/bin/env python3
"""Incremental Wikidata updater — EventStreams + targeted SPARQL.

Collects changed QIDs from the Wikidata EventStreams SSE feed over a configurable
window, filters to entities already in ArangoDB (or new persons matching the
European nationality filter), fetches updated data via SPARQL in batches of ~50
QIDs, upserts into ArangoDB, and invalidates only the affected Redis cache keys.

Usage:
  python /app/scripts/incremental_update.py
  python /app/scripts/incremental_update.py --since 2024-01-15T04:00:00Z
  python /app/scripts/incremental_update.py --window 6
  python /app/scripts/incremental_update.py --dry-run

Suggested cron schedule (daily at 04:00 UTC):
  0 4 * * * docker compose -f /path/to/humansmap/docker-compose.yml exec -T backend python /app/scripts/incremental_update.py >> /var/log/incremental_update.log 2>&1

State file: /data/wikidump/.last_incremental_timestamp
  Written after each successful run; contains an ISO-8601 UTC timestamp.
  Use --since to override for manual backfills.

Rate limits:
  EventStreams: SSE (no hard limit — be a good citizen, don't reconnect rapidly).
  SPARQL: ~1 req/sec for anonymous users. This script uses a 2s delay between
          requests and exponential backoff on 429/500/503.
"""

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

sys.path.insert(0, "/app")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import settings
from app.database import (
    get_arango_client,
    get_db,
    PERSONS,
    ORGANIZATIONS,
    EVENTS,
    RELATIONSHIPS,
    AFFILIATIONS,
    INVOLVEMENTS,
    ORG_STRUCTURE,
)

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("incremental_update")

# ── Constants ────────────────────────────────────────────────────────────────

EVENTSTREAMS_URL = "https://stream.wikimedia.org/v2/stream/recentchange"
STATE_FILE = "/data/wikidump/.last_incremental_timestamp"

SPARQL_ENDPOINT = settings.sparql_endpoint
USER_AGENT = settings.sparql_user_agent
SPARQL_DELAY = settings.sparql_delay_seconds
SPARQL_BATCH_SIZE = 50
PERSON_SCOPE = os.getenv("PERSON_SCOPE", "countries").strip().lower()
GLOBAL_MIN_STATEMENTS = int(os.getenv("GLOBAL_MIN_STATEMENTS", "10"))
if PERSON_SCOPE not in {"countries", "global"}:
    raise ValueError("PERSON_SCOPE must be countries or global")

# Backoff settings for SPARQL errors
BACKOFF_BASE = 5.0   # seconds for first retry
BACKOFF_MAX = 120.0  # maximum wait between retries
BACKOFF_RETRIES = 5

# EventStreams reconnect limits — be a good citizen
MAX_RECONNECT_DELAY = 60.0   # cap at 60s between reconnects
CONNECT_TIMEOUT = 30         # seconds for SSE connection setup
READ_TIMEOUT = 120           # seconds between SSE heartbeats before retry

# European country QIDs (kept in sync with load_from_dump.py)
EUROPEAN_COUNTRY_QIDS = {
    "Q38", "Q183", "Q142", "Q145", "Q29", "Q36", "Q218", "Q55", "Q31",
    "Q213", "Q41", "Q45", "Q34", "Q28", "Q40", "Q219", "Q35", "Q33",
    "Q214", "Q27", "Q224", "Q211", "Q232", "Q215", "Q191", "Q229", "Q244",
    "Q233", "Q20", "Q39", "Q189", "Q347", "Q228", "Q235", "Q238", "Q237",
    "Q403", "Q225", "Q222", "Q221", "Q236", "Q1246", "Q212", "Q184",
    "Q252", "Q159", "Q43", "Q30", "Q16",
}

# ── State file helpers ───────────────────────────────────────────────────────


def read_state_file() -> datetime | None:
    """Read the last successful run timestamp from the state file."""
    try:
        with open(STATE_FILE) as f:
            raw = f.read().strip()
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.warning("Could not read state file %s: %s", STATE_FILE, e)
        return None


def write_state_file(ts: datetime) -> None:
    """Write the current run's timestamp to the state file."""
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as f:
            f.write(ts.strftime("%Y-%m-%dT%H:%M:%SZ"))
        logger.info("State file updated: %s", STATE_FILE)
    except Exception as e:
        logger.warning("Could not write state file %s: %s", STATE_FILE, e)


# ── EventStreams SSE reader (stdlib only) ─────────────────────────────────────
#
# The SSE wire format is simple: lines starting with "data: " carry the payload.
# Lines starting with ":" are comments/heartbeats. Empty lines separate events.
# We implement this directly in ~40 lines to avoid the sseclient-py dependency.


def _iter_sse_lines(url: str, timeout: int = READ_TIMEOUT):
    """Open an SSE connection and yield raw lines.

    On connection errors, raises the exception so the caller can handle
    reconnection logic with backoff.
    """
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/event-stream",
            "Cache-Control": "no-cache",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        # resp is a http.client.HTTPResponse in binary mode
        buf = b""
        while True:
            chunk = resp.read(4096)
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                yield line.decode("utf-8", errors="replace")


def collect_changed_qids(
    since: datetime,
    until: datetime,
) -> set[str]:
    """Stream EventStreams from `since` to `until`, collecting changed QIDs.

    Returns a set of QIDs (e.g. {"Q937", "Q1234"}).
    Handles connection drops with exponential backoff reconnection.
    Stops when an event's timestamp passes `until`.
    """
    qids: set[str] = set()
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    url = f"{EVENTSTREAMS_URL}?since={urllib.parse.quote(since_iso)}"

    reconnect_delay = 2.0
    total_events = 0
    last_event_ts: datetime | None = None

    logger.info(
        "Connecting to EventStreams since=%s until=%s",
        since_iso,
        until.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )

    while True:
        try:
            current_data_lines: list[str] = []

            for line in _iter_sse_lines(url):
                # SSE: "data: {...}" carries payload; "" separates events;
                # ":" lines are heartbeats/comments — reset reconnect timer.

                if line.startswith(":"):
                    # Heartbeat — connection is alive
                    reconnect_delay = 2.0
                    continue

                if line.startswith("data:"):
                    payload = line[5:].strip()
                    current_data_lines.append(payload)
                    continue

                if line == "" and current_data_lines:
                    # End of event — parse it
                    raw = "".join(current_data_lines)
                    current_data_lines = []
                    reconnect_delay = 2.0  # reset on successful event

                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    # Check server_name and filter to Wikidata only
                    if event.get("server_name") != "www.wikidata.org":
                        continue

                    # Check event type — we want entity edits
                    if event.get("type") not in ("edit", "new"):
                        continue

                    total_events += 1

                    # Check timestamp — stop if we've passed `until`
                    event_ts_raw = event.get("timestamp")
                    if event_ts_raw:
                        try:
                            event_ts = datetime.fromtimestamp(
                                int(event_ts_raw), tz=timezone.utc
                            )
                            last_event_ts = event_ts
                            if event_ts >= until:
                                logger.info(
                                    "Reached until=%s (last event=%s), stopping stream",
                                    until.isoformat(),
                                    event_ts.isoformat(),
                                )
                                return qids
                        except (ValueError, TypeError):
                            pass

                    # Extract QID from title (e.g. "Q937", "P31", "L42")
                    title = event.get("title", "")
                    if title.startswith("Q") and title[1:].isdigit():
                        qids.add(title)

            # Normal end-of-stream (shouldn't happen on EventStreams but handle it)
            logger.info("EventStreams returned EOF after %d events", total_events)
            return qids

        except urllib.error.HTTPError as e:
            logger.warning(
                "EventStreams HTTP error %d, reconnecting in %.0fs...",
                e.code,
                reconnect_delay,
            )
        except Exception as e:
            logger.warning(
                "EventStreams connection error (%s), reconnecting in %.0fs...",
                type(e).__name__,
                reconnect_delay,
            )

        # If we've already passed `until` by wall-clock time, give up
        if datetime.now(timezone.utc) >= until + timedelta(minutes=5):
            logger.info("Past window deadline, returning %d QIDs collected", len(qids))
            return qids

        time.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, MAX_RECONNECT_DELAY)

        # Resume from last known event, or fall back to original since
        if last_event_ts:
            resume_iso = last_event_ts.strftime("%Y-%m-%dT%H:%M:%SZ")
            url = f"{EVENTSTREAMS_URL}?since={urllib.parse.quote(resume_iso)}"
            logger.info("Reconnecting, resuming from %s", resume_iso)


# ── DB helpers ───────────────────────────────────────────────────────────────


def get_existing_qids(db) -> set[str]:
    """Return all QIDs currently stored in persons, organizations, events."""
    qids: set[str] = set()
    for col_name in (PERSONS, ORGANIZATIONS, EVENTS):
        col = db.collection(col_name)
        cursor = db.aql.execute(f"FOR doc IN {col_name} RETURN doc._key")
        for key in cursor:
            qids.add(key)
    logger.info("Found %d existing QIDs in DB", len(qids))
    return qids


# ── SPARQL helpers ────────────────────────────────────────────────────────────


def _sparql_get(sparql: str, retries: int = BACKOFF_RETRIES) -> list[dict]:
    """Execute a SPARQL GET request with exponential backoff."""
    params = urllib.parse.urlencode({"query": sparql, "format": "json"})
    url = f"{SPARQL_ENDPOINT}?{params}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/sparql-results+json",
        },
    )
    delay = BACKOFF_BASE
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
            return data["results"]["bindings"]
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                logger.warning(
                    "SPARQL HTTP %d on attempt %d/%d, backing off %.0fs",
                    e.code, attempt, retries, delay,
                )
                time.sleep(delay)
                delay = min(delay * 2, BACKOFF_MAX)
            else:
                raise
        except Exception:
            if attempt < retries:
                logger.warning(
                    "SPARQL error on attempt %d/%d, backing off %.0fs",
                    attempt, retries, delay,
                )
                time.sleep(delay)
                delay = min(delay * 2, BACKOFF_MAX)
            else:
                raise
    return []


def _val(binding: dict, key: str) -> str | None:
    if key in binding and "value" in binding[key]:
        return binding[key]["value"]
    return None


def _qid(uri: str | None) -> str | None:
    if uri and "entity/" in uri:
        return uri.split("entity/")[-1]
    return None


# ── SPARQL fetch functions ────────────────────────────────────────────────────


def fetch_persons_by_qids(qids: list[str]) -> list[dict]:
    """Fetch person data for a batch of QIDs. Returns list of raw dicts."""
    values = " ".join(f"wd:{q}" for q in qids)
    sparql = f"""
    SELECT ?person ?personLabel ?personDescription ?descriptionIt
           ?birthDate ?deathDate ?image ?stmtCount
           ?nameItLabel ?personLabelIt ?causeOfDeathLabel ?genderLabel
    WHERE {{
      VALUES ?person {{ {values} }}
      ?person wdt:P31 wd:Q5 .
      ?person wikibase:statements ?stmtCount .
      OPTIONAL {{ ?person wdt:P569 ?birthDate . }}
      OPTIONAL {{ ?person wdt:P570 ?deathDate . }}
      OPTIONAL {{ ?person wdt:P18 ?image . }}
      OPTIONAL {{ ?person wdt:P1559 ?nameIt . }}
      OPTIONAL {{ ?person wdt:P509 ?causeOfDeath . }}
      OPTIONAL {{ ?person wdt:P21 ?gender . }}
      OPTIONAL {{ ?person rdfs:label ?personLabelIt . FILTER(LANG(?personLabelIt) = "it") }}
      OPTIONAL {{ ?person schema:description ?descriptionIt . FILTER(LANG(?descriptionIt) = "it") }}
      SERVICE wikibase:label {{
        bd:serviceParam wikibase:language "en,mul,it" .
      }}
    }}
    """
    return _sparql_get(sparql)


def fetch_nationalities_for_qids(qids: list[str]) -> dict[str, set[str]]:
    """Return {qid: set(country_qids)} for the given QIDs."""
    values = " ".join(f"wd:{q}" for q in qids)
    sparql = f"""
    SELECT ?person ?country WHERE {{
      VALUES ?person {{ {values} }}
      ?person wdt:P27 ?country .
    }}
    """
    result: dict[str, set[str]] = {}
    try:
        bindings = _sparql_get(sparql)
        for row in bindings:
            person_qid = _qid(_val(row, "person"))
            country_qid = _qid(_val(row, "country"))
            if person_qid and country_qid:
                result.setdefault(person_qid, set()).add(country_qid)
    except Exception as e:
        logger.warning("Failed to fetch nationalities: %s", e)
    return result


def fetch_global_person_qids(qids: list[str], min_statements: int = GLOBAL_MIN_STATEMENTS) -> set[str]:
    """Return globally eligible human QIDs at or above the bulk threshold."""
    values = " ".join(f"wd:{q}" for q in qids)
    sparql = f"""
    SELECT ?person WHERE {{
      VALUES ?person {{ {values} }}
      ?person wdt:P31 wd:Q5 .
      ?person wikibase:statements ?stmtCount .
      FILTER(?stmtCount >= {int(min_statements)})
    }}
    """
    try:
        return {
            qid
            for row in _sparql_get(sparql)
            if (qid := _qid(_val(row, "person")))
        }
    except Exception as exc:
        logger.warning("Failed to fetch globally eligible people: %s", exc)
        return set()


def fetch_orgs_for_persons(qids: list[str]) -> list[dict]:
    """Fetch organizations linked to persons via P102/P108/P463.

    Phase 5.1: also fetches P856 (website), P159 (headquarters + QID),
    P1128 (employees_count), P17 (country + QID) for each org (§1b / 3.2).
    """
    values = " ".join(f"wd:{q}" for q in qids)
    sparql = f"""
    SELECT DISTINCT ?org ?orgLabel ?orgLabelIt ?orgDescription ?orgDescriptionIt
           ?inception ?dissolved ?website ?hq ?hqLabel ?employees ?country ?countryLabel
    WHERE {{
      VALUES ?person {{ {values} }}
      {{
        ?person wdt:P102 ?org .
      }} UNION {{
        ?person wdt:P108 ?org .
      }} UNION {{
        ?person wdt:P463 ?org .
      }}
      # P102/P108/P463 occasionally point at a human. Never materialize that
      # counterpart as an organization as well.
      FILTER NOT EXISTS {{ ?org wdt:P31 wd:Q5 . }}
      OPTIONAL {{ ?org wdt:P571 ?inception . }}
      OPTIONAL {{ ?org wdt:P576 ?dissolved . }}
      OPTIONAL {{ ?org wdt:P856 ?website . }}
      OPTIONAL {{ ?org wdt:P159 ?hq . }}
      OPTIONAL {{ ?org wdt:P1128 ?employees . }}
      OPTIONAL {{ ?org wdt:P17 ?country . }}
      OPTIONAL {{ ?org rdfs:label ?orgLabelIt . FILTER(LANG(?orgLabelIt) = "it") }}
      OPTIONAL {{ ?org schema:description ?orgDescriptionIt . FILTER(LANG(?orgDescriptionIt) = "it") }}
      SERVICE wikibase:label {{
        bd:serviceParam wikibase:language "en,mul,it" .
      }}
    }}
    """
    return _sparql_get(sparql)


def fetch_events_for_persons(qids: list[str]) -> list[dict]:
    """Fetch events linked to persons via P1344/P793.

    Phase 5.1: also fetches P276 (location + QID), P1120 (deaths),
    P495 (country + QID) for each event (§1c / 3.3).
    """
    values = " ".join(f"wd:{q}" for q in qids)
    sparql = f"""
    SELECT DISTINCT ?event ?eventLabel ?eventLabelIt ?eventDescription
           ?eventDescriptionIt ?date ?location ?locationLabel ?deaths ?country ?countryLabel
    WHERE {{
      VALUES ?person {{ {values} }}
      {{
        ?person wdt:P1344 ?event .
      }} UNION {{
        ?person wdt:P793 ?event .
      }}
      # P1344/P793 are broad on Wikidata. Restrict this collection to entities
      # typed as events (or subclasses of event) to avoid cross-type copies.
      ?event wdt:P31/wdt:P279* wd:Q1656682 .
      OPTIONAL {{ ?event wdt:P585 ?date . }}
      OPTIONAL {{ ?event wdt:P276 ?location . }}
      OPTIONAL {{ ?event wdt:P1120 ?deaths . }}
      OPTIONAL {{ ?event wdt:P495 ?country . }}
      OPTIONAL {{ ?event rdfs:label ?eventLabelIt . FILTER(LANG(?eventLabelIt) = "it") }}
      OPTIONAL {{ ?event schema:description ?eventDescriptionIt . FILTER(LANG(?eventDescriptionIt) = "it") }}
      SERVICE wikibase:label {{
        bd:serviceParam wikibase:language "en,mul,it" .
      }}
    }}
    """
    return _sparql_get(sparql)


def fetch_edges_for_persons(qids: list[str]) -> list[dict]:
    """Fetch all relationship/affiliation/involvement edges for persons.

    Phase 5.1: includes the 4 new person↔person relationship types from §2a
    (P451 unmarried_partner, P184 doctoral_advisor, P185 doctoral_student,
    P1327 professional_collaborator).
    """
    values = " ".join(f"wd:{q}" for q in qids)
    sparql = f"""
    SELECT ?from_entity ?to_entity ?prop WHERE {{
      VALUES ?from_entity {{ {values} }}
      VALUES ?prop {{
        wdt:P26 wdt:P22 wdt:P25 wdt:P40 wdt:P3373
        wdt:P451 wdt:P184 wdt:P185 wdt:P1327
        wdt:P108 wdt:P39 wdt:P102 wdt:P463
        wdt:P1344 wdt:P793
      }}
      ?from_entity ?prop ?to_entity .
    }}
    """
    return _sparql_get(sparql)


def fetch_award_statement_counts(award_qids: list[str]) -> dict[str, int]:
    """Fetch wikibase:statements counts for a list of award QIDs.

    Used to rank awards_top5 by prominence (§1d / 3.5): the award entity with
    the most Wikidata statements is treated as most notable.
    Returns {qid: statement_count}.
    """
    if not award_qids:
        return {}
    counts: dict[str, int] = {}
    batch_size = 50
    for i in range(0, len(award_qids), batch_size):
        batch = award_qids[i : i + batch_size]
        values = " ".join(f"wd:{q}" for q in batch)
        sparql = f"""
        SELECT ?award ?stmtCount WHERE {{
          VALUES ?award {{ {values} }}
          ?award wikibase:statements ?stmtCount .
        }}
        """
        try:
            results = _sparql_get(sparql)
            for row in results:
                qid = _qid(_val(row, "award"))
                count = _val(row, "stmtCount")
                if qid and count:
                    counts[qid] = int(count)
        except Exception as e:
            logger.warning("Failed to fetch award statement counts: %s", e)
        time.sleep(SPARQL_DELAY)
    return counts


def fetch_person_rich_fields(qids: list[str]) -> dict[str, dict]:
    """Fetch P166 (awards) and P800 (notable_works) for a batch of persons.

    Returns {qid: {"award_qids": [...], "awards_top5": [...], "work_labels": [...]}}.
    Award top-5 is ranked by the award entity's own wikibase:statements count (§1d / 3.5).
    Only called for persons whose statements_count >= rich_fields_min_statements.
    """
    if not qids:
        return {}

    values = " ".join(f"wd:{q}" for q in qids)
    rich: dict[str, dict] = {
        q: {"award_qids": [], "awards_top5": [], "work_labels": []} for q in qids
    }

    # P166 — awards (collect QIDs and labels)
    awards_sparql = f"""
    SELECT ?person ?award ?awardLabel WHERE {{
      VALUES ?person {{ {values} }}
      ?person wdt:P166 ?award .
      FILTER(STRSTARTS(STR(?award), "http://www.wikidata.org/entity/Q"))
      SERVICE wikibase:label {{
        bd:serviceParam wikibase:language "en,mul,it" .
      }}
    }}
    """
    person_awards: dict[str, dict[str, str]] = {}
    try:
        results = _sparql_get(awards_sparql)
        for row in results:
            p_qid = _qid(_val(row, "person"))
            a_qid = _qid(_val(row, "award"))
            a_label = _val(row, "awardLabel") or a_qid
            if p_qid and a_qid:
                person_awards.setdefault(p_qid, {})[a_qid] = a_label
    except Exception as e:
        logger.warning("Failed to fetch P166 awards: %s", e)
    time.sleep(SPARQL_DELAY)

    # Fetch statement counts for all unique award QIDs (for ranking)
    all_award_qids = list({q for awards in person_awards.values() for q in awards})
    award_counts = fetch_award_statement_counts(all_award_qids)

    for p_qid, awards_map in person_awards.items():
        if p_qid not in rich:
            continue
        rich[p_qid]["award_qids"] = list(awards_map.keys())
        sorted_awards = sorted(
            awards_map.items(),
            key=lambda kv: award_counts.get(kv[0], 0),
            reverse=True,
        )
        rich[p_qid]["awards_top5"] = [label for _, label in sorted_awards[:5]]

    # P800 — notable works (cap 10, collect labels)
    works_sparql = f"""
    SELECT ?person ?work ?workLabel WHERE {{
      VALUES ?person {{ {values} }}
      ?person wdt:P800 ?work .
      FILTER(STRSTARTS(STR(?work), "http://www.wikidata.org/entity/Q"))
      SERVICE wikibase:label {{
        bd:serviceParam wikibase:language "en,mul,it" .
      }}
    }}
    LIMIT 500
    """
    person_works: dict[str, list[str]] = {}
    try:
        results = _sparql_get(works_sparql)
        for row in results:
            p_qid = _qid(_val(row, "person"))
            label = _val(row, "workLabel")
            if p_qid and label:
                bucket = person_works.setdefault(p_qid, [])
                if label not in bucket and len(bucket) < 10:
                    bucket.append(label)
    except Exception as e:
        logger.warning("Failed to fetch P800 notable works: %s", e)
    time.sleep(SPARQL_DELAY)

    for p_qid, works in person_works.items():
        if p_qid in rich:
            rich[p_qid]["work_labels"] = works

    return rich


def fetch_org_structure_edges(org_qids: list[str]) -> list[dict]:
    """Fetch org→org structure edges (P749/P355/P127/P1830) for given org QIDs.

    Phase 5.1 — §2b / 4.3 SPARQL parity.

    The endpoint-type filter (only emit when the counterpart is also an org) is
    enforced via FILTER NOT EXISTS: skip counterparts that are humans (Q5).
    Returns list of raw row dicts with keys: org, counterpart, prop.
    """
    if not org_qids:
        return []

    values = " ".join(f"wd:{q}" for q in org_qids)
    org_props = " ".join(
        f"wdt:{p}" for p in ("P749", "P355", "P127", "P1830")
    )
    sparql = f"""
    SELECT ?org ?counterpart ?prop WHERE {{
      VALUES ?org {{ {values} }}
      VALUES ?prop {{ {org_props} }}
      ?org ?prop ?counterpart .
      FILTER(STRSTARTS(STR(?counterpart), "http://www.wikidata.org/entity/Q"))
      FILTER NOT EXISTS {{ ?counterpart wdt:P31 wd:Q5 . }}
    }}
    """
    try:
        return _sparql_get(sparql)
    except Exception as e:
        logger.warning("Failed to fetch org_structure edges: %s", e)
        return []


# ── Upsert helpers (using DB directly — no asyncio needed for a script) ───────

NOW = datetime.now(timezone.utc).isoformat()

PROP_TO_EDGE: dict[str, tuple[str, str]] = {
    # prop_qid: (edge_collection, type)
    "P26":   ("relationships", "spouse"),
    "P22":   ("relationships", "parent"),
    "P25":   ("relationships", "parent"),
    "P40":   ("relationships", "child"),
    "P3373": ("relationships", "sibling"),
    # Phase 4.1 — new person↔person types (§2a)
    "P451":  ("relationships", "unmarried_partner"),
    "P184":  ("relationships", "doctoral_advisor"),
    "P185":  ("relationships", "doctoral_student"),
    "P1327": ("relationships", "professional_collaborator"),
    "P108":  ("affiliations", "employee"),
    "P39":   ("affiliations", "position_held"),
    "P102":  ("affiliations", "member_of"),
    "P463":  ("affiliations", "member_of"),
    "P1344": ("involvements", "participant"),
    "P793":  ("involvements", "significant_event"),
}

# Phase 4.3 — org→org structure edge properties (§2b).
# These are handled separately in upsert_org_structure_edge() because the
# endpoint-type check (both sides must be orgs) can't be expressed in PROP_TO_EDGE.
ORG_STRUCTURE_PROP_TO_TYPE: dict[str, str] = {
    "P749":  "parent_of",      # child org → parent org
    "P355":  "subsidiary_of",  # parent org → subsidiary org
    "P127":  "owned_by",       # org → owner-org (org-org only)
    "P1830": "owns",           # org → owned-org (org-org only)
}

EDGE_TO_COLLECTION: dict[str, str] = {
    "relationships": PERSONS,
    "affiliations": ORGANIZATIONS,
    "involvements": EVENTS,
}


def upsert_person(db, qid: str, row: dict, rich: dict | None = None) -> None:
    """Upsert a person document.

    `rich` is an optional dict from fetch_person_rich_fields() containing:
      - "award_qids": list of award QIDs (used to compute awards_count)
      - "awards_top5": list of top-5 award label strings (ranked by statement count)
      - "work_labels": list of notable work label strings (cap 10)
    These are only populated when the person meets the rich_fields_min_statements
    gate (§1a / 3.1).
    """
    col = db.collection(PERSONS)
    doc = {
        "_key": qid,
        "wikidata_id": qid,
        "name_en": _val(row, "personLabel") or qid,
        "name_it": _val(row, "nameItLabel") or _val(row, "personLabelIt"),
        "description_en": _val(row, "personDescription"),
        "description_it": _val(row, "descriptionIt"),
        "birth_date": _val(row, "birthDate"),
        "death_date": _val(row, "deathDate"),
        "image_url": _val(row, "image"),
        "wikidata_statements_count": int(_val(row, "stmtCount") or 0),
        "cause_of_death": _val(row, "causeOfDeathLabel"),
        "gender": _val(row, "genderLabel"),
        "source": "wikidata_sparql",
        "last_updated": NOW,
    }
    # Rich fields (gated by rich_fields_min_statements — caller is responsible for gate)
    if rich:
        award_qids = rich.get("award_qids", [])
        if award_qids:
            doc["awards_count"] = len(award_qids)
            doc["awards_top5"] = rich.get("awards_top5", [])
        work_labels = rich.get("work_labels", [])
        if work_labels:
            doc["notable_works"] = work_labels
    try:
        if col.has(qid):
            col.update(doc)
        else:
            # Insert with defaults for required list fields
            doc.setdefault("occupations", [])
            doc.setdefault("nationalities", [])
            doc.setdefault("awards_top5", [])
            doc.setdefault("notable_works", [])
            col.insert(doc)
    except Exception as e:
        logger.warning("Failed to upsert person %s: %s", qid, e)


def upsert_org(db, qid: str, row: dict) -> None:
    """Upsert an organization without duplicating a QID across vertex types.

    Phase 5.2: now writes P856 (website), P159 (headquarters + headquarters_id),
    P1128 (employees_count), P17 (country + country_id) (§1b / 3.2).
    """
    if db.collection(PERSONS).has(qid) or db.collection(EVENTS).has(qid):
        logger.warning("Skipping organization %s: QID already has another vertex type", qid)
        return
    col = db.collection(ORGANIZATIONS)
    # Parse employees_count safely (SPARQL returns numeric literals as strings)
    employees_raw = _val(row, "employees")
    employees_count: int | None = None
    if employees_raw:
        try:
            employees_count = int(float(employees_raw))
        except (ValueError, TypeError):
            pass
    doc = {
        "_key": qid,
        "wikidata_id": qid,
        "name_en": _val(row, "orgLabel") or qid,
        "name_it": _val(row, "orgLabelIt"),
        "description_en": _val(row, "orgDescription"),
        "description_it": _val(row, "orgDescriptionIt"),
        "founded": _val(row, "inception"),
        "dissolved": _val(row, "dissolved"),
        "website": _val(row, "website"),
        "headquarters": _val(row, "hqLabel"),
        "headquarters_id": _qid(_val(row, "hq")),
        "employees_count": employees_count,
        "country": _val(row, "countryLabel"),
        "country_id": _qid(_val(row, "country")),
        "type": "organization",
        "source": "wikidata_sparql",
        "last_updated": NOW,
    }
    try:
        if col.has(qid):
            col.update(doc)
        else:
            col.insert(doc)
    except Exception as e:
        logger.warning("Failed to upsert org %s: %s", qid, e)


def upsert_event(db, qid: str, row: dict) -> None:
    """Upsert an event without duplicating a QID across vertex types.

    Phase 5.2: now writes P276 (location + location_id), P1120 (deaths),
    P495 (country + country_id) (§1c / 3.3).
    """
    if db.collection(PERSONS).has(qid) or db.collection(ORGANIZATIONS).has(qid):
        logger.warning("Skipping event %s: QID already has another vertex type", qid)
        return
    col = db.collection(EVENTS)
    # Parse deaths safely
    deaths_raw = _val(row, "deaths")
    deaths: int | None = None
    if deaths_raw:
        try:
            deaths = int(float(deaths_raw))
        except (ValueError, TypeError):
            pass
    doc = {
        "_key": qid,
        "wikidata_id": qid,
        "name_en": _val(row, "eventLabel") or qid,
        "name_it": _val(row, "eventLabelIt"),
        "description_en": _val(row, "eventDescription"),
        "description_it": _val(row, "eventDescriptionIt"),
        "date": _val(row, "date"),
        "location": _val(row, "locationLabel"),
        "location_id": _qid(_val(row, "location")),
        "deaths": deaths,
        "country": _val(row, "countryLabel"),
        "country_id": _qid(_val(row, "country")),
        "type": "event",
        "source": "wikidata_sparql",
        "last_updated": NOW,
    }
    try:
        if col.has(qid):
            col.update(doc)
        else:
            col.insert(doc)
    except Exception as e:
        logger.warning("Failed to upsert event %s: %s", qid, e)


def upsert_edge(db, from_qid: str, to_qid: str, prop_qid: str) -> None:
    """Upsert a single directed edge if the target node exists in DB."""
    if prop_qid not in PROP_TO_EDGE:
        return

    edge_col_name, rel_type = PROP_TO_EDGE[prop_qid]
    to_col_name = EDGE_TO_COLLECTION[edge_col_name]

    # Only upsert if both endpoints exist
    if not db.collection(PERSONS).has(from_qid):
        return
    if not db.collection(to_col_name).has(to_qid):
        return

    edge_key = f"{from_qid}_{to_qid}_{rel_type}_{prop_qid}"
    edge_key = "".join(c if c.isalnum() or c in "_-:." else "_" for c in edge_key)

    doc = {
        "_key": edge_key,
        "_from": f"{PERSONS}/{from_qid}",
        "_to": f"{to_col_name}/{to_qid}",
        "type": rel_type,
        "wikidata_property": prop_qid,
        "source": "wikidata_sparql",
        "last_updated": NOW,
    }
    try:
        col = db.collection(edge_col_name)
        if col.has(edge_key):
            col.update(doc)
        else:
            col.insert(doc)
    except Exception as e:
        logger.warning("Failed to upsert edge %s: %s", edge_key, e)


def upsert_org_structure_edge(
    db, from_qid: str, to_qid: str, prop_qid: str
) -> None:
    """Upsert a single org→org structure edge into the org_structure collection.

    Phase 5.2 — §2b / 4.3 upsert parity.

    Only emits if both the source and target exist in the organizations collection.
    The endpoint-type check (both sides must be orgs, not persons) is already
    applied by fetch_org_structure_edges() via SPARQL FILTER NOT EXISTS.
    This function adds a second guard by verifying both QIDs exist in organizations.
    """
    if prop_qid not in ORG_STRUCTURE_PROP_TO_TYPE:
        return

    rel_type = ORG_STRUCTURE_PROP_TO_TYPE[prop_qid]
    orgs_col = db.collection(ORGANIZATIONS)

    # Both endpoints must exist in organizations
    if not orgs_col.has(from_qid):
        return
    if not orgs_col.has(to_qid):
        return

    edge_key = f"{from_qid}_{to_qid}_{rel_type}_{prop_qid}"
    edge_key = "".join(c if c.isalnum() or c in "_-:." else "_" for c in edge_key)

    doc = {
        "_key": edge_key,
        "_from": f"{ORGANIZATIONS}/{from_qid}",
        "_to": f"{ORGANIZATIONS}/{to_qid}",
        "type": rel_type,
        "wikidata_property": prop_qid,
        "edge_collection": ORG_STRUCTURE,
        "source": "wikidata_sparql",
        "last_updated": NOW,
    }
    try:
        col = db.collection(ORG_STRUCTURE)
        if col.has(edge_key):
            col.update(doc)
        else:
            col.insert(doc)
    except Exception as e:
        logger.warning("Failed to upsert org_structure edge %s: %s", edge_key, e)


# ── Redis cache invalidation ──────────────────────────────────────────────────


def _cache_key_qids(key: str) -> set[str]:
    """Return QIDs encoded in a graph cache key without scanning Redis again."""
    parts = key.split(":")
    if parts[0] == "neighborhood":
        if len(parts) >= 3 and parts[1] == "v2":
            return {parts[2]}
        if len(parts) >= 2:
            return {parts[1]}
    if parts[0] == "sp" and len(parts) >= 3:
        return {parts[1], parts[2]}
    return set()


def invalidate_cache_for_qids(qids: set[str], dry_run: bool = False) -> int:
    """Delete cache keys affected by updated QIDs in O(keyspace + QIDs).

    Previous code performed four full Redis scans for every QID. A normal
    global update of ~33,000 people therefore issued >130,000 keyspace scans
    and could spend hours in invalidation while the app served mixed states.
    """
    try:
        import redis as redis_lib
        r = redis_lib.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=3,
        )
        r.ping()
    except Exception as e:
        logger.warning("Redis unavailable, skipping cache invalidation: %s", e)
        return 0

    keys_to_delete = {f"person:{qid}" for qid in qids}

    # Scan each graph-cache family once, then test membership in memory.
    for pattern in ("neighborhood:*", "sp:*"):
        for key in r.scan_iter(match=pattern, count=1000):
            if _cache_key_qids(key) & qids:
                keys_to_delete.add(key)

    # Aggregate results can embed any updated entity, so invalidate each family
    # once. Durable feed keys are intentionally not included.
    keys_to_delete.update({"stats", "game:nationalities", "game:nationalities:v2", "trivia:nationalities"})

    # SSR page keys include the rendered content hash, so remove all page
    # variants for changed entities in one scan.  This also covers the cheap
    # fast-path keys used to keep crawler traffic off ArangoDB.
    for pattern in ("person_page:*", "organization_page:*"):
        for key in r.scan_iter(match=pattern, count=1000):
            parts = key.split(":")
            if len(parts) >= 4 and parts[3] in qids:
                keys_to_delete.add(key)

    for pattern in ("leaderboard:*", "search:*", "featured:*", "trending:*"):
        keys_to_delete.update(r.scan_iter(match=pattern, count=1000))

    unique_keys = list(keys_to_delete)
    logger.info("Cache invalidation: %d keys to delete", len(unique_keys))
    if dry_run or not unique_keys:
        return len(unique_keys)

    deleted = 0
    for i in range(0, len(unique_keys), 500):
        try:
            deleted += r.delete(*unique_keys[i : i + 500])
        except Exception as e:
            logger.warning("Redis DELETE batch failed: %s", e)

    logger.info("Deleted %d Redis keys", deleted)
    return deleted


# ── Main orchestration ────────────────────────────────────────────────────────


def run(
    since: datetime,
    until: datetime,
    dry_run: bool,
) -> None:
    """Full incremental update pipeline."""

    # ── Step 1: Connect to ArangoDB ──────────────────────────────────────────
    logger.info("Connecting to ArangoDB...")
    client = get_arango_client()
    db = get_db(client)
    try:
        db.properties()
    except Exception as e:
        logger.error("Cannot connect to ArangoDB: %s", e)
        sys.exit(1)

    # ── Step 2: Collect changed QIDs from EventStreams ───────────────────────
    logger.info(
        "Collecting EventStreams changes from %s to %s",
        since.isoformat(),
        until.isoformat(),
    )
    all_changed_qids = collect_changed_qids(since=since, until=until)
    logger.info("EventStreams: %d total changed QIDs", len(all_changed_qids))

    if not all_changed_qids:
        logger.info("No changes detected, exiting.")
        if not dry_run:
            write_state_file(until)
        return

    # ── Step 3: Cross-reference against DB ──────────────────────────────────
    existing_qids = get_existing_qids(db)

    # QIDs already tracked in DB — always update these
    tracked_qids = all_changed_qids & existing_qids

    # Unknown QIDs — check if they are new persons matching European filter
    unknown_qids = all_changed_qids - existing_qids
    logger.info(
        "Tracked QIDs to update: %d | Unknown QIDs to probe: %d",
        len(tracked_qids),
        len(unknown_qids),
    )

    # Probe unknown QIDs using the same scope as the bulk database.
    new_person_qids: set[str] = set()
    if unknown_qids:
        unknown_list = list(unknown_qids)
        if PERSON_SCOPE == "global":
            logger.info(
                "Probing %d unknown QIDs for global humans with >= %d statements...",
                len(unknown_qids), GLOBAL_MIN_STATEMENTS,
            )
        else:
            logger.info("Probing %d unknown QIDs for European nationality...", len(unknown_qids))
        for i in range(0, len(unknown_list), SPARQL_BATCH_SIZE):
            batch = unknown_list[i : i + SPARQL_BATCH_SIZE]
            try:
                if PERSON_SCOPE == "global":
                    new_person_qids.update(
                        fetch_global_person_qids(batch, GLOBAL_MIN_STATEMENTS)
                    )
                else:
                    nat_map = fetch_nationalities_for_qids(batch)
                    for qid, countries in nat_map.items():
                        if countries & EUROPEAN_COUNTRY_QIDS:
                            new_person_qids.add(qid)
            except Exception as e:
                logger.warning("New-person probe failed for batch: %s", e)
            time.sleep(SPARQL_DELAY)
        logger.info("Found %d new %s-scope persons", len(new_person_qids), PERSON_SCOPE)

    # All QIDs we will update
    qids_to_update = tracked_qids | new_person_qids
    logger.info("Total QIDs to update/insert: %d", len(qids_to_update))

    if not qids_to_update:
        logger.info("No relevant changes, exiting.")
        if not dry_run:
            write_state_file(until)
        return

    if dry_run:
        logger.info(
            "[DRY RUN] Would update %d QIDs: %s%s",
            len(qids_to_update),
            ", ".join(sorted(qids_to_update)[:20]),
            "..." if len(qids_to_update) > 20 else "",
        )
        return

    # ── Step 4: Fetch updated person data and upsert ─────────────────────────
    updated_qids: set[str] = set()
    # statement counts for each upserted person (used for rich-fields gate in step 4b)
    person_stmt_counts: dict[str, int] = {}
    qids_list = list(qids_to_update)

    logger.info("Fetching and upserting person data for %d QIDs...", len(qids_list))
    for i in range(0, len(qids_list), SPARQL_BATCH_SIZE):
        batch = qids_list[i : i + SPARQL_BATCH_SIZE]
        try:
            rows = fetch_persons_by_qids(batch)
            seen_qids: set[str] = set()
            for row in rows:
                qid = _qid(_val(row, "person"))
                if not qid or qid in seen_qids:
                    continue
                seen_qids.add(qid)
                upsert_person(db, qid, row)
                updated_qids.add(qid)
                stmt_count = int(_val(row, "stmtCount") or 0)
                person_stmt_counts[qid] = stmt_count
        except Exception as e:
            logger.error("Failed to fetch persons batch %d: %s", i // SPARQL_BATCH_SIZE, e)
        time.sleep(SPARQL_DELAY)

    logger.info("Upserted %d persons", len(updated_qids))

    # ── Step 4b: Fetch and write rich fields (awards + notable works) ─────────
    # Gated by rich_fields_min_statements (§1a / 3.1) — mirrors the dump loader gate.
    rich_threshold = settings.rich_fields_min_statements
    rich_eligible = [
        qid for qid in updated_qids
        if person_stmt_counts.get(qid, 0) >= rich_threshold
    ]
    if rich_eligible:
        logger.info(
            "Fetching rich fields (P166 awards, P800 notable_works) for %d persons "
            "(>= %d statements)...",
            len(rich_eligible), rich_threshold,
        )
        for i in range(0, len(rich_eligible), SPARQL_BATCH_SIZE):
            batch = rich_eligible[i : i + SPARQL_BATCH_SIZE]
            try:
                rich_data = fetch_person_rich_fields(batch)
                for qid, rdata in rich_data.items():
                    if not rdata.get("award_qids") and not rdata.get("work_labels"):
                        continue
                    # Update the doc in DB directly — we already upserted the base doc.
                    try:
                        col = db.collection(PERSONS)
                        update_doc = {"_key": qid, "last_updated": NOW}
                        award_qids = rdata.get("award_qids", [])
                        if award_qids:
                            update_doc["awards_count"] = len(award_qids)
                            update_doc["awards_top5"] = rdata.get("awards_top5", [])
                        work_labels = rdata.get("work_labels", [])
                        if work_labels:
                            update_doc["notable_works"] = work_labels
                        if col.has(qid):
                            col.update(update_doc)
                    except Exception as e:
                        logger.warning("Failed to write rich fields for %s: %s", qid, e)
            except Exception as e:
                logger.error("Failed to fetch rich fields batch: %s", e)
            # fetch_person_rich_fields already sleeps internally; no extra sleep needed
        logger.info("Rich fields processed for up to %d persons", len(rich_eligible))

    # Steps 5-7 all iterate over the same list of successfully upserted persons
    updated_list = list(updated_qids)

    # ── Step 5: Fetch and upsert organizations ────────────────────────────────
    upserted_org_qids: set[str] = set()
    if updated_list:
        logger.info("Fetching organizations for %d persons...", len(updated_list))
        org_count = 0
        for i in range(0, len(updated_list), SPARQL_BATCH_SIZE):
            batch = updated_list[i : i + SPARQL_BATCH_SIZE]
            try:
                rows = fetch_orgs_for_persons(batch)
                for row in rows:
                    qid = _qid(_val(row, "org"))
                    if qid:
                        upsert_org(db, qid, row)
                        upserted_org_qids.add(qid)
                        org_count += 1
            except Exception as e:
                logger.error("Failed to fetch orgs batch: %s", e)
            time.sleep(SPARQL_DELAY)
        logger.info("Upserted %d organization records", org_count)

    # ── Step 5b: Fetch and upsert org→org structure edges ────────────────────
    # For each org we just upserted, fetch its P749/P355/P127/P1830 edges (§2b / 4.3).
    if upserted_org_qids:
        org_qids_list = list(upserted_org_qids)
        logger.info(
            "Fetching org_structure edges for %d organizations...", len(org_qids_list)
        )
        org_struct_edge_count = 0
        wdt_prefix = "http://www.wikidata.org/prop/direct/"
        for i in range(0, len(org_qids_list), SPARQL_BATCH_SIZE):
            batch = org_qids_list[i : i + SPARQL_BATCH_SIZE]
            try:
                rows = fetch_org_structure_edges(batch)
                for row in rows:
                    from_qid = _qid(_val(row, "org"))
                    to_qid = _qid(_val(row, "counterpart"))
                    prop_uri = _val(row, "prop") or ""
                    prop_qid = prop_uri.replace(wdt_prefix, "")
                    if from_qid and to_qid and prop_qid:
                        upsert_org_structure_edge(db, from_qid, to_qid, prop_qid)
                        org_struct_edge_count += 1
            except Exception as e:
                logger.error("Failed to fetch org_structure edges batch: %s", e)
            time.sleep(SPARQL_DELAY)
        logger.info("Processed %d org_structure edge records", org_struct_edge_count)

    # ── Step 6: Fetch and upsert events ──────────────────────────────────────
    if updated_list:
        logger.info("Fetching events for %d persons...", len(updated_list))
        event_count = 0
        for i in range(0, len(updated_list), SPARQL_BATCH_SIZE):
            batch = updated_list[i : i + SPARQL_BATCH_SIZE]
            try:
                rows = fetch_events_for_persons(batch)
                for row in rows:
                    qid = _qid(_val(row, "event"))
                    if qid:
                        upsert_event(db, qid, row)
                        event_count += 1
            except Exception as e:
                logger.error("Failed to fetch events batch: %s", e)
            time.sleep(SPARQL_DELAY)
        logger.info("Upserted %d event records", event_count)

    # ── Step 7: Fetch and upsert edges ────────────────────────────────────────
    if updated_list:
        logger.info("Fetching edges for %d persons...", len(updated_list))
        edge_count = 0
        wdt_prefix = "http://www.wikidata.org/prop/direct/"
        for i in range(0, len(updated_list), SPARQL_BATCH_SIZE):
            batch = updated_list[i : i + SPARQL_BATCH_SIZE]
            try:
                rows = fetch_edges_for_persons(batch)
                for row in rows:
                    from_qid = _qid(_val(row, "from_entity"))
                    to_qid = _qid(_val(row, "to_entity"))
                    prop_uri = _val(row, "prop") or ""
                    prop_qid = prop_uri.replace(wdt_prefix, "")
                    if from_qid and to_qid and prop_qid:
                        upsert_edge(db, from_qid, to_qid, prop_qid)
                        edge_count += 1
            except Exception as e:
                logger.error("Failed to fetch edges batch: %s", e)
            time.sleep(SPARQL_DELAY)
        logger.info("Processed %d edge records", edge_count)

    # ── Step 8: Cache invalidation ────────────────────────────────────────────
    logger.info("Invalidating Redis cache for %d updated QIDs...", len(updated_qids))
    invalidate_cache_for_qids(updated_qids, dry_run=False)

    # ── Step 9: Save state ────────────────────────────────────────────────────
    write_state_file(until)
    logger.info(
        "Incremental update complete: %d persons updated, state saved to %s",
        len(updated_qids),
        STATE_FILE,
    )


# ── CLI ───────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Incremental Wikidata updater using EventStreams + SPARQL"
    )
    parser.add_argument(
        "--since",
        default=None,
        help=(
            "Start timestamp (ISO-8601 UTC, e.g. 2024-01-15T04:00:00Z). "
            "Overrides the state file. Defaults to last run timestamp or "
            "--window hours ago."
        ),
    )
    parser.add_argument(
        "--window",
        type=float,
        default=24.0,
        help="Collection window in hours (default: 24). Used when --since is not set "
             "and no state file exists.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Collect and report changes without writing to DB or Redis.",
    )
    args = parser.parse_args()

    # Determine `until` first — always "now"
    until = datetime.now(timezone.utc)

    # Determine `since`
    if args.since:
        try:
            since = datetime.fromisoformat(args.since.replace("Z", "+00:00"))
        except ValueError:
            logger.error("Invalid --since timestamp: %s", args.since)
            sys.exit(1)
    else:
        since = read_state_file()
        if since is None:
            since = until - timedelta(hours=args.window)
            logger.info(
                "No state file found, using --window=%.0f hours (since=%s)",
                args.window,
                since.isoformat(),
            )
        else:
            logger.info("Resuming from state file: since=%s", since.isoformat())

    if since >= until:
        logger.info("Since (%s) is not before until (%s), nothing to do.", since, until)
        sys.exit(0)

    logger.info(
        "Starting incremental update | since=%s | until=%s | dry_run=%s",
        since.isoformat(),
        until.isoformat(),
        args.dry_run,
    )

    run(since=since, until=until, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
