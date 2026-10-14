from app.release import APP_BUILD_SHA, cache_namespace


def test_cache_namespace_is_bounded_and_matches_release():
    assert cache_namespace() == APP_BUILD_SHA[:64]
    assert cache_namespace()
