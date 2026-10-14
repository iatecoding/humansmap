"""
Shared pytest fixtures for the backend test suite.

These are integration tests — they require live ArangoDB and Redis.

Run inside the Docker container:
    docker compose exec backend sh -c "pip install -r requirements-dev.txt && pytest"
"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="session")
def client():
    """Session-scoped TestClient: starts the app once, runs the lifespan,
    and reuses the same connection for all tests in the session."""
    from app.main import app

    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture(scope="session")
def first_person_qid(client):
    """Return the QID of the first person in the DB (used by tests that need a real QID)."""
    r = client.get("/api/persons")
    assert r.status_code == 200
    items = r.json()["items"]
    assert items, "persons collection is empty — load some data first"
    return items[0]["_key"]
