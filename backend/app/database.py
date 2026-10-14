"""ArangoDB client setup, collection creation, named graph, and indexes."""

import logging
import threading
import time

from arango import ArangoClient
from arango.database import StandardDatabase
from arango.exceptions import ServerConnectionError

from app.config import settings

logger = logging.getLogger(__name__)

# Collection names
PERSONS = "persons"
ORGANIZATIONS = "organizations"
EVENTS = "events"
RELATIONSHIPS = "relationships"
AFFILIATIONS = "affiliations"
INVOLVEMENTS = "involvements"
ORG_STRUCTURE = "org_structure"  # org→org ownership/hierarchy edges (§2c)
SEC_CIK_MAP = "sec_cik_map"
# sec_person_candidates: scoped pool of (org_qid, person_qid) pairs from Wikidata P108/P39+P642.
# Used by downstream Form 3/4/DEF 14A ingestion for Tier-1 name matching (§3.2).
# Document shape:
#   _key            : "{org_qid}_{person_qid}"   e.g. "Q312_Q312097"
#   org_qid         : str   e.g. "Q312"
#   person_qid      : str   e.g. "Q312097"
#   person_name_en  : str   e.g. "Tim Cook"
#   source          : str   "P108" | "P39_P642" | "P108+P39_P642"
#   last_synced     : str   ISO-8601
SEC_PERSON_CANDIDATES = "sec_person_candidates"
GRAPH_NAME = "human_graph"
SEARCH_VIEW = "search_view"
GAME_CHALLENGES = "game_challenges"
APP_METADATA = "app_metadata"

DOCUMENT_COLLECTIONS = [
    PERSONS, ORGANIZATIONS, EVENTS, SEC_CIK_MAP, SEC_PERSON_CANDIDATES, APP_METADATA
]
EDGE_COLLECTIONS = [RELATIONSHIPS, AFFILIATIONS, INVOLVEMENTS, ORG_STRUCTURE]

# One HTTP client per request-timeout value and process.  Creating a fresh
# python-arango client for every request creates a fresh requests session and
# connection pool; under crawler bursts those pools can leave hundreds of
# sockets alive at once.  The client is thread-safe and is shared by the
# synchronous database calls that run in the web worker thread pool.
_clients: dict[int | float | None, ArangoClient] = {}
_clients_lock = threading.Lock()


def get_arango_client(*, request_timeout: int | float | None = 60) -> ArangoClient:
    """Create an Arango client with an explicit HTTP request timeout.

    Interactive API calls keep the existing 60-second bound. Long-running
    maintenance jobs can opt into a larger timeout without changing request
    latency guarantees for the web application.
    """
    with _clients_lock:
        client = _clients.get(request_timeout)
        if client is None:
            client = ArangoClient(
                hosts=f"http://{settings.arango_host}:{settings.arango_port}",
                request_timeout=request_timeout,
            )
            _clients[request_timeout] = client
        return client


def close_arango_clients() -> None:
    """Close process-local HTTP sessions during application shutdown."""
    with _clients_lock:
        clients = list(_clients.values())
        _clients.clear()
    for client in clients:
        client.close()


def get_db(client: ArangoClient) -> StandardDatabase:
    return client.db(
        settings.arango_db,
        username=settings.arango_username,
        password=settings.arango_password,
    )


def ensure_database(client: ArangoClient) -> StandardDatabase:
    """Return a handle to the application database.

    The database and app user must already exist (created by arango-init/init-user.js).
    If connection fails, it likely means the init script hasn't been run yet.
    """
    try:
        db = get_db(client)
        db.properties()  # Test the connection
        return db
    except Exception as e:
        logger.error(
            "Cannot connect to database '%s' as user '%s'. "
            "Have you run arango-init/init-user.js? Error: %s",
            settings.arango_db, settings.arango_username, e,
        )
        raise


def ensure_collections(db: StandardDatabase) -> None:
    """Create document and edge collections if they don't exist."""
    for name in DOCUMENT_COLLECTIONS:
        if not db.has_collection(name):
            db.create_collection(name)
            logger.info("Created document collection '%s'", name)

    for name in EDGE_COLLECTIONS:
        if not db.has_collection(name):
            db.create_collection(name, edge=True)
            logger.info("Created edge collection '%s'", name)

    if not db.has_collection(GAME_CHALLENGES):
        db.create_collection(GAME_CHALLENGES)
        logger.info("Created document collection '%s'", GAME_CHALLENGES)


