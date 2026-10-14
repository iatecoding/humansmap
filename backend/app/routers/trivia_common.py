"""Trivia — shared constants, helpers, DB queries, and the main round endpoint.

All game-specific generators live in:
  trivia_chi_e.py      — Chi è? / Indovina chi
  trivia_vero_falso.py — Vero o Falso
  trivia_intruso.py    — L'Intruso / Intruso Graph
"""

import random
import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Query as Q, Request

from app.database import (
    get_arango_client, get_db,
    PERSONS, ORGANIZATIONS, EVENTS,
    AFFILIATIONS, RELATIONSHIPS, INVOLVEMENTS,
)
from app.rate_limit import limiter
from app.services.cache import get_cached, set_cached

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/trivia", tags=["trivia"])

# ─────────────────────── CONSTANTS ─────────────────────────────────────────────

TRIVIA_MIN_STATEMENTS = 30

TRIVIA_CATEGORIES: dict[str, list[str]] = {
    "sport": [
        "football_player", "cyclist", "tennis_player", "racing_driver",
        "basketball_player", "alpine_skier", "boxer", "sprinter",
        "football_manager", "water_polo_player", "swimmer",
        "volleyball_player", "fencer", "rugby_player",
    ],
    "politics": [
        "politician", "diplomat", "military_officer", "military_person",
        "trade_unionist",
    ],
    "music": ["singer", "composer", "musician", "pianist", "guitarist", "conductor"],
    "film_tv": [
        "actor", "film_director", "film_actor", "television_actor",
        "voice_actor", "film_producer", "television_presenter",
        "screenwriter", "theatre_director",
    ],
    "arts_culture": [
        "painter", "sculptor", "photographer", "fashion_designer",
        "architect", "writer", "poet", "journalist", "historian",
    ],
    "science": [
        "physicist", "biologist", "chemist", "mathematician",
        "scientist", "engineer", "botanist", "zoologist",
        "university_teacher",
    ],
    "business": ["businessperson", "entrepreneur", "publisher"],
}

