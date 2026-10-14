"""Generate pre-computed game challenges.

Usage:
  All challenges:        docker compose exec backend python -m scripts.generate_challenges
  Nationality-specific:  docker compose exec backend python -m scripts.generate_challenges --nationality Italian --count 20
"""

import argparse
import logging
import random
import sys
import time

from arango import ArangoClient

# Allow imports from the app package
sys.path.insert(0, "/app")
from app.config import settings
from app.database import (
    PERSONS, ORGANIZATIONS, RELATIONSHIPS, AFFILIATIONS,
    GRAPH_NAME, GAME_CHALLENGES,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def get_db():
    client = ArangoClient(hosts=f"http://{settings.arango_host}:{settings.arango_port}")
    return client.db(settings.arango_db, username=settings.arango_username, password=settings.arango_password)


def get_candidates(db, nationality=None):
    """Find well-known, well-connected persons, optionally filtered by nationality."""
    nat_filter = "FILTER @nationality IN (p.nationalities != null ? p.nationalities : [])" if nationality else ""
    aql = f"""
        FOR p IN @@persons
            FILTER p.wikidata_statements_count > @min_stmts
            {nat_filter}
            LET deg = LENGTH(
                FOR v IN 1..1 ANY p @@relationships, @@affiliations
                    RETURN 1
            )
            FILTER deg >= @min_deg
            SORT p.wikidata_statements_count DESC
            RETURN {{qid: p._key, name_en: p.name_en, name_it: p.name_it, stmts: p.wikidata_statements_count, deg: deg}}
    """
    bind_vars = {
        "@persons": PERSONS,
        "@relationships": RELATIONSHIPS,
        "@affiliations": AFFILIATIONS,
        "min_stmts": settings.game_challenge_min_statements,
        "min_deg": settings.game_challenge_min_degree,
    }
    if nationality:
        bind_vars["nationality"] = nationality
    cursor = db.aql.execute(aql, bind_vars=bind_vars, ttl=600)
    candidates = list(cursor)
    label = f" nationality={nationality}" if nationality else ""
    logger.info("Found %d candidates (statements>%d, degree>=%d%s)",
                len(candidates), settings.game_challenge_min_statements,
                settings.game_challenge_min_degree, label)
    return candidates


def find_shortest_path(db, qid1, qid2):
    """Run SHORTEST_PATH and return the path with human-readable names, or None."""
    start = f"{PERSONS}/{qid1}"
    target = f"{PERSONS}/{qid2}"
    aql = f"""
        FOR v, e IN ANY SHORTEST_PATH
            '{start}' TO '{target}'
            GRAPH '{GRAPH_NAME}'
            RETURN {{qid: v._key, name_en: v.name_en, name_it: v.name_it,
                     collection: PARSE_IDENTIFIER(v._id).collection}}
    """
    try:
        cursor = db.aql.execute(aql, ttl=30)
        path = list(cursor)
    except Exception as e:
        logger.warning("Path query failed for %s -> %s: %s", qid1, qid2, e)
        return None

    if not path or len(path) < 2:
        return None
    return path


def existing_pairs(db):
    """Load already-generated challenge pairs to avoid duplicates."""
    col = db.collection(GAME_CHALLENGES)
    pairs = set()
    for doc in col.all():
        pair = tuple(sorted([doc["start_qid"], doc["target_qid"]]))
        pairs.add(pair)
    logger.info("Loaded %d existing challenge pairs", len(pairs))
    return pairs


def generate(db, candidates, existing, target_count):
    """Generate challenges until we reach target_count new ones."""
    generated = 0
    attempts = 0
    max_attempts = target_count * 20  # safety valve
    col = db.collection(GAME_CHALLENGES)
    qids = [c["qid"] for c in candidates]
    name_map = {c["qid"]: c.get("name_it") or c.get("name_en") or c["qid"] for c in candidates}

    while generated < target_count and attempts < max_attempts:
        attempts += 1
        q1, q2 = random.sample(qids, 2)
        pair_key = tuple(sorted([q1, q2]))
        if pair_key in existing:
            continue

        path = find_shortest_path(db, q1, q2)
        if path is None:
            continue

        path_len = len(path) - 1  # edges = vertices - 1
        if path_len < settings.game_challenge_min_path or path_len > settings.game_challenge_max_path:
            continue

        challenge = {
            "start_qid": q1,
            "target_qid": q2,
            "start_name": name_map.get(q1, q1),
            "target_name": name_map.get(q2, q2),
            "shortest_path_length": path_len,
            "shortest_path": [
                {"qid": node["qid"], "name": node.get("name_it") or node.get("name_en") or node["qid"]}
                for node in path
            ],
            "active": True,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

        col.insert(challenge)
        existing.add(pair_key)
        generated += 1

        if generated % 10 == 0:
            logger.info("Generated %d/%d challenges (%d attempts)", generated, target_count, attempts)

    logger.info("Done: %d challenges generated in %d attempts", generated, attempts)
    return generated


def count_nat_challenges(db, nationality):
    """Count existing challenges where both persons share the nationality."""
    cursor = db.aql.execute(
        """FOR c IN @@col FILTER c.active == true
           LET sp = DOCUMENT(CONCAT('persons/', c.start_qid))
           LET tp = DOCUMENT(CONCAT('persons/', c.target_qid))
           FILTER @nat IN (sp.nationalities != null ? sp.nationalities : [])
              AND @nat IN (tp.nationalities != null ? tp.nationalities : [])
           COLLECT WITH COUNT INTO n RETURN n""",
        bind_vars={"@col": GAME_CHALLENGES, "nat": nationality},
    )
    return list(cursor)[0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--nationality", default=None, help="Generate challenges for a specific nationality")
    parser.add_argument("--count", type=int, default=10, help="Minimum challenges to generate per nationality")
    parser.add_argument("--all-nationalities", action="store_true", help="Generate for all main nationalities")
    args = parser.parse_args()

    db = get_db()
    existing = existing_pairs(db)

    MAIN_NATIONALITIES = [
        "Italian", "French", "German", "British", "Spanish",
        "American", "Polish", "Russian", "Swiss", "Belgian", "Austrian", "Dutch",
    ]

    if args.all_nationalities:
        for nat in MAIN_NATIONALITIES:
            already = count_nat_challenges(db, nat)
            needed = max(0, args.count - already)
            if needed == 0:
                logger.info("[%s] Already have %d challenges. Skipping.", nat, already)
                continue
            logger.info("[%s] Have %d, need %d more.", nat, already, needed)
            candidates = get_candidates(db, nationality=nat)
            if len(candidates) < 5:
                logger.warning("[%s] Only %d candidates — skipping.", nat, len(candidates))
                continue
            generate(db, candidates, existing, needed)
        return

    if args.nationality:
        already = count_nat_challenges(db, args.nationality)
        needed = max(0, args.count - already)
        if needed == 0:
            logger.info("[%s] Already have %d challenges. Nothing to do.", args.nationality, already)
            return
        candidates = get_candidates(db, nationality=args.nationality)
        if len(candidates) < 5:
            logger.error("[%s] Too few candidates (%d).", args.nationality, len(candidates))
            sys.exit(1)
        logger.info("[%s] Generating %d challenges.", args.nationality, needed)
        generate(db, candidates, existing, needed)
        return

    # Default: generate general pool
    candidates = get_candidates(db)
    if len(candidates) < 10:
        logger.error("Too few candidates (%d). Check thresholds.", len(candidates))
        sys.exit(1)
    already = len(existing)
    needed = max(0, settings.game_challenge_deck_size - already)
    if needed == 0:
        logger.info("Already have %d challenges (target: %d). Nothing to do.", already, settings.game_challenge_deck_size)
        return
    logger.info("Need %d more challenges (have %d, target %d)", needed, already, settings.game_challenge_deck_size)
    generate(db, candidates, existing, needed)


if __name__ == "__main__":
    main()
