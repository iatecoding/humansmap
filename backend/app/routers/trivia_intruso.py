"""Trivia — L'Intruso and Intruso Graph question generators."""

import random
from collections import Counter
from typing import Optional

from app.database import AFFILIATIONS, PERSONS, RELATIONSHIPS
from .trivia_common import (
    _fmt_brief,
    _extract_surname,
    _surname_giveaway,
    _occ_label,
    TRIVIA_MIN_STATEMENTS,
)


def _gen_intruso(
    pool: list[dict],
    used_qids: set[str],
    db,
    cat_occs: Optional[list[str]],
) -> Optional[dict]:
    """Generate an L'Intruso (Odd one out) question."""
    # Try org-based first (35%), then occupation-based (50%), event fallback via occupation
    roll = random.random()
    if roll < 0.40:
        result = _gen_intruso_org(pool, used_qids, db)
        if result:
            return result
    result = _gen_intruso_occupation(pool, used_qids)
    if result:
        return result
    # Final fallback: try org-based if occupation didn't work
    return _gen_intruso_org(pool, used_qids, db)


def _gen_intruso_occupation(pool: list[dict], used_qids: set[str]) -> Optional[dict]:
    """Occupation-based L'Intruso."""
    # Build occupation groups
    occ_groups: dict[str, list[dict]] = {}
    for p in pool:
        for occ in (p.get("occupations") or []):
            occ_groups.setdefault(occ, []).append(p)

    # Find occupations with ≥3 persons
    eligible = [(occ, persons) for occ, persons in occ_groups.items() if len(persons) >= 3]
    if not eligible:
        return None

    random.shuffle(eligible)
    for occ, members in eligible:
        random.shuffle(members)
        group3 = members[:3]
        group3_qids = {p["qid"] for p in group3}

        # Occupations shared by 2+ group members — intruso must not have any of these
        # (prevents visual confusion where intruso shares a secondary occupation with group)
        group_occ_counts = Counter(o for p in group3 for o in (p.get("occupations") or []))
        majority_occs = {o for o, cnt in group_occ_counts.items() if cnt >= 2}

        # Intruso: someone without this occupation AND without any majority secondary occupation
        intruso_candidates = [
            p for p in pool
            if p["qid"] not in group3_qids
            and occ not in (p.get("occupations") or [])
            and not (set(p.get("occupations") or []) & majority_occs)
        ]
        if not intruso_candidates:
            continue

        intruso = random.choice(intruso_candidates)
        if _surname_giveaway(group3, intruso):
            continue
        persons_out = group3 + [intruso]
        # Dedup safety
        seen: set[str] = set()
        persons_out = [p for p in persons_out if not (p["qid"] in seen or seen.add(p["qid"]))]
        if len(persons_out) < 4:
            continue
        random.shuffle(persons_out)

        occ_plural = _occ_label(occ, None, 'it_plural')
        occ_en_lbl = _occ_label(occ, None, 'en')
        templates_it = [
            f"Tre di loro sono {occ_plural}",
            f"Chi non è tra i {occ_plural}?",
            f"Uno di loro non è {_occ_label(occ, None, 'it_m')}. Chi?",
        ]
        templates_en = [
            f"Three of them are {occ_en_lbl}s",
            f"Who is not a {occ_en_lbl}?",
            f"One of them is not a {occ_en_lbl}. Who?",
        ]
        idx = random.randrange(len(templates_it))
        hint_it = templates_it[idx]
        hint_en = templates_en[idx]

        # Mark intruso as used (it's the "correct answer")
        used_qids.add(intruso["qid"])

        return {
            "type": "intruso",
            "hint_it": hint_it,
            "hint_en": hint_en,
            "connection_type": "occupation",
            "connection_entity": None,
            "persons": [
                {**_fmt_brief(p), "is_intruso": p["qid"] == intruso["qid"]}
                for p in persons_out
            ],
        }

    return None