# gender-aware labels: en / it_m / it_f / it_plural
OCCUPATION_LABELS: dict[str, dict[str, str]] = {
    "politician":           {"en": "politician",              "it_m": "politico",                   "it_f": "politica",                    "it_plural": "politici"},
    "actor":                {"en": "actor",                   "it_m": "attore",                     "it_f": "attrice",                     "it_plural": "attori"},
    "film_actor":           {"en": "actor",                   "it_m": "attore",                     "it_f": "attrice",                     "it_plural": "attori"},
    "television_actor":     {"en": "actor",                   "it_m": "attore",                     "it_f": "attrice",                     "it_plural": "attori"},
    "voice_actor":          {"en": "voice actor",             "it_m": "doppiatore",                 "it_f": "doppiatrice",                 "it_plural": "doppiatori"},
    "singer":               {"en": "singer",                  "it_m": "cantante",                   "it_f": "cantante",                    "it_plural": "cantanti"},
    "composer":             {"en": "composer",                "it_m": "compositore",                "it_f": "compositrice",                "it_plural": "compositori"},
    "musician":             {"en": "musician",                "it_m": "musicista",                  "it_f": "musicista",                   "it_plural": "musicisti"},
    "pianist":              {"en": "pianist",                 "it_m": "pianista",                   "it_f": "pianista",                    "it_plural": "pianisti"},
    "guitarist":            {"en": "guitarist",               "it_m": "chitarrista",                "it_f": "chitarrista",                 "it_plural": "chitarristi"},
    "conductor":            {"en": "conductor",               "it_m": "direttore d'orchestra",      "it_f": "direttrice d'orchestra",      "it_plural": "direttori d'orchestra"},
    "football_player":      {"en": "football player",         "it_m": "calciatore",                 "it_f": "calciatrice",                 "it_plural": "calciatori"},
    "cyclist":              {"en": "cyclist",                 "it_m": "ciclista",                   "it_f": "ciclista",                    "it_plural": "ciclisti"},
    "tennis_player":        {"en": "tennis player",           "it_m": "tennista",                   "it_f": "tennista",                    "it_plural": "tennisti"},
    "racing_driver":        {"en": "racing driver",           "it_m": "pilota automobilistico",     "it_f": "pilota automobilistica",      "it_plural": "piloti automobilistici"},
    "basketball_player":    {"en": "basketball player",       "it_m": "cestista",                   "it_f": "cestista",                    "it_plural": "cestisti"},
    "alpine_skier":         {"en": "alpine skier",            "it_m": "sciatore alpino",            "it_f": "sciatrice alpina",            "it_plural": "sciatori alpini"},
    "boxer":                {"en": "boxer",                   "it_m": "pugile",                     "it_f": "pugile",                      "it_plural": "pugili"},
    "sprinter":             {"en": "sprinter",                "it_m": "velocista",                  "it_f": "velocista",                   "it_plural": "velocisti"},
    "football_manager":     {"en": "football manager",        "it_m": "allenatore di calcio",       "it_f": "allenatrice di calcio",       "it_plural": "allenatori di calcio"},
    "water_polo_player":    {"en": "water polo player",       "it_m": "pallanuotista",              "it_f": "pallanuotista",               "it_plural": "pallanuotisti"},
    "swimmer":              {"en": "swimmer",                 "it_m": "nuotatore",                  "it_f": "nuotatrice",                  "it_plural": "nuotatori"},
    "volleyball_player":    {"en": "volleyball player",       "it_m": "pallavolista",               "it_f": "pallavolista",                "it_plural": "pallavolisti"},
    "fencer":               {"en": "fencer",                  "it_m": "schermidore",                "it_f": "schermidrice",                "it_plural": "schermidori"},
    "rugby_player":         {"en": "rugby player",            "it_m": "rugbista",                   "it_f": "rugbista",                    "it_plural": "rugbisti"},
    "diplomat":             {"en": "diplomat",                "it_m": "diplomatico",                "it_f": "diplomatica",                 "it_plural": "diplomatici"},
    "military_officer":     {"en": "military officer",        "it_m": "ufficiale militare",         "it_f": "ufficiale militare",          "it_plural": "ufficiali militari"},
    "military_person":      {"en": "military person",         "it_m": "militare",                   "it_f": "militare",                    "it_plural": "militari"},
    "trade_unionist":       {"en": "trade unionist",          "it_m": "sindacalista",               "it_f": "sindacalista",                "it_plural": "sindacalisti"},
    "film_director":        {"en": "film director",           "it_m": "regista",                    "it_f": "regista",                     "it_plural": "registi"},
    "film_producer":        {"en": "film producer",           "it_m": "produttore",                 "it_f": "produttrice",                 "it_plural": "produttori"},
    "television_presenter": {"en": "television presenter",    "it_m": "conduttore",                 "it_f": "conduttrice",                 "it_plural": "conduttori"},
    "screenwriter":         {"en": "screenwriter",            "it_m": "sceneggiatore",              "it_f": "sceneggiatrice",              "it_plural": "sceneggiatori"},
    "theatre_director":     {"en": "theatre director",        "it_m": "regista teatrale",           "it_f": "regista teatrale",            "it_plural": "registi teatrali"},
    "painter":              {"en": "painter",                 "it_m": "pittore",                    "it_f": "pittrice",                    "it_plural": "pittori"},
    "sculptor":             {"en": "sculptor",                "it_m": "scultore",                   "it_f": "scultrice",                   "it_plural": "scultori"},
    "photographer":         {"en": "photographer",            "it_m": "fotografo",                  "it_f": "fotografa",                   "it_plural": "fotografi"},
    "fashion_designer":     {"en": "fashion designer",        "it_m": "stilista",                   "it_f": "stilista",                    "it_plural": "stilisti"},
    "architect":            {"en": "architect",               "it_m": "architetto",                 "it_f": "architetta",                  "it_plural": "architetti"},
    "writer":               {"en": "writer",                  "it_m": "scrittore",                  "it_f": "scrittrice",                  "it_plural": "scrittori"},
    "poet":                 {"en": "poet",                    "it_m": "poeta",                      "it_f": "poetessa",                    "it_plural": "poeti"},
    "journalist":           {"en": "journalist",              "it_m": "giornalista",                "it_f": "giornalista",                 "it_plural": "giornalisti"},
    "historian":            {"en": "historian",               "it_m": "storico",                    "it_f": "storica",                     "it_plural": "storici"},
    "physicist":            {"en": "physicist",               "it_m": "fisico",                     "it_f": "fisica",                      "it_plural": "fisici"},
    "biologist":            {"en": "biologist",               "it_m": "biologo",                    "it_f": "biologa",                     "it_plural": "biologi"},
    "chemist":              {"en": "chemist",                 "it_m": "chimico",                    "it_f": "chimica",                     "it_plural": "chimici"},
    "mathematician":        {"en": "mathematician",           "it_m": "matematico",                 "it_f": "matematica",                  "it_plural": "matematici"},
    "scientist":            {"en": "scientist",               "it_m": "scienziato",                 "it_f": "scienziata",                  "it_plural": "scienziati"},
    "engineer":             {"en": "engineer",                "it_m": "ingegnere",                  "it_f": "ingegnere",                   "it_plural": "ingegneri"},
    "botanist":             {"en": "botanist",                "it_m": "botanico",                   "it_f": "botanica",                    "it_plural": "botanici"},
    "zoologist":            {"en": "zoologist",               "it_m": "zoologo",                    "it_f": "zoologa",                     "it_plural": "zoologi"},
    "university_teacher":   {"en": "university teacher",      "it_m": "professore universitario",   "it_f": "professoressa universitaria", "it_plural": "professori universitari"},
    "businessperson":       {"en": "businessperson",          "it_m": "imprenditore",               "it_f": "imprenditrice",               "it_plural": "imprenditori"},
    "entrepreneur":         {"en": "entrepreneur",            "it_m": "imprenditore",               "it_f": "imprenditrice",               "it_plural": "imprenditori"},
    "publisher":            {"en": "publisher",               "it_m": "editore",                    "it_f": "editrice",                    "it_plural": "editori"},
    "judge":                {"en": "judge",                   "it_m": "giudice",                    "it_f": "giudice",                     "it_plural": "giudici"},
    "lawyer":               {"en": "lawyer",                  "it_m": "avvocato",                   "it_f": "avvocatessa",                 "it_plural": "avvocati"},
    "criminal":             {"en": "criminal",                "it_m": "criminale",                  "it_f": "criminale",                   "it_plural": "criminali"},
    "baseball_player":          {"en": "baseball player",              "it_m": "giocatore di baseball",             "it_f": "giocatrice di baseball",             "it_plural": "giocatori di baseball"},
    "philanthropist":           {"en": "philanthropist",               "it_m": "filantropo",                        "it_f": "filantropa",                         "it_plural": "filantropi"},
    "american_football_player": {"en": "American football player",     "it_m": "giocatore di football americano",   "it_f": "giocatrice di football americano",   "it_plural": "giocatori di football americano"},
    "badminton_player":         {"en": "badminton player",             "it_m": "giocatore di badminton",            "it_f": "giocatrice di badminton",            "it_plural": "giocatori di badminton"},
    "record_producer":          {"en": "record producer",              "it_m": "produttore discografico",           "it_f": "produttrice discografica",           "it_plural": "produttori discografici"},
    "political_scientist":      {"en": "political scientist",          "it_m": "politologo",                        "it_f": "politologa",                         "it_plural": "politologi"},
    "printmaker":               {"en": "printmaker",                   "it_m": "incisore",                          "it_f": "incisoressa",                        "it_plural": "incisori"},
    "essayist":                 {"en": "essayist",                     "it_m": "saggista",                          "it_f": "saggista",                           "it_plural": "saggisti"},
    "psychiatrist":             {"en": "psychiatrist",                  "it_m": "psichiatra",                        "it_f": "psichiatra",                         "it_plural": "psichiatri"},
    "field_hockey_player":      {"en": "field hockey player",          "it_m": "giocatore di hockey su prato",      "it_f": "giocatrice di hockey su prato",      "it_plural": "giocatori di hockey su prato"},
    "economist":                {"en": "economist",                    "it_m": "economista",                        "it_f": "economista",                         "it_plural": "economisti"},
    "teacher":                  {"en": "teacher",                      "it_m": "insegnante",                        "it_f": "insegnante",                         "it_plural": "insegnanti"},
    "philosopher":              {"en": "philosopher",                  "it_m": "filosofo",                          "it_f": "filosofa",                           "it_plural": "filosofi"},
    "sociologist":              {"en": "sociologist",                  "it_m": "sociologo",                         "it_f": "sociologa",                          "it_plural": "sociologi"},
    "model":                    {"en": "model",                        "it_m": "modello",                           "it_f": "modella",                            "it_plural": "modelli"},
    "civil_servant":            {"en": "civil servant",                "it_m": "funzionario pubblico",              "it_f": "funzionaria pubblica",               "it_plural": "funzionari pubblici"},
    "banker":                   {"en": "banker",                       "it_m": "banchiere",                         "it_f": "banchiera",                          "it_plural": "banchieri"},
    "public_official":          {"en": "public official",              "it_m": "funzionario pubblico",              "it_f": "funzionaria pubblica",               "it_plural": "funzionari pubblici"},
    "podcaster":                {"en": "podcaster",                    "it_m": "podcaster",                         "it_f": "podcaster",                          "it_plural": "podcaster"},
    "activist":                 {"en": "activist",                     "it_m": "attivista",                         "it_f": "attivista",                          "it_plural": "attivisti"},
    "consultant":               {"en": "consultant",                   "it_m": "consulente",                        "it_f": "consulente",                         "it_plural": "consulenti"},
    "executive":                {"en": "executive",                    "it_m": "dirigente",                         "it_f": "dirigente",                          "it_plural": "dirigenti"},
    "financier":                {"en": "financier",                    "it_m": "finanziere",                        "it_f": "finanziera",                         "it_plural": "finanzieri"},
    "astronaut":                {"en": "astronaut",                    "it_m": "astronauta",                        "it_f": "astronauta",                         "it_plural": "astronauti"},
    "clergyman":                {"en": "clergyman",                    "it_m": "religioso",                         "it_f": "religiosa",                          "it_plural": "religiosi"},
    "keyboardist":              {"en": "keyboardist",                  "it_m": "tastierista",                       "it_f": "tastierista",                        "it_plural": "tastieristi"},
    "lobbyist":                 {"en": "lobbyist",                     "it_m": "lobbista",                          "it_f": "lobbista",                           "it_plural": "lobbisti"},
    "investor":                 {"en": "investor",                     "it_m": "investitore",                       "it_f": "investitrice",                       "it_plural": "investitori"},
    "intelligence_officer":     {"en": "intelligence officer",         "it_m": "agente dei servizi segreti",        "it_f": "agente dei servizi segreti",         "it_plural": "agenti dei servizi segreti"},
    "board_member":             {"en": "board member",                 "it_m": "membro del consiglio",              "it_f": "membro del consiglio",               "it_plural": "membri del consiglio"},
}

