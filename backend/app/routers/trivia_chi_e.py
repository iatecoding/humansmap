"""Trivia — Chi è? and Indovina chi question generators."""

import random
from typing import Optional

from app.database import AFFILIATIONS
from .trivia_common import (
    _fmt_brief,
    _occ_label,
    _era_clue,
    _get_connection_clue,
    _first_sentence,
    TRIVIA_MIN_STATEMENTS,
)


def _gen_chi_e(
    pool: list[dict],
    used_qids: set[str],
    db,
    cat_occs: Optional[list[str]],
) -> Optional[dict]:
    """Generate a Chi è? (Who is it?) question."""
    candidates = [p for p in pool if p["qid"] not in used_qids]
    random.shuffle(candidates)

    for person in candidates:
        qid = person["qid"]
        occs = person.get("occupations") or []
        if not occs:
            continue

        # Build clues — ordered from vaguest to most revealing
        clues = []
        main_occ = occs[0]
        gender = person.get("gender")

        # 1. Era (vaguest — just a decade)
        era_it, era_en = _era_clue(person.get("birth_date"))
        if era_it:
            clues.append({"type": "era", "text_it": era_it, "text_en": era_en})

        # 2. Occupation
        occ_it = _occ_label(main_occ, gender, "it")
        occ_en = _occ_label(main_occ, gender, "en")
        clues.append({"type": "occupation", "text_it": occ_it, "text_en": occ_en})

        # 3. Nationality (if available)
        nats = person.get("nationalities") or []
        if nats:
            nat = nats[0]
            clues.append({"type": "nationality", "text_it": nat, "text_en": nat})

        # 4. Connection to another person or org (the juicy clue)
        conn_it, conn_en = _get_connection_clue(db, qid)
        if conn_it:
            clues.append({"type": "connection", "text_it": conn_it, "text_en": conn_en})

        # 5. Description (most revealing — shown last)
        desc_it = _first_sentence(person.get("description_it"))
        desc_en = _first_sentence(person.get("description_en"))
        if desc_it or desc_en:
            clues.append({
                "type": "description",
                "text_it": desc_it or desc_en,
                "text_en": desc_en or desc_it,
            })

        # Need at least 1 clue (occupation always present if occs non-empty)
        # Pick distractors: same gender + same category + similar notability
        gender = person.get("gender")
        p_stmts = person.get("wikidata_statements_count") or 0
        dist_pool = [
            p for p in pool
            if p["qid"] != qid
            and (gender is None or p.get("gender") == gender)
            and (
                cat_occs is None
                or bool(set(p.get("occupations") or []) & set(cat_occs))
            )
        ]
        dist_pool.sort(key=lambda p: abs((p.get("wikidata_statements_count") or 0) - p_stmts))
        # Fallback without gender filter if not enough candidates
        if len(dist_pool) < 3:
            dist_pool = [
                p for p in pool
                if p["qid"] != qid
                and (cat_occs is None or bool(set(p.get("occupations") or []) & set(cat_occs)))
            ]
        if len(dist_pool) < 3:
            dist_pool = [p for p in pool if p["qid"] != qid]
        if len(dist_pool) < 3:
            continue

        random.shuffle(dist_pool)
        # Deduplicate by QID
        seen_qids: set[str] = {qid}
        distractors = []
        for d in dist_pool:
            if d["qid"] not in seen_qids:
                seen_qids.add(d["qid"])
                distractors.append(d)
            if len(distractors) == 3:
                break
        if len(distractors) < 3:
            continue

        options = [{**_fmt_brief(person), "correct": True}] + [
            {**_fmt_brief(d), "correct": False} for d in distractors
        ]
        random.shuffle(options)

        used_qids.add(qid)
        return {
            "type": "chi_e",
            "person": {
                **_fmt_brief(person),
                "description_it": desc_it,
                "description_en": desc_en,
            },
            "clues": clues,
            "options": options,
        }

    return None