def _gen_intruso_org(pool: list[dict], used_qids: set[str], db) -> Optional[dict]:
    """Organization-based L'Intruso."""
    pool_qids = [p["qid"] for p in pool]
    if not pool_qids:
        return None

    pool_from_ids = [f"persons/{q}" for q in pool_qids]

    cur = db.aql.execute(
        """
        FOR a IN @@aff
          FILTER a._from IN @pool_from_ids
          LET org = DOCUMENT(a._to)
          FILTER org != null AND (org.name_it != null OR org.name_en != null)
          COLLECT org_id = a._to, org_name_it = org.name_it, org_name_en = org.name_en
            AGGREGATE member_qids = UNIQUE(SPLIT(a._from, '/')[1])
          FILTER LENGTH(member_qids) >= 3
          SORT RAND()
          LIMIT 5
          RETURN {org_id, org_name_it, org_name_en, member_qids}
        """,
        bind_vars={"@aff": AFFILIATIONS, "pool_from_ids": pool_from_ids},
    )
    org_groups = list(cur)
    if not org_groups:
        return None

    pool_by_qid = {p["qid"]: p for p in pool}
    random.shuffle(org_groups)

    for group in org_groups:
        member_qids: list[str] = list(dict.fromkeys(group.get("member_qids") or []))  # dedup, preserve order
        members = [pool_by_qid[q] for q in member_qids if q in pool_by_qid]
        if len(members) < 3:
            continue

        random.shuffle(members)
        group3 = members[:3]
        group3_qids = {p["qid"] for p in group3}
        member_qid_set = set(member_qids)

        intruso_candidates = [
            p for p in pool
            if p["qid"] not in group3_qids
            and p["qid"] not in member_qid_set
        ]
        if not intruso_candidates:
            continue

        intruso = random.choice(intruso_candidates)
        if _surname_giveaway(group3, intruso):
            continue
        persons_out = group3 + [intruso]
        # Dedup safety
        seen: set[str] = set()
        persons_out = [p for p in persons_out if not (p["qid"] in seen or seen.add(p["qid"]))]
        if len(persons_out) < 4:
            continue
        random.shuffle(persons_out)

        org_it = group.get("org_name_it") or group.get("org_name_en") or ""
        org_en = group.get("org_name_en") or group.get("org_name_it") or ""

        used_qids.add(intruso["qid"])

        org_templates_it = [
            f"Tre di loro facevano parte di {org_it}",
            f"Chi non ha mai fatto parte di {org_it}?",
            f"Uno di loro non è legato a {org_it}. Chi?",
        ]
        org_templates_en = [
            f"Three of them were part of {org_en}",
            f"Who was never part of {org_en}?",
            f"One of them has no ties to {org_en}. Who?",
        ]
        oidx = random.randrange(len(org_templates_it))

        return {
            "type": "intruso",
            "hint_it": org_templates_it[oidx],
            "hint_en": org_templates_en[oidx],
            "connection_type": "organization",
            "connection_entity": {"name_it": org_it, "name_en": org_en},
            "persons": [
                {**_fmt_brief(p), "is_intruso": p["qid"] == intruso["qid"]}
                for p in persons_out
            ],
        }

    return None