# ─────────────────────── HELPERS ────────────────────────────────────────────

def _get_db():
    client = get_arango_client()
    return get_db(client)


def _fmt_brief(p: dict) -> dict:
    """Return minimal person dict for trivia responses."""
    qid = p.get("qid") or p.get("_key")
    return {
        "qid": qid,
        "name_it": p.get("name_it") or p.get("name_en"),
        "name_en": p.get("name_en"),
        "image_cached_path": f"/api/images/{qid}.jpg" if qid else None,
        "gender": p.get("gender"),
        "birth_date": p.get("birth_date"),
        "death_date": p.get("death_date"),
        "occupations": (p.get("occupations") or [])[:3],
    }


def _extract_surname(p: dict) -> Optional[str]:
    """Return the last word of a person's name (rough surname heuristic)."""
    name = p.get("name_it") or p.get("name_en") or ""
    parts = name.strip().split()
    return parts[-1].lower() if parts else None


def _surname_giveaway(group3: list[dict], intruso: dict) -> bool:
    """Return True if the surname pattern trivially reveals the intruso.

    Triggered when ≥2 of the 3 connected persons share a surname AND the
    intruso does not share that surname — making the odd one out obvious
    without needing any other knowledge.
    """
    intruso_surname = _extract_surname(intruso)
    group_surnames = [_extract_surname(p) for p in group3]
    # Count surname frequencies in the group
    surname_counts: dict[str, int] = {}
    for s in group_surnames:
        if s:
            surname_counts[s] = surname_counts.get(s, 0) + 1
    # If any surname appears ≥2 times in the group AND the intruso lacks it → giveaway
    for surname, count in surname_counts.items():
        if count >= 2 and intruso_surname != surname:
            return True
    return False


