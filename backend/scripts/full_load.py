"""Full data load for Italian public figures — all categories, paginated, efficient.

Run inside the backend container:
  docker compose exec backend python /app/scripts/full_load.py

Or locally (with ArangoDB accessible):
  cd backend && python scripts/full_load.py

Options via env vars:
  SKIP_IMAGES=1           skip image downloading (images on by default)
  RESUME=1                skip persons already in DB, only fetch missing edges
  MIN_STATEMENTS=10       override notability threshold
  ONLY_QIDS=Q937857,...   comma-separated QIDs to fetch (top-up mode)
  SKIP_RESIDENCY=1        skip residency-based fetch
"""

import asyncio
import json
import logging
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError

sys.path.insert(0, "/app")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.database import init_db, PERSONS, ORGANIZATIONS, EVENTS
from app.models.schemas import PersonData, OrganizationData, EventData, EdgeData
from app.services.image_cache import download_and_optimize_image

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("full_load")

# ── Configuration ────────────────────────────────────────────────────────────

SPARQL_ENDPOINT = os.getenv("SPARQL_ENDPOINT", "https://query.wikidata.org/sparql")
USER_AGENT = "HumansMap/1.0 (admin@humansmap.com) python-urllib/3.x"
DELAY = float(os.getenv("SPARQL_DELAY", "1.5"))
MIN_STATEMENTS = int(os.getenv("MIN_STATEMENTS", "10"))
SKIP_IMAGES = os.getenv("SKIP_IMAGES", "0") == "1"
RESUME = os.getenv("RESUME", "0") == "1"
SKIP_RESIDENCY = os.getenv("SKIP_RESIDENCY", "0") == "1"
ONLY_QIDS = [q.strip() for q in os.getenv("ONLY_QIDS", "").split(",") if q.strip()]
PAGE_SIZE = 1000   # SPARQL results per page
BATCH_SIZE = 100   # persons per relationship query

# Italy QID on Wikidata
ITALY_QID = "Q38"

# All occupation categories to fetch
OCCUPATION_CATEGORIES = {
    # Politics & law
    "Q82955":   "politician",
    "Q16533":   "judge",
    "Q40348":   "lawyer",
    # Business
    "Q43845":   "businessperson",
    # Media & arts
    "Q1930187": "journalist",
    "Q33999":   "actor",
    "Q2526255": "film_director",
    "Q177220":  "singer",
    "Q36834":   "composer",
    "Q1028181": "painter",
    "Q1281618": "sculptor",
    "Q36180":   "writer",
    "Q33231":   "photographer",
    # Military & crime
    "Q47064":   "military_person",
    "Q2159907": "criminal",
    # Sport
    "Q937857":  "football_player",
    "Q11338576": "cyclist",
    "Q19204627": "tennis_player",
    "Q10871364": "racing_driver",
    "Q3665646": "basketball_player",
    "Q18116754": "alpine_skier",
    "Q12362622": "boxer",
    "Q10843263": "sprinter",
    "Q13582652": "football_manager",
    "Q11513337": "water_polo_player",
    "Q2736":    "swimmer",
}

# ── SPARQL helpers ────────────────────────────────────────────────────────────

def sparql_get(query: str) -> list[dict]:
    """Execute a SPARQL query via GET, return bindings list. Raises on error."""
    params = urllib.parse.urlencode({"query": query, "format": "json"})
    url = f"{SPARQL_ENDPOINT}?{params}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read())
        return data["results"]["bindings"]
    except HTTPError as e:
        if e.code == 429:
            logger.warning("Rate-limited (429) — sleeping 30s then retrying")
            import time; time.sleep(30)
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = json.loads(resp.read())
            return data["results"]["bindings"]
        raise


def val(binding: dict, key: str) -> str | None:
    if key in binding and "value" in binding[key]:
        return binding[key]["value"]
    return None


def qid(uri: str | None) -> str | None:
    if uri and "entity/" in uri:
        return uri.split("entity/")[-1]
    return None


# ── Person fetching with pagination ──────────────────────────────────────────

