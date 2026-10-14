from unittest.mock import MagicMock


def test_empty_relationship_pool_uses_bounded_anchor_query():
    from scripts.feed.generate_feed import _build_relationship_cards

    db = MagicMock()
    db.aql.execute.side_effect = [[], []]

    assert _build_relationship_cards(db) == []

    assert db.aql.execute.call_count == 2
    couple_call = db.aql.execute.call_args_list[1]
    assert "FOR anchor_id IN @anchor_ids" in couple_call.args[0]
    assert "1..1 ANY anchor_id relationships" in couple_call.args[0]
    assert couple_call.kwargs["bind_vars"]["anchor_ids"] == []


def test_person_detail_lookup_uses_primary_keys():
    from scripts.feed.generate_feed import _fetch_persons_by_qids

    db = MagicMock()
    db.aql.execute.return_value = []

    assert _fetch_persons_by_qids(db, ["Q1", "Q2"]) == {}

    query = db.aql.execute.call_args.args[0]
    assert "FOR qid IN @qids" in query
    assert 'DOCUMENT(CONCAT("persons/", qid))' in query
    assert "FOR p IN persons" not in query