def _first_sentence(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    for sep in (". ", ".\n", "! ", "? "):
        idx = text.find(sep)
        if idx != -1:
            return text[: idx + 1].strip()
    return text[:250].strip() or None


def _occ_label(occ: str, gender: Optional[str], field: str) -> str:
    """Return gender-aware Italian or English occupation label."""
    lbl = OCCUPATION_LABELS.get(occ)
    if not lbl:
        if occ not in OCCUPATION_LABELS:
            logger.warning("Occupation not in OCCUPATION_LABELS map: %s", occ)
        return occ  # raw fallback
    if field == "en":
        return lbl["en"]
    if field == "it_plural":
        return lbl.get("it_plural", lbl.get("it_m", occ))
    # Italian singular
    if gender == "female":
        return lbl.get("it_f", lbl.get("it_m", occ))
    return lbl.get("it_m", occ)


def _article_it(word: str, gender: Optional[str]) -> str:
    """Return Italian indefinite article with trailing space: 'un ', 'una ', or 'un\\'.'"""
    if gender == "female":
        first = word[0].lower() if word else ""
        if first in "aeiouàèéìíîòóùú":
            return "un'"  # elision: directly prepended, no space needed
        return "una "
    return "un "


def _era_clue(birth_date) -> tuple[Optional[str], Optional[str]]:
    if not birth_date:
        return None, None
    try:
        year = int(str(birth_date)[:4])
        if year < 1800 or year > 2010:
            return None, None
        decade = (year // 10) * 10
        short = str(decade)[-2:]
        return f"Anni '{short}", f"{decade}s"
    except Exception:
        return None, None


def _category_occs(category: Optional[str]) -> Optional[list[str]]:
    if not category or category.lower() in ("tutte", "all", ""):
        return None
    return TRIVIA_CATEGORIES.get(category.lower())


def _pick_wrong_occ(
    correct_occ: str,
    person_occs: list[str],
    cat_occs: Optional[list[str]] = None,
) -> Optional[str]:
    """Pick a plausible but wrong occupation.

    When a category filter is active, prefer occupations from that same category
    so the false statement stays thematically coherent (e.g. 'cantante' not 'politico'
    in a music round).  Falls back to any other-domain occupation if needed.
    """
    person_occ_set = set(person_occs)

    # Prefer: different occupation within the active category
    if cat_occs:
        same_cat_wrong = [o for o in cat_occs if o not in person_occ_set]
        if same_cat_wrong:
            return random.choice(same_cat_wrong)

    # Fallback: different-domain occupation (original logic)
    correct_cats = [cat for cat, occs in TRIVIA_CATEGORIES.items() if correct_occ in occs]
    excluded: set[str] = set(person_occs)
    for cat in correct_cats:
        excluded.update(TRIVIA_CATEGORIES[cat])
    candidates = [o for o in OCCUPATION_LABELS if o not in excluded]
    if not candidates:
        candidates = [o for o in OCCUPATION_LABELS if o not in person_occ_set]
    return random.choice(candidates) if candidates else None


# ─────────────────── DB QUERIES ─────────────────────────────────────────────

def _query_pool(
    db,
    nat: Optional[str],
    cat_occs: Optional[list[str]],
    pool_size: int = 80,
    living: Optional[bool] = None,
    difficulty: Optional[str] = None,
) -> list[dict]:
    """Notability-weighted random pool of qualifying persons."""
    # Easy: heavily favor famous persons; Hard: flatten weights for obscure picks
    if difficulty == "hard":
        weight_expr = """
          LET w = p.wikidata_statements_count > 500 ? 2 :
                  (p.wikidata_statements_count > 150 ? 1.5 : 1)
        """
    elif difficulty == "easy":
        weight_expr = """
          LET w = p.wikidata_statements_count > 500 ? 10 :
                  (p.wikidata_statements_count > 150 ? 4 : 1)
        """
    else:
        weight_expr = """
          LET w = p.wikidata_statements_count > 500 ? 6 :
                  (p.wikidata_statements_count > 150 ? 3 : 1)
        """
    aql = """
        LET candidates = (
          FOR p IN @@persons
          FILTER p.wikidata_statements_count >= @min_stmts
          FILTER @nat == null OR @nat IN (p.nationalities != null ? p.nationalities : [])
          FILTER @living == null OR (@living == true ? p.death_date == null : p.death_date != null)
          FILTER @cat_occs == null OR LENGTH(INTERSECTION(
            p.occupations != null ? p.occupations : [], @cat_occs)) > 0
          SORT p.wikidata_statements_count DESC
          LIMIT @candidate_pool_size
          RETURN p
        )
        FOR p IN candidates
          """ + weight_expr + """
          SORT RAND() * w DESC
          LIMIT @pool_size
          RETURN {
            qid: p._key,
            name_it: p.name_it,
            name_en: p.name_en,
            gender: p.gender,
            occupations: p.occupations,
            nationalities: p.nationalities,
            description_it: p.description_it,
            description_en: p.description_en,
            birth_date: p.birth_date,
            death_date: p.death_date,
            image_url: p.image_url,
            wikidata_statements_count: p.wikidata_statements_count
          }
        """
    cursor = db.aql.execute(
        aql,
        bind_vars={
            "@persons": PERSONS,
            "min_stmts": TRIVIA_MIN_STATEMENTS,
            "nat": nat,
            "cat_occs": cat_occs,
            "pool_size": pool_size,
            "candidate_pool_size": max(pool_size * 20, 500),
            "living": living,
        },
    )
    return list(cursor)


_EDGE_CLUE_IT = {
    "spouse": "Sposato/a con {}",
    "parent": "Genitore di {}",
    "father": "Padre di {}",
    "mother": "Madre di {}",
    "child": "Figlio/a di {}",
    "sibling": "Fratello/sorella di {}",
    "relative": "Parente di {}",
}
_EDGE_CLUE_EN = {
    "spouse": "Married to {}",
    "parent": "Parent of {}",
    "father": "Father of {}",
    "mother": "Mother of {}",
    "child": "Child of {}",
    "sibling": "Sibling of {}",
    "relative": "Related to {}",
}
_AFF_CLUE_IT = {
    "employee": "Ha lavorato per {}",
    "position_held": "Ha ricoperto un ruolo in {}",
    "member_of": "Membro di {}",
    "educated_at": "Ha studiato a {}",
    "founder": "Fondatore di {}",
    "chief_executive": "Dirigente di {}",
    "board_member": "Nel consiglio di {}",
    "chairperson": "Presidente di {}",
    "owner": "Proprietario di {}",
}
_AFF_CLUE_EN = {
    "employee": "Worked at {}",
    "position_held": "Held a position at {}",
    "member_of": "Member of {}",
    "educated_at": "Studied at {}",
    "founder": "Founded {}",
    "chief_executive": "Executive at {}",
    "board_member": "Board member of {}",
    "chairperson": "Chairperson of {}",
    "owner": "Owner of {}",
}


def _get_connection_clue(db, qid: str) -> tuple[Optional[str], Optional[str]]:
    """Return (text_it, text_en) for a real graph connection, or (None, None)."""
    pid = f"persons/{qid}"

    # Try relationships first — more interesting clues (family, spouse)
    cur = db.aql.execute(
        """
        FOR r IN @@rel
          FILTER r._from == @pid OR r._to == @pid
          LET other_id = r._from == @pid ? r._to : r._from
          FILTER STARTS_WITH(other_id, 'persons/')
          LET other = DOCUMENT(other_id)
          FILTER other != null AND (other.name_en != null OR other.name_it != null)
          SORT RAND()
          LIMIT 1
          RETURN {type: r.type, name_it: other.name_it, name_en: other.name_en}
        """,
        bind_vars={"@rel": RELATIONSHIPS, "pid": pid},
    )
    rows = list(cur)
    if rows:
        r = rows[0]
        nit = r.get("name_it") or r.get("name_en") or ""
        nen = r.get("name_en") or r.get("name_it") or ""
        edge_type = r.get("type", "")
        tpl_it = _EDGE_CLUE_IT.get(edge_type, "Collegato/a a {}")
        tpl_en = _EDGE_CLUE_EN.get(edge_type, "Connected to {}")
        return tpl_it.format(nit), tpl_en.format(nen)

    # Fall back to affiliations
    cur = db.aql.execute(
        """
        FOR a IN @@aff
          FILTER a._from == @pid
          LET org = DOCUMENT(a._to)
          FILTER org != null AND (org.name_en != null OR org.name_it != null)
          SORT RAND()
          LIMIT 1
          RETURN {type: a.type, name_it: org.name_it, name_en: org.name_en}
        """,
        bind_vars={"@aff": AFFILIATIONS, "pid": pid},
    )
    rows = list(cur)
    if rows:
        r = rows[0]
        nit = r.get("name_it") or r.get("name_en") or ""
        nen = r.get("name_en") or r.get("name_it") or ""
        aff_type = r.get("type", "")
        tpl_it = _AFF_CLUE_IT.get(aff_type, "Membro di {}")
        tpl_en = _AFF_CLUE_EN.get(aff_type, "Member of {}")
        return tpl_it.format(nit), tpl_en.format(nen)

    return None, None


def _shuffle_no_3_consecutive(questions: list[dict]) -> list[dict]:
    """Shuffle ensuring no 3 consecutive questions of the same type."""
    if len(questions) <= 2:
        random.shuffle(questions)
        return questions
    for _ in range(30):
        random.shuffle(questions)
        valid = True
        for i in range(2, len(questions)):
            if questions[i]["type"] == questions[i - 1]["type"] == questions[i - 2]["type"]:
                valid = False
                break
        if valid:
            return questions
    return questions  # best effort


# ─────────────────────── ENDPOINTS ──────────────────────────────────────────

SCOPE_NATIONALITIES = {
    "AD","AL","AM","AT","AZ","BA","BE","BG","BY","CH","CY","CZ","DE","DK","EE",
    "ES","FI","FO","FR","GB","GE","GR","HR","HU","IE","IS","IT","LI","LT","LU",
    "LV","MC","MD","ME","MK","MT","NL","NO","PL","PT","RO","RS","RU","SE","SI",
    "SK","SM","TR","UA","VA","XK",
    "US","CA",
}


@router.get("/nationalities")
@limiter.limit("60/minute")
async def get_trivia_nationalities(request: Request):
    """Return in-scope nationalities with ≥20 persons above the notability threshold."""
    cache_key = "trivia:nationalities:v2"
    cached = get_cached(cache_key)
    if cached is not None:
        return cached

    db = _get_db()
    cursor = db.aql.execute(
        """
        FOR p IN @@persons
          FILTER p.wikidata_statements_count >= @min_stmts
          FILTER p.nationalities != null AND LENGTH(p.nationalities) > 0
          FOR nat IN p.nationalities
            FILTER nat IN @scope
            COLLECT nationality = nat WITH COUNT INTO cnt
            FILTER cnt >= 20
            SORT cnt DESC
            RETURN nationality
        """,
        bind_vars={
            "@persons": PERSONS,
            "min_stmts": TRIVIA_MIN_STATEMENTS,
            "scope": list(SCOPE_NATIONALITIES),
        },
    )
    result = {"nationalities": list(cursor)}
    set_cached(cache_key, result, ttl_seconds=3600)
    return result


@router.get("/round")
@limiter.limit("30/minute")
async def get_round(
    request: Request,
    category: Optional[str] = Q(None),
    nationality: Optional[str] = Q(None),
    living: Optional[bool] = Q(None),
    difficulty: Optional[str] = Q(None),
):
    """Generate a full 6-question mixed trivia round."""
    from app.routers.trivia_chi_e import _gen_chi_e, _gen_indovina_chi
    from app.routers.trivia_vero_falso import _gen_vero_falso, _gen_vero_falso_rel
    from app.routers.trivia_intruso import _gen_intruso, _gen_intruso_graph

    db = _get_db()
    cat_occs = _category_occs(category)
    # Validate difficulty
    if difficulty and difficulty not in ("easy", "hard"):
        difficulty = None

    # Build pool with progressive fallbacks if filtered pool is too small.
    # Nationality is always respected first — only drop as absolute last resort.
    pool = _query_pool(db, nationality, cat_occs, pool_size=80, living=living, difficulty=difficulty)
    if len(pool) < 8 and cat_occs:
        pool = _query_pool(db, nationality, None, pool_size=80, living=living, difficulty=difficulty)
    if len(pool) < 8:
        pool = _query_pool(db, None, None, pool_size=80)

    used_qids: set[str] = set()
    questions: list[dict] = []

    def _append(q):
        if q:
            questions.append(q)

    def _first(*fns):
        for fn in fns:
            q = fn()
            if q:
                return q
        return None

    if difficulty == "easy":
        # 6-question easy round: progressive reveals, relationship V/F, clear intruso
        _append(_gen_chi_e(pool, used_qids, db, cat_occs))
        _append(_first(
            lambda: _gen_vero_falso_rel(pool, used_qids, db),
            lambda: _gen_vero_falso(pool, used_qids, db, "easy", cat_occs),
        ))
        _append(_gen_intruso(pool, used_qids, db, cat_occs))
        _append(_gen_indovina_chi(pool, used_qids, db, cat_occs, nationality))
        _append(_gen_chi_e(pool, used_qids, db, cat_occs))
        _append(_first(
            lambda: _gen_vero_falso_rel(pool, used_qids, db),
            lambda: _gen_vero_falso(pool, used_qids, db, "easy", cat_occs),
            lambda: _gen_chi_e(pool, used_qids, db, cat_occs),
        ))

    elif difficulty == "hard":
        # 6-question hard round: relationship V/F, graph intruso, harder chi_e
        _append(_gen_chi_e(pool, used_qids, db, cat_occs))
        _append(_first(
            lambda: _gen_vero_falso_rel(pool, used_qids, db),
            lambda: _gen_vero_falso(pool, used_qids, db, "hard", cat_occs),
        ))
        _append(_first(
            lambda: _gen_intruso_graph(pool, used_qids, db),
            lambda: _gen_intruso(pool, used_qids, db, cat_occs),
        ))
        _append(_gen_indovina_chi(pool, used_qids, db, cat_occs, nationality))
        _append(_gen_vero_falso(pool, used_qids, db, "hard", cat_occs))
        _append(_first(
            lambda: _gen_intruso_graph(pool, used_qids, db),
            lambda: _gen_chi_e(pool, used_qids, db, cat_occs),
            lambda: _gen_vero_falso_rel(pool, used_qids, db),
        ))

    else:
        # Default 6-question mix
        _append(_gen_chi_e(pool, used_qids, db, cat_occs))
        _append(_first(
            lambda: _gen_vero_falso_rel(pool, used_qids, db),
            lambda: _gen_vero_falso(pool, used_qids, db, "easy", cat_occs),
        ))
        _append(_gen_intruso(pool, used_qids, db, cat_occs))
        _append(_gen_indovina_chi(pool, used_qids, db, cat_occs, nationality))
        _append(_gen_chi_e(pool, used_qids, db, cat_occs))
        _append(_first(
            lambda: _gen_intruso_graph(pool, used_qids, db),
            lambda: _gen_intruso(pool, used_qids, db, cat_occs),
            lambda: _gen_vero_falso(pool, used_qids, db, "hard", cat_occs),
        ))

    questions = _shuffle_no_3_consecutive(questions)

    return {
        "round_id": str(uuid.uuid4()),
        "questions": questions,
        "filters": {
            "category": category,
            "nationality": nationality,
            "living": living,
            "difficulty": difficulty,
        },
    }
