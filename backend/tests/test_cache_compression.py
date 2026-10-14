import json

from app.services.cache import _decode_value, _encode_value


def test_small_cache_values_remain_plain_text():
    raw = json.dumps({"ok": True})
    assert _encode_value(raw) == raw
    assert _decode_value(raw) == raw


def test_large_cache_values_round_trip_with_compression():
    raw = json.dumps({"vertices": [{"name": "repeated payload"}] * 1000})
    encoded = _encode_value(raw)
    assert encoded.startswith("z1:")
    assert len(encoded) < len(raw)
    assert _decode_value(encoded) == raw


def test_existing_uncompressed_values_remain_readable():
    raw = "<html><body>legacy cache entry</body></html>"
    assert _decode_value(raw) == raw