def fetch_persons_by_occupation(occ_qid: str, occ_label: str) -> dict[str, PersonData]:
    """Fetch all Italian persons with this occupation using OFFSET pagination."""
    all_persons: dict[str, PersonData] = {}
    offset = 0

    while True:
        query = f"""
        SELECT ?person ?personLabel ?personDescription ?descriptionIt
               ?birthDate ?deathDate ?image ?stmtCount
               ?nameItLabel ?personLabelIt ?causeOfDeathLabel ?genderLabel
               ?birthPlaceLabel ?deathPlaceLabel
        WHERE {{
          ?person wdt:P27 wd:{ITALY_QID} .
          ?person wdt:P106 wd:{occ_qid} .
          ?person wikibase:statements ?stmtCount .
          FILTER(?stmtCount >= {MIN_STATEMENTS})
          OPTIONAL {{ ?person wdt:P569 ?birthDate . }}
          OPTIONAL {{ ?person wdt:P570 ?deathDate . }}
          OPTIONAL {{ ?person wdt:P18 ?image . }}
          OPTIONAL {{ ?person wdt:P1559 ?nameIt . }}
          OPTIONAL {{ ?person wdt:P509 ?causeOfDeath . }}
          OPTIONAL {{ ?person wdt:P21 ?gender . }}
          OPTIONAL {{ ?person wdt:P19 ?birthPlace . }}
          OPTIONAL {{ ?person wdt:P20 ?deathPlace . }}
          OPTIONAL {{ ?person rdfs:label ?personLabelIt . FILTER(LANG(?personLabelIt) = "it") }}
          OPTIONAL {{ ?person schema:description ?descriptionIt . FILTER(LANG(?descriptionIt) = "it") }}
          SERVICE wikibase:label {{
            bd:serviceParam wikibase:language "it,en" .
          }}
        }}
        ORDER BY ?person
        LIMIT {PAGE_SIZE} OFFSET {offset}
        """

        try:
            results = sparql_get(query)
        except Exception as e:
            logger.error("SPARQL failed (occ=%s offset=%d): %s", occ_qid, offset, e)
            break

        if not results:
            break

        for row in results:
            person_qid = qid(val(row, "person"))
            if not person_qid or person_qid in all_persons:
                continue
            all_persons[person_qid] = PersonData(
                wikidata_id=person_qid,
                name_en=val(row, "personLabel") or person_qid,
                name_it=val(row, "nameItLabel") or val(row, "personLabelIt"),
                description_en=val(row, "personDescription"),
                description_it=val(row, "descriptionIt"),
                birth_date=val(row, "birthDate"),
                death_date=val(row, "deathDate"),
                image_url=val(row, "image"),
                wikidata_statements_count=int(val(row, "stmtCount") or 0),
                occupations=[occ_label],
                nationalities=["Italian"],
                cause_of_death=val(row, "causeOfDeathLabel"),
                gender=val(row, "genderLabel"),
                birth_place=val(row, "birthPlaceLabel"),
                death_place=val(row, "deathPlaceLabel"),
                source="wikidata_sparql",
            )

        logger.info("  %s (offset=%d): +%d → total %d", occ_label, offset, len(results), len(all_persons))

        if len(results) < PAGE_SIZE:
            break  # last page

        offset += PAGE_SIZE
        import time; time.sleep(DELAY)

    return all_persons


