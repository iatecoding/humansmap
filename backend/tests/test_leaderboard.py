"""
Integration tests for /api/leaderboard endpoints.

Tests cover:
  - offset=0 (default) and offset=500 (deep page) behaviour
  - has_more heuristic (true when len(items) == limit)
  - cache key isolation between different offsets
  - response shape for persons and orgs
  - limit cap (le=500) and offset cap (le=5000)

Run inside the backend container:
    docker compose exec backend sh -c "pip install -r requirements-dev.txt && pytest tests/test_leaderboard.py"
"""

from fastapi.testclient import TestClient


# ── /api/leaderboard/degree ────────────────────────────────────────────────────

def test_degree_leaderboard_shape(client: TestClient):
    r = client.get("/api/leaderboard/degree?limit=5")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert "offset" in body
    assert "has_more" in body
    assert "limit" in body
    assert isinstance(body["items"], list)
    assert isinstance(body["has_more"], bool)
    assert body["offset"] == 0
    assert body["limit"] == 5


def test_degree_leaderboard_item_fields(client: TestClient):
    r = client.get("/api/leaderboard/degree?limit=3")
    assert r.status_code == 200
    items = r.json()["items"]
    if not items:
        return  # empty DB is valid in CI; just don't crash
    item = items[0]
    assert "qid" in item
    assert "name_en" in item
    assert "name_it" in item  # keep name_it — used by App.vue onPersonSelect
    assert "degree" in item
    assert "has_image" in item
    assert isinstance(item["has_image"], bool)


def test_degree_leaderboard_has_more_heuristic(client: TestClient):
    # When limit equals returned count, has_more must be True
    r = client.get("/api/leaderboard/degree?limit=2")
    assert r.status_code == 200
    body = r.json()
    if len(body["items"]) == 2:
        assert body["has_more"] is True
    else:
        # Fewer items than limit → end of data
        assert body["has_more"] is False


def test_degree_leaderboard_offset_zero_vs_500(client: TestClient):
    r0 = client.get("/api/leaderboard/degree?limit=5&offset=0")
    r500 = client.get("/api/leaderboard/degree?limit=5&offset=500")
    assert r0.status_code == 200
    assert r500.status_code == 200
    body0 = r0.json()
    body500 = r500.json()
    assert body0["offset"] == 0
    assert body500["offset"] == 500
    # offset=0 and offset=500 must be distinct cache entries — different items (or empty)
    qids0 = {item["qid"] for item in body0["items"]}
    qids500 = {item["qid"] for item in body500["items"]}
    # No overlap expected (different pages); if both are non-empty they must be disjoint
    if qids0 and qids500:
        assert qids0.isdisjoint(qids500), "Offset pages must not overlap"


def test_degree_leaderboard_limit_cap(client: TestClient):
    # limit > 500 must be rejected
    r = client.get("/api/leaderboard/degree?limit=501")
    assert r.status_code == 422


def test_degree_leaderboard_offset_cap(client: TestClient):
    # offset > 5000 must be rejected
    r = client.get("/api/leaderboard/degree?offset=5001")
    assert r.status_code == 422


def test_degree_leaderboard_default_limit(client: TestClient):
    r = client.get("/api/leaderboard/degree")
    assert r.status_code == 200
    body = r.json()
    assert body["limit"] == 60  # default changed from 20 to 60


def test_degree_leaderboard_status_filter(client: TestClient):
    r = client.get("/api/leaderboard/degree?status=deceased&limit=5")
    assert r.status_code == 200
    assert "items" in r.json()


def test_degree_leaderboard_invalid_status(client: TestClient):
    r = client.get("/api/leaderboard/degree?status=zombie")
    assert r.status_code == 422


# ── /api/leaderboard/degree?sort_by ─────────────────────────────────────────────

def test_degree_leaderboard_birth_date_sort(client: TestClient):
    r = client.get("/api/leaderboard/degree?sort_by=birth_date&limit=10")
    assert r.status_code == 200
    items = r.json()["items"]
    # Every ranked person has a known birth date and the page is descending.
    dates = [item["birth_date"] for item in items]
    assert all(dates)
    assert dates == sorted(dates, reverse=True)


