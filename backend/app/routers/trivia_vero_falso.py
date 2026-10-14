"""Trivia — Vero o Falso question generators."""

import random
from typing import Optional

from app.database import AFFILIATIONS, ORGANIZATIONS, RELATIONSHIPS
from .trivia_common import (
    _fmt_brief,
    _extract_surname,
    _occ_label,
    _article_it,
    _pick_wrong_occ,
    _first_sentence,
    _EDGE_CLUE_IT,
    _EDGE_CLUE_EN,
)

_INTERESTING_REL_TYPES = ["spouse", "parent", "father", "mother", "child"]

_REL_LABEL_IT = {
    "spouse": "Coniuge",
    "parent": "Genitore",
    "father": "Padre",
    "mother": "Madre",
    "child": "Figlio/a",
}
_REL_LABEL_EN = {
    "spouse": "Spouse",
    "parent": "Parent",
    "father": "Father",
    "mother": "Mother",
    "child": "Child",
}


def _gen_vero_falso(
    pool: list[dict],
    used_qids: set[str],
    db,
    difficulty: str,
    cat_occs: Optional[list[str]] = None,
) -> Optional[dict]:
    """Generate a Vero o Falso (True or False) question."""
    candidates = [p for p in pool if p["qid"] not in used_qids]
    random.shuffle(candidates)

    for person in candidates:
        qid = person["qid"]
        gender = person.get("gender")
        name = person.get("name_it") or person.get("name_en") or ""
        occs = person.get("occupations") or []
        if not occs:
            continue

        main_occ = occs[0]
        fact_it = _first_sentence(person.get("description_it") or person.get("description_en"))
        fact_en = _first_sentence(person.get("description_en") or person.get("description_it"))

        if difficulty == "hard":
            others_from_ids = [f"persons/{p['qid']}" for p in pool if p["qid"] != qid]
            result = _gen_vero_falso_hard(db, person, name, gender, fact_it, fact_en, others_from_ids)
            if result:
                used_qids.add(qid)
                return result
            # If hard fails, fall through to easy

        # Easy: varied statement types
        is_deceased = bool(person.get("death_date"))
        verb_it = "era" if is_deceased else "è"
        verb_en_past = "was" if is_deceased else "is"
        nato_a = "Nato" if gender != "female" else "Nata"

        # Pick a variant: occupation (60%), birth decade (40%)
        birth_date = person.get("birth_date")
        variant = "occupation"
        if birth_date and random.random() < 0.4:
            try:
                real_year = int(str(birth_date)[:4])
                if 1800 <= real_year <= 2010:
                    variant = "birth_decade"
            except Exception:
                pass

        is_true = random.random() > 0.5

        if variant == "birth_decade":
            real_decade = (real_year // 10) * 10
            if is_true:
                decade = real_decade
                stmt_it = f"{name}: {nato_a} negli anni '{str(decade)[-2:]}"
                stmt_en = f"{name} was born in the {decade}s"
                correction_it = None
                correction_en = None
            else:
                # Pick a wrong decade (offset by 2-4 decades)
                offset = random.choice([-30, -20, 20, 30])
                wrong_decade = real_decade + offset
                if wrong_decade < 1800:
                    wrong_decade = real_decade + abs(offset)
                stmt_it = f"{name}: {nato_a} negli anni '{str(wrong_decade)[-2:]}"
                stmt_en = f"{name} was born in the {wrong_decade}s"
                correction_it = f"In realtà, {nato_a.lower()} negli anni '{str(real_decade)[-2:]}"
                correction_en = f"Actually, born in the {real_decade}s"
        else:
            # Occupation variant
            if is_true:
                occ_it = _occ_label(main_occ, gender, "it")
                occ_en = _occ_label(main_occ, gender, "en")
                art = _article_it(occ_it, gender)
                stmt_it = f"{name} {verb_it} {art}{occ_it}"
                stmt_en = f"{name} {verb_en_past} a {occ_en}"
                correction_it = None
                correction_en = None
            else:
                wrong_occ = _pick_wrong_occ(main_occ, occs, cat_occs)
                if not wrong_occ:
                    continue
                wrong_it = _occ_label(wrong_occ, gender, "it")
                wrong_en = _occ_label(wrong_occ, gender, "en")
                art = _article_it(wrong_it, gender)
                stmt_it = f"{name} {verb_it} {art}{wrong_it}"
                stmt_en = f"{name} {verb_en_past} a {wrong_en}"
                real_it = _occ_label(main_occ, gender, "it")
                real_en = _occ_label(main_occ, gender, "en")
                real_art = _article_it(real_it, gender)
                correction_it = f"In realtà, {verb_it} {real_art}{real_it}"
                correction_en = f"Actually, {verb_en_past} a {real_en}"

        used_qids.add(qid)
        return {
            "type": "vero_falso",
            "statement_it": stmt_it,
            "statement_en": stmt_en,
            "answer": is_true,
            "person": _fmt_brief(person),
            "fact_it": fact_it or "",
            "fact_en": fact_en or "",
            "difficulty": "easy",
            "correction_it": correction_it,
            "correction_en": correction_en,
        }

    return None


def _gen_vero_falso_hard(
    db,
    person: dict,
    name: str,
    gender,
    fact_it,
    fact_en,
    pool_from_ids: Optional[list[str]] = None,
) -> Optional[dict]:
    """Edge-based Vero o Falso — uses real affiliations.

    For false statements, the wrong org is preferentially chosen from orgs
    that other pool members belong to (same nationality/category context),
    so the distractor stays thematically coherent instead of being a random
    org from any country or domain.
    """
    qid = person["qid"]
    pid = f"persons/{qid}"
    is_true = random.random() > 0.5

    cur = db.aql.execute(
        """
        FOR a IN @@aff
          FILTER a._from == @pid
          LET org = DOCUMENT(a._to)
          FILTER org != null AND (org.name_en != null OR org.name_it != null)
          SORT RAND()
          LIMIT 3
          RETURN {org_id: a._to, org_name_it: org.name_it, org_name_en: org.name_en}
        """,
        bind_vars={"@aff": AFFILIATIONS, "pid": pid},
    )
    affiliations = list(cur)
    if not affiliations:
        return None

    real = affiliations[0]
    org_it = real.get("org_name_it") or real.get("org_name_en") or ""
    org_en = real.get("org_name_en") or real.get("org_name_it") or ""

    is_deceased = bool(person.get("death_date"))
    verb_it = "era" if is_deceased else "è"
    verb_en = "was" if is_deceased else "is"

    if is_true:
        stmt_it = f"{name} {verb_it} membro di {org_it}"
        stmt_en = f"{name} {verb_en} a member of {org_en}"
        correction_it = None
        correction_en = None
    else:
        person_org_ids = [a["org_id"] for a in affiliations]
        wrong_orgs = []

        # First: try orgs connected to other pool members (same nationality/category)
        if pool_from_ids:
            cur2 = db.aql.execute(
                """
                FOR a IN @@aff
                  FILTER a._from IN @pool_from_ids
                  FILTER a._to NOT IN @person_orgs
                  LET org = DOCUMENT(a._to)
                  FILTER org != null AND (org.name_en != null OR org.name_it != null)
                  SORT RAND()
                  LIMIT 1
                  RETURN {name_it: org.name_it, name_en: org.name_en}
                """,
                bind_vars={
                    "@aff": AFFILIATIONS,
                    "pool_from_ids": pool_from_ids[:60],
                    "person_orgs": person_org_ids,
                },
            )
            wrong_orgs = list(cur2)

        # Fallback: any org in DB
        if not wrong_orgs:
            cur2 = db.aql.execute(
                """
                FOR org IN @@orgs
                  FILTER org._id NOT IN @person_orgs
                  FILTER org.name_en != null OR org.name_it != null
                  SORT RAND()
                  LIMIT 1
                  RETURN {name_it: org.name_it, name_en: org.name_en}
                """,
                bind_vars={"@orgs": ORGANIZATIONS, "person_orgs": person_org_ids},
            )
            wrong_orgs = list(cur2)

        if not wrong_orgs:
            return None
        wnit = wrong_orgs[0].get("name_it") or wrong_orgs[0].get("name_en") or ""
        wnen = wrong_orgs[0].get("name_en") or wrong_orgs[0].get("name_it") or ""
        stmt_it = f"{name} {verb_it} membro di {wnit}"
        stmt_en = f"{name} {verb_en} a member of {wnen}"
        correction_it = f"In realtà, {verb_it} membro di {org_it}"
        correction_en = f"Actually, {verb_en} a member of {org_en}"

    return {
        "type": "vero_falso",
        "statement_it": stmt_it,
        "statement_en": stmt_en,
        "answer": is_true,
        "person": _fmt_brief(person),
        "fact_it": fact_it or "",
        "fact_en": fact_en or "",
        "difficulty": "hard",
        "correction_it": correction_it,
        "correction_en": correction_en,
    }


def _gen_vero_falso_rel(
    pool: list[dict],
    used_qids: set[str],
    db,
) -> Optional[dict]:
    """Vero o Falso using personal relationships (spouse, sibling, parent/child).
    1 targeted AQL query per candidate — no extra overhead.
    """
    candidates = [p for p in pool if p["qid"] not in used_qids]
    random.shuffle(candidates)

    for person in candidates:
        qid = person["qid"]
        pid = f"persons/{qid}"

        cur = db.aql.execute(
            """
            FOR r IN @@rel
              FILTER r._from == @pid OR r._to == @pid
              FILTER r.type IN @types
              LET other_id = r._from == @pid ? r._to : r._from
              FILTER STARTS_WITH(other_id, 'persons/')
              LET other = DOCUMENT(other_id)
              FILTER other != null AND (other.name_en != null OR other.name_it != null)
              SORT RAND()
              LIMIT 4
              RETURN {type: r.type, qid: other._key, name_it: other.name_it, name_en: other.name_en}
            """,
            bind_vars={"@rel": RELATIONSHIPS, "pid": pid, "types": _INTERESTING_REL_TYPES},
        )
        rels = list(cur)
        if not rels:
            continue

        # Skip relationships where both persons share a surname — same giveaway as siblings
        rels = [r for r in rels if _extract_surname(person) != _extract_surname(r)]
        if not rels:
            continue

        rel = rels[0]
        rel_type = rel["type"]
        real_qids = {r["qid"] for r in rels}

        name_it = person.get("name_it") or person.get("name_en") or ""
        name_en = person.get("name_en") or person.get("name_it") or ""
        real_name_it = rel.get("name_it") or rel.get("name_en") or ""
        real_name_en = rel.get("name_en") or rel.get("name_it") or ""

        tpl_it = _EDGE_CLUE_IT.get(rel_type, "Collegato/a a {}")
        tpl_en = _EDGE_CLUE_EN.get(rel_type, "Connected to {}")

        is_true = random.random() < 0.5

        if is_true:
            stmt_it = f"{name_it}: {tpl_it.format(real_name_it)}"
            stmt_en = f"{name_en}: {tpl_en.format(real_name_en)}"
            correction_it = ""
            correction_en = ""
        else:
            false_pool = [p for p in pool if p["qid"] not in real_qids and p["qid"] != qid]
            if not false_pool:
                continue
            wrong = random.choice(false_pool)
            wrong_name_it = wrong.get("name_it") or wrong.get("name_en") or ""
            wrong_name_en = wrong.get("name_en") or wrong.get("name_it") or ""
            stmt_it = f"{name_it}: {tpl_it.format(wrong_name_it)}"
            stmt_en = f"{name_en}: {tpl_en.format(wrong_name_en)}"
            correction_it = f"In realtà: {tpl_it.format(real_name_it)}"
            correction_en = f"Actually: {tpl_en.format(real_name_en)}"

        used_qids.add(qid)
        return {
            "type": "vero_falso",
            "statement_it": stmt_it,
            "statement_en": stmt_en,
            "answer": is_true,
            "person": _fmt_brief(person),
            "related_person": {
                "qid": rel["qid"],
                "name_it": rel.get("name_it"),
                "name_en": rel.get("name_en"),
                "image_cached_path": f"/api/images/{rel['qid']}.jpg",
            },
            "rel_label_it": _REL_LABEL_IT.get(rel_type, "Collegato/a"),
            "rel_label_en": _REL_LABEL_EN.get(rel_type, "Connected"),
            "fact_it": "",
            "fact_en": "",
            "difficulty": "relationship",
            "correction_it": correction_it,
            "correction_en": correction_en,
        }

    return None
