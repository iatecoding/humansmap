"""
domains.py — Map Wikidata occupation strings to 8 broad editorial domains.

Used by the connections-feed generator to detect "cross-domain" pairs
(e.g. a musician and a politician) that are editorially surprising.
No I/O, no external dependencies.

Also provides Phase 13Q taxonomy helpers: 5 feed category ids
(politics, business, sport, science_education, arts) and the functions
``categories_for_domains`` / ``categories_for_org`` used when tagging cards.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Editorial display labels (used on feed cards — e.g. "Music", "Politics")
# ---------------------------------------------------------------------------
# Maps primary_occupation keys → short editorial label shown to end users.
# Lifted from build_animated_prototype.py's DOMAIN_LABELS and extended.
# The internal DOMAINS dict (below) maps to coarser buckets used for
# cross-domain detection; this dict maps to finer display labels.

DOMAIN_LABELS: dict[str, str] = {
    # Music
    "singer":           "Music",
    "musician":         "Music",
    "composer":         "Music",
    "pianist":          "Music",
    "guitarist":        "Music",
    "conductor":        "Music",
    "keyboardist":      "Music",
    "cellist":          "Music",
    "violinist":        "Music",
    "drummer":          "Music",
    "songwriter":       "Music",
    "rapper":           "Music",
    "dj":               "Music",
    "organist":         "Music",
    "record_producer":  "Music",
    # Film / TV
    "actor":                   "Film",
    "film_actor":              "Film",
    "screenwriter":            "Film",
    "film_director":           "Film",
    "television_actor":        "Film",
    "television_presenter":    "Film",
    "theatre_director":        "Film",
    "film_producer":           "Film",
    "television_director":     "Film",
    "stage_actor":             "Film",
    "comedian":                "Film",
    "voice_actor":             "Film",
    # Literature
    "writer":       "Literature",
    "poet":         "Literature",
    "essayist":     "Literature",
    "novelist":     "Literature",
    "playwright":   "Literature",
    "illustrator":  "Literature",
    # Ideas (philosophy / social science)
    "philosopher":         "Ideas",
    "sociologist":         "Ideas",
    "historian":           "Ideas",
    "psychologist":        "Ideas",
    "political_scientist": "Ideas",
    "anthropologist":      "Ideas",
    "linguist":            "Ideas",
    # Science
    "scientist":          "Science",
    "mathematician":      "Science",
    "physicist":          "Science",
    "chemist":            "Science",
    "biologist":          "Science",
    "botanist":           "Science",
    "zoologist":          "Science",
    "astronomer":         "Science",
    "university_teacher": "Science",
    "researcher":         "Science",
    "engineer_scientist": "Science",
    "economist":          "Science",
    # Politics
    "politician":      "Politics",
    "diplomat":        "Politics",
    "judge":           "Politics",
    "public_official": "Politics",
    "civil_servant":   "Politics",
    "head_of_state":   "Politics",
    "monarch":         "Politics",
    "revolutionary":   "Politics",
    # Business
    "businessperson": "Business",
    "entrepreneur":   "Business",
    "banker":         "Business",
    "financier":      "Business",
    "investor":       "Business",
    "executive":      "Business",
    "lobbyist":       "Business",
    "publisher":      "Business",
    "merchant":       "Business",
    "consultant":     "Business",
    # Art (visual)
    "painter":         "Art",
    "sculptor":        "Art",
    "photographer":    "Art",
    "architect":       "Art",
    "designer":        "Art",
    "printmaker":      "Art",
    "fashion_designer":"Art",
    "art_historian":   "Art",
    # Media / journalism
    "journalist": "Media",
    # Sport
    "athlete":                  "Sport",
    "football_player":          "Sport",
    "basketball_player":        "Sport",
    "tennis_player":            "Sport",
    "boxer":                    "Sport",
    "football_manager":         "Sport",
    "american_football_player": "Sport",
    "baseball_player":          "Sport",
    "alpine_skier":             "Sport",
    "swimmer":                  "Sport",
    "sprinter":                 "Sport",
    "cyclist":                  "Sport",
    "racing_driver":            "Sport",
    "golfer":                   "Sport",
    "chess_player":             "Sport",
    "water_polo_player":        "Sport",
    "badminton_player":         "Sport",
    # Modelling / other
    "model":            "Modelling",
    "military_person":  "Military",
    "military_officer": "Military",
    "lawyer":           "Law",
    "trade_unionist":   "Politics",
    "intelligence_officer": "Politics",
    "astronaut":        "Science",
    "philanthropist":   "Philanthropy",
    "activist":         "Activism",
    "clergyman":        "Religion",
    "priest":           "Religion",
    "bishop":           "Religion",
    "nun":              "Religion",
    "aristocrat":       "Nobility",
    "noble":            "Nobility",
    "engineer":         "Engineering",
    "teacher":          "Education",
}

# ---------------------------------------------------------------------------
# Domain catalogue
# ---------------------------------------------------------------------------

DOMAINS: dict[str, str] = {
    # music_performance
    "singer": "music_performance",
    "musician": "music_performance",
    "composer": "music_performance",
    "pianist": "music_performance",
    "guitarist": "music_performance",
    "conductor": "music_performance",
    "keyboardist": "music_performance",
    "record_producer": "music_performance",
    "voice_actor": "music_performance",
    "cellist": "music_performance",
    "violinist": "music_performance",
    "drummer": "music_performance",
    "songwriter": "music_performance",
    "rapper": "music_performance",
    "dj": "music_performance",
    "organist": "music_performance",
    # film_tv
    "actor": "film_tv",
    "film_actor": "film_tv",
    "screenwriter": "film_tv",
    "film_director": "film_tv",
    "television_actor": "film_tv",
    "television_presenter": "film_tv",
    "theatre_director": "film_tv",
    "film_producer": "film_tv",
    "television_director": "film_tv",
    "stage_actor": "film_tv",
    "comedian": "film_tv",
    # art_literature
    "writer": "art_literature",
    "painter": "art_literature",
    "poet": "art_literature",
    "essayist": "art_literature",
    "sculptor": "art_literature",
    "printmaker": "art_literature",
    "photographer": "art_literature",
    "architect": "art_literature",
    "fashion_designer": "art_literature",
    "novelist": "art_literature",
    "playwright": "art_literature",
    "illustrator": "art_literature",
    "designer": "art_literature",
    "art_historian": "art_literature",
    # science_academia
    "university_teacher": "science_academia",
    "scientist": "science_academia",
    "mathematician": "science_academia",
    "physicist": "science_academia",
    "chemist": "science_academia",
    "biologist": "science_academia",
    "botanist": "science_academia",
    "zoologist": "science_academia",
    "economist": "science_academia",
    "sociologist": "science_academia",
    "philosopher": "science_academia",
    "historian": "science_academia",
    "astronomer": "science_academia",
    "psychologist": "science_academia",
    "engineer_scientist": "science_academia",
    "researcher": "science_academia",
    "anthropologist": "science_academia",
    "linguist": "science_academia",
    # politics
    "politician": "politics",
    "diplomat": "politics",
    "judge": "politics",
    "public_official": "politics",
    "civil_servant": "politics",
    "intelligence_officer": "politics",
    "trade_unionist": "politics",
    "political_scientist": "politics",
    "revolutionary": "politics",
    "monarch": "politics",
    "head_of_state": "politics",
    # business_finance
    "businessperson": "business_finance",
    "entrepreneur": "business_finance",
    "banker": "business_finance",
    "financier": "business_finance",
    "investor": "business_finance",
    "consultant": "business_finance",
    "executive": "business_finance",
    "lobbyist": "business_finance",
    "publisher": "business_finance",
    "merchant": "business_finance",
    # sport
    "football_player": "sport",
    "basketball_player": "sport",
    "tennis_player": "sport",
    "boxer": "sport",
    "football_manager": "sport",
    "american_football_player": "sport",
    "baseball_player": "sport",
    "alpine_skier": "sport",
    "swimmer": "sport",
    "sprinter": "sport",
    "water_polo_player": "sport",
    "badminton_player": "sport",
    "cyclist": "sport",
    "racing_driver": "sport",
    "athlete": "sport",
    "golfer": "sport",
    "chess_player": "sport",
    # military_religion_other
    "military_person": "military_religion_other",
    "military_officer": "military_religion_other",
    "engineer": "military_religion_other",
    "journalist": "military_religion_other",
    "teacher": "military_religion_other",
    "lawyer": "military_religion_other",
    "model": "military_religion_other",
    "philanthropist": "military_religion_other",
    "activist": "military_religion_other",
    "clergyman": "military_religion_other",
    "astronaut": "military_religion_other",
    "priest": "military_religion_other",
    "bishop": "military_religion_other",
    "nun": "military_religion_other",
    "aristocrat": "military_religion_other",
    "noble": "military_religion_other",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# §A1 — Phase 13Q category taxonomy (5 interest ids)
# ---------------------------------------------------------------------------

# Maps DOMAINS coarse bucket → feed category id.
# military_religion_other is intentionally unmapped (returns nothing).
DOMAIN_TO_CATEGORY: dict[str, str] = {
    "politics":              "politics",
    "business_finance":      "business",
    "sport":                 "sport",
    "science_academia":      "science_education",
    "music_performance":     "arts",
    "film_tv":               "arts",
    "art_literature":        "arts",
}

# Maps org.type value → feed category id.
ORG_TYPE_TO_CATEGORY: dict[str, str] = {
    # science_education
    "university":              "science_education",
    "college":                 "science_education",
    "educational_institution": "science_education",
    "secondary_school":        "science_education",
    # business
    "company":                 "business",
    "bank":                    "business",
    "foundation":              "business",
    # sport
    "sports_club":             "sport",
    "sports_facility":         "sport",
    "stadium":                 "sport",
    # politics
    "political_party":         "politics",
    "government_agency":       "politics",
    "international_organization": "politics",
    # arts
    "band":                    "arts",
    "record_label":            "arts",
    "publisher":               "arts",
    "museum":                  "arts",
    "magazine":                "arts",
    "newspaper":               "arts",
}

# Deterministic ordering of the 5 taxonomy ids
CATEGORY_ORDER: list[str] = ["politics", "business", "sport", "science_education", "arts"]


def categories_for_domains(*occupation_lists: list[str] | None) -> list[str]:
    """Return sorted, deduped category ids derived from one or more occupation lists.

    Each list is a person's ``occupations`` list.  The coarse domain of each
    occupation is looked up in ``DOMAINS``, then mapped via ``DOMAIN_TO_CATEGORY``.
    ``military_religion_other`` is not mapped and produces no category.

    Ordering follows ``CATEGORY_ORDER`` (deterministic, not insertion order).
    """
    cats: set[str] = set()
    for occ_list in occupation_lists:
        if not occ_list:
            continue
        for occ in occ_list:
            key = _normalise(occ)
            domain = DOMAINS.get(key)
            if domain:
                cat = DOMAIN_TO_CATEGORY.get(domain)
                if cat:
                    cats.add(cat)
    return [c for c in CATEGORY_ORDER if c in cats]


def categories_for_org(
    org_type: str | None,
    people_occupations: list[list[str] | None] | None = None,
) -> list[str]:
    """Return category ids for an organisation card.

    Org type wins: if ``org_type`` maps to a category, that category is always
    present.  People's occupations are unioned in via ``categories_for_domains``
    (so a sports club whose members are also politicians may get both 'sport'
    and 'politics').  Returns list in ``CATEGORY_ORDER`` order.
    """
    cats: set[str] = set()

    if org_type:
        org_cat = ORG_TYPE_TO_CATEGORY.get(org_type.lower())
        if org_cat:
            cats.add(org_cat)

    if people_occupations:
        for cat in categories_for_domains(*people_occupations):
            cats.add(cat)

    return [c for c in CATEGORY_ORDER if c in cats]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _normalise(occupation: str) -> str:
    """Lowercase and replace spaces with underscores for lookup."""
    return occupation.strip().lower().replace(" ", "_")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def domain_of(occupations: list[str] | None) -> str | None:
    """Return the domain of the first occupation in *occupations* that maps.

    Normalises each entry (lowercase, spaces→underscores) before lookup.
    Returns ``None`` if the list is empty, None, or no entry matches.
    """
    if not occupations:
        return None
    for occ in occupations:
        key = _normalise(occ)
        domain = DOMAINS.get(key)
        if domain is not None:
            return domain
    return None


def is_cross_domain(
    occ_a: list[str] | None,
    occ_b: list[str] | None,
) -> bool:
    """Return True only when both lists map to *different* known domains.

    If either person has no recognisable domain the pair is not considered
    cross-domain (we can't make the editorial claim with confidence).
    """
    da = domain_of(occ_a)
    db = domain_of(occ_b)
    if da is None or db is None:
        return False
    return da != db


def pretty_occupation(occupation: str | None) -> str:
    """Convert an internal occupation key to a human-readable label.

    ``"film_actor"`` → ``"Film actor"``.  Returns ``""`` for None/empty input.
    """
    if not occupation:
        return ""
    return occupation.replace("_", " ").capitalize()


def domain_label(occupations_or_primary: list[str] | str | None) -> str | None:
    """Return the editorial display label for an occupation or list of occupations.

    Accepts either a list (checks each entry in order) or a single string.
    Returns labels like "Music", "Film", "Politics", "Science", etc.
    Returns ``None`` if no match is found.

    Examples::

        domain_label("film_actor")      → "Film"
        domain_label(["composer"])      → "Music"
        domain_label(["unknown_thing"]) → None
    """
    if occupations_or_primary is None:
        return None

    candidates: list[str]
    if isinstance(occupations_or_primary, str):
        candidates = [occupations_or_primary]
    else:
        candidates = list(occupations_or_primary)

    for occ in candidates:
        key = _normalise(occ)
        label = DOMAIN_LABELS.get(key)
        if label is not None:
            return label
    return None