def ensure_graph(db: StandardDatabase) -> None:
    """Register the named graph so ArangoDB can traverse across all edge types.

    Idempotent: if the graph already exists, ensures the org_structure and
    involvements (events→orgs direction added in §10.5.a) edge definitions are
    present.  Other definitions are left as-is to avoid conflicts on live
    databases.
    """
    _ORG_STRUCTURE_DEF = {
        "edge_collection": ORG_STRUCTURE,
        "from_vertex_collections": [ORGANIZATIONS],
        "to_vertex_collections": [ORGANIZATIONS],
    }

    # §10.5.a: 8-K material-event involvements run events → organizations
    # (the original definition only covers persons → events).  We store both
    # directions in the same edge collection; ArangoDB allows multi-source /
    # multi-destination vertex collections on a single edge definition.
    _INVOLVEMENTS_DEF = {
        "edge_collection": INVOLVEMENTS,
        "from_vertex_collections": [PERSONS, EVENTS],
        "to_vertex_collections": [EVENTS, ORGANIZATIONS],
    }

    if db.has_graph(GRAPH_NAME):
        graph = db.graph(GRAPH_NAME)
        existing_edge_cols = {ed["edge_collection"] for ed in graph.edge_definitions()}

        if ORG_STRUCTURE not in existing_edge_cols:
            graph.create_edge_definition(**_ORG_STRUCTURE_DEF)
            logger.info(
                "Added '%s' edge definition to existing graph '%s'",
                ORG_STRUCTURE, GRAPH_NAME,
            )

        # Widen involvements to also cover events→orgs if the existing
        # definition is the narrower persons→events version.
        if INVOLVEMENTS in existing_edge_cols:
            inv_def = next(
                ed for ed in graph.edge_definitions()
                if ed["edge_collection"] == INVOLVEMENTS
            )
            needs_events_src = EVENTS not in inv_def.get("from_vertex_collections", [])
            needs_orgs_dst = ORGANIZATIONS not in inv_def.get("to_vertex_collections", [])
            if needs_events_src or needs_orgs_dst:
                graph.replace_edge_definition(**_INVOLVEMENTS_DEF)
                logger.info(
                    "Widened '%s' edge definition in graph '%s' to cover "
                    "events→organizations direction (§10.5.a)",
                    INVOLVEMENTS, GRAPH_NAME,
                )
        return

    db.create_graph(
        GRAPH_NAME,
        edge_definitions=[
            {
                "edge_collection": RELATIONSHIPS,
                "from_vertex_collections": [PERSONS],
                "to_vertex_collections": [PERSONS],
            },
            {
                "edge_collection": AFFILIATIONS,
                "from_vertex_collections": [PERSONS],
                "to_vertex_collections": [ORGANIZATIONS],
            },
            _INVOLVEMENTS_DEF,
            _ORG_STRUCTURE_DEF,
        ],
    )
    logger.info("Created named graph '%s'", GRAPH_NAME)


def ensure_indexes(db: StandardDatabase) -> None:
    """Create all indexes as specified in the project spec."""
    persons = db.collection(PERSONS)
    persons.add_index({"type": "hash", "fields": ["name_en"], "unique": False})
    persons.add_index({"type": "hash", "fields": ["name_it"], "unique": False})
    persons.add_index({"type": "persistent", "fields": ["occupations[*]"]})
    persons.add_index({"type": "persistent", "fields": ["primary_occupation"]})
    persons.add_index({"type": "persistent", "fields": ["wikidata_statements_count"]})
    persons.add_index({"type": "persistent", "fields": ["last_updated"]})
    persons.add_index({"type": "persistent", "fields": ["nationalities[*]"]})
    persons.add_index({"type": "persistent", "fields": ["awards_count"]})  # future "most awarded" discovery (§4c)
    persons.add_index({"type": "persistent", "fields": ["sec_cik"], "sparse": True})  # SEC EDGAR linkage (§4.4)
    persons.add_index({"type": "persistent", "fields": ["birth_date"], "sparse": True})  # leaderboard sort-by-birth

    organizations = db.collection(ORGANIZATIONS)
    organizations.add_index({"type": "hash", "fields": ["name_en"], "unique": False})
    organizations.add_index({"type": "persistent", "fields": ["country_id"]})  # powers top-100 audits (§4c)
    organizations.add_index({"type": "persistent", "fields": ["sec_cik"], "sparse": True})  # SEC EDGAR linkage (§4.4)
    organizations.add_index({"type": "persistent", "fields": ["founded"], "sparse": True})  # leaderboard sort-by-founded
    organizations.add_index({"type": "persistent", "fields": ["type"]})  # future org-by-type queries (stats, admin pages)
    organizations.add_index({"type": "persistent", "fields": ["affiliation_count"]})  # materialized org leaderboard

    events = db.collection(EVENTS)
    events.add_index({"type": "hash", "fields": ["name_en"], "unique": False})
    events.add_index({"type": "persistent", "fields": ["date"]})

    for edge_col_name in EDGE_COLLECTIONS:
        col = db.collection(edge_col_name)
        col.add_index({"type": "hash", "fields": ["type"], "unique": False})

    challenges = db.collection(GAME_CHALLENGES)
    challenges.add_index({"type": "persistent", "fields": ["active"]})

    # SEC EDGAR dedup + reverse-lookup indexes (§4.4)
    affiliations = db.collection(AFFILIATIONS)
    affiliations.add_index({"type": "persistent", "fields": ["accession"], "sparse": True})  # dedup key for Form 4 / SC 13D-G edges

    sec_cik_map = db.collection(SEC_CIK_MAP)
    sec_cik_map.add_index({"type": "persistent", "fields": ["wikidata_id"], "unique": True})  # QID→CIK reverse lookup

    # sec_person_candidates: HOT lookup path at ingestion time
    sec_person_candidates = db.collection(SEC_PERSON_CANDIDATES)
    sec_person_candidates.add_index({"type": "persistent", "fields": ["org_qid"]})     # lookup by org (hot path)
    sec_person_candidates.add_index({"type": "persistent", "fields": ["person_qid"]})  # reverse lookup by person

    logger.info("All indexes created/verified")


