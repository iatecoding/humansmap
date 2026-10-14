"""Fix wrong occupation labels in existing DB records using direct AQL updates.

The original OCCUPATION_MAP had ~23 wrong Wikidata QIDs. Since the DB stores
label strings (not QIDs), and each wrong label came from exactly one wrong QID,
we can correct everything with label swaps — no external calls needed.

Run inside the backend container:
  docker compose exec backend python /app/scripts/fix_occupations.py [--dry-run]
"""

import logging
import sys

sys.path.insert(0, "/app")
from app.database import get_arango_client, get_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("fix_occupations")

DRY_RUN = "--dry-run" in sys.argv

# ── All swaps: wrong_label → correct_label ────────────────────────────────────
#
# Every entry here is because a Wikidata QID was wrong in the original map.
# The "correct" label is what those persons actually are.
#
# Where the actual occupation is broad (e.g. banker → businessperson, jazz
# musician → musician), we map to the closest existing label in our system.

SWAPS = {
    # Direct swaps — wrong QID was a completely different sport/profession
    "racing_driver":       "baseball_player",        # Q10871364 = baseball player
    "boxer":               "philanthropist",          # Q12362622 = philanthropist
    "cyclist":             "boxer",                   # Q11338576 = boxer
    "tennis_player":       "american_football_player",# Q19204627 = American football player
    "rugby_player":        "badminton_player",        # Q13141064 = badminton player
    "television_presenter":"record_producer",         # Q183945   = record producer
    "fashion_designer":    "political_scientist",     # Q1238570  = political scientist
    "architect":           "psychiatrist",            # Q211346   = psychiatrist
    "volleyball_player":   "essayist",                # Q11774202 = essayist
    "sprinter":            "field_hockey_player",     # Q10843263 = field hockey player
    "model":               "printmaker",              # Q11569986 = printmaker
    # television_actor ↔ university_teacher were swapped with each other
    "television_actor":    "university_teacher",      # Q1622272  = university teacher
    "university_teacher":  "businessperson",          # Q806798   = banker → closest label
    # Approximate mappings — actual occupation collapses into a broader category
    "theatre_director":    "musician",                # Q15981151 = jazz musician
    "clergyman":           "actor",                   # Q2259451  = stage actor
    "football_manager":    "engineer",                # Q13582652 = civil engineer
    "fencer":              "musician",                # Q1075651  = keyboardist
    "podcaster":           "philanthropist",          # Q15472169 = patron of the arts
    # Remove-only: Q2736 is the sport association football itself (3 persons), not an occupation
    # Q728425 = balloonist (193 persons) — too niche to add
}

REMOVE = [
    "swimmer",    # Q2736  = the sport itself, not a person occupation
    "diplomat",   # Q728425 = balloonist — 193 persons, too niche
]


def run_aql(db, query, bind=None):
    cursor = db.aql.execute(query, bind_vars=bind or {})
    return list(cursor)


BATCH_SIZE = 2000  # persons per AQL batch to avoid OOM


def count_label(db, label):
    return len(run_aql(db,
        "FOR p IN persons FILTER @l IN (p.occupations != null ? p.occupations : []) RETURN 1",
        {"l": label}
    ))


def get_keys_with_label(db, label):
    return run_aql(db,
        "FOR p IN persons FILTER @l IN (p.occupations != null ? p.occupations : []) RETURN p._key",
        {"l": label}
    )


def swap_in_batches(db, wrong, correct):
    keys = get_keys_with_label(db, wrong)
    for i in range(0, len(keys), BATCH_SIZE):
        batch = keys[i:i + BATCH_SIZE]
        db.aql.execute(
            """
            FOR key IN @keys
              LET p = DOCUMENT(CONCAT("persons/", key))
              FILTER p != null
              LET fixed = (
                FOR o IN (p.occupations != null ? p.occupations : [])
                  RETURN o == @wrong ? @correct : o
              )
              UPDATE key WITH { occupations: fixed } IN persons
            """,
            bind_vars={"keys": batch, "wrong": wrong, "correct": correct},
        )
        log.info("  ... %d / %d", min(i + BATCH_SIZE, len(keys)), len(keys))


def remove_in_batches(db, label):
    keys = get_keys_with_label(db, label)
    for i in range(0, len(keys), BATCH_SIZE):
        batch = keys[i:i + BATCH_SIZE]
        db.aql.execute(
            """
            FOR key IN @keys
              LET p = DOCUMENT(CONCAT("persons/", key))
              FILTER p != null
              LET fixed = (
                FOR o IN (p.occupations != null ? p.occupations : [])
                  FILTER o != @lbl
                  RETURN o
              )
              UPDATE key WITH { occupations: fixed } IN persons
            """,
            bind_vars={"keys": batch, "lbl": label},
        )
        log.info("  ... %d / %d", min(i + BATCH_SIZE, len(keys)), len(keys))


def main():
    client = get_arango_client()
    db = get_db(client)

    # ── Swaps ─────────────────────────────────────────────────────────────────
    for wrong, correct in SWAPS.items():
        count = count_label(db, wrong)
        log.info("SWAP  %-26s → %-26s (%d persons)", wrong, correct, count)
        if count and not DRY_RUN:
            swap_in_batches(db, wrong, correct)

    # ── Removals ──────────────────────────────────────────────────────────────
    for label in REMOVE:
        count = count_label(db, label)
        log.info("REMOVE %-26s                          (%d persons)", label, count)
        if count and not DRY_RUN:
            remove_in_batches(db, label)

    log.info("Done%s", " [DRY RUN — no changes written]" if DRY_RUN else "")


if __name__ == "__main__":
    main()
