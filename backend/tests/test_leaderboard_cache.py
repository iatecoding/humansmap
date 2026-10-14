from app.routers.leaderboard import (ORG_SORT_OPTIONS, PERSON_SORT_OPTIONS,
                                     _org_counts_ready, _org_sort_key,
                                     _page_cache_key, _page_response,
                                     _person_sort_sql)


class _Collection:
    def __init__(self, marker):
        self.marker = marker

    def get(self, key):
        assert key == "leaderboard_org_counts"
        return self.marker


class _Db:
    def __init__(self, marker):
        self.marker = marker

    def collection(self, name):
        assert name == "app_metadata"
        return _Collection(self.marker)


def test_page_cache_key_is_shared_across_response_limits():
    key = _page_cache_key("persons", ["all", "living", "all", "degree"], 0)
    assert key == "leaderboard:v4:persons:all:living:all:degree:0"
    assert "%3A" in _page_cache_key("persons", ["value:with:colons"], 0)


def test_page_cache_key_includes_sort_dimension():
    degree = _page_cache_key("persons", ["all", "living", "all", "degree"], 0)
    birth = _page_cache_key("persons", ["all", "living", "all", "birth_date"], 0)
    assert degree != birth


def test_page_response_slices_one_canonical_page():
    page = {"items": [{"qid": f"Q{i}"} for i in range(500)], "has_more": True}
    response = _page_response(page, limit=60, offset=0, metadata={"status": "living"})
    assert len(response["items"]) == 60
    assert response["has_more"] is True
    assert response["limit"] == 60
    assert response["status"] == "living"


def test_page_response_detects_more_items_inside_cached_page():
    page = {"items": [{"qid": "Q1"}, {"qid": "Q2"}], "has_more": False}
    assert _page_response(page, limit=1, offset=0, metadata={})["has_more"] is True
    assert _page_response(page, limit=2, offset=0, metadata={})["has_more"] is False


def test_materialized_counts_require_ready_versioned_marker():
    assert _org_counts_ready(_Db({"status": "ready", "version": 1})) is True
    assert _org_counts_ready(_Db({"status": "building", "version": 1})) is False
    assert _org_counts_ready(_Db({"status": "ready", "version": 2})) is False
    assert _org_counts_ready(_Db(None)) is False


# ── Exposed sort matrix ────────────────────────────────────────────────────────

def test_only_index_backed_person_sorts_are_exposed():
    # nationality is deliberately absent: sorting on nationalities[0] cannot use
    # the nationalities[*] array index and full-scans the collection.
    assert PERSON_SORT_OPTIONS == {"degree", "birth_date"}


def test_person_sort_degree_uses_notability_index():
    assert _person_sort_sql("degree") == ("", "SORT p.wikidata_statements_count DESC")


def test_person_sort_birth_date_filters_nulls_for_index_use():
    sort_filter, sort_clause = _person_sort_sql("birth_date")
    assert sort_filter == "FILTER p.birth_date != null"
    assert sort_clause == "SORT p.birth_date DESC"


def test_person_sort_falls_back_to_degree():
    assert _person_sort_sql("nationality") == _person_sort_sql("degree")


def test_org_sorts_exposed():
    assert ORG_SORT_OPTIONS == {"members", "founded"}


def test_org_sort_normalizes_occupations_to_members():
    assert _org_sort_key("occupations", "founded") == "members"
    assert _org_sort_key("business", "founded") == "founded"
    assert _org_sort_key(None, None) == "members"