def _indovina_chi_distractors(
    correct_qid: str,
    gender: Optional[str],
    cat_occs: Optional[list[str]],
    pool: list[dict],
    extra_excluded: Optional[set] = None,
    nationality: Optional[str] = None,
) -> Optional[list[dict]]:
    """Pick 3 deduplicated distractors with same gender + category + nationality."""
    excluded = {correct_qid} | (extra_excluded or set())

    def _nat_ok(p: dict) -> bool:
        if not nationality:
            return True
        nats = p.get("nationalities") or []
        return nationality in nats

    # Strictest: gender + category + nationality
    dist_pool = [
        p for p in pool
        if p["qid"] not in excluded
        and (gender is None or p.get("gender") == gender)
        and (cat_occs is None or bool(set(p.get("occupations") or []) & set(cat_occs)))
        and _nat_ok(p)
    ]
    # Drop category filter but keep nationality
    if len(dist_pool) < 3:
        dist_pool = [
            p for p in pool
            if p["qid"] not in excluded
            and (gender is None or p.get("gender") == gender)
            and _nat_ok(p)
        ]
    # Last resort: drop nationality too (should be rare)
    if len(dist_pool) < 3:
        dist_pool = [
            p for p in pool
            if p["qid"] not in excluded
            and (gender is None or p.get("gender") == gender)
        ]
    if len(dist_pool) < 3:
        return None
    random.shuffle(dist_pool)
    seen: set[str] = set(excluded)
    distractors = []
    for d in dist_pool:
        if d["qid"] not in seen:
            seen.add(d["qid"])
            distractors.append(d)
        if len(distractors) == 3:
            break
    return distractors if len(distractors) == 3 else None


def _gen_indovina_chi_member(
    pool: list[dict],
    used_qids: set[str],
    db,
    cat_occs: Optional[list[str]],
    nationality: Optional[str] = None,
) -> Optional[dict]:
    candidates = [
        p for p in pool
        if p["qid"] not in used_qids
        and (not nationality or nationality in (p.get("nationalities") or []))
    ]
    random.shuffle(candidates)

    for person in candidates:
        qid = person["qid"]
        pid = f"persons/{qid}"

        # Fetch multiple orgs so we can try them in order until one gives clean distractors
        cur = db.aql.execute(
            """
            FOR a IN @@aff
              FILTER a._from == @pid
              LET org = DOCUMENT(a._to)
              FILTER org != null AND (org.name_en != null OR org.name_it != null)
              SORT RAND()
              LIMIT 6
              RETURN {org_id: a._to, name_it: org.name_it, name_en: org.name_en}
            """,
            bind_vars={"@aff": AFFILIATIONS, "pid": pid},
        )
        affs = list(cur)
        if not affs:
            continue

        distractors = _indovina_chi_distractors(
            qid, person.get("gender"), cat_occs, pool,
            nationality=nationality,
        )
        if not distractors:
            continue

        dist_ids = [f"persons/{d['qid']}" for d in distractors]

        # Try each org until we find one where all 3 distractors are non-members
        chosen_org = None
        clean_distractors = None
        for org in affs:
            org_id = org["org_id"]
            cur2 = db.aql.execute(
                """
                FOR a IN @@aff
                  FILTER a._to == @org_id AND a._from IN @dist_ids
                  RETURN SPLIT(a._from, '/')[1]
                """,
                bind_vars={"@aff": AFFILIATIONS, "org_id": org_id, "dist_ids": dist_ids},
            )
            also_members = set(list(cur2))
            candidate_clean = [d for d in distractors if d["qid"] not in also_members]
            if len(candidate_clean) >= 3:
                chosen_org = org
                clean_distractors = candidate_clean[:3]
                break

        # If no org gave 3 clean distractors, skip this person entirely
        if chosen_org is None or clean_distractors is None:
            continue

        org_it = chosen_org.get("name_it") or chosen_org.get("name_en") or ""
        org_en = chosen_org.get("name_en") or chosen_org.get("name_it") or ""

        options = [{**_fmt_brief(person), "correct": True}] + [
            {**_fmt_brief(d), "correct": False} for d in clean_distractors
        ]
        random.shuffle(options)
        used_qids.add(qid)
        return {
            "type": "indovina_chi",
            "clue_type": "member",
            "clue_it": f"Fa parte di {org_it}",
            "clue_en": f"Is a member of {org_en}",
            "options": options,
        }

    return None


def _gen_indovina_chi(
    pool: list[dict],
    used_qids: set[str],
    db,
    cat_occs: Optional[list[str]],
    nationality: Optional[str] = None,
) -> Optional[dict]:
    """Generate an Indovina chi question (org-membership clue)."""
    return _gen_indovina_chi_member(pool, used_qids, db, cat_occs, nationality)
