from unittest.mock import Mock

from app.routers.trivia_common import _query_pool
from app.routers.trivia_intruso import _gen_intruso_graph


def test_query_pool_bounds_candidates_before_random_sort():
    db = Mock()
    db.aql.execute.return_value = []

    _query_pool(db, None, None, pool_size=80)

    query = db.aql.execute.call_args.args[0]
    bind_vars = db.aql.execute.call_args.kwargs["bind_vars"]
    assert bind_vars["candidate_pool_size"] == 1600
    assert query.index("LIMIT @candidate_pool_size") < query.index("SORT RAND()")


def test_graph_intruder_uses_bounded_pool_for_impostor(monkeypatch):
    monkeypatch.setattr("app.routers.trivia_intruso.random.shuffle", lambda values: None)
    pool = [
        {
            "qid": "QH",
            "name_en": "Hub",
            "gender": "female",
            "occupations": ["writer"],
        },
        {
            "qid": "QI",
            "name_en": "Impostor",
            "gender": "female",
            "occupations": ["writer"],
        },
    ]
    neighbors = [
        {
            "qid": f"QN{i}",
            "name_en": f"Neighbor {i}",
            "gender": "female",
            "occupations": ["writer"],
            "wikidata_statements_count": 100,
        }
        for i in range(4)
    ]

    db = Mock()
    db.aql.execute.side_effect = [
        neighbors,
        [f"persons/QN{i}" for i in range(4)],
    ]

    result = _gen_intruso_graph(pool, set(), db)

    assert result is not None
    assert result["type"] == "intruso_graph"
    assert db.aql.execute.call_count == 2
    queries = [call.args[0] for call in db.aql.execute.call_args_list]
    assert all("FOR p IN @@persons" not in query for query in queries)
