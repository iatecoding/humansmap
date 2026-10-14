"""
Pytest tests for the WikidataSPARQLProvider.

Requires: network access to Wikidata SPARQL endpoint. When the public endpoint
is unreachable or returning 5xx (it is a shared, rate-limited service), these
tests skip instead of failing — a 502 from Wikidata is not a code regression.
A reachable endpoint that returns no data still fails, so real breakage is
caught.

Run inside the Docker container:
    docker compose exec backend pytest tests/test_sparql_provider.py -s
"""

import asyncio
import time

import pytest
import app.config as cfg

# Use high threshold to keep tests fast (fewer results)
cfg.settings.notability_min_statements = 100
cfg.settings.sparql_delay_seconds = 1.0

from app.providers.wikidata_sparql import WikidataSPARQLProvider, _sparql_query_sync


def _endpoint_available(attempts: int = 3) -> tuple[bool, Exception | None]:
    """Probe the SPARQL endpoint with a trivial query, retrying transient 5xx."""
    probe = "SELECT ?s WHERE { ?s ?p ?o } LIMIT 1"
    last: Exception | None = None
    for i in range(attempts):
        try:
            if _sparql_query_sync(probe, cfg.settings.sparql_endpoint, cfg.settings.sparql_user_agent):
                return True, None
        except Exception as exc:  # noqa: BLE001 - any transport/HTTP failure counts
            last = exc
        if i < attempts - 1:
            time.sleep(2 * (i + 1))
    return False, last


async def _fetch_persons_with_retries(provider: WikidataSPARQLProvider, attempts: int = 3) -> list:
    """The provider tolerates errors by returning []; retry before concluding."""
    for i in range(attempts):
        results = await provider.fetch_persons(occupation_qids=["Q82955"])  # politician
        if results:
            return results
        if i < attempts - 1:
            await asyncio.sleep(2 * (i + 1))
    return []


@pytest.fixture(scope="module")
async def persons():
    p = WikidataSPARQLProvider()
    results = await _fetch_persons_with_retries(p)
    if not results:
        available, err = _endpoint_available()
        if not available:
            pytest.skip(f"Wikidata SPARQL endpoint unavailable: {err}")
        pytest.fail("Wikidata SPARQL endpoint reachable but returned no politicians")
    return results


def test_persons_not_empty(persons):
    assert len(persons) > 0


def test_persons_have_valid_qids(persons):
    for p in persons[:10]:
        assert p.wikidata_id.startswith("Q"), f"Bad QID: {p.wikidata_id}"


def test_persons_have_names(persons):
    for p in persons[:10]:
        assert p.name_en, f"Person {p.wikidata_id} has no name_en"


@pytest.fixture(scope="module")
async def edges(persons):
    p = WikidataSPARQLProvider()
    sample_ids = [person.wikidata_id for person in persons[:3]]
    return await p.fetch_relationships(sample_ids)


def test_edges_have_valid_collections(edges):
    for e in edges:
        assert e.edge_collection in ("relationships", "affiliations", "involvements"), \
            f"Unexpected collection: {e.edge_collection}"


def test_edges_have_valid_qids(edges):
    for e in edges:
        assert e.from_id.startswith("Q"), f"Bad from_id: {e.from_id}"
        assert e.to_id.startswith("Q"), f"Bad to_id: {e.to_id}"


def test_edges_have_types(edges):
    for e in edges:
        assert e.type, f"Edge {e.from_id}->{e.to_id} has no type"