def ensure_search_view(db: StandardDatabase) -> None:
    """Create or recreate ArangoSearch view with proper per-language analyzers."""
    existing_analyzers = {a["name"] for a in db.analyzers()}

    # text_it: Italian stemming + accent normalization
    it_analyzer_name = f"{settings.arango_db}::text_it"
    if it_analyzer_name not in existing_analyzers:
        db.create_analyzer(
            "text_it",
            "text",
            {
                "locale": "it",
                "case": "lower",
                "accent": False,
                "stemming": True,
                "stopwords": [],
            },
            ["frequency", "norm", "position"],
        )
        logger.info("Created 'text_it' analyzer")

    # text_norm: accent-insensitive, no stemming — catches "Francois" → "François"
    norm_analyzer_name = f"{settings.arango_db}::text_norm"
    if norm_analyzer_name not in existing_analyzers:
        db.create_analyzer(
            "text_norm",
            "text",
            {
                "locale": "en",
                "case": "lower",
                "accent": False,
                "stemming": False,
                "stopwords": [],
            },
            ["frequency", "norm", "position"],
        )
        logger.info("Created 'text_norm' analyzer")

    link_def = {
        "analyzers": ["text_en"],
        "fields": {
            "name_en": {"analyzers": ["text_en", "text_norm"]},
            "name_it": {"analyzers": ["text_it"]},
        },
        "includeAllFields": False,
        "storeValues": "none",
        "trackListPositions": False,
    }
    stored_values = [{"fields": ["wikidata_statements_count"], "compression": "none"}]

    desired_links = {
        PERSONS: link_def,
        ORGANIZATIONS: link_def,
    }

    existing_views = {v["name"] for v in db.views()}
    if SEARCH_VIEW in existing_views:
        try:
            db.update_arangosearch_view(
                name=SEARCH_VIEW,
                properties={"links": desired_links, "storedValues": stored_values},
            )
            logger.info("Updated ArangoSearch view '%s' (persons + orgs only)", SEARCH_VIEW)
        except Exception as e:
            logger.warning("Could not update view: %s", e)
        return

    db.create_arangosearch_view(
        name=SEARCH_VIEW,
        properties={
            "links": desired_links,
            "storedValues": stored_values,
        },
    )
    logger.info("Created ArangoSearch view '%s' with text_en + text_it analyzers", SEARCH_VIEW)


def wait_for_arango(client: ArangoClient, retries: int = 15, delay: float = 3.0) -> None:
    """Block until ArangoDB is reachable, with exponential-ish backoff."""
    for attempt in range(1, retries + 1):
        try:
            db = get_db(client)
            db.properties()
            logger.info("ArangoDB is ready")
            return
        except Exception:
            logger.info("ArangoDB not ready yet (attempt %d/%d), waiting %.0fs...", attempt, retries, delay)
            time.sleep(delay)
    raise RuntimeError("ArangoDB did not become ready in time")


def init_db() -> StandardDatabase:
    """Full initialization: database, collections, graph, indexes."""
    client = get_arango_client()
    wait_for_arango(client)
    db = ensure_database(client)
    ensure_collections(db)
    ensure_graph(db)
    ensure_indexes(db)
    ensure_search_view(db)
    logger.info("Database initialization complete")
    return db
