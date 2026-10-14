"""
Integration tests for /api/events/{qid} endpoint.

Requires: ArangoDB with data loaded.
"""

import pytest
from fastapi.testclient import TestClient

from app.database import EVENTS, get_arango_client, get_db


@pytest.fixture(scope="session")
def first_event_qid():
    db = get_db(get_arango_client())
    cursor = db.aql.execute(f"FOR e IN {EVENTS} LIMIT 1 RETURN e._key")
    qids = list(cursor)
    if not qids:
        pytest.skip("events collection is empty")
    return qids[0]


def test_get_event_valid_qid(client: TestClient, first_event_qid: str):
    r = client.get(f"/api/events/{first_event_qid}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == first_event_qid or body["_key"] == first_event_qid
    assert "name_en" in body


def test_get_event_not_found(client: TestClient):
    r = client.get("/api/events/Q999999999999")
    assert r.status_code == 404


def test_get_event_invalid_qid(client: TestClient):
    r = client.get("/api/events/not-a-qid")
    assert r.status_code == 422