def _gen_intruso_graph(
    pool: list[dict],
    used_qids: set[str],
    db,
) -> Optional[dict]:
    """Graph-variant L'Intruso: hub + real person-neighbors + 1 impostor.

    Visual challenge: identify the fake node injected into a real neighborhood.
    Stays lean — two targeted AQL queries per hub candidate.
    """
    candidates = [p for p in pool if p["qid"] not in used_qids]
    random.shuffle(candidates)

    for hub in candidates:
        hub_qid = hub["qid"]
        hub_pid = f"persons/{hub_qid}"

        # Fetch person-to-person neighbors — direct relationship edges first
        cur = db.aql.execute(
            """
            FOR e IN @@rels
              FILTER e._from == @pid OR e._to == @pid
              LET other_id = e._from == @pid ? e._to : e._from
              FILTER STARTS_WITH(other_id, 'persons/')
              LET other = DOCUMENT(other_id)
              FILTER other != null
                AND other.wikidata_statements_count >= @min_stmts
                AND (other.name_en != null OR other.name_it != null)
              SORT RAND()
              LIMIT 8
              RETURN DISTINCT {
                qid: other._key,
                name_it: other.name_it,
                name_en: other.name_en,
                gender: other.gender,
                occupations: other.occupations,
                wikidata_statements_count: other.wikidata_statements_count
              }
            """,
            bind_vars={
                "@rels": RELATIONSHIPS,
                "pid": hub_pid,
                "min_stmts": TRIVIA_MIN_STATEMENTS,
            },
        )
        neighbors = list(cur)

        # Fallback: co-members of the same org (affiliation 2-hop)
        if len(neighbors) < 4:
            cur = db.aql.execute(
                """
                FOR a1 IN @@aff
                  FILTER a1._from == @pid
                  LIMIT 3
                  FOR a2 IN @@aff
                    FILTER a2._to == a1._to AND a2._from != @pid
                    FILTER STARTS_WITH(a2._from, 'persons/')
                    LET other = DOCUMENT(a2._from)
                    FILTER other != null
                      AND other.wikidata_statements_count >= @min_stmts
                      AND (other.name_en != null OR other.name_it != null)
                    SORT RAND()
                    LIMIT 8
                    RETURN DISTINCT {
                      qid: other._key,
                      name_it: other.name_it,
                      name_en: other.name_en,
                      gender: other.gender,
                      occupations: other.occupations,
                      wikidata_statements_count: other.wikidata_statements_count
                    }
                """,
                bind_vars={
                    "@aff": AFFILIATIONS,
                    "pid": hub_pid,
                    "min_stmts": TRIVIA_MIN_STATEMENTS,
                },
            )
            neighbors = list(cur)

        # Dedup (affiliation fallback can return duplicates across orgs)
        seen_n: set[str] = set()
        neighbors = [n for n in neighbors if not (n["qid"] in seen_n or seen_n.add(n["qid"]))]

        if len(neighbors) < 4:
            continue

        # Pick 4 neighbors and swap one with an impostor
        random.shuffle(neighbors)
        real_neighbors = neighbors[:4]
        swap_target = random.choice(real_neighbors)

        # Pre-fetch ALL hub neighbors so we can exclude them without per-row traversal
        cur_all = db.aql.execute(
            """
            FOR _v IN 1..1 ANY @hub GRAPH 'human_graph'
              FILTER STARTS_WITH(_v._id, 'persons/')
              LIMIT 2000
              RETURN _v._id
            """,
            bind_vars={"hub": hub_pid},
        )
        all_neighbor_ids = list(cur_all)
        excluded_ids = list(set(all_neighbor_ids + [hub_pid] + [f"persons/{n['qid']}" for n in neighbors]))
        swap_gender = swap_target.get("gender")
        swap_occs = (swap_target.get("occupations") or [])[:5]

        # Select the impostor from the already bounded round pool. A corpus-wide
        # SORT RAND() here took over 60 seconds on the global dataset.
        excluded_qids = {entity_id.rsplit("/", 1)[-1] for entity_id in excluded_ids}

        def eligible_impostors(require_occupation: bool, require_gender: bool):
            return [
                person for person in pool
                if person["qid"] not in excluded_qids
                and (not require_gender or swap_gender is None or person.get("gender") == swap_gender)
                and (
                    not require_occupation
                    or not swap_occs
                    or bool(set(person.get("occupations") or []) & set(swap_occs))
                )
            ]

        impostors = eligible_impostors(require_occupation=True, require_gender=True)
        if not impostors:
            impostors = eligible_impostors(require_occupation=False, require_gender=True)
        if not impostors:
            impostors = eligible_impostors(require_occupation=False, require_gender=False)
        if not impostors:
            continue
        random.shuffle(impostors)

        impostor = impostors[0]
        hub_name = hub.get("name_it") or hub.get("name_en") or ""

        nodes = []
        for n in real_neighbors:
            if n["qid"] == swap_target["qid"]:
                nodes.append({**_fmt_brief(impostor), "is_impostor": True})
            else:
                nodes.append({**_fmt_brief(n), "is_impostor": False})
        random.shuffle(nodes)

        used_qids.add(hub_qid)
        hub_name_it = hub.get("name_it") or hub.get("name_en") or ""
        hub_name_en = hub.get("name_en") or hub.get("name_it") or ""
        return {
            "type": "intruso_graph",
            "hub": _fmt_brief(hub),
            "hub_name_it": hub_name_it,
            "hub_name_en": hub_name_en,
            "hint_it": f"Gli altri tre conoscono davvero {hub_name_it}",
            "hint_en": f"The other three have a real connection to {hub_name_en}",
            "nodes": nodes,
        }

    return None
