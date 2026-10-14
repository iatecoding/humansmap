"""
Tests for the HumansMap connections feed pipeline and /api/feed endpoint.

Coverage:
    - Scorer threshold behaviour (relationship, dynasty, org)
    - Dynasty surname-coherence filter (§3.2)
    - Endpoint pagination, cursor arithmetic, 503-when-empty
    - Phase 13Q §B4: category param, soft-boost serendipity, missing-card
      resilience, empty/unknown categories, 503-when-meta-absent

Run inside the backend container:
    docker compose exec backend sh -c \
        "pip install -r requirements-dev.txt && pytest tests/test_feed.py -v"
"""

from __future__ import annotations

import json
import math
import unittest.mock as mock
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.domain.feed_contract import FEED_CONTENT_VERSION


# ===========================================================================
# Helpers that don't need a live DB
# ===========================================================================


# ---------------------------------------------------------------------------
# Relationship scorer
# ---------------------------------------------------------------------------

class TestRelationshipScorer:
    """Unit tests for _score_relationship."""

    def _make_edge(
        self,
        stmts_a: int = 300,
        stmts_b: int = 300,
        edge_type: str = "spouse",
        occ_a: list[str] | None = None,
        occ_b: list[str] | None = None,
    ) -> dict:
        return {
            "type": edge_type,
            "start_date": None,
            "end_date": None,
            "a": {
                "qid": "Q1",
                "name": "Alice",
                "desc": "An actor",
                "occupations": occ_a or ["actor"],
                "primary_occ": (occ_a or ["actor"])[0],
                "notable_works": [],
                "birth_date": None,
                "death_date": None,
                "stmts": stmts_a,
            },
            "b": {
                "qid": "Q2",
                "name": "Bob",
                "desc": "A politician",
                "occupations": occ_b or ["politician"],
                "primary_occ": (occ_b or ["politician"])[0],
                "notable_works": [],
                "birth_date": None,
                "death_date": None,
                "stmts": stmts_b,
            },
        }

    def test_score_is_between_zero_and_one(self):
        from scripts.feed.generate_feed import _score_relationship
        edge = self._make_edge()
        score = _score_relationship(edge)
        assert 0.0 <= score <= 1.0

    def test_cross_domain_scores_higher_than_same_domain(self):
        from scripts.feed.generate_feed import _score_relationship
        cross = self._make_edge(occ_a=["actor"], occ_b=["politician"])
        same = self._make_edge(occ_a=["actor"], occ_b=["film_actor"])
        assert _score_relationship(cross) > _score_relationship(same)

    def test_married_scores_higher_than_collaborators(self):
        from scripts.feed.generate_feed import _score_relationship
        married = self._make_edge(edge_type="spouse")
        collab = self._make_edge(edge_type="professional_collaborator")
        assert _score_relationship(married) > _score_relationship(collab)

    def test_high_notability_scores_higher(self):
        from scripts.feed.generate_feed import _score_relationship
        notable = self._make_edge(stmts_a=5000, stmts_b=5000)
        obscure = self._make_edge(stmts_a=121, stmts_b=121)
        assert _score_relationship(notable) > _score_relationship(obscure)

    def test_symmetric_pair_consistent_score(self):
        """Swapping a and b should give the same score (symmetric)."""
        from scripts.feed.generate_feed import _score_relationship
        edge = self._make_edge(stmts_a=400, stmts_b=600)
        edge_flipped = dict(edge, a=edge["b"], b=edge["a"])
        # Scores may differ slightly due to fame_asymmetry being symmetric (min/max)
        # — they should be equal since min and max are the same regardless of order.
        assert abs(_score_relationship(edge) - _score_relationship(edge_flipped)) < 1e-9


# ---------------------------------------------------------------------------
# Dynasty surname-coherence filter
# ---------------------------------------------------------------------------

class TestDynastyCoherence:
    """Unit tests for the surname-coherence rejection logic."""

    def _make_member(
        self, qid: str, name: str, family_name: str | None, stmts: int = 200
    ) -> dict:
        return {
            "qid":        qid,
            "name":       name,
            "desc":       "A musician",
            "primary_occ": "musician",
            "occupations": ["musician"],
            "birth_date": None,
            "death_date": None,
            "stmts":      stmts,
            "family_name": family_name,
        }

    def test_junk_surname_of_prefix_is_rejected(self):
        from scripts.feed.generate_feed import _is_junk_surname
        assert _is_junk_surname("of England") is True

    def test_junk_surname_empty_is_rejected(self):
        from scripts.feed.generate_feed import _is_junk_surname
        assert _is_junk_surname("") is True

    def test_junk_surname_none_is_rejected(self):
        from scripts.feed.generate_feed import _is_junk_surname
        assert _is_junk_surname(None) is True  # type: ignore[arg-type]

    def test_valid_surname_not_rejected(self):
        from scripts.feed.generate_feed import _is_junk_surname
        assert _is_junk_surname("Bach") is False
        assert _is_junk_surname("Kennedy") is False
        assert _is_junk_surname("Grimm") is False

    def test_modal_family_name_returns_most_common(self):
        from scripts.feed.generate_feed import _modal_family_name
        members = [
            self._make_member("Q1", "Johann Bach", "Bach"),
            self._make_member("Q2", "Carl Bach", "Bach"),
            self._make_member("Q3", "Anne Other", "Other"),
        ]
        assert _modal_family_name(members) == "Bach"

    def test_modal_family_name_ignores_null(self):
        from scripts.feed.generate_feed import _modal_family_name
        members = [
            self._make_member("Q1", "Alice Bach", "Bach"),
            self._make_member("Q2", "Bob Unknown", None),
            self._make_member("Q3", "Carol Bach", "Bach"),
        ]
        assert _modal_family_name(members) == "Bach"

    def test_modal_family_name_none_when_all_null(self):
        from scripts.feed.generate_feed import _modal_family_name
        members = [
            self._make_member("Q1", "Alice X", None),
            self._make_member("Q2", "Bob X", None),
        ]
        assert _modal_family_name(members) is None

    def test_dynasty_score_increases_with_member_count(self):
        from scripts.feed.generate_feed import _score_dynasty
        small = [self._make_member(f"Q{i}", "Bach", "Bach") for i in range(3)]
        large = [self._make_member(f"Q{i}", "Bach", "Bach") for i in range(10)]
        depths_small = {m["qid"]: 0 for m in small}
        depths_large = {m["qid"]: i % 3 for i, m in enumerate(large)}
        assert _score_dynasty(large, depths_large) > _score_dynasty(small, depths_small)

    def test_dynasty_score_between_zero_and_one(self):
        from scripts.feed.generate_feed import _score_dynasty
        members = [self._make_member(f"Q{i}", "Bach", "Bach", stmts=300) for i in range(5)]
        depths = {m["qid"]: i % 3 for i, m in enumerate(members)}
        score = _score_dynasty(members, depths)
        assert 0.0 <= score <= 1.0