def fetch_persons_by_residency() -> dict[str, PersonData]:
    """Fetch persons with Italian residency (P551=Q38) not already covered by citizenship."""
    all_persons: dict[str, PersonData] = {}
    offset = 0

    while True:
        query = f"""
        SELECT ?person ?personLabel ?personDescription ?descriptionIt
               ?birthDate ?deathDate ?image ?stmtCount
               ?nameItLabel ?personLabelIt ?causeOfDeathLabel ?genderLabel
        WHERE {{
          ?person wdt:P551 wd:{ITALY_QID} .
          ?person wikibase:statements ?stmtCount .
          FILTER(?stmtCount >= {MIN_STATEMENTS})
          OPTIONAL {{ ?person wdt:P569 ?birthDate . }}
          OPTIONAL {{ ?person wdt:P570 ?deathDate . }}
          OPTIONAL {{ ?person wdt:P18 ?image . }}
          OPTIONAL {{ ?person wdt:P1559 ?nameIt . }}
          OPTIONAL {{ ?person wdt:P509 ?causeOfDeath . }}
          OPTIONAL {{ ?person wdt:P21 ?gender . }}
          OPTIONAL {{ ?person rdfs:label ?personLabelIt . FILTER(LANG(?personLabelIt) = "it") }}
          OPTIONAL {{ ?person schema:description ?descriptionIt . FILTER(LANG(?descriptionIt) = "it") }}
          SERVICE wikibase:label {{
            bd:serviceParam wikibase:language "en,it" .
          }}
        }}
        ORDER BY ?person
        LIMIT {PAGE_SIZE} OFFSET {offset}
        """

        try:
            results = sparql_get(query)
        except Exception as e:
            logger.error("SPARQL residency (offset=%d): %s", offset, e)
            break

        if not results:
            break

        for row in results:
            person_qid = qid(val(row, "person"))
            if not person_qid or person_qid in all_persons:
                continue
            all_persons[person_qid] = PersonData(
                wikidata_id=person_qid,
                name_en=val(row, "personLabel") or person_qid,
                name_it=val(row, "nameItLabel") or val(row, "personLabelIt"),
                description_en=val(row, "personDescription"),
                description_it=val(row, "descriptionIt"),
                birth_date=val(row, "birthDate"),
                death_date=val(row, "deathDate"),
                image_url=val(row, "image"),
                wikidata_statements_count=int(val(row, "stmtCount") or 0),
                occupations=["resident"],
                nationalities=["Italian"],
                cause_of_death=val(row, "causeOfDeathLabel"),
                gender=val(row, "genderLabel"),
                source="wikidata_sparql",
            )

        logger.info("  residency (offset=%d): +%d → total %d", offset, len(results), len(all_persons))

        if len(results) < PAGE_SIZE:
            break

        offset += PAGE_SIZE
        import time; time.sleep(DELAY)

    return all_persons


# ── Organization fetching ─────────────────────────────────────────────────────

def fetch_organizations_batch(person_qids: list[str]) -> dict[str, OrganizationData]:
    values = " ".join(f"wd:{q}" for q in person_qids)
    query = f"""
    SELECT DISTINCT ?org ?orgLabel ?orgLabelIt ?orgDescription ?orgDescriptionIt ?inception ?dissolved WHERE {{
      VALUES ?person {{ {values} }}
      {{
        ?person wdt:P102 ?org .
      }} UNION {{
        ?person wdt:P108 ?org .
      }} UNION {{
        ?person wdt:P463 ?org .
      }}
      OPTIONAL {{ ?org wdt:P571 ?inception . }}
      OPTIONAL {{ ?org wdt:P576 ?dissolved . }}
      OPTIONAL {{ ?org rdfs:label ?orgLabelIt . FILTER(LANG(?orgLabelIt) = "it") }}
      OPTIONAL {{ ?org schema:description ?orgDescriptionIt . FILTER(LANG(?orgDescriptionIt) = "it") }}
      SERVICE wikibase:label {{
        bd:serviceParam wikibase:language "en,it" .
      }}
    }}
    """
    results = sparql_get(query)
    orgs = {}
    for row in results:
        org_qid = qid(val(row, "org"))
        if org_qid and org_qid not in orgs:
            orgs[org_qid] = OrganizationData(
                wikidata_id=org_qid,
                name_en=val(row, "orgLabel") or org_qid,
                name_it=val(row, "orgLabelIt"),
                description_en=val(row, "orgDescription"),
                description_it=val(row, "orgDescriptionIt"),
                type="organization",
                founded=val(row, "inception"),
                dissolved=val(row, "dissolved"),
                source="wikidata_sparql",
            )

    # Also fetch P39 (position_held) targets — offices/roles like "Presidente del Consiglio"
    pos_query = f"""
    SELECT DISTINCT ?org ?orgLabel ?orgLabelIt ?orgDescription ?orgDescriptionIt WHERE {{
      VALUES ?person {{ {values} }}
      ?person wdt:P39 ?org .
      OPTIONAL {{ ?org rdfs:label ?orgLabelIt . FILTER(LANG(?orgLabelIt) = "it") }}
      OPTIONAL {{ ?org schema:description ?orgDescriptionIt . FILTER(LANG(?orgDescriptionIt) = "it") }}
      SERVICE wikibase:label {{
        bd:serviceParam wikibase:language "en,it" .
      }}
    }}
    """
    try:
        pos_results = sparql_get(pos_query)
        for row in pos_results:
            org_qid = qid(val(row, "org"))
            if org_qid and org_qid not in orgs:
                orgs[org_qid] = OrganizationData(
                    wikidata_id=org_qid,
                    name_en=val(row, "orgLabel") or org_qid,
                    name_it=val(row, "orgLabelIt"),
                    description_en=val(row, "orgDescription"),
                    description_it=val(row, "orgDescriptionIt"),
                    type="position",
                    source="wikidata_sparql",
                )
    except Exception as e:
        logger.error("Position fetch failed: %s", e)
    return orgs


