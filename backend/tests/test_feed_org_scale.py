from unittest.mock import MagicMock


def test_org_affiliations_start_from_indexed_notable_people():
    from scripts.feed.generate_feed import _fetch_org_affiliations

    db = MagicMock()
    db.aql.execute.return_value = []

    assert _fetch_org_affiliations(db, "member_of", 120) == []

    call = db.aql.execute.call_args
    query = call.args[0]
    assert "FOR p IN persons" in query
    assert "FILTER p.wikidata_statements_count >= @min_stmts" in query
    assert "FOR o, e IN 1..1 OUTBOUND p affiliations" in query
    assert "FOR e IN affiliations" not in query
    assert call.kwargs["bind_vars"] == {
        "aff_types": ["member_of"],
        "min_stmts": 120,
    }


def test_org_affiliations_accept_multiple_types_in_one_pass():
    from scripts.feed.generate_feed import _fetch_org_affiliations

    db = MagicMock()
    db.aql.execute.return_value = []
    types = ["founder", "employee", "owner"]

    assert _fetch_org_affiliations(db, types, 30) == []

    call = db.aql.execute.call_args
    assert "FILTER e.type IN @aff_types" in call.args[0]
    assert call.kwargs["bind_vars"]["aff_types"] == types