# ---------------------------------------------------------------------------
# Organisation scorer
# ---------------------------------------------------------------------------

class TestOrgScorer:
    """Unit tests for _score_org_card."""

    def _make_people(self, n: int, stmts: int = 500) -> list[dict]:
        return [
            {"person_qid": f"Q{i}", "person_name": f"Person {i}",
             "person_desc": "CEO", "person_stmts": stmts}
            for i in range(n)
        ]

    def test_score_between_zero_and_one(self):
        from scripts.feed.generate_feed import _score_org_card
        people = self._make_people(3)
        score = _score_org_card(people, org_stmts=5000, total_affiliates=5, relation_key="founded_by")
        assert 0.0 <= score <= 1.0

    def test_founder_beats_members(self):
        from scripts.feed.generate_feed import _score_org_card
        people = self._make_people(3, stmts=500)
        founded = _score_org_card(people, 5000, 3, "founded_by")
        members = _score_org_card(people, 5000, 3, "members")
        assert founded > members

    def test_empty_people_returns_zero(self):
        from scripts.feed.generate_feed import _score_org_card
        assert _score_org_card([], 5000, 0, "founded_by") == 0.0

    def test_mega_hub_specificity_penalty(self):
        """Org with 200 affiliates should score lower on specificity than one with 3."""
        from scripts.feed.generate_feed import _score_org_card
        people = self._make_people(3)
        specific = _score_org_card(people, 5000, 3,   "board")
        mega     = _score_org_card(people, 5000, 200, "board")
        assert specific > mega


# ---------------------------------------------------------------------------
# Domain label helper
# ---------------------------------------------------------------------------

class TestDomainLabel:
    """Unit tests for domain_label() in domains.py."""

    def test_returns_label_for_known_occupation(self):
        from scripts.feed.domains import domain_label
        assert domain_label("singer") == "Music"
        assert domain_label("politician") == "Politics"
        assert domain_label("film_actor") == "Film"

    def test_returns_none_for_unknown(self):
        from scripts.feed.domains import domain_label
        assert domain_label("underwater_welder") is None

    def test_accepts_list(self):
        from scripts.feed.domains import domain_label
        assert domain_label(["underwater_welder", "composer"]) == "Music"

    def test_returns_none_for_empty_list(self):
        from scripts.feed.domains import domain_label
        assert domain_label([]) is None

    def test_returns_none_for_none(self):
        from scripts.feed.domains import domain_label
        assert domain_label(None) is None


# ---------------------------------------------------------------------------
# Interleave helper
# ---------------------------------------------------------------------------

class TestInterleaveCards:
    """Verify that _interleave_cards mixes card types."""

    def _make_cards(self, card_type: str, n: int) -> list[dict]:
        return [{"id": f"{card_type}_{i}", "type": card_type, "score": 1.0} for i in range(n)]

    def test_all_types_present_in_output(self):
        from scripts.feed.generate_feed import _interleave_cards
        rels = self._make_cards("relationship", 10)
        dyns = self._make_cards("dynasty", 5)
        orgs = self._make_cards("organization", 5)
        result = _interleave_cards(rels, dyns, orgs)
        types_seen = {c["type"] for c in result}
        assert "relationship" in types_seen
        assert "dynasty" in types_seen
        assert "organization" in types_seen

    def test_total_count_preserved(self):
        from scripts.feed.generate_feed import _interleave_cards
        rels = self._make_cards("relationship", 6)
        dyns = self._make_cards("dynasty", 4)
        orgs = self._make_cards("organization", 4)
        result = _interleave_cards(rels, dyns, orgs)
        assert len(result) == 14

    def test_no_long_runs_of_same_type(self):
        """No more than 3 consecutive cards of the same type."""
        from scripts.feed.generate_feed import _interleave_cards
        rels = self._make_cards("relationship", 20)
        dyns = self._make_cards("dynasty", 10)
        orgs = self._make_cards("organization", 10)
        result = _interleave_cards(rels, dyns, orgs)

        max_run = 0
        run = 1
        for i in range(1, len(result)):
            if result[i]["type"] == result[i - 1]["type"]:
                run += 1
            else:
                run = 1
            max_run = max(max_run, run)

        assert max_run <= 3, f"Max same-type run was {max_run} (expected ≤ 3)"


# ---------------------------------------------------------------------------
# Day-seeded shuffle
# ---------------------------------------------------------------------------

