import pytest
from fastapi import HTTPException

from app.routers.graph import _neighborhood_cache_key, _parse_node_types


def test_node_types_are_validated_and_canonicalized():
    parsed, canonical = _parse_node_types("event, person,organization,person")
    assert parsed == {"person", "organization", "event"}
    assert canonical == "event,organization,person"


@pytest.mark.parametrize("value", ["", ",", "person,company"])
def test_invalid_node_types_are_rejected(value):
    with pytest.raises(HTTPException) as exc:
        _parse_node_types(value)
    assert exc.value.status_code == 422


def test_neighborhood_cache_key_covers_response_shape():
    base = _neighborhood_cache_key("Q22686", 1, 250, "event,organization,person")
    assert base != _neighborhood_cache_key("Q22686", 2, 250, "event,organization,person")
    assert base != _neighborhood_cache_key("Q22686", 1, 1000, "event,organization,person")
    assert base != _neighborhood_cache_key("Q22686", 1, 250, "organization")
