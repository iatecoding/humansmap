"""
Pytest tests for ArangoDB connection, schema creation, and indexes.

Requires: ArangoDB running and accessible.

Run inside the Docker container:
    docker compose exec backend pytest tests/test_database.py
"""

import pytest
from app.database import (
    get_arango_client, init_db,
    PERSONS, ORGANIZATIONS, EVENTS,
    RELATIONSHIPS, AFFILIATIONS, INVOLVEMENTS,
    GRAPH_NAME,
    DOCUMENT_COLLECTIONS, EDGE_COLLECTIONS,
)


@pytest.fixture(scope="module")
def db():
    client = get_arango_client()
    return init_db()


def test_connection(db):
    assert db.name is not None


def test_document_collections_exist(db):
    for name in DOCUMENT_COLLECTIONS:
        assert db.has_collection(name), f"Missing document collection: {name}"


def test_edge_collections_exist(db):
    for name in EDGE_COLLECTIONS:
        col = db.collection(name)
        assert col.properties()["type"] == 3, f"'{name}' should be edge collection (type=3)"


def test_named_graph_exists(db):
    assert db.has_graph(GRAPH_NAME), f"Named graph '{GRAPH_NAME}' missing"


def test_named_graph_edge_definitions(db):
    graph = db.graph(GRAPH_NAME)
    edge_defs = {ed["edge_collection"]: ed for ed in graph.edge_definitions()}
    assert RELATIONSHIPS in edge_defs, "relationships edge not in graph"
    assert AFFILIATIONS in edge_defs, "affiliations edge not in graph"
    assert INVOLVEMENTS in edge_defs, "involvements edge not in graph"


def test_persons_has_multiple_indexes(db):
    persons_col = db.collection(PERSONS)
    indexes = persons_col.indexes()
    # Primary index + at least 4 we create (name_en, name_it, occupations, wikidata_statements_count)
    assert len(indexes) > 1, "Expected multiple indexes on persons"
