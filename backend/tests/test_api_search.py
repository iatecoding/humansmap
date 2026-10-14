"""
Integration tests for /api/search.

Requires: ArangoDB with ArangoSearch view and data loaded.
"""

from fastapi.testclient import TestClient


def test_search_returns_shape(client: TestClient):
    r = client.get("/api/search?q=mario")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert "count" in body
    assert "query" in body
    assert body["query"] == "mario"
    assert isinstance(body["items"], list)
    assert body["count"] == len(body["items"])


def test_search_item_has_required_fields(client: TestClient):
    r = client.get("/api/search?q=mario")
    assert r.status_code == 200
    items = r.json()["items"]
    for item in items[:3]:  # spot-check first few results
        assert "_key" in item or "id" in item
        assert "name_en" in item or "name_it" in item


def test_search_limit_respected(client: TestClient):
    r = client.get("/api/search?q=mario&limit=5")
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) <= 5


def test_search_limit_max(client: TestClient):
    """limit=101 exceeds the API max of 100 — should be rejected."""
    r = client.get("/api/search?q=mario&limit=101")
    assert r.status_code == 422


def test_search_single_character_rejected(client: TestClient):
    r = client.get("/api/search?q=a")
    assert r.status_code == 422


def test_search_whitespace_only_rejected(client: TestClient):
    r = client.get("/api/search?q=%20%20")
    assert r.status_code == 422


def test_search_empty_query_rejected(client: TestClient):
    """min_length=2 — empty q should be rejected."""
    r = client.get("/api/search?q=")
    assert r.status_code == 422


def test_search_missing_query_rejected(client: TestClient):
    r = client.get("/api/search")
    assert r.status_code == 422