class TestDaySeededShuffle:
    """Tests for _shuffled_cards."""

    def _make_cards(self, n: int) -> list[dict]:
        return [{"id": str(i), "type": "relationship", "score": float(n - i)} for i in range(n)]

    def test_card_count_preserved(self):
        """_shuffled_cards returns the same number of cards as input."""
        from app.routers.feed import _shuffled_cards
        cards = self._make_cards(40)
        shuffled = _shuffled_cards(cards)
        assert len(shuffled) == 40

    def test_same_day_same_order(self):
        """Two calls on the same day produce the same order."""
        from app.routers.feed import _shuffled_cards
        cards = self._make_cards(40)
        first = _shuffled_cards(cards)
        second = _shuffled_cards(cards)
        assert [c["id"] for c in first] == [c["id"] for c in second]

    def test_empty_input(self):
        from app.routers.feed import _shuffled_cards
        assert _shuffled_cards([]) == []


# ===========================================================================
# Endpoint integration tests  (require live Redis + app running)
# ===========================================================================


def _make_v2_redis_mock(cards: list[dict]) -> MagicMock:
    """Build a MagicMock Redis client pre-loaded with v2-layout feed data.

    Sets up:
      feed:meta:v2            — meta JSON
      feed:order:all:v2       — JSON array of all IDs
      feed:order:cat:<c>:v2   — JSON array of IDs per category
      feed:cardmap:v2         — HASH: id → JSON body (via hmget)
    """
    import json as _json
    from app.routers.feed import FEED_META_KEY, FEED_ORDER_ALL_KEY, FEED_ORDER_CAT_PREFIX

    # Build category ID lists
    cat_ids: dict[str, list[str]] = {}
    for c in cards:
        for cat in c.get("categories") or []:
            cat_ids.setdefault(cat, []).append(c["id"])

    meta = {
        "count": len(cards),
        "generated_at": "2026-01-01T00:00:00+00:00",
        "content_version": FEED_CONTENT_VERSION,
        "by_type": {},
        "by_category": {cat: len(ids) for cat, ids in cat_ids.items()},
        "categories": ["politics", "business", "sport", "science_education", "arts"],
    }

    all_ids = [c["id"] for c in cards]
    card_map = {c["id"]: _json.dumps(c, separators=(",", ":")) for c in cards}

    def _get(key: str) -> str | None:
        if key == FEED_META_KEY:
            return _json.dumps(meta)
        if key == FEED_ORDER_ALL_KEY:
            return _json.dumps(all_ids)
        for cat, ids in cat_ids.items():
            if key == f"{FEED_ORDER_CAT_PREFIX}{cat}:v2":
                return _json.dumps(ids)
        return None

    def _hmget(hash_key: str, fields: list[str]) -> list[str | None]:
        return [card_map.get(f) for f in fields]

    mock_r = MagicMock()
    mock_r.get.side_effect = _get
    mock_r.hmget.side_effect = _hmget
    return mock_r


class TestFeedEndpoint:
    """Integration tests for GET /api/feed (v2 layout).

    These require the FastAPI app to be available (run in-container).
    Redis is mocked so tests can run without a populated feed.
    """

    def test_missing_feed_recovers_before_serving(self, client: TestClient):
        """A Redis flush rebuilds the pool instead of stranding the feed at 503."""
        missing_redis = MagicMock()
        missing_redis.get.return_value = None
        cards = [{"id": "1", "type": "relationship", "score": 1.0, "categories": []}]
        ready_redis = _make_v2_redis_mock(cards)

        with patch("app.routers.feed.get_redis", side_effect=[missing_redis, ready_redis]), \
             patch("app.routers.feed.ensure_feed_ready", return_value=True) as rebuild, \
             patch("app.routers.feed._id_array_cache", {}):
            response = client.get("/api/feed")

        assert response.status_code == 200
        assert response.json()["cards"] == cards
        rebuild.assert_called_once()

    def test_503_when_redis_unavailable(self, client: TestClient):
        """Endpoint must 503 (not 500) when Redis raises an exception."""
        with patch("app.routers.feed.get_redis") as mock_get_redis:
            mock_get_redis.side_effect = Exception("connection refused")

            r = client.get("/api/feed")
            assert r.status_code == 503

    def test_pagination_with_mock_data(self, client: TestClient):
        """With mock data in Redis, cursor and next_cursor must be correct."""
        fake_cards = [
            {"id": str(i), "type": "relationship", "score": 1.0, "categories": []}
            for i in range(50)
        ]

        with patch("app.routers.feed.get_redis") as mock_get_redis:
            mock_get_redis.return_value = _make_v2_redis_mock(fake_cards)

            # First page
            r = client.get("/api/feed?cursor=0&limit=10")
            assert r.status_code == 200
            body = r.json()
            assert "cards" in body
            assert "next_cursor" in body
            assert "total" in body
            assert body["total"] == 50
            assert len(body["cards"]) == 10
            assert body["next_cursor"] == 10

    def test_last_page_has_null_next_cursor(self, client: TestClient):
        """next_cursor must be null when the page reaches the end."""
        fake_cards = [
            {"id": str(i), "type": "relationship", "score": 1.0, "categories": []}
            for i in range(15)
        ]

        with patch("app.routers.feed.get_redis") as mock_get_redis:
            mock_get_redis.return_value = _make_v2_redis_mock(fake_cards)

            r = client.get("/api/feed?cursor=10&limit=10")
            assert r.status_code == 200
            body = r.json()
            assert len(body["cards"]) == 5
            assert body["next_cursor"] is None

    def test_limit_capped_at_max(self, client: TestClient):
        """limit > MAX_LIMIT must be rejected with 422."""
        r = client.get("/api/feed?limit=200")
        assert r.status_code == 422

    def test_cursor_past_end_returns_empty_page(self, client: TestClient):
        """A cursor beyond the total returns an empty cards list, null next_cursor."""
        fake_cards = [
            {"id": str(i), "type": "relationship", "score": 1.0, "categories": []}
            for i in range(5)
        ]

        with patch("app.routers.feed.get_redis") as mock_get_redis:
            mock_get_redis.return_value = _make_v2_redis_mock(fake_cards)

            r = client.get("/api/feed?cursor=100&limit=10")
            assert r.status_code == 200
            body = r.json()
            assert body["cards"] == []
            assert body["next_cursor"] is None
            assert body["total"] == 5

    def test_lang_param_accepted(self, client: TestClient):
        """lang param must not cause errors (no-op for now)."""
        fake_cards = [{"id": "1", "type": "relationship", "score": 1.0, "categories": []}]

        with patch("app.routers.feed.get_redis") as mock_get_redis:
            mock_get_redis.return_value = _make_v2_redis_mock(fake_cards)

            r = client.get("/api/feed?lang=fr&limit=1")
            assert r.status_code == 200

    def test_response_shape(self, client: TestClient):
        """Response always has cards, next_cursor, total keys."""
        fake_cards = [{"id": "x", "type": "dynasty", "score": 0.9, "categories": ["arts"]}]

        with patch("app.routers.feed.get_redis") as mock_get_redis:
            mock_get_redis.return_value = _make_v2_redis_mock(fake_cards)

            r = client.get("/api/feed")
            assert r.status_code == 200
            body = r.json()
            assert set(body.keys()) == {"cards", "next_cursor", "total"}
            assert isinstance(body["cards"], list)
            assert isinstance(body["total"], int)


