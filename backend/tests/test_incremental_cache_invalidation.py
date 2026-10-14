import scripts.incremental_update as updater


def test_cache_key_qids_supports_legacy_and_v2_neighborhoods():
    assert updater._cache_key_qids("neighborhood:Q22686:d2") == {"Q22686"}
    assert updater._cache_key_qids(
        "neighborhood:v2:Q22686:d1:l250:types=event,organization,person"
    ) == {"Q22686"}


def test_cache_key_qids_supports_both_shortest_path_endpoints():
    assert updater._cache_key_qids("sp:Q22686:Q76") == {"Q22686", "Q76"}
    assert updater._cache_key_qids("search:v2:Donald Trump:10") == set()
