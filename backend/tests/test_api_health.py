"""
Integration tests for /api/health.

No database required — tests the endpoint in isolation.
"""

from fastapi.testclient import TestClient

from app.release import APP_BUILD_SHA


def test_health_returns_ok(client: TestClient):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["build"] == APP_BUILD_SHA
    assert r.headers["x-humansmap-build"] == APP_BUILD_SHA


def test_health_content_type(client: TestClient):
    r = client.get("/api/health")
    assert "application/json" in r.headers["content-type"]
