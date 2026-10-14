"""ETL orchestrator — consumes any DataProvider and loads data into ArangoDB."""

import logging
from datetime import datetime, timezone

from arango.database import StandardDatabase

from app.config import settings
from app.database import PERSONS, ORGANIZATIONS, EVENTS
from app.models.schemas import PersonData, OrganizationData, EventData, EdgeData, SyncMetadata
from app.providers.base import DataProvider
from app.services.image_cache import download_and_optimize_image

logger = logging.getLogger(__name__)


def _upsert_person(db: StandardDatabase, person: PersonData) -> None:
    col = db.collection(PERSONS)
    doc = person.model_dump()
    doc["_key"] = person.wikidata_id
    doc["last_updated"] = datetime.now(timezone.utc).isoformat()
    if col.has(person.wikidata_id):
        col.update(doc)
    else:
        col.insert(doc)


def _upsert_organization(db: StandardDatabase, org: OrganizationData) -> None:
    col = db.collection(ORGANIZATIONS)
    doc = org.model_dump()
    doc["_key"] = org.wikidata_id
    doc["last_updated"] = datetime.now(timezone.utc).isoformat()
    if col.has(org.wikidata_id):
        col.update(doc)
    else:
        col.insert(doc)


def _upsert_event(db: StandardDatabase, event: EventData) -> None:
    col = db.collection(EVENTS)
    doc = event.model_dump()
    doc["_key"] = event.wikidata_id
    doc["last_updated"] = datetime.now(timezone.utc).isoformat()
    if col.has(event.wikidata_id):
        col.update(doc)
    else:
        col.insert(doc)


def _collection_for_edge(edge: EdgeData) -> str:
    """Determine the from/to collection prefixes based on edge_collection."""
    return edge.edge_collection


def _from_collection(edge: EdgeData) -> str:
    return PERSONS  # All edges originate from persons


def _to_collection(edge: EdgeData) -> str:
    if edge.edge_collection == "relationships":
        return PERSONS
    elif edge.edge_collection == "affiliations":
        return ORGANIZATIONS
    elif edge.edge_collection == "involvements":
        return EVENTS
    return PERSONS


def _upsert_edge(db: StandardDatabase, edge: EdgeData) -> None:
    col = db.collection(edge.edge_collection)
    from_col = _from_collection(edge)
    to_col = _to_collection(edge)

    from_id = f"{from_col}/{edge.from_id}"
    to_id = f"{to_col}/{edge.to_id}"

    # Check if target node exists (it may not if it wasn't fetched)
    target_col = db.collection(to_col)
    if not target_col.has(edge.to_id):
        logger.debug("Skipping edge %s → %s: target not in DB", from_id, to_id)
        return

    # Build a unique key for dedup
    edge_key = f"{edge.from_id}_{edge.to_id}_{edge.type}_{edge.wikidata_property or 'none'}"
    # Sanitize key (ArangoDB keys: alphanumeric, _, -, :, .)
    edge_key = "".join(c if c.isalnum() or c in "_-:." else "_" for c in edge_key)

    doc = edge.model_dump()
    doc["_key"] = edge_key
    doc["_from"] = from_id
    doc["_to"] = to_id
    doc["last_updated"] = datetime.now(timezone.utc).isoformat()
    # Remove fields that aren't stored on the edge doc
    doc.pop("from_id", None)
    doc.pop("to_id", None)
    doc.pop("edge_collection", None)

    try:
        if col.has(edge_key):
            col.update(doc)
        else:
            col.insert(doc)
    except Exception as e:
        logger.warning("Failed to upsert edge %s: %s", edge_key, e)


async def run_etl(
    db: StandardDatabase,
    provider: DataProvider,
    process_images: bool = True,
    **filters,
) -> SyncMetadata:
    """Run the full ETL pipeline for a given provider.

    1. Fetch persons → upsert
    2. Fetch organizations → upsert
    3. Fetch events → upsert
    4. Fetch relationships/edges → upsert
    5. Optionally download and optimize images
    """
    metadata = SyncMetadata(
        provider=provider.get_provider_name(),
        started_at=datetime.now(timezone.utc),
    )

    # Step 1: Fetch and upsert persons
    logger.info("ETL: Fetching persons from %s...", provider.get_provider_name())
    try:
        persons = await provider.fetch_persons(**filters)
    except Exception as e:
        logger.error("Failed to fetch persons: %s", e)
        metadata.errors.append(f"fetch_persons: {e}")
        metadata.completed_at = datetime.now(timezone.utc)
        return metadata

    person_ids = []
    for person in persons:
        try:
            _upsert_person(db, person)
            person_ids.append(person.wikidata_id)
            metadata.persons_processed += 1
        except Exception as e:
            logger.warning("Failed to upsert person %s: %s", person.wikidata_id, e)
            metadata.errors.append(f"upsert_person {person.wikidata_id}: {e}")

    logger.info("ETL: Upserted %d persons", metadata.persons_processed)

    # Step 2: Fetch and upsert organizations
    logger.info("ETL: Fetching organizations...")
    try:
        organizations = await provider.fetch_organizations(person_ids)
        for org in organizations:
            try:
                _upsert_organization(db, org)
            except Exception as e:
                logger.warning("Failed to upsert org %s: %s", org.wikidata_id, e)
    except Exception as e:
        logger.error("Failed to fetch organizations: %s", e)
        metadata.errors.append(f"fetch_organizations: {e}")

    # Step 3: Fetch and upsert events
    logger.info("ETL: Fetching events...")
    try:
        events = await provider.fetch_events(person_ids)
        for event in events:
            try:
                _upsert_event(db, event)
            except Exception as e:
                logger.warning("Failed to upsert event %s: %s", event.wikidata_id, e)
    except Exception as e:
        logger.error("Failed to fetch events: %s", e)
        metadata.errors.append(f"fetch_events: {e}")

    # Step 4: Fetch and upsert edges
    logger.info("ETL: Fetching relationships/edges...")
    try:
        edges = await provider.fetch_relationships(person_ids)
        for edge in edges:
            _upsert_edge(db, edge)
            metadata.edges_processed += 1
    except Exception as e:
        logger.error("Failed to fetch relationships: %s", e)
        metadata.errors.append(f"fetch_relationships: {e}")

    logger.info("ETL: Upserted %d edges", metadata.edges_processed)

    # Step 5: Download and optimize images
    if process_images:
        logger.info("ETL: Processing images (min_statements=%d)...", settings.image_min_statements)
        for person in persons:
            if person.image_url and (
                person.wikidata_statements_count is None
                or person.wikidata_statements_count > settings.image_min_statements
            ):
                try:
                    cached_path = await download_and_optimize_image(
                        person.wikidata_id,
                        person.image_url,
                        person.image_license,
                    )
                    if cached_path:
                        # Update person record with cached path
                        db.collection(PERSONS).update({
                            "_key": person.wikidata_id,
                            "image_cached_path": cached_path,
                        })
                        metadata.images_processed += 1
                except Exception as e:
                    logger.warning("Image processing failed for %s: %s", person.wikidata_id, e)

    metadata.completed_at = datetime.now(timezone.utc)
    logger.info(
        "ETL complete: %d persons, %d edges, %d images, %d errors",
        metadata.persons_processed,
        metadata.edges_processed,
        metadata.images_processed,
        len(metadata.errors),
    )
    return metadata