# ── Event fetching ────────────────────────────────────────────────────────────

def fetch_events_batch(person_qids: list[str]) -> dict[str, EventData]:
    values = " ".join(f"wd:{q}" for q in person_qids)
    query = f"""
    SELECT DISTINCT ?event ?eventLabel ?eventLabelIt ?eventDescription ?eventDescriptionIt ?date WHERE {{
      VALUES ?person {{ {values} }}
      {{
        ?person wdt:P1344 ?event .
      }} UNION {{
        ?person wdt:P793 ?event .
      }}
      OPTIONAL {{ ?event wdt:P585 ?date . }}
      OPTIONAL {{ ?event rdfs:label ?eventLabelIt . FILTER(LANG(?eventLabelIt) = "it") }}
      OPTIONAL {{ ?event schema:description ?eventDescriptionIt . FILTER(LANG(?eventDescriptionIt) = "it") }}
      SERVICE wikibase:label {{
        bd:serviceParam wikibase:language "en,it" .
      }}
    }}
    """
    results = sparql_get(query)
    events = {}
    for row in results:
        event_qid = qid(val(row, "event"))
        if event_qid and event_qid not in events:
            events[event_qid] = EventData(
                wikidata_id=event_qid,
                name_en=val(row, "eventLabel") or event_qid,
                name_it=val(row, "eventLabelIt"),
                type="event",
                date=val(row, "date"),
                description_en=val(row, "eventDescription"),
                description_it=val(row, "eventDescriptionIt"),
                source="wikidata_sparql",
            )
    return events


# ── Relationship fetching (combined — 3 queries per batch instead of 11) ──────