class TestFeedStorage:
    def test_generated_feed_keys_do_not_expire(self):
        """The durable feed pool must outlive ordinary Redis cache TTLs."""
        from scripts.feed.generate_feed import _write_to_redis

        redis_client = MagicMock()
        pipeline = MagicMock()
        redis_client.pipeline.return_value = pipeline
        card = {
            "id": "card-1",
            "type": "relationship",
            "categories": ["arts"],
            "countries": ["IT"],
        }

        with patch("scripts.feed.generate_feed.get_redis", return_value=redis_client):
            _write_to_redis([card], {"relationship": 1})

        pipeline.expire.assert_not_called()
        assert all("ex" not in call.kwargs for call in pipeline.set.call_args_list)


# ---------------------------------------------------------------------------
# §B4 — Phase 13Q new tests: categories, soft-boost, resilience
# ---------------------------------------------------------------------------


class TestCategoryTaxonomy:
    """Unit tests for Phase 13Q taxonomy helpers in domains.py."""

    def test_categories_for_domains_politician(self):
        from scripts.feed.domains import categories_for_domains
        result = categories_for_domains(["politician"])
        assert "politics" in result

    def test_categories_for_domains_musician(self):
        from scripts.feed.domains import categories_for_domains
        result = categories_for_domains(["singer"])
        assert "arts" in result

    def test_categories_for_domains_scientist(self):
        from scripts.feed.domains import categories_for_domains
        result = categories_for_domains(["physicist"])
        assert "science_education" in result

    def test_categories_for_domains_athlete(self):
        from scripts.feed.domains import categories_for_domains
        result = categories_for_domains(["football_player"])
        assert "sport" in result

    def test_categories_for_domains_businessperson(self):
        from scripts.feed.domains import categories_for_domains
        result = categories_for_domains(["entrepreneur"])
        assert "business" in result

    def test_categories_for_domains_military_unmapped(self):
        """military_religion_other domain should produce no category."""
        from scripts.feed.domains import categories_for_domains
        result = categories_for_domains(["military_person"])
        assert result == []

    def test_categories_for_domains_multiple_lists(self):
        """Union across multiple person occupation lists."""
        from scripts.feed.domains import categories_for_domains
        result = categories_for_domains(["politician"], ["singer"])
        assert "politics" in result
        assert "arts" in result

    def test_categories_for_domains_deduped(self):
        """Same category from two lists appears only once."""
        from scripts.feed.domains import categories_for_domains
        result = categories_for_domains(["politician"], ["diplomat"])
        assert result.count("politics") == 1

    def test_categories_for_domains_deterministic_order(self):
        """Order always follows CATEGORY_ORDER regardless of input order."""
        from scripts.feed.domains import categories_for_domains, CATEGORY_ORDER
        result = categories_for_domains(["singer"], ["politician"])
        # politics comes before arts in CATEGORY_ORDER
        politics_idx = CATEGORY_ORDER.index("politics")
        arts_idx = CATEGORY_ORDER.index("arts")
        assert politics_idx < arts_idx
        assert result.index("politics") < result.index("arts")

    def test_categories_for_domains_unknown_occupation(self):
        from scripts.feed.domains import categories_for_domains
        result = categories_for_domains(["underwater_basket_weaver"])
        assert result == []

    def test_categories_for_domains_none_list(self):
        from scripts.feed.domains import categories_for_domains
        result = categories_for_domains(None, ["politician"])
        assert "politics" in result

    def test_categories_for_org_type_wins(self):
        """Org type is always included even if people have different domain."""
        from scripts.feed.domains import categories_for_org
        result = categories_for_org("university", [["politician"]])
        assert "science_education" in result
        # People's domain also present
        assert "politics" in result

    def test_categories_for_org_company(self):
        from scripts.feed.domains import categories_for_org
        result = categories_for_org("company")
        assert "business" in result

    def test_categories_for_org_sports_club(self):
        from scripts.feed.domains import categories_for_org
        result = categories_for_org("sports_club")
        assert "sport" in result

    def test_categories_for_org_political_party(self):
        from scripts.feed.domains import categories_for_org
        result = categories_for_org("political_party")
        assert "politics" in result

    def test_categories_for_org_band(self):
        from scripts.feed.domains import categories_for_org
        result = categories_for_org("band")
        assert "arts" in result

    def test_categories_for_org_unknown_type_no_crash(self):
        from scripts.feed.domains import categories_for_org
        result = categories_for_org("mystery_entity")
        assert isinstance(result, list)

    def test_categories_for_org_none_type(self):
        from scripts.feed.domains import categories_for_org
        result = categories_for_org(None, [["politician"]])
        assert "politics" in result