def test_degree_leaderboard_birth_date_sort_with_filters(client: TestClient):
    r = client.get(
        "/api/leaderboard/degree?sort_by=birth_date&status=deceased&limit=10"
    )
    assert r.status_code == 200
    dates = [item["birth_date"] for item in r.json()["items"]]
    assert all(dates)
    assert dates == sorted(dates, reverse=True)


def test_degree_leaderboard_nationality_sort_not_exposed(client: TestClient):
    # Removed: sorting on nationalities[0] cannot use the array index.
    r = client.get("/api/leaderboard/degree?sort_by=nationality")
    assert r.status_code == 422


def test_degree_leaderboard_unknown_sort_rejected(client: TestClient):
    r = client.get("/api/leaderboard/degree?sort_by=bogus")
    assert r.status_code == 422


# ── /api/leaderboard/orgs ─────────────────────────────────────────────────────

def test_orgs_leaderboard_shape(client: TestClient):
    r = client.get("/api/leaderboard/orgs?limit=5")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert "offset" in body
    assert "has_more" in body
    assert "limit" in body
    assert isinstance(body["items"], list)
    assert isinstance(body["has_more"], bool)
    assert body["offset"] == 0
    assert body["limit"] == 5


def test_orgs_leaderboard_item_fields(client: TestClient):
    r = client.get("/api/leaderboard/orgs?limit=3")
    assert r.status_code == 200
    items = r.json()["items"]
    if not items:
        return
    item = items[0]
    assert "qid" in item
    assert "name_en" in item
    assert "member_count" in item
    assert "has_image" in item


def test_orgs_leaderboard_offset_pagination(client: TestClient):
    r0 = client.get("/api/leaderboard/orgs?limit=5&offset=0")
    r5 = client.get("/api/leaderboard/orgs?limit=5&offset=5")
    assert r0.status_code == 200
    assert r5.status_code == 200
    assert r0.json()["offset"] == 0
    assert r5.json()["offset"] == 5
    qids0 = {item["qid"] for item in r0.json()["items"]}
    qids5 = {item["qid"] for item in r5.json()["items"]}
    if qids0 and qids5:
        assert qids0.isdisjoint(qids5), "Offset pages must not overlap"


def test_orgs_leaderboard_limit_cap(client: TestClient):
    r = client.get("/api/leaderboard/orgs?limit=501")
    assert r.status_code == 422


def test_orgs_leaderboard_occupations_category(client: TestClient):
    r = client.get("/api/leaderboard/orgs?category=occupations&limit=5")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    # occupation items have type == "occupation"
    for item in body["items"]:
        assert item.get("type") == "occupation"


def test_orgs_leaderboard_business_category(client: TestClient):
    r = client.get("/api/leaderboard/orgs?category=business&limit=5")
    assert r.status_code == 200
    assert "items" in r.json()


def test_orgs_leaderboard_founded_sort(client: TestClient):
    r = client.get("/api/leaderboard/orgs?sort_by=founded&limit=10")
    assert r.status_code == 200
    founded = [i["founded"] for i in r.json()["items"] if i.get("founded")]
    assert founded == sorted(founded, reverse=True)


def test_orgs_leaderboard_unknown_sort_rejected(client: TestClient):
    r = client.get("/api/leaderboard/orgs?sort_by=bogus")
    assert r.status_code == 422


def test_orgs_leaderboard_occupations_ignores_sort(client: TestClient):
    # Occupation aggregates are always ranked by member count; the sort value is
    # accepted but normalised so there is one cache entry per page.
    r = client.get("/api/leaderboard/orgs?category=occupations&sort_by=founded&limit=5")
    assert r.status_code == 200
    for item in r.json()["items"]:
        assert item.get("type") == "occupation"


# ── Cache key isolation ────────────────────────────────────────────────────────

def test_cache_key_differs_by_offset(client: TestClient):
    """Two calls with different offsets must return distinct offset values in body,
    confirming they hit separate cache entries."""
    r1 = client.get("/api/leaderboard/degree?limit=10&offset=0")
    r2 = client.get("/api/leaderboard/degree?limit=10&offset=10")
    assert r1.json()["offset"] == 0
    assert r2.json()["offset"] == 10


def test_cache_key_differs_by_limit(client: TestClient):
    r1 = client.get("/api/leaderboard/degree?limit=5&offset=0")
    r2 = client.get("/api/leaderboard/degree?limit=10&offset=0")
    assert r1.json()["limit"] == 5
    assert r2.json()["limit"] == 10
