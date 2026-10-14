"""
Integration tests for /api/persons endpoints.

Requires: ArangoDB with data loaded.
"""

from fastapi.testclient import TestClient


# ── /api/persons (list) ───────────────────────────────────────────────────────

def test_list_persons_shape(client: TestClient):
    r = client.get("/api/persons")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert "total" in body
    assert "page" in body
    assert "page_size" in body
    assert isinstance(body["items"], list)
    assert isinstance(body["total"], int)
    assert body["total"] >= 0


def test_list_persons_pagination(client: TestClient):
    r = client.get("/api/persons?page=1&page_size=5")
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) <= 5
    assert body["page"] == 1
    assert body["page_size"] == 5


def test_list_persons_sort_desc(client: TestClient):
    # Use wikidata_statements_count — it has a persistent index so SORT+LIMIT is fast
    r = client.get("/api/persons?sort=wikidata_statements_count&order=desc&page_size=10")
    assert r.status_code == 200
    items = r.json()["items"]
    counts = [p.get("wikidata_statements_count", 0) for p in items]
    assert counts == sorted(counts, reverse=True)


def test_list_persons_invalid_sort(client: TestClient):
    r = client.get("/api/persons?sort=injected_field")
    assert r.status_code == 422


# ── /api/persons/{qid} (detail) ───────────────────────────────────────────────

def test_get_person_valid_qid(client: TestClient, first_person_qid: str):
    r = client.get(f"/api/persons/{first_person_qid}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == first_person_qid or body["_key"] == first_person_qid
    assert "name_en" in body


def test_get_person_not_found(client: TestClient):
    r = client.get("/api/persons/Q999999999999")
    assert r.status_code == 404


def test_get_person_invalid_qid_format(client: TestClient):
    r = client.get("/api/persons/not-a-qid")
    assert r.status_code == 422


# ── /api/persons/{qid}/connections ───────────────────────────────────────────

def test_get_connections_shape(client: TestClient, first_person_qid: str):
    r = client.get(f"/api/persons/{first_person_qid}/connections")
    assert r.status_code == 200
    body = r.json()
    assert "person" in body
    assert "connections" in body
    assert "count" in body
    assert isinstance(body["connections"], list)
    assert body["count"] == len(body["connections"])


def test_get_connections_not_found(client: TestClient):
    r = client.get("/api/persons/Q999999999999/connections")
    assert r.status_code == 404
