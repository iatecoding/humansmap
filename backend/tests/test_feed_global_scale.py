from unittest.mock import MagicMock


def test_relationship_fetch_starts_from_indexed_notable_people():
    from scripts.feed.generate_feed import (
        RELATION_LABEL_MAP,
        _fetch_relationship_edges,
    )

    db = MagicMock()
    db.aql.execute.return_value = [{"edge": "qualified"}]

    assert _fetch_relationship_edges(db) == [{"edge": "qualified"}]

    db.aql.execute.assert_called_once()
    query = db.aql.execute.call_args.args[0]
    bind_vars = db.aql.execute.call_args.kwargs["bind_vars"]
    assert "FOR a IN persons" in query
    assert "FILTER a.wikidata_statements_count >= @min_stmts" in query
    assert "FOR b, e IN 1..1 OUTBOUND a relationships" in query
    assert set(bind_vars["types"]) == set(RELATION_LABEL_MAP)


def test_family_fetch_starts_from_indexed_notable_people():
    from scripts.feed.generate_feed import _fetch_family_edges

    db = MagicMock()
    db.aql.execute.return_value = [{"edge": "qualified"}]

    assert _fetch_family_edges(db) == [{"edge": "qualified"}]

    db.aql.execute.assert_called_once()
    query = db.aql.execute.call_args.args[0]
    bind_vars = db.aql.execute.call_args.kwargs["bind_vars"]
    assert "FOR a IN persons" in query
    assert "FOR b, e IN 1..1 OUTBOUND a relationships" in query
    assert set(bind_vars["types"]) == {
        "parent",
        "child",
        "sibling",
        "father",
        "mother",
    }


def test_sport_query_filters_before_grouping():
    from scripts.feed.generate_feed import _build_sport_cards

    db = MagicMock()
    db.aql.execute.return_value = []

    assert _build_sport_cards(db) == []

    query = db.aql.execute.call_args.args[0]
    edge_scan = query.index("FOR e IN affiliations")
    type_filter = query.index("FILTER e.type == 'sports_team_member'")
    grouping = query.index("COLLECT org_id = e._to")
    assert edge_scan < type_filter < grouping
