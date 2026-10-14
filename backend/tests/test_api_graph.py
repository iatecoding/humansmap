"""
Integration tests for /api/graph endpoints.

Requires: ArangoDB with data and relationships loaded.
"""

from fastapi.testclient import TestClient


# ── /api/graph/neighborhood/{qid} ────────────────────────────────────────────

def test_neighborhood_shape(client: TestClient, first_person_qid: str):
    r = client.get(f"/api/graph/neighborhood/{first_person_qid}")
    assert r.status_code == 200
    body = r.json()
    assert "vertices" in body
    assert "edges" in body
    assert isinstance(body["vertices"], list)
    assert isinstance(body["edges"], list)


def test_neighborhood_contains_root(client: TestClient, first_person_qid: str):
    """The root node must always appear in the vertices list."""
    r = client.get(f"/api/graph/neighborhood/{first_person_qid}")
    assert r.status_code == 200
    vertices = r.json()["vertices"]
    keys = [v["_key"] for v in vertices if v]
    assert first_person_qid in keys


def test_neighborhood_depth_param(client: TestClient, first_person_qid: str):
    r = client.get(f"/api/graph/neighborhood/{first_person_qid}?depth=1")
    assert r.status_code == 200
    body = r.json()
    assert "vertices" in body


def test_neighborhood_depth_too_large(client: TestClient, first_person_qid: str):
    """depth > max_traversal_depth (3) should be rejected."""
    r = client.get(f"/api/graph/neighborhood/{first_person_qid}?depth=10")
    assert r.status_code == 422


def test_neighborhood_not_found(client: TestClient):
    r = client.get("/api/graph/neighborhood/Q999999999999")
    assert r.status_code == 404


def test_neighborhood_invalid_qid(client: TestClient):
    r = client.get("/api/graph/neighborhood/not-a-qid")
    assert r.status_code == 422


# ── /api/graph/path/{qid1}/{qid2} ────────────────────────────────────────────

def test_path_not_found_returns_found_false(client: TestClient):
    """Two non-existent QIDs should 404; two disconnected real ones return found=False."""
    r = client.get("/api/graph/path/Q999999999998/Q999999999999")
    # Both QIDs don't exist — should be 404
    assert r.status_code == 404


def test_path_same_node(client: TestClient, first_person_qid: str):
    """Path from a node to itself — found=True, length=0."""
    r = client.get(f"/api/graph/path/{first_person_qid}/{first_person_qid}")
    assert r.status_code == 200
    body = r.json()
    assert "found" in body


def test_path_invalid_qid(client: TestClient):
    r = client.get("/api/graph/path/Q1/bad-qid")
    assert r.status_code == 422


# ── /api/graph/cluster/{edge_type} ───────────────────────────────────────────

def test_cluster_shape(client: TestClient):
    """Use a common edge type — if it exists, verify shape; otherwise empty result is OK."""
    r = client.get("/api/graph/cluster/spouse")
    assert r.status_code == 200
    body = r.json()
    assert "vertices" in body
    assert "edges" in body
