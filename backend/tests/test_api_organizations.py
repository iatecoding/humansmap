"""
Integration tests for /api/organizations/{qid} endpoint.

Requires: ArangoDB with data loaded.
"""

import pytest
from fastapi.testclient import TestClient

from app.database import ORGANIZATIONS, get_arango_client, get_db


@pytest.fixture(scope="session")
def first_org_qid():
    db = get_db(get_arango_client())
    cursor = db.aql.execute(f"FOR o IN {ORGANIZATIONS} LIMIT 1 RETURN o._key")
    qids = list(cursor)
    if not qids:
        pytest.skip("organizations collection is empty")
    return qids[0]


def test_get_organization_valid_qid(client: TestClient, first_org_qid: str):
    r = client.get(f"/api/organizations/{first_org_qid}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == first_org_qid or body["_key"] == first_org_qid
    assert "name_en" in body


def test_get_organization_not_found(client: TestClient):
    r = client.get("/api/organizations/Q999999999999")
    assert r.status_code == 404


def test_get_organization_invalid_qid(client: TestClient):
    r = client.get("/api/organizations/not-a-qid")
    assert r.status_code == 422