class TestFeedCategoryParam:
    """§B4 — Category filtering and soft-boost via /api/feed?categories=..."""

    def _make_mixed_cards(self) -> list[dict]:
        """30 cards: 10 politics, 10 sport, 10 arts."""
        cards = []
        for i in range(10):
            cards.append({
                "id": f"pol_{i}",
                "type": "relationship",
                "score": 0.9,
                "categories": ["politics"],
            })
        for i in range(10):
            cards.append({
                "id": f"spo_{i}",
                "type": "organization",
                "score": 0.8,
                "categories": ["sport"],
            })
        for i in range(10):
            cards.append({
                "id": f"art_{i}",
                "type": "dynasty",
                "score": 0.7,
                "categories": ["arts"],
            })
        return cards

    def test_category_filter_front_loads_selected(self, client: TestClient):
        """When categories=politics, politics cards appear before others in the page."""
        cards = self._make_mixed_cards()

        with patch("app.routers.feed.get_redis") as mock_get_redis:
            mock_get_redis.return_value = _make_v2_redis_mock(cards)
            # Patch _id_array_cache to avoid cross-test interference
            with patch("app.routers.feed._id_array_cache", {}):
                r = client.get("/api/feed?categories=politics&limit=30&cursor=0")
                assert r.status_code == 200
                body = r.json()
                returned_cards = body["cards"]
                # First several cards should be politics (or serendipity splices)
                politics_ids = {c["id"] for c in returned_cards if "politics" in c.get("categories", [])}
                # All 10 politics cards must be somewhere in the result
                assert len(politics_ids) == 10

    def test_unknown_category_silently_ignored(self, client: TestClient):
        """Unknown category ids must be ignored; valid ones still applied."""
        cards = self._make_mixed_cards()

        with patch("app.routers.feed.get_redis") as mock_get_redis:
            mock_get_redis.return_value = _make_v2_redis_mock(cards)
            with patch("app.routers.feed._id_array_cache", {}):
                r = client.get("/api/feed?categories=politics,totally_fake,sport")
                assert r.status_code == 200

    def test_empty_categories_falls_back_to_all(self, client: TestClient):
        """categories= (empty string) behaves same as no categories param."""
        cards = self._make_mixed_cards()

        with patch("app.routers.feed.get_redis") as mock_get_redis:
            mock_get_redis.return_value = _make_v2_redis_mock(cards)
            with patch("app.routers.feed._id_array_cache", {}):
                r = client.get("/api/feed?categories=&limit=30")
                assert r.status_code == 200
                body = r.json()
                assert body["total"] == 30

    def test_all_unknown_categories_falls_back_to_all(self, client: TestClient):
        """All-unknown categories param: falls back to full unfiltered feed."""
        cards = self._make_mixed_cards()

        with patch("app.routers.feed.get_redis") as mock_get_redis:
            mock_get_redis.return_value = _make_v2_redis_mock(cards)
            with patch("app.routers.feed._id_array_cache", {}):
                r = client.get("/api/feed?categories=xyzzy,foobar&limit=30")
                assert r.status_code == 200
                body = r.json()
                # Falls back to all — total should equal all cards
                assert body["total"] == 30

    def test_serendipity_interval(self):
        """_build_ordered_ids splices ~1 serendipity card every 6 from remainder."""
        from app.routers.feed import _build_ordered_ids, SERENDIPITY_INTERVAL

        # 30 politics + 30 unrelated
        pol_ids = [f"pol_{i}" for i in range(30)]
        oth_ids = [f"oth_{i}" for i in range(30)]
        all_ids = pol_ids + oth_ids

        cat_ids_map = {"politics": pol_ids}
        result = _build_ordered_ids(all_ids, cat_ids_map, ["politics"])

        # Count serendipity cards (non-politics) in the first 36 positions
        # With 30 politics cards and interval=6, expect ~5 serendipity splices
        first_36 = result[:36]
        serendipity_count = sum(1 for cid in first_36 if cid.startswith("oth_"))
        # Should be roughly 30 // 6 = 5 (allow ±2 for boundary effects)
        assert 3 <= serendipity_count <= 7, (
            f"Expected ~5 serendipity cards in first 36 positions, got {serendipity_count}"
        )

    def test_serendipity_deduplication(self):
        """No card id appears twice in _build_ordered_ids output."""
        from app.routers.feed import _build_ordered_ids

        pol_ids = [f"pol_{i}" for i in range(20)]
        oth_ids = [f"oth_{i}" for i in range(20)]
        all_ids = pol_ids + oth_ids

        cat_ids_map = {"politics": pol_ids}
        result = _build_ordered_ids(all_ids, cat_ids_map, ["politics"])

        assert len(result) == len(set(result)), "Duplicate IDs found in ordered output"

    def test_build_ordered_ids_tail_coverage(self):
        """All IDs from all_ids appear in the result (full tail coverage)."""
        from app.routers.feed import _build_ordered_ids

        pol_ids = [f"pol_{i}" for i in range(10)]
        oth_ids = [f"oth_{i}" for i in range(10)]
        all_ids = pol_ids + oth_ids

        cat_ids_map = {"politics": pol_ids}
        result = _build_ordered_ids(all_ids, cat_ids_map, ["politics"])

        assert set(result) == set(all_ids), "Some IDs missing from ordered output"