def fetch_edges_batch(person_qids: list[str]) -> list[EdgeData]:
    """Fetch all edges for a batch of persons in 3 combined queries."""
    edges: list[EdgeData] = []
    values = " ".join(f"wd:{q}" for q in person_qids)

    # 1) Family relationships — combine all properties with OPTIONAL
    family_query = f"""
    SELECT ?person ?prop ?related WHERE {{
      VALUES ?person {{ {values} }}
      VALUES (?prop) {{
        (wdt:P26) (wdt:P22) (wdt:P25) (wdt:P40) (wdt:P3373) (wdt:P1038)
      }}
      ?person ?prop ?related .
    }}
    """
    family_prop_map = {
        "http://www.wikidata.org/prop/direct/P26": ("relationships", "spouse", "P26"),
        "http://www.wikidata.org/prop/direct/P22": ("relationships", "parent", "P22"),
        "http://www.wikidata.org/prop/direct/P25": ("relationships", "parent", "P25"),
        "http://www.wikidata.org/prop/direct/P40": ("relationships", "child", "P40"),
        "http://www.wikidata.org/prop/direct/P3373": ("relationships", "sibling", "P3373"),
        "http://www.wikidata.org/prop/direct/P1038": ("relationships", "relative", "P1038"),
    }
    try:
        results = sparql_get(family_query)
        for row in results:
            from_qid = qid(val(row, "person"))
            to_qid = qid(val(row, "related"))
            prop_uri = val(row, "prop")
            if from_qid and to_qid and prop_uri in family_prop_map:
                collection, rel_type, prop_id = family_prop_map[prop_uri]
                edges.append(EdgeData(
                    from_id=from_qid, to_id=to_qid,
                    edge_collection=collection, type=rel_type,
                    wikidata_property=prop_id, source="wikidata_sparql",
                ))
    except Exception as e:
        logger.error("Family edges query failed: %s", e)

    import time; time.sleep(DELAY)

    # 2) Affiliations — person → organization
    aff_query = f"""
    SELECT ?person ?prop ?org WHERE {{
      VALUES ?person {{ {values} }}
      VALUES (?prop) {{
        (wdt:P108) (wdt:P39) (wdt:P102) (wdt:P463) (wdt:P69)
      }}
      ?person ?prop ?org .
    }}
    """
    aff_prop_map = {
        "http://www.wikidata.org/prop/direct/P108": ("affiliations", "employee", "P108"),
        "http://www.wikidata.org/prop/direct/P39":  ("affiliations", "position_held", "P39"),
        "http://www.wikidata.org/prop/direct/P102": ("affiliations", "member_of", "P102"),
        "http://www.wikidata.org/prop/direct/P463": ("affiliations", "member_of", "P463"),
        "http://www.wikidata.org/prop/direct/P69":  ("affiliations", "educated_at", "P69"),
    }
    try:
        results = sparql_get(aff_query)
        for row in results:
            from_qid = qid(val(row, "person"))
            to_qid = qid(val(row, "org"))
            prop_uri = val(row, "prop")
            if from_qid and to_qid and prop_uri in aff_prop_map:
                collection, aff_type, prop_id = aff_prop_map[prop_uri]
                edges.append(EdgeData(
                    from_id=from_qid, to_id=to_qid,
                    edge_collection=collection, type=aff_type,
                    wikidata_property=prop_id, source="wikidata_sparql",
                ))
    except Exception as e:
        logger.error("Affiliation edges query failed: %s", e)

    import time; time.sleep(DELAY)

    # 3) Involvements — person → event
    inv_query = f"""
    SELECT ?person ?prop ?event WHERE {{
      VALUES ?person {{ {values} }}
      VALUES (?prop) {{
        (wdt:P1344) (wdt:P793)
      }}
      ?person ?prop ?event .
    }}
    """
    inv_prop_map = {
        "http://www.wikidata.org/prop/direct/P1344": ("involvements", "participant", "P1344"),
        "http://www.wikidata.org/prop/direct/P793":  ("involvements", "significant_event", "P793"),
    }
    try:
        results = sparql_get(inv_query)
        for row in results:
            from_qid = qid(val(row, "person"))
            to_qid = qid(val(row, "event"))
            prop_uri = val(row, "prop")
            if from_qid and to_qid and prop_uri in inv_prop_map:
                collection, inv_type, prop_id = inv_prop_map[prop_uri]
                edges.append(EdgeData(
                    from_id=from_qid, to_id=to_qid,
                    edge_collection=collection, type=inv_type,
                    wikidata_property=prop_id, source="wikidata_sparql",
                ))
    except Exception as e:
        logger.error("Involvement edges query failed: %s", e)

    import time; time.sleep(DELAY)

    return edges


# ── DB upsert helpers ─────────────────────────────────────────────────────────

def upsert_person(db, person: PersonData) -> None:
    col = db.collection(PERSONS)
    doc = person.model_dump()
    doc["_key"] = person.wikidata_id
    doc["last_updated"] = datetime.now(timezone.utc).isoformat()
    if col.has(person.wikidata_id):
        # Merge occupations if already exists
        existing = col.get(person.wikidata_id)
        merged_occs = list(set((existing.get("occupations") or []) + person.occupations))
        doc["occupations"] = merged_occs
        col.update(doc)
    else:
        col.insert(doc)


def upsert_org(db, org: OrganizationData) -> None:
    col = db.collection(ORGANIZATIONS)
    doc = org.model_dump()
    doc["_key"] = org.wikidata_id
    doc["last_updated"] = datetime.now(timezone.utc).isoformat()
    col.insert(doc) if not col.has(org.wikidata_id) else col.update(doc)


def upsert_event(db, event: EventData) -> None:
    col = db.collection(EVENTS)
    doc = event.model_dump()
    doc["_key"] = event.wikidata_id
    doc["last_updated"] = datetime.now(timezone.utc).isoformat()
    col.insert(doc) if not col.has(event.wikidata_id) else col.update(doc)


