"""
Integration tests for /api/trivia endpoints.

Requires: ArangoDB with a reasonably populated dataset (≥20 persons per nationality).

Round response shape:
  {
    "questions": [...],   # list of question objects
    "round_id": "...",    # UUID
    "filters": {...}      # applied filters
  }
"""

from fastapi.testclient import TestClient


# ── /api/trivia/nationalities ─────────────────────────────────────────────────

def test_nationalities_shape(client: TestClient):
    r = client.get("/api/trivia/nationalities")
    assert r.status_code == 200
    body = r.json()
    assert "nationalities" in body
    assert isinstance(body["nationalities"], list)


def test_nationalities_are_strings(client: TestClient):
    r = client.get("/api/trivia/nationalities")
    assert r.status_code == 200
    for nat in r.json()["nationalities"]:
        assert isinstance(nat, str)
        assert len(nat) > 0


# ── /api/trivia/round ─────────────────────────────────────────────────────────

def test_round_response_shape(client: TestClient):
    r = client.get("/api/trivia/round")
    assert r.status_code == 200
    body = r.json()
    assert "questions" in body
    assert "round_id" in body
    assert "filters" in body
    assert isinstance(body["questions"], list)
    assert len(body["questions"]) > 0


def test_round_question_has_type(client: TestClient):
    r = client.get("/api/trivia/round")
    assert r.status_code == 200
    questions = r.json()["questions"]
    for q in questions:
        assert "type" in q, f"question missing 'type': {list(q.keys())}"


def test_round_category_filter(client: TestClient):
    """Valid category should return 200 with same shape."""
    r = client.get("/api/trivia/round?category=sport")
    assert r.status_code == 200
    body = r.json()
    assert "questions" in body
    assert "round_id" in body


def test_round_invalid_difficulty_falls_back(client: TestClient):
    """Invalid difficulty is silently ignored — should still return 200."""
    r = client.get("/api/trivia/round?difficulty=impossible")
    assert r.status_code == 200
    assert "questions" in r.json()


def test_round_difficulty_easy(client: TestClient):
    r = client.get("/api/trivia/round?difficulty=easy")
    assert r.status_code == 200
    assert "questions" in r.json()


def test_round_difficulty_hard(client: TestClient):
    r = client.get("/api/trivia/round?difficulty=hard")
    assert r.status_code == 200
    assert "questions" in r.json()