class TestFeedMissingCardResilience:
    """§B4 — Endpoint must gracefully skip cards whose body is missing from cardmap."""

    def test_missing_card_bodies_dropped_silently(self, client: TestClient):
        """HMGET returning None for some IDs should not crash the endpoint."""
        import json as _json
        from app.routers.feed import FEED_META_KEY, FEED_ORDER_ALL_KEY

        cards = [
            {"id": "good_1", "type": "relationship", "score": 0.9, "categories": []},
            {"id": "good_2", "type": "dynasty",      "score": 0.8, "categories": []},
        ]
        missing_id = "ghost_card"
        all_ids = ["good_1", missing_id, "good_2"]

        meta = {
            "count": 3,
            "generated_at": "2026-01-01T00:00:00+00:00",
            "content_version": FEED_CONTENT_VERSION,
            "by_type": {},
            "by_category": {},
            "categories": [],
        }
        card_map = {c["id"]: _json.dumps(c, separators=(",", ":")) for c in cards}

        def _get(key: str) -> str | None:
            if key == FEED_META_KEY:
                return _json.dumps(meta)
            if key == FEED_ORDER_ALL_KEY:
                return _json.dumps(all_ids)
            return None

        def _hmget(hash_key: str, fields: list[str]) -> list[str | None]:
            return [card_map.get(f) for f in fields]

        mock_r = MagicMock()
        mock_r.get.side_effect = _get
        mock_r.hmget.side_effect = _hmget

        with patch("app.routers.feed.get_redis") as mock_get_redis:
            mock_get_redis.return_value = mock_r
            with patch("app.routers.feed._id_array_cache", {}):
                r = client.get("/api/feed?cursor=0&limit=10")
                assert r.status_code == 200
                body = r.json()
                # Should return 2 good cards (ghost_card silently dropped)
                returned_ids = [c["id"] for c in body["cards"]]
                assert "ghost_card" not in returned_ids
                assert "good_1" in returned_ids
                assert "good_2" in returned_ids

    def test_missing_meta_attempts_recovery(self, client: TestClient):
        """A genuine rebuild failure still returns a clear 503."""
        with patch("app.routers.feed.get_redis") as mock_get_redis:
            mock_r = MagicMock()
            mock_r.get.return_value = None
            mock_get_redis.return_value = mock_r
            with patch("app.routers.feed._id_array_cache", {}), patch(
                "app.routers.feed.ensure_feed_ready", return_value=False
            ) as rebuild:
                r = client.get("/api/feed")
                assert r.status_code == 503
                assert "feed not generated" in r.json()["detail"]
                rebuild.assert_called_once()


# ---------------------------------------------------------------------------
# Nationality blend helpers (unit tests — no live Redis/app needed)
# ---------------------------------------------------------------------------


class TestCountriesFromPersons:
    """Unit tests for _countries_from_persons helper in generate_feed.py."""

    def test_basic_union(self):
        from scripts.feed.generate_feed import _countries_from_persons
        result = _countries_from_persons(["US", "GB"], ["FR"])
        assert set(result) == {"US", "GB", "FR"}

    def test_deduplication(self):
        from scripts.feed.generate_feed import _countries_from_persons
        result = _countries_from_persons(["US"], ["US", "GB"])
        assert result.count("US") == 1

    def test_none_list_ignored(self):
        from scripts.feed.generate_feed import _countries_from_persons
        result = _countries_from_persons(None, ["IT"])
        assert result == ["IT"]

    def test_empty_list_ignored(self):
        from scripts.feed.generate_feed import _countries_from_persons
        result = _countries_from_persons([], ["DE"])
        assert result == ["DE"]

    def test_invalid_codes_filtered(self):
        """Codes that are not exactly 2 letters are filtered out."""
        from scripts.feed.generate_feed import _countries_from_persons
        result = _countries_from_persons(["USA", "X", "GB", ""])
        assert result == ["GB"]

    def test_uppercase_normalisation(self):
        from scripts.feed.generate_feed import _countries_from_persons
        result = _countries_from_persons(["it", "FR"])
        assert "IT" in result
        assert "FR" in result

    def test_capped_at_four(self):
        from scripts.feed.generate_feed import _countries_from_persons
        result = _countries_from_persons(["US", "GB", "FR", "DE", "IT"])
        assert len(result) <= 4

    def test_deterministic_sorted_order(self):
        from scripts.feed.generate_feed import _countries_from_persons
        r1 = _countries_from_persons(["US", "FR"], ["GB"])
        r2 = _countries_from_persons(["GB"], ["FR", "US"])
        assert r1 == r2

    def test_empty_all_gives_empty(self):
        from scripts.feed.generate_feed import _countries_from_persons
        assert _countries_from_persons(None, None, []) == []