def upsert_edge(db, edge: EdgeData) -> None:
    col = db.collection(edge.edge_collection)
    to_col_map = {
        "relationships": PERSONS,
        "affiliations": ORGANIZATIONS,
        "involvements": EVENTS,
    }
    to_col = to_col_map.get(edge.edge_collection, PERSONS)

    if not db.collection(to_col).has(edge.to_id):
        return  # target not in DB, skip

    edge_key = f"{edge.from_id}_{edge.to_id}_{edge.type}_{edge.wikidata_property or 'none'}"
    edge_key = "".join(c if c.isalnum() or c in "_-:." else "_" for c in edge_key)

    doc = edge.model_dump()
    doc["_key"] = edge_key
    doc["_from"] = f"{PERSONS}/{edge.from_id}"
    doc["_to"] = f"{to_col}/{edge.to_id}"
    doc["last_updated"] = datetime.now(timezone.utc).isoformat()
    doc.pop("from_id", None)
    doc.pop("to_id", None)
    doc.pop("edge_collection", None)

    try:
        col.insert(doc) if not col.has(edge_key) else col.update(doc)
    except Exception as e:
        logger.debug("Edge upsert failed %s: %s", edge_key, e)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    import time

    logger.info("=== Full Load: Italian Public Figures ===")
    logger.info("Settings: MIN_STATEMENTS=%d, DELAY=%.1fs, SKIP_IMAGES=%s, RESUME=%s",
                MIN_STATEMENTS, DELAY, SKIP_IMAGES, RESUME)
    start = time.time()

    db = init_db()

    # ── Step 1: Fetch all persons ─────────────────────────────────────────────
    logger.info("Step 1: Fetching persons from Wikidata...")
    all_persons: dict[str, PersonData] = {}

    # Filter to specific QIDs if ONLY_QIDS is set (top-up mode)
    categories_to_fetch = {
        qid: label for qid, label in OCCUPATION_CATEGORIES.items()
        if not ONLY_QIDS or qid in ONLY_QIDS
    }
    logger.info("Fetching %d occupation categories%s",
                len(categories_to_fetch),
                f" (top-up: {ONLY_QIDS})" if ONLY_QIDS else "")

    # By occupation (Italian citizens)
    for occ_qid, occ_label in categories_to_fetch.items():
        logger.info("  Fetching occupation: %s (%s)...", occ_label, occ_qid)
        new_persons = fetch_persons_by_occupation(occ_qid, occ_label)
        merged = 0
        for pqid, person in new_persons.items():
            if pqid in all_persons:
                # Merge occupations
                existing_occs = all_persons[pqid].occupations or []
                if occ_label not in existing_occs:
                    all_persons[pqid].occupations = existing_occs + [occ_label]
                merged += 1
            else:
                all_persons[pqid] = person
        logger.info("  → %d new, %d merged, running total: %d", len(new_persons) - merged, merged, len(all_persons))
        time.sleep(DELAY)

    # By residency
    if not SKIP_RESIDENCY:
        logger.info("  Fetching by residency (P551=Q38)...")
        residency_persons = fetch_persons_by_residency()
        new_residency = 0
        for pqid, person in residency_persons.items():
            if pqid not in all_persons:
                all_persons[pqid] = person
                new_residency += 1
        logger.info("  → %d additional persons from residency, total: %d", new_residency, len(all_persons))

    logger.info("Step 1 complete: %d persons in %.0fs", len(all_persons), time.time() - start)

    # ── Step 2: Upsert persons ────────────────────────────────────────────────
    logger.info("Step 2: Upserting persons into ArangoDB...")
    person_ids: list[str] = []
    upsert_count = 0
    skip_count = 0

    for person in all_persons.values():
        if RESUME and db.collection(PERSONS).has(person.wikidata_id):
            skip_count += 1
            person_ids.append(person.wikidata_id)
            continue
        try:
            upsert_person(db, person)
            person_ids.append(person.wikidata_id)
            upsert_count += 1
        except Exception as e:
            logger.warning("Failed to upsert person %s: %s", person.wikidata_id, e)

    logger.info("Step 2 complete: %d upserted, %d skipped (RESUME)", upsert_count, skip_count)

    # ── Step 3: Fetch + upsert organizations ──────────────────────────────────
    logger.info("Step 3: Fetching organizations (%d person batches of %d)...",
                (len(person_ids) + BATCH_SIZE - 1) // BATCH_SIZE, BATCH_SIZE)
    all_orgs: dict[str, OrganizationData] = {}
    for i in range(0, len(person_ids), BATCH_SIZE):
        batch = person_ids[i:i + BATCH_SIZE]
        try:
            batch_orgs = fetch_organizations_batch(batch)
            for org_qid, org in batch_orgs.items():
                if org_qid not in all_orgs:
                    all_orgs[org_qid] = org
                    upsert_org(db, org)
        except Exception as e:
            logger.error("Org batch %d failed: %s", i // BATCH_SIZE, e)
        if (i // BATCH_SIZE) % 10 == 0:
            logger.info("  orgs batch %d/%d — %d orgs so far",
                        i // BATCH_SIZE + 1, (len(person_ids) + BATCH_SIZE - 1) // BATCH_SIZE, len(all_orgs))
        time.sleep(DELAY)

    logger.info("Step 3 complete: %d organizations", len(all_orgs))

    # ── Step 4: Fetch + upsert events ────────────────────────────────────────
    logger.info("Step 4: Fetching events...")
    all_events: dict[str, EventData] = {}
    for i in range(0, len(person_ids), BATCH_SIZE):
        batch = person_ids[i:i + BATCH_SIZE]
        try:
            batch_events = fetch_events_batch(batch)
            for event_qid, event in batch_events.items():
                if event_qid not in all_events:
                    all_events[event_qid] = event
                    upsert_event(db, event)
        except Exception as e:
            logger.error("Event batch %d failed: %s", i // BATCH_SIZE, e)
        if (i // BATCH_SIZE) % 10 == 0:
            logger.info("  events batch %d/%d — %d events so far",
                        i // BATCH_SIZE + 1, (len(person_ids) + BATCH_SIZE - 1) // BATCH_SIZE, len(all_events))
        time.sleep(DELAY)

    logger.info("Step 4 complete: %d events", len(all_events))

    # ── Step 5: Fetch + upsert edges ─────────────────────────────────────────
    logger.info("Step 5: Fetching relationships (3 queries per batch of %d)...", BATCH_SIZE)
    total_edges = 0
    for i in range(0, len(person_ids), BATCH_SIZE):
        batch = person_ids[i:i + BATCH_SIZE]
        try:
            edges = fetch_edges_batch(batch)
            for edge in edges:
                upsert_edge(db, edge)
            total_edges += len(edges)
        except Exception as e:
            logger.error("Edge batch %d failed: %s", i // BATCH_SIZE, e)
        if (i // BATCH_SIZE) % 5 == 0:
            logger.info("  edge batch %d/%d — %d edges so far",
                        i // BATCH_SIZE + 1, (len(person_ids) + BATCH_SIZE - 1) // BATCH_SIZE, total_edges)

    logger.info("Step 5 complete: %d edges", total_edges)

    # ── Step 6: Download images ───────────────────────────────────────────────
    images_done = 0
    if not SKIP_IMAGES:
        logger.info("Step 6: Downloading images for %d persons...", len(all_persons))
        persons_with_image = [p for p in all_persons.values() if p.image_url]
        logger.info("  %d persons have image URLs", len(persons_with_image))
        for person in persons_with_image:
            try:
                cached_path = await download_and_optimize_image(
                    person.wikidata_id,
                    person.image_url,
                    person.image_license,
                )
                if cached_path:
                    db.collection(PERSONS).update({
                        "_key": person.wikidata_id,
                        "image_cached_path": cached_path,
                    })
                    images_done += 1
            except Exception as e:
                logger.debug("Image failed for %s: %s", person.wikidata_id, e)
        logger.info("Step 6 complete: %d images cached", images_done)
    else:
        logger.info("Step 6: Skipped (SKIP_IMAGES=1)")

    elapsed = time.time() - start
    logger.info("=== Full Load Complete ===")
    logger.info("  Persons:       %d", len(person_ids))
    logger.info("  Organizations: %d", len(all_orgs))
    logger.info("  Events:        %d", len(all_events))
    logger.info("  Edges:         %d", total_edges)
    logger.info("  Images:        %d", images_done)
    logger.info("  Total time:    %.0f minutes", elapsed / 60)


if __name__ == "__main__":
    asyncio.run(main())
