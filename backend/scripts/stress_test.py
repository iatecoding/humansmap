"""Stress test for the HumansMap API.

Runs from inside the backend container to hit endpoints directly.
Tests concurrent load to estimate how many users the site can handle.
"""

import asyncio
import time
import statistics
import urllib.request
import urllib.error
import json
import itertools
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = "http://backend:8000/api"

# Endpoints that simulate real user behavior
ENDPOINTS = {
    "health":        "/health",
    "stats":         "/stats",
    "search":        "/search?q=Roma&limit=10",
    "neighborhood":  None,  # filled dynamically after search
    "edge_types":    "/edge-types",
    "person":        None,  # filled dynamically
}

# Test parameters
CONCURRENCY_LEVELS = [10, 25, 50, 100, 200]
REQUESTS_PER_LEVEL = 200
_CLIENT_SEQUENCE = itertools.count(1)


def benchmark_client_ip():
    """Unique RFC 2544 address so capacity tests do not measure per-user throttling."""
    n = next(_CLIENT_SEQUENCE)
    return f"198.18.{(n >> 8) % 254}.{(n % 254) + 1}"


def fetch(url, timeout=10, client_ip=None):
    """Single HTTP request, returns (status, latency_ms, size_bytes)."""
    start = time.monotonic()
    try:
        headers = {"X-Real-IP": client_ip} if client_ip else {}
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            latency = (time.monotonic() - start) * 1000
            return (resp.status, latency, len(body))
    except urllib.error.HTTPError as e:
        latency = (time.monotonic() - start) * 1000
        return (e.code, latency, 0)
    except Exception as e:
        latency = (time.monotonic() - start) * 1000
        return (0, latency, 0)


def discover_qids():
    """Get some real QIDs from the search endpoint for realistic tests."""
    entities = []
    for query in ["Roma", "Milano", "Berlusconi", "Dante", "Verdi"]:
        try:
            url = f"{BASE}/search?q={query}&limit=3"
            req = urllib.request.Request(url, headers={"X-Real-IP": benchmark_client_ip()})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                for item in data.get("items", []):
                    qid = item.get("qid") or item.get("_key")
                    if qid:
                        entities.append((qid, item.get("_collection")))
        except Exception:
            pass
    return entities


def run_concurrent_test(urls, concurrency, total_requests):
    """Run total_requests against urls with given concurrency."""
    results = []
    url_cycle = [urls[i % len(urls)] for i in range(total_requests)]

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        start = time.monotonic()
        futures = [pool.submit(fetch, url, 10, benchmark_client_ip()) for url in url_cycle]
        for f in as_completed(futures):
            results.append(f.result())
        wall_time = time.monotonic() - start

    statuses = [r[0] for r in results]
    latencies = [r[1] for r in results]
    ok = sum(1 for s in statuses if 200 <= s < 400)
    errors = sum(1 for s in statuses if s == 0 or s >= 400)

    return {
        "ok": ok,
        "errors": errors,
        "rps": total_requests / wall_time,
        "wall_time_s": round(wall_time, 2),
        "latency_p50": round(statistics.median(latencies), 1),
        "latency_p95": round(sorted(latencies)[int(len(latencies) * 0.95)], 1),
        "latency_p99": round(sorted(latencies)[int(len(latencies) * 0.99)], 1),
        "latency_max": round(max(latencies), 1),
    }


def main():
    print("=" * 70)
    print("STRESS TEST — HumansMap API")
    print("=" * 70)

    # Discover real data
    print("\nDiscovering QIDs for realistic tests...")
    entities = discover_qids()
    entity_qids = [qid for qid, _ in entities]
    person_qids = [qid for qid, collection in entities if collection == "persons"]
    print(f"  Found {len(entity_qids)} entities ({len(person_qids)} people): {entity_qids[:5]}...")

    # Build URL sets
    search_urls = [
        f"{BASE}/search?q={q}&limit=10"
        for q in ["Roma", "Milano", "Napoli", "Firenze", "Torino",
                   "Dante", "Verdi", "Leonardo", "Galileo", "Marco"]
    ]

    neighborhood_urls = [f"{BASE}/graph/neighborhood/{qid}?depth=1&limit=20" for qid in entity_qids[:10]] if entity_qids else []
    person_urls = [f"{BASE}/persons/{qid}" for qid in person_qids[:10]] if person_qids else []

    test_suites = {
        "Health (baseline)": [f"{BASE}/health"],
        "Search queries": search_urls,
        "Stats": [f"{BASE}/stats"],
        "Edge types": [f"{BASE}/edge-types"],
    }
    if person_urls:
        test_suites["Person detail"] = person_urls
    if neighborhood_urls:
        test_suites["Graph neighborhood"] = neighborhood_urls

    # Mixed realistic traffic (what real users actually do)
    mixed_urls = []
    if search_urls:
        mixed_urls.extend(search_urls * 3)  # searches are most common
    if neighborhood_urls:
        mixed_urls.extend(neighborhood_urls * 2)
    if person_urls:
        mixed_urls.extend(person_urls * 2)
    mixed_urls.extend([f"{BASE}/edge-types"] * 2)
    mixed_urls.extend([f"{BASE}/stats"])

    if mixed_urls:
        test_suites["MIXED (realistic traffic)"] = mixed_urls

    # Run tests
    for suite_name, urls in test_suites.items():
        print(f"\n{'─' * 70}")
        print(f"  {suite_name}")
        print(f"{'─' * 70}")
        print(f"  {'Concurrency':>12}  {'RPS':>8}  {'p50ms':>7}  {'p95ms':>7}  {'p99ms':>7}  {'max ms':>7}  {'Errors':>6}")

        for c in CONCURRENCY_LEVELS:
            r = run_concurrent_test(urls, c, REQUESTS_PER_LEVEL)
            err_str = f"{r['errors']}" if r['errors'] == 0 else f"\033[91m{r['errors']}\033[0m"
            print(f"  {c:>12}  {r['rps']:>8.1f}  {r['latency_p50']:>7.1f}  {r['latency_p95']:>7.1f}  {r['latency_p99']:>7.1f}  {r['latency_max']:>7.1f}  {err_str:>6}")

    # Summary
    print(f"\n{'=' * 70}")
    print("INTERPRETATION")
    print(f"{'=' * 70}")
    print("""
A typical user session:
  - 1 search (300ms) + 1 neighborhood load (500ms) + browsing (idle)
  - ~2-3 API requests per active interaction, with pauses between clicks
  - Average user generates ~10 requests/minute during active use

Rough formula: Max concurrent active users ≈ Mixed RPS × 6
  (assumes 10 req/min per user = 1 req per 6 seconds)
""")


if __name__ == "__main__":
    main()