class TestBlendByNationality:
    """Unit tests for _blend_by_nationality in feed.py."""

    def test_output_length_unchanged(self):
        from app.routers.feed import _blend_by_nationality
        ids = [f"id_{i}" for i in range(30)]
        nat_set = {f"id_{i}" for i in range(0, 30, 3)}  # every 3rd
        result = _blend_by_nationality(ids, nat_set)
        assert len(result) == 30

    def test_no_duplicates(self):
        from app.routers.feed import _blend_by_nationality
        ids = [f"id_{i}" for i in range(30)]
        nat_set = {f"id_{i}" for i in range(10)}
        result = _blend_by_nationality(ids, nat_set)
        assert len(result) == len(set(result))

    def test_all_ids_present(self):
        from app.routers.feed import _blend_by_nationality
        ids = [f"id_{i}" for i in range(20)]
        nat_set = {f"id_{i}" for i in range(10)}
        result = _blend_by_nationality(ids, nat_set)
        assert set(result) == set(ids)

    def test_home_world_ratio_approx_2_to_1(self):
        """Over a large window, home:world ratio should be ~2:1."""
        from app.routers.feed import _blend_by_nationality, NAT_BLEND_HOME, NAT_BLEND_WORLD
        # 60 home, 30 world
        home_ids = [f"home_{i}" for i in range(60)]
        world_ids = [f"world_{i}" for i in range(30)]
        # mix them in base list: home first, world appended
        ids = home_ids + world_ids
        nat_set = set(home_ids)
        result = _blend_by_nationality(ids, nat_set)

        # Check first 45 positions for ~2:1 ratio
        window = result[:45]
        home_count = sum(1 for x in window if x.startswith("home_"))
        world_count = sum(1 for x in window if x.startswith("world_"))
        # Should be 30 home, 15 world (exact for NAT_BLEND_HOME=2, NAT_BLEND_WORLD=1)
        expected_home = 45 * NAT_BLEND_HOME // (NAT_BLEND_HOME + NAT_BLEND_WORLD)
        assert abs(home_count - expected_home) <= 3, (
            f"Expected ~{expected_home} home in first 45, got {home_count}"
        )

    def test_no_home_falls_back_to_world_only(self):
        """With no home IDs, result is just the world list unchanged."""
        from app.routers.feed import _blend_by_nationality
        ids = ["a", "b", "c"]
        nat_set: set[str] = set()
        result = _blend_by_nationality(ids, nat_set)
        assert result == ids

    def test_no_world_gives_home_only(self):
        """With all IDs in home, result equals input list."""
        from app.routers.feed import _blend_by_nationality
        ids = ["a", "b", "c"]
        nat_set = {"a", "b", "c"}
        result = _blend_by_nationality(ids, nat_set)
        assert result == ids

    def test_empty_input(self):
        from app.routers.feed import _blend_by_nationality
        assert _blend_by_nationality([], set()) == []


