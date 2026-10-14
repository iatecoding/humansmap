"""Load the small real demo graph into ArangoDB.

The demo graph (`backend/data/demo/demo_graph.json`) is a curated slice of the
live graph: ~50 real public figures, their organizations and events, and the
real relations between them, all sourced from Wikidata (CC0). It is small
enough to commit and lets you exercise every part of the site (Explore, Play,
Leaderboards, Search) without downloading the multi-hundred-gigabyte Wikidata
dump or running the SEC ingestion pipeline.

Run it against an empty or throwaway database:

    docker compose exec backend python scripts/seed_demo_data.py

Or directly, honouring the usual ARANGO_* environment variables:

    python backend/scripts/seed_demo_data.py

Options:

    --file PATH   Alternate graph JSON (default: ../data/demo/demo_graph.json)
    --skip-challenges   Do not (re)compute the path-finding challenges
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib

from app.database import (
    AFFILIATIONS,
    EVENTS,
    GAME_CHALLENGES,
    GRAPH_NAME,
    INVOLVEMENTS,
    ORG_STRUCTURE,
    ORGANIZATIONS,
    PERSONS,
    RELATIONSHIPS,
    init_db,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("seed_demo_data")

DEFAULT_FILE = pathlib.Path(__file__).resolve().parent.parent / "data" / "demo" / "demo_graph.json"


def _load(path: pathlib.Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    for key in ("persons", "organizations", "events"):
        if not data.get(key):
            raise SystemExit(f"demo graph {path} has no '{key}'")
    return data


def _insert_documents(db, collection: str, rows: list[dict]) -> None:
    if not rows:
        return
    db.collection(collection).insert_many(rows, overwrite=True)
    logger.info("  %-16s %d documents", collection, len(rows))


def _insert_edges(db, collection: str, rows: list[dict]) -> None:
    if not rows:
        return
    db.collection(collection).insert_many(rows, overwrite=True)
    logger.info("  %-16s %d edges", collection, len(rows))


def _build_challenges(db, authored: list[dict]) -> int:
    """Compute real shortest paths for the authored (start, target) pairs."""
    persons = {
        doc["_key"]: doc
        for doc in db.collection(PERSONS).all()
    }
    aql = """
        FOR v, e IN ANY SHORTEST_PATH @start TO @end GRAPH @graph
            RETURN {vertex: v._id, edge: e}
    """
    rows: list[dict] = []
    for pair in authored:
        start, target = pair["start_qid"], pair["target_qid"]
        if start not in persons or target not in persons:
            logger.warning("skipping challenge %s -> %s (unknown person)", start, target)
            continue
        path = list(db.aql.execute(aql, bind_vars={
            "start": f"persons/{start}",
            "end": f"persons/{target}",
            "graph": GRAPH_NAME,
        }))
        # Match generate_challenges.py: keep only usable 2–6 hop challenges.
        if not 2 <= len(path) <= 6:
            logger.warning("skipping challenge %s -> %s (length %d)", start, target, len(path))
            continue
        shared = set(persons[start].get("nationalities") or []) & \
            set(persons[target].get("nationalities") or [])
        rows.append({
            "_key": f"{start}_{target}",
            "start_qid": start,
            "start_name": persons[start].get("name_en") or start,
            "target_qid": target,
            "target_name": persons[target].get("name_en") or target,
            "shortest_path_length": len(path),
            "shortest_path": [
                {"qid": step["vertex"].split("/", 1)[1]} for step in path
            ],
            "nationality": sorted(shared)[0] if shared else None,
            "active": True,
        })
    if rows:
        db.collection(GAME_CHALLENGES).insert_many(rows, overwrite=True)
    logger.info("  %-16s %d challenges", GAME_CHALLENGES, len(rows))
    return len(rows)


def seed(path: pathlib.Path, *, with_challenges: bool = True) -> dict:
    data = _load(path)
    logger.info("Seeding demo graph from %s", path)
    db = init_db()

    _insert_documents(db, PERSONS, data["persons"])
    _insert_documents(db, ORGANIZATIONS, data["organizations"])
    _insert_documents(db, EVENTS, data["events"])
    _insert_edges(db, RELATIONSHIPS, data["relationships"])
    _insert_edges(db, AFFILIATIONS, data["affiliations"])
    _insert_edges(db, INVOLVEMENTS, data["involvements"])
    _insert_edges(db, ORG_STRUCTURE, data.get("org_structure", []))

    challenges = (_build_challenges(db, data.get("game_challenges", []))
                  if with_challenges else 0)

    counts = {
        "persons": len(data["persons"]),
        "organizations": len(data["organizations"]),
        "events": len(data["events"]),
        "edges": sum(len(data.get(k, [])) for k in
                     ("relationships", "affiliations", "involvements", "org_structure")),
        "challenges": challenges,
    }
    logger.info(
        "Demo seed complete: %d persons, %d orgs, %d events, %d edges, "
        "%d challenges. Flush Redis for a clean slate: docker compose exec redis "
        "redis-cli FLUSHDB",
        counts["persons"], counts["organizations"], counts["events"],
        counts["edges"], counts["challenges"],
    )
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", default=os.getenv("DEMO_GRAPH_FILE", str(DEFAULT_FILE)),
                        help="path to the demo graph JSON")
    parser.add_argument("--skip-challenges", action="store_true",
                        help="do not compute game challenges")
    args = parser.parse_args()
    seed(pathlib.Path(args.file), with_challenges=not args.skip_challenges)


if __name__ == "__main__":
    main()