class TestFeedNationalityParam:
    """§13P.3 — Nationality param on /api/feed endpoint."""

    def _make_nat_mock(
        self,
        cards: list[dict],
        nat_code: str,
        nat_ids: list[str],
    ) -> MagicMock:
        """Build a Redis mock that includes a nationality order key."""
        import json as _json
        from app.routers.feed import (
            FEED_META_KEY,
            FEED_ORDER_ALL_KEY,
            FEED_ORDER_CAT_PREFIX,
            FEED_ORDER_NAT_PREFIX,
        )

        cat_ids: dict[str, list[str]] = {}
        for c in cards:
            for cat in c.get("categories") or []:
                cat_ids.setdefault(cat, []).append(c["id"])

        meta = {
            "count": len(cards),
            "generated_at": "2026-01-01T00:00:00+00:00",
            "content_version": FEED_CONTENT_VERSION,
            "by_type": {},
            "by_category": {},
            "categories": list(cat_ids.keys()),
            "countries": [nat_code],
        }
        all_ids = [c["id"] for c in cards]
        card_map = {c["id"]: _json.dumps(c, separators=(",", ":")) for c in cards}

        def _get(key: str) -> str | None:
            if key == FEED_META_KEY:
                return _json.dumps(meta)
            if key == FEED_ORDER_ALL_KEY:
                return _json.dumps(all_ids)
            if key == f"{FEED_ORDER_NAT_PREFIX}{nat_code}:v2":
                return _json.dumps(nat_ids)
            for cat, ids in cat_ids.items():
                if key == f"{FEED_ORDER_CAT_PREFIX}{cat}:v2":
                    return _json.dumps(ids)
            return None

        def _hmget(hash_key: str, fields: list[str]) -> list[str | None]:
            return [card_map.get(f) for f in fields]

        mock_r = MagicMock()
        mock_r.get.side_effect = _get
        mock_r.hmget.side_effect = _hmget
        return mock_r

    def _make_cards_with_countries(self, n_home: int, n_world: int, nat_code: str) -> list[dict]:
        """Create home + world cards for a nationality blend test."""
        cards = []
        for i in range(n_home):
            cards.append({
                "id": f"home_{i}",
                "type": "relationship",
                "score": 0.9,
                "categories": [],
                "countries": [nat_code],
            })
        for i in range(n_world):
            cards.append({
                "id": f"world_{i}",
                "type": "relationship",
                "score": 0.8,
                "categories": [],
                "countries": ["XX"],
            })
        return cards

    def test_no_nationality_unchanged(self, client: TestClient):
        """Omitting nationality param returns unblended feed."""
        cards = self._make_cards_with_countries(20, 10, "IT")
        nat_ids = [c["id"] for c in cards if "IT" in c.get("countries", [])]

        with patch("app.routers.feed.get_redis") as mock_get_redis:
            mock_get_redis.return_value = self._make_nat_mock(cards, "IT", nat_ids)
            with patch("app.routers.feed._id_array_cache", {}):
                r = client.get("/api/feed?limit=30")
                assert r.status_code == 200
                body = r.json()
                assert body["total"] == 30

    def test_valid_nationality_blends_home(self, client: TestClient):
        """With nationality=IT, home cards should dominate the first window."""
        cards = self._make_cards_with_countries(20, 10, "IT")
        nat_ids = [c["id"] for c in cards if "IT" in c.get("countries", [])]

        with patch("app.routers.feed.get_redis") as mock_get_redis:
            mock_get_redis.return_value = self._make_nat_mock(cards, "IT", nat_ids)
            with patch("app.routers.feed._id_array_cache", {}):
                r = client.get("/api/feed?nationality=IT&limit=30&seed=42")
                assert r.status_code == 200
                body = r.json()
                returned = body["cards"]
                home_count = sum(1 for c in returned if "IT" in c.get("countries", []))
                # Should be more home than world (~2:1)
                assert home_count > len(returned) // 2, (
                    f"Expected >half home cards, got {home_count}/{len(returned)}"
                )

    def test_invalid_nationality_ignored(self, client: TestClient):
        """A non-ISO2 nationality param is silently ignored."""
        cards = self._make_cards_with_countries(10, 10, "IT")
        nat_ids = [c["id"] for c in cards if "IT" in c.get("countries", [])]

        with patch("app.routers.feed.get_redis") as mock_get_redis:
            mock_get_redis.return_value = self._make_nat_mock(cards, "IT", nat_ids)
            with patch("app.routers.feed._id_array_cache", {}):
                r = client.get("/api/feed?nationality=NOTACODE&limit=20")
                assert r.status_code == 200

    def test_unknown_nationality_not_in_meta_ignored(self, client: TestClient):
        """A valid ISO2 code not present in meta countries is silently ignored."""
        cards = self._make_cards_with_countries(10, 10, "IT")
        nat_ids = [c["id"] for c in cards if "IT" in c.get("countries", [])]

        with patch("app.routers.feed.get_redis") as mock_get_redis:
            mock_get_redis.return_value = self._make_nat_mock(cards, "IT", nat_ids)
            with patch("app.routers.feed._id_array_cache", {}):
                # ZZ is a valid ISO2 pattern but not in meta's countries (only IT is)
                r = client.get("/api/feed?nationality=ZZ&limit=20")
                assert r.status_code == 200
                body = r.json()
                assert body["total"] == 20

    def test_nationality_and_categories_compose(self, client: TestClient):
        """nationality + categories params can be combined without error."""
        cards = []
        for i in range(10):
            cards.append({
                "id": f"home_pol_{i}",
                "type": "relationship",
                "score": 0.9,
                "categories": ["politics"],
                "countries": ["IT"],
            })
        for i in range(10):
            cards.append({
                "id": f"world_art_{i}",
                "type": "dynasty",
                "score": 0.7,
                "categories": ["arts"],
                "countries": ["XX"],
            })

        nat_ids = [c["id"] for c in cards if "IT" in c.get("countries", [])]

        with patch("app.routers.feed.get_redis") as mock_get_redis:
            mock_get_redis.return_value = self._make_nat_mock(cards, "IT", nat_ids)
            with patch("app.routers.feed._id_array_cache", {}):
                r = client.get("/api/feed?nationality=IT&categories=politics&limit=20")
                assert r.status_code == 200
                body = r.json()
                assert body["total"] == 20

    def test_missing_nat_list_falls_back_to_no_boost(self, client: TestClient):
        """If nat list is absent from Redis, the feed is returned without a blend."""
        import json as _json
        from app.routers.feed import FEED_META_KEY, FEED_ORDER_ALL_KEY

        cards = [
            {"id": f"c{i}", "type": "relationship", "score": 0.9,
             "categories": [], "countries": ["IT"]}
            for i in range(5)
        ]
        all_ids = [c["id"] for c in cards]
        card_map = {c["id"]: _json.dumps(c, separators=(",", ":")) for c in cards}
        meta = {
            "count": 5,
            "generated_at": "2026-01-01T00:00:00+00:00",
            "content_version": FEED_CONTENT_VERSION,
            "by_type": {}, "by_category": {},
            "categories": [],
            "countries": ["IT"],  # IT in meta but nat list key is missing
        }

        def _get(key: str) -> str | None:
            if key == FEED_META_KEY:
                return _json.dumps(meta)
            if key == FEED_ORDER_ALL_KEY:
                return _json.dumps(all_ids)
            return None  # nat list key returns None → fall back

        def _hmget(hash_key: str, fields: list[str]) -> list[str | None]:
            return [card_map.get(f) for f in fields]

        mock_r = MagicMock()
        mock_r.get.side_effect = _get
        mock_r.hmget.side_effect = _hmget

        with patch("app.routers.feed.get_redis") as mock_get_redis:
            mock_get_redis.return_value = mock_r
            with patch("app.routers.feed._id_array_cache", {}):
                r = client.get("/api/feed?nationality=IT&limit=10")
                assert r.status_code == 200
                body = r.json()
                # Falls back gracefully — all 5 cards returned
                assert body["total"] == 5


def test_org_affiliation_query_projects_role_dates():
    from scripts.feed.generate_feed import _fetch_org_affiliations

    db = MagicMock()
    db.aql.execute.return_value = []

    assert _fetch_org_affiliations(db, "chief_executive", 30) == []

    query = db.aql.execute.call_args.args[0]
    assert "role_start_date:       e.start_date" in query
    assert "role_end_date:         e.end_date" in query


def test_feed_readiness_rejects_stale_generated_content():
    from app.routers.feed import (
        FEED_CARDMAP_KEY,
        FEED_META_KEY,
        FEED_ORDER_ALL_KEY,
        _feed_is_ready,
    )

    metadata = {
        "generated_at": "2026-01-01T00:00:00+00:00",
        "content_version": FEED_CONTENT_VERSION - 1,
    }
    redis = MagicMock()
    redis.get.side_effect = lambda key: (
        json.dumps(metadata)
        if key == FEED_META_KEY
        else json.dumps([])
        if key == FEED_ORDER_ALL_KEY
        else None
    )
    redis.exists.side_effect = lambda key: key == FEED_CARDMAP_KEY

    assert _feed_is_ready(redis) is None

    metadata["content_version"] = FEED_CONTENT_VERSION
    assert _feed_is_ready(redis) == metadata
