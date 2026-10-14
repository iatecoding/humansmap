"""Stream Wikidata N-Triples or full JSON dumps into HumansMap.\n\nTwo-pass mode first selects people and referenced QIDs, then streams the same\nsource again to write the selected graph. DUMP_URL takes precedence over\nDUMP_FILE; remote runs must use a pinned HTTPS release URL.\n\nModes:\n  --preflight  validate source and scope without reading dump data or ArangoDB\n  --pass1      identify selected people and referenced QIDs\n  --pass2      load the QIDs written by pass 1\n  (none)       single-pass mode for small or pre-filtered inputs\n\nImportant environment variables:\n  DUMP_FILE, DUMP_URL, INPUT_FORMAT, PERSON_SCOPE, PASS1_OUTPUT_DIR,\n  PASS1_MIN_STATEMENTS, MIN_STATEMENTS, BATCH_SIZE, SKIP_EXISTING,\n  SKIP_EDGES, DRY_RUN.\n"""

import argparse
import bz2
import gzip
import io
import json
import logging
import os
import re
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import quote as _url_quote
from urllib.request import Request, urlopen

# NTriples Unicode escape patterns: \uXXXX (BMP) and \UXXXXXXXX (full)
_NT_U4 = re.compile(r"\\u([0-9A-Fa-f]{4})")
_NT_U8 = re.compile(r"\\U([0-9A-Fa-f]{8})")

sys.path.insert(0, "/app")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.database import (
    init_db, PERSONS, ORGANIZATIONS, EVENTS,
    RELATIONSHIPS, AFFILIATIONS, INVOLVEMENTS, ORG_STRUCTURE,
)
from app.config import settings
from scripts.dump_edges import (
    EDGE_PROPERTIES,
    EVENT_REVERSE_PROPERTIES,
    ORG_REVERSE_PROPERTIES,
    ORG_STRUCTURE_PROPERTIES,
    build_edge_docs,
    build_org_structure_edge_docs,
    build_reverse_edge_docs,
    parse_date,
    sanitize_key,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("load_dump")

# ── Configuration ────────────────────────────────────────────────────────────

DUMP_FILE = os.getenv("DUMP_FILE", "/data/wikidump/latest-truthy.nt.bz2")
# A pinned HTTPS URL can be streamed directly for two-pass full-JSON imports.
# Do not use a moving `latest-*` URL for a run: pass 1 and pass 2 must read
# the exact same dump release.
DUMP_URL = os.getenv("DUMP_URL", "")
REMOTE_TIMEOUT_SECONDS = int(os.getenv("REMOTE_TIMEOUT_SECONDS", "120"))
PASS1_OUTPUT_DIR = os.getenv("PASS1_OUTPUT_DIR", "/app/pass1")
MIN_STATEMENTS = int(os.getenv("MIN_STATEMENTS", "1"))
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "500"))
EDGE_FLUSH_INTERVAL = int(os.getenv("EDGE_FLUSH_INTERVAL", "400000"))
SKIP_EXISTING = os.getenv("SKIP_EXISTING", "0") == "1"
SKIP_EDGES = os.getenv("SKIP_EDGES", "0") == "1"
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"

# ── URI prefixes ─────────────────────────────────────────────────────────────

WD_ENTITY = "http://www.wikidata.org/entity/"
WDT_DIRECT = "http://www.wikidata.org/prop/direct/"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
SCHEMA_DESC = "http://schema.org/description"

# Statement-level URIs (for qualifier extraction)
WD_PROP_STMT = "http://www.wikidata.org/prop/statement/"  # ps: — statement main value
WD_PROP_QUAL = "http://www.wikidata.org/prop/qualifier/"  # pq: — qualifier on statement
WD_STATEMENT = "http://www.wikidata.org/entity/statement/"  # statement node URI prefix

# Qualifier properties to extract from statement nodes. Dates go onto
# start_date/end_date on edges; P1810 (named-as) and P1545 (series ordinal)
# get persisted as named_as/ordinal on the edge doc — non-obvious context the
# JSON dump surfaces that truthy NT throws away.
QUAL_PROPS = {"P580", "P582", "P585", "P1810", "P1545"}

# Non-date qualifier projection: stmt["quals"][src] → edge_doc[dst]
EDGE_QUAL_FIELDS = {"P1810": "named_as", "P1545": "ordinal"}

# Pass-1 notability shortcut: skip persons with fewer than N direct statements
# (they'd never satisfy the rich-fields threshold anyway). Materially cuts the
# needed_refs set and therefore pass-2 work on the full 140 GB dump.
PASS1_MIN_STATEMENTS = int(os.getenv("PASS1_MIN_STATEMENTS", "30"))

# Input format: "nt" (NTriples) | "json" (Wikidata JSON dump) | "auto"
# "auto" dispatches from DUMP_FILE extension; stdin defaults to "nt" unless
# INPUT_FORMAT is explicitly set.
INPUT_FORMAT = os.getenv("INPUT_FORMAT", "auto").lower()
PERSON_SCOPE = os.getenv("PERSON_SCOPE", "countries").lower()
PASS1_PERSONS_FILE = "target_persons.txt"
PASS1_LEGACY_PERSONS_FILE = "european_qids.txt"

# ── European country filter ──────────────────────────────────────────────────
# P27 (country of citizenship) values that qualify a person as European.
# Includes all sovereign European states (EU + non-EU).

TARGET_COUNTRY_QIDS = {
    # EU members
    "Q38",   # Italy
    "Q183",  # Germany
    "Q142",  # France
    "Q145",  # United Kingdom
    "Q29",   # Spain
    "Q36",   # Poland
    "Q218",  # Romania
    "Q55",   # Netherlands
    "Q31",   # Belgium
    "Q213",  # Czech Republic
    "Q41",   # Greece
    "Q45",   # Portugal
    "Q34",   # Sweden
    "Q28",   # Hungary
    "Q40",   # Austria
    "Q219",  # Bulgaria
    "Q35",   # Denmark
    "Q33",   # Finland
    "Q214",  # Slovakia
    "Q27",   # Ireland
    "Q224",  # Croatia
    "Q211",  # Lithuania
    "Q232",  # Latvia
    "Q215",  # Slovenia
    "Q191",  # Estonia
    "Q229",  # Cyprus
    "Q244",  # Luxembourg
    "Q233",  # Malta
    # Non-EU European
    "Q20",   # Norway
    "Q39",   # Switzerland
    "Q189",  # Iceland
    "Q347",  # Liechtenstein
    "Q228",  # Andorra
    "Q235",  # Monaco
    "Q238",  # San Marino
    "Q237",  # Vatican City
    "Q403",  # Serbia
    "Q225",  # Bosnia and Herzegovina
    "Q222",  # Albania
    "Q221",  # North Macedonia
    "Q236",  # Montenegro
    "Q1246", # Kosovo
    "Q212",  # Ukraine
    "Q184",  # Belarus
    "Q252",  # Moldova
    "Q159",  # Russia
    "Q43",   # Turkey
    # North America
    "Q30",   # United States
    "Q16",   # Canada
}

# ── Entity classification ────────────────────────────────────────────────────

PERSON_QIDS = {"Q5"}

ORG_QIDS = {
    "Q4830453": "company",
    "Q891723":  "company",
    "Q783794":  "company",
    "Q2912397": "company",
    "Q161726":  "company",
    "Q6881511": "company",
    "Q134161":  "company",
    "Q658255":  "company",
    "Q3196867": "company",
    "Q18388277": "company",
    "Q2659904": "company",
    "Q7278":    "political_party",
    "Q3918":    "university",
    "Q7272":    "university",
    "Q476028":  "sports_club",
    "Q15275719": "news_agency",
    "Q15265344": "broadcaster",
    "Q1616075": "television_station",
    "Q1193236": "radio_station",
    "Q11032":   "newspaper",
    "Q41298":   "magazine",
    "Q2085381": "publisher",
    "Q3153117": "bank",
    "Q22687":   "bank",
    "Q484652":  "international_organization",
    "Q163740":  "nonprofit",
    "Q9842":    "primary_school",
    "Q9826":    "secondary_school",
    "Q180958":  "foundation",
    "Q5341295": "educational_institution",
    "Q39614":   "organization",
    "Q43229":   "organization",
    "Q4358176": "organization",
    "Q1664720": "organization",
    "Q2085381": "publisher",
    "Q327333":  "government_agency",
    "Q1081138": "sports_facility",   # sports facility (specific)
    "Q13226383": "organization",     # generic "facility" — too broad for sports
    "Q1007870": "art_gallery",
    "Q33506":   "museum",
    "Q483110":  "stadium",
    # Public offices / positions (P39 targets)
    "Q294414":   "position",   # public office
    "Q4164871":  "position",   # position (abstract)
    "Q56005748": "position",   # government position
    "Q7187":     "position",   # government minister
    "Q83307":    "position",   # minister
    "Q30461":    "position",   # president
}

EVENT_QIDS = {
    "Q1190554":  "occurrence",
    "Q178561":   "battle",
    "Q198":      "war",
    "Q40231":    "election",
    "Q645883":   "military_operation",
    "Q3839081":  "disaster",
    "Q1318941":  "assassination",
    "Q2223653":  "political_scandal",
    "Q11483816": "referendum",
    "Q13418847": "historical_event",
    "Q3505845":  "terrorist_attack",
    "Q124757":   "political_crisis",
    "Q891854":   "massacre",
    "Q175331":   "protest",
    "Q7283":     "terrorism",
    "Q188055":   "bombing",
    "Q10931":    "revolution",
    "Q1656682":  "event",
    "Q15275719": "incident",
    "Q132821":   "murder",
    "Q110999040": "armed_conflict",
    "Q8740":     "coup_d_etat",
}

# Skip silently — taxonomy, geography, etc.
SKIP_QIDS = {
    "Q16521", "Q16970", "Q486972", "Q1134686", "Q310890",  # taxonomy
    "Q515", "Q6256", "Q5119", "Q3624078", "Q82794",        # geography
    "Q15089", "Q3924474", "Q954172", "Q1288568",            # commune/frazione
    "Q8502", "Q4022", "Q46831", "Q12284",                   # mountain/river/pass/canal
    "Q3685476", "Q614316",                                   # nature reserve
    "Q16560",                                                # palace
    "Q747074", "Q30612", "Q126807",                         # taxonomy ranks
    "Q13442814",                                             # scholarly article
    "Q11266439",                                             # Wikimedia template
    "Q4167836", "Q4167410", "Q11753321",                    # disambiguation/category
}

# Geographic P31 types — we capture labels from these for birth/death place resolution
PLACE_QIDS = {
    "Q515", "Q6256", "Q5119", "Q3624078", "Q82794",
    "Q15089", "Q3924474", "Q954172", "Q1288568",
    "Q486972", "Q3957", "Q532", "Q1549591",
    "Q747074", "Q16560",
    "Q1637706",  # città metropolitana
    "Q2074737",  # comune of Italy (modern)
}

# ── Occupation QID → string ──────────────────────────────────────────────────

OCCUPATION_MAP = {
    # Core professions — verified correct
    "Q82955":    "politician",
    "Q16533":    "judge",
    "Q40348":    "lawyer",
    "Q43845":    "businessperson",
    "Q1930187":  "journalist",
    "Q33999":    "actor",
    "Q2526255":  "film_director",
    "Q177220":   "singer",
    "Q36834":    "composer",
    "Q1028181":  "painter",
    "Q1281618":  "sculptor",
    "Q36180":    "writer",
    "Q33231":    "photographer",
    "Q47064":    "military_person",
    "Q2159907":  "criminal",
    "Q49757":    "poet",
    "Q639669":   "musician",
    "Q2306091":  "sociologist",
    "Q4964182":  "philosopher",
    "Q201788":   "historian",
    "Q169470":   "physicist",
    "Q864503":   "biologist",
    "Q593644":   "chemist",
    "Q170790":   "mathematician",
    "Q901":      "scientist",
    "Q188094":   "economist",
    "Q37226":    "teacher",
    "Q158852":   "conductor",
    "Q131524":   "entrepreneur",
    "Q189290":   "military_officer",
    "Q15627169": "trade_unionist",
    "Q11631":    "astronaut",
    "Q2374149":  "botanist",
    "Q350979":   "zoologist",
    "Q81096":    "engineer",
    "Q3282637":  "film_producer",
    "Q2516866":  "publisher",
    "Q486748":   "pianist",
    "Q855091":   "guitarist",
    "Q2405480":  "voice_actor",
    "Q10800557": "film_actor",
    # Sports — corrected QIDs
    "Q937857":   "football_player",    # association football player ✓
    "Q2125610":  "cyclist",            # fixed: Q11338576 was boxer, not cyclist
    "Q11338576": "boxer",              # fixed: this IS boxer
    "Q10833314": "tennis_player",      # fixed: Q19204627 was American football player
    "Q3665646":  "basketball_player",  # ✓
    "Q4144610":  "alpine_skier",       # fixed: Q18116754 was a school
    "Q4009406":  "sprinter",           # fixed: Q10843263 was field hockey player
    "Q628099":   "football_manager",   # fixed: Q13582652 was civil engineer
    "Q17524364": "water_polo_player",  # fixed: Q11513337 was athletics competitor
    "Q10843402": "swimmer",            # fixed: Q2736 was the sport itself
    "Q15117302": "volleyball_player",  # fixed: Q11774202 was essayist
    "Q13381863": "fencer",             # fixed: Q1075651 was keyboardist
    "Q13415036": "rugby_player",       # fixed: Q13141064 was badminton player
    "Q10871364": "baseball_player",    # fixed: was wrongly mapped as racing_driver
    # Arts & media — corrected QIDs
    "Q3387717":  "theatre_director",   # fixed: Q15981151 was jazz musician
    "Q28389":    "screenwriter",       # ✓ (removed Q2865819 which was opera singer)
    "Q3501317":  "fashion_designer",   # fixed: Q1238570 was political scientist
    "Q42973":    "architect",          # fixed: Q211346 was psychiatrist
    "Q4610556":  "model",              # fixed: Q11569986 was printmaker
    "Q10798782": "television_actor",   # fixed: Q1622272 was university teacher
    "Q947873":   "television_presenter", # fixed: Q183945 was record producer
    "Q15077007": "podcaster",          # fixed: Q15472169 was patron of the arts
    # Other — corrected QIDs
    "Q1622272":  "university_teacher", # fixed: Q806798 was banker
    "Q193391":   "diplomat",           # fixed: Q728425 was balloonist
    "Q2259532":  "clergyman",          # fixed: Q2259451 was stage actor
    "Q12362622": "philanthropist",     # fixed: was wrongly mapped as boxer
    # Occupations added after QID audit — correct QIDs verified
    "Q19204627": "american_football_player",
    "Q13141064": "badminton_player",
    "Q183945":   "record_producer",
    "Q1238570":  "political_scientist",
    "Q11569986": "printmaker",
    "Q11774202": "essayist",
    "Q211346":   "psychiatrist",
    "Q10843263": "field_hockey_player",
    "Q1075651":  "keyboardist",        # maps to musician in trivia/display
    "Q13582652": "engineer",           # civil engineer → engineer
    "Q15981151": "musician",           # jazz musician → musician
    "Q2259451":  "actor",              # stage actor → actor
    "Q15472169": "philanthropist",     # patron of the arts → philanthropist
    # Investigative tier — surfaces business/political power structure
    "Q806798":   "banker",             # Wikidata label "banker"; previously mapped to businessperson
    "Q484876":   "executive",          # chief executive officer
    "Q212238":   "civil_servant",
    "Q11499147": "activist",
    # Investigative tier — batch 2 (live-verified via wikidata.org search, 2026-04-14)
    "Q557880":    "investor",            # "person who allocates capital with the expectation of a financial return"
    "Q1979607":   "financier",           # "person who makes their living from investments"
    "Q11986654":  "lobbyist",            # "person involved in lobbying"
    "Q5121444":   "intelligence_officer", # "person employed by an organization to collect, compile and/or analyze information"
    "Q2824523":   "board_member",        # "position in an organization"
    "Q15978655":  "consultant",          # "professional who provides advice in their specific field of expertise"
    "Q599151":    "public_official",     # Wikidata label is "official"; stored as public_official for clarity
    # Still deferred — no unambiguous generic QID (sub-role items exist but would narrow the match):
    #   producer (only film_producer Q3282637 is clean; TV/record producer QIDs diverge)
    #   academic (closest is researcher Q1650915, but overlaps with science_researcher — skip to avoid double-counting)
}

# Gender QID → string
GENDER_MAP = {
    "Q6581097": "male",
    "Q6581072": "female",
    "Q1097630": "intersex",
    "Q1052281": "transgender_female",
    "Q2449503": "transgender_male",
}

# Nationality QID → ISO 3166-1 alpha-2 short code
#
# Worldwide coverage: ~190 sovereign states (UN member set + observers,
# plus a handful of widely-recognised non-members such as TW/HK/MO/PS).
# Used to populate the `nationalities[]` field on persons whose P27
# (country of citizenship) resolves to a known Wikidata country entity.
#
# Important: this map does NOT widen the person inclusion filter — that
# is governed by TARGET_COUNTRY_QIDS above. Already-included persons whose
# P27 sits outside the historical 12-country map will now resolve to an
# ISO code instead of being dropped.
#
# Storage convention: canonical ISO alpha-2 (e.g. "IT", "JP", "BR"). The
# frontend renders the demonym from the `nationalities` i18n block.
#
# QID drift policy: if a downstream check shows a QID resolving to the
# wrong country (Wikidata occasionally renumbers), patch here with a
# trailing comment in the style "fixed: was X, now Y" — same pattern the
# OCCUPATION_MAP above uses.
NATIONALITY_MAP = {
    # ── Europe ──────────────────────────────────────────────────────────
    "Q38":     "IT",   # Italy
    "Q142":    "FR",   # France
    "Q183":    "DE",   # Germany
    "Q29":     "ES",   # Spain
    "Q145":    "GB",   # United Kingdom
    "Q159":    "RU",   # Russia
    "Q39":     "CH",   # Switzerland
    "Q40":     "AT",   # Austria
    "Q31":     "BE",   # Belgium
    "Q55":     "NL",   # Netherlands
    "Q36":     "PL",   # Poland
    "Q45":     "PT",   # Portugal
    "Q35":     "DK",   # Denmark
    "Q34":     "SE",   # Sweden
    "Q33":     "FI",   # Finland
    "Q20":     "NO",   # Norway
    "Q189":    "IS",   # Iceland
    "Q27":     "IE",   # Ireland
    "Q28":     "HU",   # Hungary
    "Q213":    "CZ",   # Czech Republic
    "Q214":    "SK",   # Slovakia
    "Q215":    "SI",   # Slovenia
    "Q224":    "HR",   # Croatia
    "Q225":    "BA",   # Bosnia and Herzegovina
    "Q403":    "RS",   # Serbia
    "Q236":    "ME",   # Montenegro
    "Q221":    "MK",   # North Macedonia
    "Q222":    "AL",   # Albania
    "Q41":     "GR",   # Greece
    "Q43":     "TR",   # Turkey
    "Q229":    "CY",   # Cyprus
    "Q233":    "MT",   # Malta
    "Q218":    "RO",   # Romania
    "Q219":    "BG",   # Bulgaria
    "Q217":    "MD",   # Moldova
    "Q212":    "UA",   # Ukraine
    "Q184":    "BY",   # Belarus
    "Q37":     "LT",   # Lithuania
    "Q211":    "LV",   # Latvia
    "Q191":    "EE",   # Estonia
    "Q32":     "LU",   # Luxembourg
    "Q347":    "LI",   # Liechtenstein
    "Q235":    "MC",   # Monaco
    "Q238":    "SM",   # San Marino
    "Q237":    "VA",   # Vatican City
    "Q228":    "AD",   # Andorra
    # ── Americas ────────────────────────────────────────────────────────
    "Q30":     "US",   # United States
    "Q16":     "CA",   # Canada
    "Q96":     "MX",   # Mexico
    "Q155":    "BR",   # Brazil
    "Q414":    "AR",   # Argentina
    "Q298":    "CL",   # Chile
    "Q717":    "VE",   # Venezuela
    "Q739":    "CO",   # Colombia
    "Q419":    "PE",   # Peru
    "Q736":    "EC",   # Ecuador
    "Q733":    "PY",   # Paraguay
    "Q77":     "UY",   # Uruguay
    "Q750":    "BO",   # Bolivia
    "Q734":    "GY",   # Guyana
    "Q730":    "SR",   # Suriname
    "Q241":    "CU",   # Cuba
    "Q786":    "DO",   # Dominican Republic
    "Q790":    "HT",   # Haiti
    "Q781":    "AG",   # Antigua and Barbuda
    "Q244":    "BB",   # Barbados
    "Q757":    "VC",   # Saint Vincent and the Grenadines
    "Q763":    "KN",   # Saint Kitts and Nevis
    "Q760":    "LC",   # Saint Lucia
    "Q784":    "DM",   # Dominica
    "Q769":    "GD",   # Grenada
    "Q778":    "BS",   # Bahamas
    "Q766":    "JM",   # Jamaica
    "Q754":    "TT",   # Trinidad and Tobago
    "Q242":    "BZ",   # Belize
    "Q800":    "CR",   # Costa Rica
    "Q811":    "NI",   # Nicaragua
    "Q783":    "HN",   # Honduras
    "Q792":    "SV",   # El Salvador
    "Q774":    "GT",   # Guatemala
    "Q804":    "PA",   # Panama
    # ── Asia ────────────────────────────────────────────────────────────
    "Q148":    "CN",   # China
    "Q17":     "JP",   # Japan
    "Q884":    "KR",   # South Korea
    "Q423":    "KP",   # North Korea
    "Q668":    "IN",   # India
    "Q843":    "PK",   # Pakistan
    "Q902":    "BD",   # Bangladesh
    "Q854":    "LK",   # Sri Lanka
    "Q826":    "MV",   # Maldives
    "Q833":    "MY",   # Malaysia
    "Q334":    "SG",   # Singapore
    "Q252":    "ID",   # Indonesia
    "Q928":    "PH",   # Philippines
    "Q869":    "TH",   # Thailand
    "Q881":    "VN",   # Vietnam
    "Q424":    "KH",   # Cambodia (fixed: Q874 is Turkmenistan, not Cambodia)
    "Q819":    "LA",   # Laos
    "Q836":    "MM",   # Myanmar
    "Q921":    "BN",   # Brunei
    "Q574":    "TL",   # Timor-Leste
    "Q794":    "IR",   # Iran
    "Q796":    "IQ",   # Iraq
    "Q801":    "IL",   # Israel
    "Q219060": "PS",   # Palestine
    "Q822":    "LB",   # Lebanon
    "Q858":    "SY",   # Syria
    "Q810":    "JO",   # Jordan
    "Q851":    "SA",   # Saudi Arabia
    "Q842":    "OM",   # Oman
    "Q878":    "AE",   # United Arab Emirates
    "Q846":    "QA",   # Qatar
    "Q817":    "KW",   # Kuwait
    "Q398":    "BH",   # Bahrain
    "Q805":    "YE",   # Yemen
    "Q399":    "AM",   # Armenia
    "Q227":    "AZ",   # Azerbaijan
    "Q230":    "GE",   # Georgia
    "Q889":    "AF",   # Afghanistan
    "Q232":    "KZ",   # Kazakhstan
    "Q813":    "KG",   # Kyrgyzstan
    "Q863":    "TJ",   # Tajikistan
    "Q874":    "TM",   # Turkmenistan
    "Q265":    "UZ",   # Uzbekistan
    "Q837":    "NP",   # Nepal
    "Q917":    "BT",   # Bhutan
    "Q865":    "TW",   # Taiwan
    "Q14773":  "MO",   # Macau (special administrative region)
    "Q8646":   "HK",   # Hong Kong (special administrative region)
    "Q711":    "MN",   # Mongolia
    # ── Africa ──────────────────────────────────────────────────────────
    "Q79":     "EG",   # Egypt
    "Q1016":   "LY",   # Libya
    "Q262":    "DZ",   # Algeria
    "Q1028":   "MA",   # Morocco
    "Q948":    "TN",   # Tunisia
    "Q1041":   "SN",   # Senegal
    "Q1019":   "MG",   # Madagascar
    "Q1027":   "MU",   # Mauritius
    "Q1037":   "RW",   # Rwanda
    "Q967":    "BI",   # Burundi
    "Q1029":   "MZ",   # Mozambique
    "Q954":    "ZM",   # Zambia
    "Q1009":   "CM",   # Cameroon
    "Q1042":   "SC",   # Seychelles
    "Q1008":   "CI",   # Côte d'Ivoire
    "Q1033":   "NG",   # Nigeria
    "Q117":    "GH",   # Ghana
    "Q258":    "ZA",   # South Africa
    "Q1020":   "MW",   # Malawi
    "Q1006":   "GN",   # Guinea
    "Q1011":   "CV",   # Cape Verde
    "Q1014":   "LR",   # Liberia
    "Q1007":   "GW",   # Guinea-Bissau
    "Q1005":   "GM",   # Gambia
    "Q1013":   "LS",   # Lesotho
    "Q1030":   "NA",   # Namibia
    "Q1000":   "GA",   # Gabon
    "Q971":    "CG",   # Congo (Brazzaville)
    "Q974":    "CD",   # DR Congo
    "Q945":    "TG",   # Togo
    "Q962":    "BJ",   # Benin
    "Q1032":   "NE",   # Niger
    "Q1045":   "SO",   # Somalia
    "Q1049":   "SD",   # Sudan
    "Q958":    "SS",   # South Sudan
    "Q115":    "ET",   # Ethiopia
    "Q986":    "ER",   # Eritrea
    "Q977":    "DJ",   # Djibouti
    "Q1036":   "UG",   # Uganda
    "Q114":    "KE",   # Kenya
    "Q924":    "TZ",   # Tanzania
    "Q1050":   "SZ",   # Eswatini
    "Q1044":   "SL",   # Sierra Leone
    "Q1039":   "ST",   # São Tomé and Príncipe
    "Q1004":   "GQ",   # Equatorial Guinea
    "Q657":    "TD",   # Chad
    "Q953":    "ZW",   # Zimbabwe
    "Q963":    "BW",   # Botswana
    "Q912":    "ML",   # Mali
    "Q929":    "CF",   # Central African Republic
    "Q965":    "BF",   # Burkina Faso
    "Q1183":   "KM",   # Comoros
    # ── Oceania ─────────────────────────────────────────────────────────
    "Q408":    "AU",   # Australia
    "Q664":    "NZ",   # New Zealand
    "Q691":    "PG",   # Papua New Guinea
    "Q678":    "TO",   # Tonga
    "Q683":    "WS",   # Samoa
    "Q685":    "SB",   # Solomon Islands
    "Q686":    "VU",   # Vanuatu
    "Q672":    "TV",   # Tuvalu
    "Q702":    "FM",   # Micronesia
    "Q709":    "MH",   # Marshall Islands
    "Q695":    "PW",   # Palau
    "Q710":    "KI",   # Kiribati
    "Q712":    "FJ",   # Fiji
}

# Properties whose target QIDs we need to resolve to labels but don't create
# graph edges for. Pass-1 adds these targets to `needed_refs` so pass-2 sees
# the entity and captures its labels into `entity_labels` for later lookup.
#
# - Person rich fields:  P509 cause_of_death, P166 awards, P800 notable_works
# - Org fields:          P159 headquarters, P17 country
# - Event fields:        P276 location, P495 country
LABEL_REF_PROPERTIES = {
    # Person rich fields (original)
    "P509", "P166", "P800",
    # Org/event location (original)
    "P159", "P17", "P276", "P495",
    # Person rich QID fields (JSON-dump expansion — shared-attribute
    # connections: same field, same religion, same conflict, same degree,
    # same instrument, etc.)
    "P101", "P140", "P136", "P1303", "P512", "P97", "P1196", "P641",
    "P103", "P1412", "P607", "P735", "P734",
    # Person-location QID fields (resolved like birth/death place)
    "P551", "P119", "P937", "P1321",
    # Org rich QID fields
    "P452", "P1056", "P1454", "P414", "P361", "P463", "P740", "P131",
}

# ── Rich-field property maps (JSON-dump expansion) ──────────────────────────
# QID-valued person props → (doc_field, kind) where kind is "single" or "multi".
# Resolved to labels via entity_labels at build time. All of these must also be
# present in LABEL_REF_PROPERTIES so pass-1 seeds their targets into needed_refs.
PERSON_QID_LABEL_PROPS: dict[str, tuple[str, str]] = {
    "P101":  ("field_of_work",     "multi"),
    "P140":  ("religion",          "single"),
    "P136":  ("genres",            "multi"),
    "P1303": ("instruments",       "multi"),
    "P512":  ("academic_degree",   "single"),
    "P97":   ("noble_title",       "single"),
    "P1196": ("manner_of_death",   "single"),
    "P641":  ("sport",             "single"),
    "P103":  ("native_language",   "single"),
    "P1412": ("languages_spoken",  "multi"),
    "P607":  ("conflicts",         "multi"),
    "P735":  ("given_name",        "single"),
    "P734":  ("family_name",       "single"),
    "P1321": ("place_of_origin",   "single"),
}

# Person location-like props resolved via the same entity_labels lookup — we
# don't use place_labels here because these QIDs aren't necessarily typed as
# PLACE in classify_entity (P937 e.g. can be an org or city), and LABEL_REF
# gets them into entity_labels via needed_refs anyway.
PERSON_PLACE_LABEL_PROPS: dict[str, str] = {
    "P551": "residence",
    "P119": "burial_place",
    "P937": "work_location",
}

# Person simple (non-QID) props. Value is stored verbatim.
# Note: dates go through parse_date; URLs/IDs do not.
PERSON_SIMPLE_PROPS: dict[str, tuple[str, str]] = {
    "P856":  ("official_website", "url"),
    "P2031": ("floruit_start",    "date"),
    "P2032": ("floruit_end",      "date"),
    "P345":  ("imdb_id",          "id"),
    "P214":  ("viaf_id",          "id"),
    "P213":  ("isni",             "id"),
    "P373":  ("commons_category", "id"),
    "P1559": ("native_name",      "str"),  # monolingualtext → text
}

# Org QID-valued props → (doc_field, kind)
ORG_QID_LABEL_PROPS: dict[str, tuple[str, str]] = {
    "P452":  ("industries",      "multi"),
    "P1056": ("products",        "multi"),
    "P1454": ("legal_form",      "single"),
    "P414":  ("stock_exchange",  "single"),
    "P740":  ("formation_place", "single"),
    "P131":  ("location",        "single"),
}

# Org simple props.
ORG_SIMPLE_PROPS: dict[str, tuple[str, str]] = {
    "P373":  ("commons_category", "id"),
    "P2139": ("revenue",          "num"),
    "P1448": ("official_name",    "str"),
    "P1813": ("short_name",       "str"),
    "P154":  ("logo_url",         "url"),
}

# Award / order / prize P31 classes used to seed the `award_counts` dict during
# pass-1/pass-2. An entity whose P31 includes one of these has its
# wikidata_statements_count tracked as a proxy for prestige (§1d).
AWARD_CLASS_QIDS = {
    "Q618779",   # award
    "Q193622",   # order
    "Q4220917",  # film award
}

# ── Dump stream opener ───────────────────────────────────────────────────────

def is_remote_source(source: str) -> bool:
    return source.startswith("https://") or source.startswith("http://")


def source_metadata(source: str) -> dict[str, str | int | None]:
    """Return lightweight source metadata without downloading a dump body."""
    if not is_remote_source(source):
        if not os.path.exists(source):
            raise FileNotFoundError(f"Dump file not found: {source}")
        return {
            "source": source,
            "kind": "file",
            "size": os.path.getsize(source),
            "etag": None,
            "last_modified": str(int(os.path.getmtime(source))),
        }

    request = Request(source, method="HEAD", headers={"User-Agent": "HumansMap-ingestion/1.0"})
    with urlopen(request, timeout=REMOTE_TIMEOUT_SECONDS) as response:
        headers = response.headers
        size = headers.get("Content-Length")
        return {
            "source": response.geturl(),
            "kind": "remote",
            "size": int(size) if size and size.isdigit() else None,
            "etag": headers.get("ETag"),
            "last_modified": headers.get("Last-Modified"),
        }


def validate_dump_source(source: str) -> dict[str, str | int | None]:
    """Validate a dump source before starting an expensive pass."""
    if not source or source == "-":
        return {"source": "stdin", "kind": "stdin", "size": None, "etag": None, "last_modified": None}
    if is_remote_source(source):
        if not source.startswith("https://"):
            raise ValueError("Remote dumps must use HTTPS")
        if "/latest-" in source.lower():
            raise ValueError("Remote dump URLs must be pinned to one release, not a latest-* alias")
    return source_metadata(source)


def _remote_dump_lines(source: str):
    """Stream a remote dump once; callers must restart the whole phase on failure.

    A bzip2 stream cannot be safely resumed at an arbitrary HTTP byte offset.
    The detached ingestion runner therefore checkpoints only completed phases,
    guaranteeing that a retry always starts from a clean pass boundary.
    """
    request = Request(source, headers={"User-Agent": "HumansMap-ingestion/1.0"})
    response = urlopen(request, timeout=REMOTE_TIMEOUT_SECONDS)
    binary_stream = None
    text_stream = None
    try:
        lower = source.lower()
        if lower.endswith(".bz2"):
            binary_stream = bz2.BZ2File(response)
        elif lower.endswith(".gz"):
            binary_stream = gzip.GzipFile(fileobj=response)
        else:
            binary_stream = response
        text_stream = io.TextIOWrapper(binary_stream, encoding="utf-8")
        for line in text_stream:
            yield line
    except Exception as exc:
        raise RuntimeError(f"Remote dump stream failed; restart this complete pass: {source}") from exc
    finally:
        if text_stream is not None:
            text_stream.close()
        elif binary_stream is not None:
            binary_stream.close()
        response.close()




def open_dump_stream(dump_path: str):
    """Open the dump as a text stream.

    Priority:
      1. stdin  — when dump_path is '-' or empty (primary: piped from lbzip2)
      2. .bz2   — try lbzip2 subprocess first (parallel), fall back to bz2.open()
      3. .gz    — gzip.open() (legacy pre-filtered dumps)
      4. plain  — open as text
    """
    if not dump_path or dump_path == "-":
        logger.info("Reading from stdin")
        return sys.stdin

    if is_remote_source(dump_path):
        logger.info("Streaming remote dump: %s", dump_path)
        return _remote_dump_lines(dump_path)

    if dump_path.endswith(".bz2"):
        # Try lbzip2 for parallel decompression (~3-4x faster on 8 cores)
        try:
            proc = subprocess.Popen(
                ["lbzip2", "-dc", dump_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            logger.info("Decompressing with lbzip2: %s", dump_path)
            return _lbzip2_lines(proc)
        except FileNotFoundError:
            logger.info("lbzip2 not found, falling back to bz2.open()")
            return bz2.open(dump_path, "rt", encoding="utf-8")

    if dump_path.endswith(".gz"):
        return gzip.open(dump_path, "rt", encoding="utf-8")

    return open(dump_path, "rt", encoding="utf-8")


def _lbzip2_lines(proc: subprocess.Popen):
    """Decode lines from an lbzip2 subprocess; raise if it exits non-zero.

    A corrupt or truncated dump makes lbzip2 exit non-zero, but the raw
    stdout stream up to that point looks like a normal (if short) stream —
    without this check, load_dump()/run_pass1() would just quietly finish
    early and report success on a partial dump. stderr is discarded
    (DEVNULL) rather than captured, to avoid any risk of a full-pipe
    deadlock while we're only draining stdout here.
    """
    try:
        for raw in proc.stdout:
            yield raw.decode("utf-8", errors="replace")
    finally:
        proc.stdout.close()
        returncode = proc.wait()
        if returncode != 0:
            raise RuntimeError(
                f"lbzip2 exited with code {returncode} — dump stream is likely truncated/corrupt"
            )


# ── NTriples parser ──────────────────────────────────────────────────────────

def extract_qid(uri: str) -> str | None:
    """Extract QID from a Wikidata entity URI."""
    if WD_ENTITY in uri:
        qid = uri.split(WD_ENTITY)[-1]
        if qid.startswith("Q"):
            return qid
    return None


def extract_statement_parent(uri: str) -> str | None:
    """Extract parent entity QID from a statement node URI.

    e.g. http://www.wikidata.org/entity/statement/Q937-abc-def → Q937
    """
    if WD_STATEMENT not in uri:
        return None
    suffix = uri[len(WD_STATEMENT):]  # "Q937-abc-def-..."
    qid_part = suffix.split("-", 1)[0]
    if qid_part.startswith("Q"):
        return qid_part
    return None


def parse_nt_line(line: str) -> tuple | None:
    """Parse a single NTriples line into (subject_uri, predicate_uri, object_value, meta).

    meta is:
      - For language literals: the language tag (e.g. "en", "it")
      - For typed literals: the type URI
      - For URIs: None

    Returns None for unparseable lines.
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    # Subject: <URI>
    if line[0] != "<":
        return None
    end_s = line.index(">")
    subject = line[1:end_s]

    # Predicate: <URI>
    rest = line[end_s + 2:]  # skip '> '
    if not rest or rest[0] != "<":
        return None
    end_p = rest.index(">")
    predicate = rest[1:end_p]

    # Object: remainder before ' .'
    obj_str = rest[end_p + 2:]  # skip '> '
    # Remove trailing ' .'
    if obj_str.endswith(" ."):
        obj_str = obj_str[:-2]
    elif obj_str.endswith("."):
        obj_str = obj_str[:-1]
    obj_str = obj_str.strip()

    if not obj_str:
        return None

    # Parse object
    if obj_str.startswith("<") and obj_str.endswith(">"):
        # URI
        return (subject, predicate, obj_str[1:-1], None)

    if obj_str.startswith('"'):
        # Literal — find closing quote (handle escaped quotes)
        i = 1
        while i < len(obj_str):
            if obj_str[i] == "\\" and i + 1 < len(obj_str):
                i += 2
                continue
            if obj_str[i] == '"':
                break
            i += 1
        value = obj_str[1:i]
        # Unescape NTriples escape sequences (including \uXXXX and \UXXXXXXXX)
        value = value.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
        value = _NT_U4.sub(lambda m: chr(int(m.group(1), 16)), value)
        value = _NT_U8.sub(lambda m: chr(int(m.group(1), 16)), value)
        after = obj_str[i + 1:]

        if after.startswith("@"):
            # Language tag
            lang = after[1:].strip()
            return (subject, predicate, value, lang)
        elif after.startswith("^^<") and after.endswith(">"):
            # Typed literal
            type_uri = after[3:-1]
            return (subject, predicate, value, type_uri)
        else:
            # Plain literal
            return (subject, predicate, value, None)

    return None


# ── Wikidata JSON dump parser ────────────────────────────────────────────────
# Wikidata ships two dump shapes:
#   - latest-truthy.nt.bz2  / latest-all.nt.bz2   (NTriples; parsed above)
#   - latest-all.json.gz                           (per-entity JSON; parsed here)
# The JSON dump delivers each entity pre-assembled as one JSON object per line,
# wrapped in a top-level "[ ... ]" array (commas terminate every line except
# the last). Qualifiers live inline under claims[P…][i].qualifiers — no
# reified statement nodes to reassemble. The parser produces the same entity
# dict shape the NT path accumulates (labels/descriptions/p31/props/stmt_nodes
# /direct_count) so downstream code (flush_entity, edge builders, doc
# builders) is reused verbatim.

def _json_snak_value(snak: dict | None, prop_id: str | None = None) -> str | None:
    """Extract a stored value from a Wikidata JSON snak.

    Returns None for unknown-value / no-value snaks and for datatypes the
    loader doesn't consume (globe-coordinate, geo-shape, tabular-data).
    Values are normalized to the same shape the NT accumulator stores:
      - wikibase-entityid   → "Qxxx" / "Pxxx"
      - string (commonsMedia) → constructed Special:FilePath URL (matches NT)
      - string (plain)      → the literal value
      - time                → ISO-ish string with leading '+' stripped
      - monolingualtext     → the text content
      - quantity            → the amount string (sign prefix stripped)
    """
    if not snak or snak.get("snaktype") != "value":
        return None
    dv = snak.get("datavalue") or {}
    dv_type = dv.get("type")
    dv_value = dv.get("value")

    if dv_type == "wikibase-entityid":
        if isinstance(dv_value, dict):
            qid = dv_value.get("id")
            if isinstance(qid, str) and qid:
                return qid
            num_id = dv_value.get("numeric-id")
            etype = dv_value.get("entity-type", "item")
            if isinstance(num_id, int):
                prefix = "Q" if etype == "item" else ("P" if etype == "property" else None)
                if prefix:
                    return f"{prefix}{num_id}"
        return None

    if dv_type == "string":
        if not isinstance(dv_value, str):
            return None
        # Commons media datatype: filename → Special:FilePath URL, matching NT.
        if snak.get("datatype") == "commonsMedia":
            return f"http://commons.wikimedia.org/wiki/Special:FilePath/{_url_quote(dv_value)}"
        return dv_value

    if dv_type == "time":
        if not isinstance(dv_value, dict):
            return None
        t = dv_value.get("time") or ""
        # JSON serializes CE dates as "+YYYY-..." and BC as "-YYYY-...".
        # parse_date() only splits on "T"; strip the leading '+' so CE dates
        # match the NT form. Keep '-' so BC sign survives (downstream tolerant).
        if t.startswith("+"):
            t = t[1:]
        return t or None

    if dv_type == "monolingualtext":
        if isinstance(dv_value, dict):
            return dv_value.get("text") or None
        return None

    if dv_type == "quantity":
        if isinstance(dv_value, dict):
            amount = dv_value.get("amount") or ""
            return amount.lstrip("+") or None
        return None

    return None


# Edge-relevant properties — any claim for one of these gets a synthetic
# statement node so build_edge_docs()'s qualified path can fire with
# start/end dates from qualifiers.
_EDGE_LIKE_PROPS = (
    set(EDGE_PROPERTIES.keys())
    | set(ORG_REVERSE_PROPERTIES.keys())
    | set(ORG_STRUCTURE_PROPERTIES.keys())
    | set(EVENT_REVERSE_PROPERTIES.keys())
)


def parse_json_entity(obj: dict) -> tuple[str | None, dict | None]:
    """Convert a Wikidata JSON dump entity to the internal (qid, entity) pair.

    Returns (None, None) for non-item entities or malformed objects. The
    returned entity dict matches new_entity()'s schema so flush_entity /
    builders consume it without any format awareness.
    """
    qid = obj.get("id")
    if not isinstance(qid, str) or not qid.startswith("Q"):
        return (None, None)
    if obj.get("type") and obj["type"] != "item":
        # Properties/lexemes live in the same dump but aren't loaded.
        return (None, None)

    entity = new_entity()

    labels = obj.get("labels") or {}
    for lang in ("en", "mul", "it"):
        blk = labels.get(lang)
        if isinstance(blk, dict):
            val = blk.get("value")
            if isinstance(val, str) and val:
                entity["labels"][lang] = val
    if not entity["labels"]:
        for lang in sorted(labels):
            blk = labels.get(lang)
            val = blk.get("value") if isinstance(blk, dict) else None
            if isinstance(val, str) and val:
                entity["labels"]["fallback"] = val
                break

    descs = obj.get("descriptions") or {}
    for lang in ("en", "it"):
        blk = descs.get(lang)
        if isinstance(blk, dict):
            val = blk.get("value")
            if isinstance(val, str) and val:
                entity["descriptions"][lang] = val

    # English aliases for search. Italian aliases are skipped by user decision —
    # en-only keeps the alias list tight and still improves fuzzy search.
    aliases = obj.get("aliases") or {}
    en_aliases = aliases.get("en")
    if isinstance(en_aliases, list):
        for a in en_aliases:
            if isinstance(a, dict):
                v = a.get("value")
                if isinstance(v, str) and v:
                    entity["aliases_en"].append(v)

    # Sitelinks → Wikipedia URLs per language. The JSON dump stores title-only;
    # we reconstruct the canonical URL so the frontend can link out directly.
    sitelinks = obj.get("sitelinks") or {}
    for lang_wiki, url_lang in (("enwiki", "en"), ("itwiki", "it")):
        sl = sitelinks.get(lang_wiki)
        if isinstance(sl, dict):
            title = sl.get("title")
            if isinstance(title, str) and title:
                entity["sitelinks"][url_lang] = (
                    f"https://{url_lang}.wikipedia.org/wiki/{_url_quote(title.replace(' ', '_'))}"
                )

    claims = obj.get("claims") or {}
    stmt_seq = 0
    for prop_id, claim_list in claims.items():
        if not isinstance(claim_list, list):
            continue

        # Statement-rank preference: if any claim for this property has
        # rank="preferred", restrict to preferred claims only. This matches
        # how Wikidata's truthy export behaves (preferred shadows normal).
        # Deprecated is always skipped.
        has_preferred = any(
            isinstance(c, dict) and c.get("rank") == "preferred"
            for c in claim_list
        )

        for claim in claim_list:
            if not isinstance(claim, dict):
                continue
            rank = claim.get("rank")
            if rank == "deprecated":
                continue
            if has_preferred and rank != "preferred":
                continue
            mainsnak = claim.get("mainsnak") or {}
            value = _json_snak_value(mainsnak, prop_id)
            if value is None:
                continue

            entity["direct_count"] += 1

            if prop_id == "P31":
                entity["p31"].append(value)
                continue

            entity["props"].setdefault(prop_id, []).append(value)

            # Synthesize a statement node for edge-relevant claims so qualifiers
            # (P580/P582/P585 dates + P1810 named-as + P1545 ordinal) flow into
            # the edge builder. Non-edge props skip this.
            if prop_id in _EDGE_LIKE_PROPS:
                quals_out: dict[str, str] = {}
                qualifiers = claim.get("qualifiers") or {}
                for qprop in QUAL_PROPS:
                    qsnaks = qualifiers.get(qprop)
                    if not isinstance(qsnaks, list):
                        continue
                    for qsnak in qsnaks:
                        qval = _json_snak_value(qsnak, qprop)
                        if qval is not None:
                            quals_out[qprop] = qval
                            break
                stmt_seq += 1
                statement_guid = claim.get("id")
                if not isinstance(statement_guid, str) or not statement_guid:
                    statement_guid = f"{qid}#{prop_id}#{stmt_seq}"
                stmt_uri = statement_guid
                entity["stmt_nodes"][stmt_uri] = {
                    "prop": prop_id,
                    "value": value,
                    "quals": quals_out,
                    "statement_guid": statement_guid,
                    "rank": rank or "normal",
                }

    return (qid, entity)


def iter_json_entities(stream):
    """Yield entity JSON objects from a Wikidata JSON dump stream.

    The dump is a single top-level JSON array. We consume it line-by-line
    (each entity is on its own line, terminated by ","), skipping the
    opening "[" and closing "]". Any malformed entity fails the phase after
    parsing so a partial dump cannot be reported as successfully ingested.
    """
    bad = 0
    for line in stream:
        s = line.strip()
        if not s or s in ("[", "]"):
            continue
        if s.endswith(","):
            s = s[:-1]
        try:
            yield json.loads(s)
        except json.JSONDecodeError as e:
            bad += 1
            if bad <= 5:
                logger.warning("iter_json_entities: skip malformed line (%s)", e)
            elif bad == 6:
                logger.warning("iter_json_entities: further malformed-line warnings suppressed")
    if bad:
        raise RuntimeError(
            f"Wikidata JSON dump contained {bad} malformed entity line(s)"
        )


def detect_input_format(dump_path: str, explicit: str = "auto") -> str:
    """Resolve the effective input format.

    Precedence: explicit flag > INPUT_FORMAT env > filename extension > "nt".
    Stdin with no explicit setting defaults to "nt" for backward compat.
    """
    explicit = (explicit or "auto").lower()
    if explicit in ("nt", "json"):
        return explicit
    # explicit == auto — fall back to env, then extension
    env = INPUT_FORMAT
    if env in ("nt", "json"):
        return env
    if dump_path and dump_path != "-":
        lower = dump_path.lower()
        if (lower.endswith(".json") or lower.endswith(".json.bz2") or
                lower.endswith(".json.gz") or lower.endswith(".jsonl") or
                lower.endswith(".jsonl.bz2") or lower.endswith(".jsonl.gz")):
            return "json"
        if lower.endswith(".nt") or lower.endswith(".nt.bz2") or lower.endswith(".nt.gz"):
            return "nt"
    return "nt"


def person_matches_scope(citizenships: list[str]) -> bool:
    """Return whether citizenship values satisfy the configured person scope."""
    if PERSON_SCOPE == "global":
        return True
    if PERSON_SCOPE == "countries":
        return any(qid in TARGET_COUNTRY_QIDS for qid in citizenships)
    raise ValueError("PERSON_SCOPE must be 'countries' or 'global'")


# ── Entity classification ────────────────────────────────────────────────────

def classify_entity(p31_qids: list[str]) -> tuple[str, str | None]:
    """Classify entity by P31 values.

    Returns (category, type_label):
      - ("person", None)
      - ("organization", "company")
      - ("event", "battle")
      - ("place", None)       — geographic entity, capture labels only
      - ("skip", None)
      - ("unknown", None)
    """
    for qid in p31_qids:
        if qid in PERSON_QIDS:
            return ("person", None)
    for qid in p31_qids:
        if qid in ORG_QIDS:
            return ("organization", ORG_QIDS[qid])
    for qid in p31_qids:
        if qid in EVENT_QIDS:
            return ("event", EVENT_QIDS[qid])
    for qid in p31_qids:
        if qid in PLACE_QIDS:
            return ("place", None)
    for qid in p31_qids:
        if qid in SKIP_QIDS:
            return ("skip", None)
    return ("unknown", None)


# ── Document builders ────────────────────────────────────────────────────────

def _resolve_label(qid: str, entity_labels: dict) -> str | None:
    """Resolve a referenced QID to a human-readable label.

    English is preferred (the DB is English-centric per CLAUDE.md and EN
    coverage is far more uniform on Wikidata), with Italian as a fallback
    for the rare entities missing an EN label.
    """
    labels = entity_labels.get(qid)
    if not labels:
        return None
    return labels.get("en") or labels.get("it")


def best_label(labels: dict) -> str | None:
    """Return a deterministic display label for an entity in preference order."""
    return (
        labels.get("en")
        or labels.get("mul")
        or labels.get("it")
        or labels.get("fallback")
    )


def _resolve_qid_field(
    entity: dict,
    prop_id: str,
    kind: str,
    entity_labels: dict,
    cap: int = 10,
):
    """Resolve prop_id's QID values to labels via entity_labels.

    Returns str|None for kind="single"; list[str] (possibly empty) for "multi".
    Preserves order; dedups; caps multi at ``cap`` to bound doc size.
    """
    qids = [q for q in entity["props"].get(prop_id, []) if isinstance(q, str) and q.startswith("Q")]
    if kind == "single":
        for q in qids:
            lbl = _resolve_label(q, entity_labels)
            if lbl:
                return lbl
        return None
    out: list[str] = []
    seen: set[str] = set()
    for q in qids:
        lbl = _resolve_label(q, entity_labels)
        if lbl and lbl not in seen:
            seen.add(lbl)
            out.append(lbl)
        if len(out) >= cap:
            break
    return out


def _simple_field_value(entity: dict, prop_id: str, kind: str):
    """Extract the first value for a simple (non-QID) prop, typed by ``kind``.

    kind in {"url","id","str","date","num"}. Returns None if absent/unparsable.
    """
    raw = next(iter(entity["props"].get(prop_id, [])), None)
    if raw is None:
        return None
    if kind == "date":
        return parse_date(raw)
    if kind == "num":
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None
    # url / id / str are all stored verbatim
    return raw


def build_person_doc(
    qid: str,
    entity: dict,
    place_labels: dict,
    entity_labels: dict | None = None,
    award_counts: dict | None = None,
) -> dict | None:
    """Build a person document from accumulated entity triples."""
    name_en = best_label(entity["labels"])
    if not name_en:
        return None

    entity_labels = entity_labels or {}
    award_counts = award_counts or {}

    # Map occupations
    occupations = []
    for occ_qid in entity["props"].get("P106", []):
        if occ_qid in OCCUPATION_MAP:
            occ = OCCUPATION_MAP[occ_qid]
            if occ not in occupations:
                occupations.append(occ)

    # Map gender
    gender = None
    for g_qid in entity["props"].get("P21", []):
        if g_qid in GENDER_MAP:
            gender = GENDER_MAP[g_qid]
            break

    # Map nationalities
    nationalities = []
    for nat_qid in entity["props"].get("P27", []):
        if nat_qid in NATIONALITY_MAP:
            nat = NATIONALITY_MAP[nat_qid]
            if nat not in nationalities:
                nationalities.append(nat)

    # Image URL
    image_url = None
    for url in entity["props"].get("P18", []):
        image_url = url
        break

    # Birth/death place — resolve QID to label via place_labels lookup
    birth_place_id = None
    birth_place = None
    for bp_qid in entity["props"].get("P19", []):
        if bp_qid.startswith("Q"):
            birth_place_id = bp_qid
            labels = place_labels.get(bp_qid)
            if labels:
                birth_place = labels.get("it") or labels.get("en")
            break

    death_place_id = None
    death_place = None
    for dp_qid in entity["props"].get("P20", []):
        if dp_qid.startswith("Q"):
            death_place_id = dp_qid
            labels = place_labels.get(dp_qid)
            if labels:
                death_place = labels.get("it") or labels.get("en")
            break

    # ── Rich fields (gated by notability threshold) ──────────────────────
    # Awards, notable works, cause of death — only populated for
    # sufficiently-statemented persons (default: 50+ direct statements).
    cause_of_death = None
    awards_count: int | None = None
    awards_top5: list[str] = []
    notable_works: list[str] = []

    if entity["direct_count"] >= settings.rich_fields_min_statements:
        # P509 — cause of death (single value; resolve QID → label)
        for cod_qid in entity["props"].get("P509", []):
            if cod_qid.startswith("Q"):
                label = _resolve_label(cod_qid, entity_labels)
                if label:
                    cause_of_death = label
                break

        # P166 — awards: count all, rank by award-entity statement count,
        # then resolve top 5 QIDs → labels (see §1d).
        award_qids = [
            a for a in entity["props"].get("P166", []) if a.startswith("Q")
        ]
        if award_qids:
            awards_count = len(award_qids)
            # Sort DESC by award_counts.get(qid, 0); ties broken by QID for determinism.
            ranked = sorted(
                award_qids,
                key=lambda q: (-award_counts.get(q, 0), q),
            )
            for aq in ranked:
                label = _resolve_label(aq, entity_labels)
                if label:
                    awards_top5.append(label)
                if len(awards_top5) >= 5:
                    break

        # P800 — notable works: cap 10, resolve QID → label, preserve order.
        for work_qid in entity["props"].get("P800", []):
            if not work_qid.startswith("Q"):
                continue
            label = _resolve_label(work_qid, entity_labels)
            if label and label not in notable_works:
                notable_works.append(label)
            if len(notable_works) >= 10:
                break

    doc = {
        "_key": qid,
        "wikidata_id": qid,
        "name_en": name_en,
        "name_it": entity["labels"].get("it"),
        "description_en": entity["descriptions"].get("en"),
        "description_it": entity["descriptions"].get("it"),
        "birth_date": parse_date(next(iter(entity["props"].get("P569", [])), None)),
        "death_date": parse_date(next(iter(entity["props"].get("P570", [])), None)),
        "birth_place": birth_place,
        "birth_place_id": birth_place_id,
        "death_place": death_place,
        "death_place_id": death_place_id,
        "image_url": image_url,
        "occupations": occupations,
        "primary_occupation": occupations[0] if occupations else None,
        "nationalities": nationalities,
        "gender": gender,
        "cause_of_death": cause_of_death,
        "awards_count": awards_count,
        "awards_top5": awards_top5,
        "notable_works": notable_works,
        "aliases_en": entity.get("aliases_en") or None,
        "wikipedia_url_en": entity.get("sitelinks", {}).get("en"),
        "wikipedia_url_it": entity.get("sitelinks", {}).get("it"),
        "wikidata_statements_count": entity["direct_count"],
        "source": "wikidata_dump",
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }

    # Rich QID-valued fields (field of work, religion, instruments, degree,
    # conflicts, etc.) — resolved via entity_labels. Multi-fields capped at 10
    # so a single pathological entity can't blow up the doc.
    for prop_id, (field, kind) in PERSON_QID_LABEL_PROPS.items():
        doc[field] = _resolve_qid_field(entity, prop_id, kind, entity_labels)

    # Location-like person props (residence, burial, work_location) — resolved
    # via entity_labels; fallback to place_labels only.
    for prop_id, field in PERSON_PLACE_LABEL_PROPS.items():
        for q in entity["props"].get(prop_id, []):
            if isinstance(q, str) and q.startswith("Q"):
                lbl = _resolve_label(q, entity_labels)
                if not lbl:
                    pl = place_labels.get(q)
                    if pl:
                        lbl = pl.get("it") or pl.get("en")
                doc[field] = lbl
                doc[f"{field}_id"] = q
                break
        else:
            doc.setdefault(field, None)
            doc.setdefault(f"{field}_id", None)

    # Simple (non-QID) person fields: website, IDs, floruit, native name.
    for prop_id, (field, kind) in PERSON_SIMPLE_PROPS.items():
        doc[field] = _simple_field_value(entity, prop_id, kind)

    return doc


def build_org_doc(qid: str, entity: dict, org_type: str, entity_labels: dict | None = None) -> dict | None:
    """Build an organization document from accumulated entity triples."""
    name_en = best_label(entity["labels"])
    if not name_en:
        return None

    entity_labels = entity_labels or {}

    # Logo (P154) or fallback image (P18)
    image_url = None
    for url in entity["props"].get("P154", []):
        image_url = url
        break
    if not image_url:
        for url in entity["props"].get("P18", []):
            image_url = url
            break

    # P856 — official website (plain URL literal)
    website = None
    for url in entity["props"].get("P856", []):
        if url:
            website = url
            break

    # P159 — headquarters (QID → label)
    headquarters_id = None
    headquarters = None
    for hq_qid in entity["props"].get("P159", []):
        if hq_qid.startswith("Q"):
            headquarters_id = hq_qid
            headquarters = _resolve_label(hq_qid, entity_labels)
            break

    # P17 — country (QID → label). Drives the top-N-per-country filter (3.4).
    country_id = None
    country = None
    for c_qid in entity["props"].get("P17", []):
        if c_qid.startswith("Q"):
            country_id = c_qid
            country = _resolve_label(c_qid, entity_labels)
            break

    # P1128 — number of employees (int literal)
    employees_count = None
    for raw in entity["props"].get("P1128", []):
        try:
            employees_count = int(float(raw))
        except (TypeError, ValueError):
            continue
        break

    doc = {
        "_key": qid,
        "wikidata_id": qid,
        "name_en": name_en,
        "name_it": entity["labels"].get("it"),
        "description_en": entity["descriptions"].get("en"),
        "description_it": entity["descriptions"].get("it"),
        "type": org_type,
        "founded": parse_date(next(iter(entity["props"].get("P571", [])), None)),
        "dissolved": parse_date(next(iter(entity["props"].get("P576", [])), None)),
        "image_url": image_url,
        "website": website,
        "headquarters": headquarters,
        "headquarters_id": headquarters_id,
        "employees_count": employees_count,
        "country": country,
        "country_id": country_id,
        "aliases_en": entity.get("aliases_en") or None,
        "wikipedia_url_en": entity.get("sitelinks", {}).get("en"),
        "wikipedia_url_it": entity.get("sitelinks", {}).get("it"),
        "wikidata_statements_count": entity["direct_count"],
        "source": "wikidata_dump",
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }

    # Rich QID-valued org fields — industries/products drive
    # non-apparent connections ("two companies in the same industry",
    # "both on LSE"), which is the user's primary product goal.
    for prop_id, (field, kind) in ORG_QID_LABEL_PROPS.items():
        doc[field] = _resolve_qid_field(entity, prop_id, kind, entity_labels)

    # Simple org fields — website is already handled above; keep ORG_SIMPLE_PROPS
    # for additions that don't duplicate existing fields.
    for prop_id, (field, kind) in ORG_SIMPLE_PROPS.items():
        doc[field] = _simple_field_value(entity, prop_id, kind)

    return doc


def build_event_doc(qid: str, entity: dict, event_type: str, entity_labels: dict | None = None) -> dict | None:
    """Build an event document from accumulated entity triples."""
    name_en = best_label(entity["labels"])
    if not name_en:
        return None

    entity_labels = entity_labels or {}

    date = parse_date(next(iter(entity["props"].get("P585", [])), None))
    if not date:
        date = parse_date(next(iter(entity["props"].get("P580", [])), None))

    # P276 — location (QID → label)
    location_id = None
    location = None
    for loc_qid in entity["props"].get("P276", []):
        if loc_qid.startswith("Q"):
            location_id = loc_qid
            location = _resolve_label(loc_qid, entity_labels)
            break

    # P495 — country of origin / country (QID → label)
    country_id = None
    country = None
    for c_qid in entity["props"].get("P495", []):
        if c_qid.startswith("Q"):
            country_id = c_qid
            country = _resolve_label(c_qid, entity_labels)
            break

    # P1120 — number of deaths (int literal)
    deaths = None
    for raw in entity["props"].get("P1120", []):
        try:
            deaths = int(float(raw))
        except (TypeError, ValueError):
            continue
        break

    doc = {
        "_key": qid,
        "wikidata_id": qid,
        "name_en": name_en,
        "name_it": entity["labels"].get("it"),
        "description_en": entity["descriptions"].get("en"),
        "description_it": entity["descriptions"].get("it"),
        "type": event_type,
        "date": date,
        "location": location,
        "location_id": location_id,
        "country": country,
        "country_id": country_id,
        "deaths": deaths,
        "source": "wikidata_dump",
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
    return doc


# ── Batch upsert helpers ─────────────────────────────────────────────────────

def _write_edges_raw(db, edges: list[dict], label: str) -> int:
    """Write edges to their collections without orphan check.

    Used for interim flushes during streaming to keep the in-memory buffer
    bounded. Orphaned edges (endpoints not yet / never written to DB) are
    cleaned up after all nodes exist via _cleanup_orphan_edges().
    Returns total count written.
    """
    if not edges or DRY_RUN:
        return 0
    by_col: dict[str, list] = {
        RELATIONSHIPS: [], AFFILIATIONS: [], INVOLVEMENTS: [], ORG_STRUCTURE: []
    }
    for edge in edges:
        col = edge.get("edge_collection")
        if col in by_col:
            doc = {k: v for k, v in edge.items() if k != "edge_collection"}
            by_col[col].append(doc)
    written = 0
    for col, batch in by_col.items():
        for i in range(0, len(batch), BATCH_SIZE):
            written += batch_upsert(db, col, batch[i:i + BATCH_SIZE], f"{label}-{col}-{i}")
    return written


def _cleanup_orphan_edges(db) -> int:
    """Remove orphaned / invalid edges from all edge collections.

    Called after all nodes are written when at least one interim edge flush
    has occurred. Handles two cases:
      1. Either endpoint doesn't exist in any node collection.
      2. An org_structure endpoint is not an organization.

    Organization-origin affiliation edges are valid SEC 13F holdings and must
    never be removed by dump cleanup.
    Returns total edges removed.
    """
    total_removed = 0

    # Case 1: missing endpoint in any collection
    for col in [RELATIONSHIPS, AFFILIATIONS, INVOLVEMENTS, ORG_STRUCTURE]:
        q = f"""
        FOR e IN {col}
          LET src = DOCUMENT(e._from)
          LET tgt = DOCUMENT(e._to)
          FILTER src == null OR tgt == null
          REMOVE e IN {col}
          COLLECT WITH COUNT INTO n
          RETURN n
        """
        try:
            cursor = db.aql.execute(q)
            removed = next(iter(cursor), 0)
            if removed:
                logger.info("  orphan cleanup %s: removed %d", col, removed)
                total_removed += removed
        except Exception as e:
            logger.warning("  orphan cleanup failed for %s: %s", col, e)


    # Case 2: org_structure where an endpoint is not an org
    q = f"""
    FOR e IN {ORG_STRUCTURE}
      FILTER NOT STARTS_WITH(e._from, "{ORGANIZATIONS}/")
         OR NOT STARTS_WITH(e._to,   "{ORGANIZATIONS}/")
      REMOVE e IN {ORG_STRUCTURE}
      COLLECT WITH COUNT INTO n
      RETURN n
    """
    try:
        cursor = db.aql.execute(q)
        removed = next(iter(cursor), 0)
        if removed:
            logger.info("  org_structure non-org cleanup: removed %d", removed)
            total_removed += removed
    except Exception as e:
        logger.warning("  org_structure non-org cleanup failed: %s", e)

    return total_removed


def batch_upsert(db, collection_name: str, docs: list[dict], label: str = "") -> int:
    """Bulk upsert documents. Returns count of successfully upserted docs."""
    if not docs or DRY_RUN:
        return len(docs)
    col = db.collection(collection_name)
    try:
        result = col.import_bulk(docs, on_duplicate="update")
        created = result.get("created", 0)
        updated = result.get("updated", 0)
        errors = result.get("errors", 0)
        if errors:
            logger.warning("Batch %s: %d created, %d updated, %d errors",
                           label, created, updated, errors)
            raise RuntimeError(f"Bulk upsert reported {errors} document error(s)")
        return created + updated
    except Exception as e:
        logger.error("Batch upsert failed for %s: %s", label, e)
        # Fallback: try individual inserts
        count = 0
        failed = 0
        for doc in docs:
            try:
                if col.has(doc["_key"]):
                    col.update(doc)
                else:
                    col.insert(doc)
                count += 1
            except Exception as doc_e:
                failed += 1
                if failed <= 5:
                    logger.warning("  individual upsert failed for %s: %s", doc.get("_key"), doc_e)
        if failed:
            logger.warning("Batch %s: %d/%d individual upserts failed", label, failed, len(docs))
            raise RuntimeError(
                f"Batch {label or collection_name}: {failed}/{len(docs)} documents failed"
            )
        return count


def _apply_doc_updates(col, updates: list[dict], label: str) -> int:
    """Apply a list of {"_key": ..., field: value, ...} patches via col.update().

    Used by the post-stream label-resolution passes (place/rich/org/event).
    Failures are counted and logged (first 5 in detail) instead of being
    silently swallowed — a doc-level update failure here previously vanished
    with no trace. Returns the count of successful updates.
    """
    applied = 0
    failed = 0
    for doc in updates:
        try:
            col.update(doc)
            applied += 1
        except Exception as e:
            failed += 1
            if failed <= 5:
                logger.warning("  %s: update failed for %s: %s", label, doc.get("_key"), e)
    if failed:
        logger.warning("%s: %d/%d doc updates failed", label, failed, len(updates))
    return applied


def merge_occupations(db, person_docs: list[dict]) -> None:
    """For persons already in DB, merge occupations (union) instead of overwrite."""
    col = db.collection(PERSONS)
    qids = [d["_key"] for d in person_docs]

    # Fetch existing occupations in one query
    cursor = db.aql.execute(
        f"FOR d IN {PERSONS} FILTER d._key IN @keys RETURN {{k: d._key, occ: d.occupations}}",
        bind_vars={"keys": qids},
    )
    existing = {r["k"]: r["occ"] or [] for r in cursor}

    for doc in person_docs:
        qid = doc["_key"]
        if qid in existing and existing[qid]:
            merged = list(existing[qid])
            for occ in doc.get("occupations", []):
                if occ not in merged:
                    merged.append(occ)
            doc["occupations"] = merged
            doc["primary_occupation"] = merged[0] if merged else None


# ── Streaming entity accumulator ─────────────────────────────────────────────

def new_entity():
    """Create a fresh entity accumulator."""
    return {
        "labels": {},
        "descriptions": {},
        "p31": [],
        "props": {},
        "direct_count": 0,
        "stmt_nodes": {},  # stmt_uri → {prop, value, quals: {P580: ..., P582: ...}}
        "sitelinks": {},   # lang → wikipedia URL (populated by JSON path only)
        "aliases_en": [],  # English aliases (JSON path only)
    }


def accumulate_triple(entity: dict, predicate: str, value: str, meta: str | None):
    """Add a parsed triple to the current entity accumulator (direct properties)."""
    if predicate == RDFS_LABEL:
        if meta in ("en", "it"):
            entity["labels"][meta] = value

    elif predicate == SCHEMA_DESC:
        if meta in ("en", "it"):
            entity["descriptions"][meta] = value

    elif predicate.startswith(WDT_DIRECT):
        prop_id = predicate[len(WDT_DIRECT):]
        entity["direct_count"] += 1

        if prop_id == "P31":
            qid = extract_qid(value) if value.startswith("http") else value
            if qid:
                entity["p31"].append(qid)
        else:
            # Store the value — either a QID (from URI) or literal
            if value.startswith("http") and WD_ENTITY in value:
                stored = extract_qid(value)
            elif value.startswith("http://commons.wikimedia.org"):
                stored = value  # Keep Commons URLs as-is
            else:
                stored = value  # Literal value (date, string, etc.)

            if stored:
                entity["props"].setdefault(prop_id, []).append(stored)


def accumulate_statement_triple(entity: dict, stmt_uri: str, predicate: str, value: str, meta: str | None):
    """Handle a triple from a statement node (ps:/pq: predicates).

    Extracts:
    - ps:Pxxx → the statement's main value (same as wdt: but per-statement)
    - pq:P580/P582/P585 → qualifier dates (start time, end time, point in time)
    """
    if predicate.startswith(WD_PROP_STMT):
        prop_id = predicate[len(WD_PROP_STMT):]
        # Determine the main value of this statement
        if value.startswith("http") and WD_ENTITY in value:
            stored = extract_qid(value)
        elif value.startswith("http://commons.wikimedia.org"):
            stored = value
        else:
            stored = value

        if stored:
            node = entity["stmt_nodes"].setdefault(stmt_uri, {"prop": None, "value": None, "quals": {}})
            node["prop"] = prop_id
            node["value"] = stored

    elif predicate.startswith(WD_PROP_QUAL):
        qual_prop = predicate[len(WD_PROP_QUAL):]
        if qual_prop in QUAL_PROPS:
            node = entity["stmt_nodes"].setdefault(stmt_uri, {"prop": None, "value": None, "quals": {}})
            node["quals"][qual_prop] = value


# ── Top-N orgs per country filter ───────────────────────────────────────────

def filter_orgs_top_n(
    org_docs: list[dict],
    top_n_per_country: int,
    no_country_cap: int,
) -> list[dict]:
    """Drop long-tail orgs by country.

    Group ``org_docs`` by ``country_id``. For each country, sort DESC by
    ``wikidata_statements_count`` and keep the first ``top_n_per_country``.
    Orgs with no ``country_id`` go in a single "no-country" bucket capped at
    ``no_country_cap`` entries by the same statement-count sort.

    Pure function — no DB / IO. See subtask 3.4 in the project design notes.

    Tiebreaker on equal statement counts: QID lexical order (determinism).
    """
    by_country: dict[str | None, list[dict]] = {}
    for doc in org_docs:
        key = doc.get("country_id") or None
        by_country.setdefault(key, []).append(doc)

    kept: list[dict] = []
    for country_key, docs in by_country.items():
        docs_sorted = sorted(
            docs,
            key=lambda d: (-(d.get("wikidata_statements_count") or 0), d.get("_key", "")),
        )
        cap = no_country_cap if country_key is None else top_n_per_country
        kept.extend(docs_sorted[:cap])
    return kept


# ── Main load function ──────────────────────────────────────────────────────

def load_dump(stream, db, allowed_qids: set | None = None, input_format: str = "nt") -> dict:
    """Streaming load: nodes + edges buffered in memory.

    stream        — iterable of text lines (from open_dump_stream or stdin)
    db            — ArangoDB database handle (None for DRY_RUN)
    allowed_qids  — if provided (pass 2 mode), only process entities in this set;
                    Italian citizenship filter is skipped (already encoded in set).
                    If None (single-pass mode), filter persons to P27=Q38.
    input_format  — "nt" or "json". Switches the streaming driver; all
                    downstream logic (classification, edge building, writes)
                    is format-agnostic.

    Nodes are batch-upserted during streaming. Edges are buffered and inserted
    after all nodes exist, avoiding orphan edges.
    """
    logger.info("=== Loading nodes + buffering edges (single pass) ===")
    start = time.time()

    existing_keys = set()
    if SKIP_EXISTING and not DRY_RUN:
        logger.info("Loading existing keys for resume mode...")
        for col_name in [PERSONS, ORGANIZATIONS, EVENTS]:
            cursor = db.aql.execute(f"FOR d IN {col_name} RETURN d._key")
            existing_keys.update(cursor)
        logger.info("  %d existing entities found", len(existing_keys))

    person_batch = []
    # Orgs are deferred (not streamed to DB in batches) so the top-N-per-country
    # filter can drop long-tail orgs before any write happens.
    all_org_docs: list[dict] = []
    event_batch = []
    person_qids = set()
    all_edges = []  # buffered in memory; flushed periodically via EDGE_FLUSH_INTERVAL
    edges_interim_written = 0  # total edges written by interim flushes
    place_labels = {}  # QID → {"en": "Rome", "it": "Roma"} for birth/death place resolution
    # Generic label lookup for any entity we see in pass2 (awards, works, causes,
    # headquarters, countries, locations). Fills progressively during streaming;
    # late-arriving labels are resolved in a post-stream pass.
    entity_labels: dict[str, dict] = {}
    # Award prestige proxy: QID → wikidata_statements_count. Populated during
    # streaming for entities whose P31 matches AWARD_CLASS_QIDS, then further
    # seeded post-stream with any P166-target QID (§1d / 3.5).
    award_counts: dict[str, int] = {}
    # Statement count snapshot for every allowed entity — used to seed
    # award_counts for P166 targets whose P31 is not an award class.
    entity_statement_counts: dict[str, int] = {}
    # Track QIDs referenced by P166 (person awards) for the 3.5 pass to seed.
    p166_targets: set[str] = set()
    # Per-person raw QID lists for rich-field resolution after streaming.
    # qid → {"cod": qid|None, "awards": [qid,...], "works": [qid,...]}
    persons_pending_rich: dict[str, dict] = {}
    # Per-org/event referenced-place QIDs pending late label resolution.
    # qid → {"hq": qid|None, "country": qid|None}     (orgs)
    # qid → {"location": qid|None, "country": qid|None} (events)
    orgs_pending_labels: dict[str, dict] = {}
    events_pending_labels: dict[str, dict] = {}
    unknown_types = Counter()

    stats = {
        "lines": 0, "entities": 0,
        "persons": 0, "organizations": 0, "events": 0,
        "orgs_candidates": 0, "orgs_dropped": 0,
        "places_captured": 0,
        "skipped_notable": 0, "skipped_existing": 0,
        "skipped_type": 0, "unknown_type": 0,
        "edges_buffered": 0,
        "stmt_nodes_parsed": 0,
        "qualified_edges": 0,
    }

    current_qid = None
    current_entity = new_entity()

    def flush_entity(qid, entity):
        """Process a completed entity: build node doc + buffer edges."""
        if not qid or not entity["p31"]:
            return

        # Two-pass mode: skip if not in allowed set (fast path — ~99% of entities)
        if allowed_qids is not None and qid not in allowed_qids:
            return

        # Capture labels of every allowed entity for later QID→label resolution
        # (awards, notable works, causes of death, headquarters, countries, …).
        # Cheap: bounded by allowed_qids size.
        if entity["labels"]:
            entity_labels[qid] = dict(entity["labels"])

        # Remember statement counts so the 3.5 post-stream seeding pass can
        # fill award_counts for P166 targets whose P31 does not classify them
        # as an award (the plan §1d calls for seeding from any referenced
        # P166 target, not just P31-marked awards/orders/film-awards).
        entity_statement_counts[qid] = entity["direct_count"]

        # Track award prestige (§1d / 3.5): for any entity whose P31 marks it
        # as an award/order/film-award, remember its statement count directly.
        if any(q in AWARD_CLASS_QIDS for q in entity["p31"]):
            award_counts[qid] = entity["direct_count"]

        if SKIP_EXISTING and qid in existing_keys:
            stats["skipped_existing"] += 1
            return

        stats["entities"] += 1
        category, type_label = classify_entity(entity["p31"])

        if category == "place":
            # Capture labels for birth/death place resolution, don't create a node
            if entity["labels"]:
                place_labels[qid] = dict(entity["labels"])
                stats["places_captured"] += 1
            return

        if category == "skip":
            stats["skipped_type"] += 1
            return

        if category == "unknown":
            stats["unknown_type"] += 1
            for p31 in entity["p31"]:
                unknown_types[p31] += 1
            return

        # Notability filter
        if entity["direct_count"] < MIN_STATEMENTS:
            stats["skipped_notable"] += 1
            return

        # Track statement node stats
        if entity["stmt_nodes"]:
            stats["stmt_nodes_parsed"] += len(entity["stmt_nodes"])

        if category == "person":
            # Single-pass mode: filter to configured person scope
            if allowed_qids is None and not person_matches_scope(entity["props"].get("P27", [])):
                stats["skipped_notable"] += 1
                return
            # Seed 3.5 award ranking: remember every P166 target referenced by
            # a captured person so its statement count can be filled in later.
            award_qids = [a for a in entity["props"].get("P166", []) if a.startswith("Q")]
            work_qids = [w for w in entity["props"].get("P800", []) if w.startswith("Q")]
            cod_qid = next(
                (c for c in entity["props"].get("P509", []) if c.startswith("Q")),
                None,
            )
            for aq in award_qids:
                p166_targets.add(aq)
            doc = build_person_doc(qid, entity, place_labels, entity_labels, award_counts)
            if doc:
                person_batch.append(doc)
                person_qids.add(qid)
                stats["persons"] += 1
                # Remember raw QIDs so a post-stream pass can rebuild any field
                # that resolves to labels, since many target QIDs (awards,
                # genres, conflicts, concepts) are flushed AFTER the person
                # who references them and aren't in entity_labels yet at
                # build-time.
                rich_qid_props_raw = {
                    p: [q for q in entity["props"].get(p, []) if isinstance(q, str) and q.startswith("Q")]
                    for p in PERSON_QID_LABEL_PROPS
                }
                rich_place_props_raw = {
                    p: next(
                        (q for q in entity["props"].get(p, []) if isinstance(q, str) and q.startswith("Q")),
                        None,
                    )
                    for p in PERSON_PLACE_LABEL_PROPS
                }
                has_any_rich = (
                    award_qids or work_qids or cod_qid
                    or any(rich_qid_props_raw.values())
                    or any(rich_place_props_raw.values())
                )
                if entity["direct_count"] >= settings.rich_fields_min_statements and has_any_rich:
                    persons_pending_rich[qid] = {
                        "cod": cod_qid,
                        "awards": award_qids,
                        "works": work_qids,
                        "qid_props": rich_qid_props_raw,
                        "place_props": rich_place_props_raw,
                    }
                # Buffer edges from this person's properties
                if not SKIP_EDGES:
                    edges = build_edge_docs(qid, entity)
                    all_edges.extend(edges)
                    stats["edges_buffered"] += len(edges)

        elif category == "organization":
            doc = build_org_doc(qid, entity, type_label, entity_labels)
            if doc:
                # Do NOT batch-write during streaming. Org writes are deferred
                # until after streaming so the top-N-per-country filter (§1e)
                # can drop long-tail orgs before any DB write happens.
                all_org_docs.append(doc)
                stats["organizations"] += 1
                # Remember org's headquarters/country QIDs so a post-stream pass
                # can fill any labels that arrived after the org. Also capture
                # industries/products/legal_form/etc. for re-resolution —
                # same late-label problem as persons.
                hq_id = doc.get("headquarters_id")
                c_id = doc.get("country_id")
                org_qid_props_raw = {
                    p: [q for q in entity["props"].get(p, []) if isinstance(q, str) and q.startswith("Q")]
                    for p in ORG_QID_LABEL_PROPS
                }
                if hq_id or c_id or any(org_qid_props_raw.values()):
                    orgs_pending_labels[qid] = {
                        "hq": hq_id,
                        "country": c_id,
                        "qid_props": org_qid_props_raw,
                    }
                # Buffer reverse edges: org properties pointing to persons.
                # Edges to dropped orgs are removed by the existing orphan
                # filter (which checks endpoint existence in PERSONS/ORGS/EVENTS).
                if not SKIP_EDGES:
                    edges = build_reverse_edge_docs(qid, entity, ORGANIZATIONS, ORG_REVERSE_PROPERTIES)
                    all_edges.extend(edges)
                    stats["edges_buffered"] += len(edges)
                    # Org→org structure edges (P749/P355/P127/P1830). Endpoint-type
                    # check (both orgs) happens at insert time via org_qids set.
                    org_struct_edges = build_org_structure_edge_docs(qid, entity)
                    all_edges.extend(org_struct_edges)
                    stats["edges_buffered"] += len(org_struct_edges)

        elif category == "event":
            doc = build_event_doc(qid, entity, type_label, entity_labels)
            if doc:
                event_batch.append(doc)
                stats["events"] += 1
                loc_id = doc.get("location_id")
                c_id = doc.get("country_id")
                if loc_id or c_id:
                    events_pending_labels[qid] = {"location": loc_id, "country": c_id}
                # Buffer reverse edges: event properties pointing to persons
                if not SKIP_EDGES:
                    edges = build_reverse_edge_docs(qid, entity, EVENTS, EVENT_REVERSE_PROPERTIES)
                    all_edges.extend(edges)
                    stats["edges_buffered"] += len(edges)

        # Flush node batches when full
        if len(person_batch) >= BATCH_SIZE:
            if not DRY_RUN:
                merge_occupations(db, person_batch)
                batch_upsert(db, PERSONS, person_batch, f"persons-{stats['persons']}")
            person_batch.clear()

        if len(event_batch) >= BATCH_SIZE:
            if not DRY_RUN:
                batch_upsert(db, EVENTS, event_batch, f"events-{stats['events']}")
            event_batch.clear()

    # ── Stream the dump ──────────────────────────────────────────────────────
    if input_format == "json":
        # JSON path: each line is one pre-assembled entity. Parse → flush.
        # No per-subject buffering needed; no current_qid/current_entity usage.
        for obj in iter_json_entities(stream):
            stats["lines"] += 1
            qid, entity = parse_json_entity(obj)
            if qid and entity is not None:
                flush_entity(qid, entity)

            if stats["lines"] % 100_000 == 0:
                elapsed = time.time() - start
                logger.info(
                    "  %dK entities | %d persons, %d orgs, %d events | %d edges buf | %.0fs",
                    stats["lines"] // 1000,
                    stats["persons"], stats["organizations"], stats["events"],
                    len(all_edges), elapsed,
                )
                if len(all_edges) >= EDGE_FLUSH_INTERVAL:
                    logger.info("  Edge buffer at %d — flushing interim...", len(all_edges))
                    written = _write_edges_raw(db, all_edges, f"interim-{stats['lines'] // 1000}K")
                    logger.info("  Interim flush: %d edges written", written)
                    edges_interim_written += written
                    all_edges.clear()
    else:
        for line in stream:
            stats["lines"] += 1

            parsed = parse_nt_line(line)
            if not parsed:
                continue

            subject, predicate, value, meta = parsed

            subj_qid = extract_qid(subject)

            if subj_qid:
                # Regular entity triple
                if subj_qid != current_qid:
                    if current_qid:
                        flush_entity(current_qid, current_entity)
                    current_qid = subj_qid
                    current_entity = new_entity()

                accumulate_triple(current_entity, predicate, value, meta)

            else:
                # Check if this is a statement node triple (ps:/pq:)
                # Note: truthy dump has no statement nodes — this path is a no-op,
                # but kept for compatibility with full/filtered dumps.
                parent_qid = extract_statement_parent(subject)
                if parent_qid and parent_qid == current_qid:
                    accumulate_statement_triple(current_entity, subject, predicate, value, meta)

            if stats["lines"] % 1_000_000 == 0:
                elapsed = time.time() - start
                logger.info(
                    "  %dM lines | %d persons, %d orgs, %d events | %d edges buf | %.0fs",
                    stats["lines"] // 1_000_000,
                    stats["persons"], stats["organizations"], stats["events"],
                    len(all_edges), elapsed,
                )
                if len(all_edges) >= EDGE_FLUSH_INTERVAL:
                    logger.info("  Edge buffer at %d — flushing interim...", len(all_edges))
                    written = _write_edges_raw(db, all_edges, f"interim-{stats['lines'] // 1_000_000}M")
                    logger.info("  Interim flush: %d edges written", written)
                    edges_interim_written += written
                    all_edges.clear()

        # NT path only — JSON path flushes each entity as it arrives.
        if current_qid:
            flush_entity(current_qid, current_entity)

    # Apply top-N-per-country filter BEFORE writing orgs. Orgs dropped here
    # never enter the DB, and any edges pointing at them are later eliminated
    # by the existing orphan filter in the edge-insert step.
    kept_org_qids: set[str] = set()
    if all_org_docs:
        kept_orgs = filter_orgs_top_n(
            all_org_docs,
            top_n_per_country=settings.org_top_n_per_country,
            no_country_cap=settings.org_no_country_cap,
        )
        kept_org_qids = {d["_key"] for d in kept_orgs}
        dropped = len(all_org_docs) - len(kept_orgs)
        stats["orgs_candidates"] = len(all_org_docs)
        stats["orgs_dropped"] = dropped
        logger.info(
            "  Org filter: %d candidates → %d kept (%d dropped; top-%d per country, %d cap no-country)",
            len(all_org_docs),
            len(kept_orgs),
            dropped,
            settings.org_top_n_per_country,
            settings.org_no_country_cap,
        )
        stats["organizations"] = len(kept_orgs)
        # Drop label-pending entries for orgs that didn't make the cut.
        for qid in list(orgs_pending_labels.keys()):
            if qid not in kept_org_qids:
                orgs_pending_labels.pop(qid, None)
    else:
        kept_orgs = []

    if not DRY_RUN:
        if person_batch:
            merge_occupations(db, person_batch)
            batch_upsert(db, PERSONS, person_batch, "persons-final")
        if kept_orgs:
            for i in range(0, len(kept_orgs), BATCH_SIZE):
                batch = kept_orgs[i:i + BATCH_SIZE]
                batch_upsert(db, ORGANIZATIONS, batch, f"orgs-{i // BATCH_SIZE}")
        if event_batch:
            batch_upsert(db, EVENTS, event_batch, "events-final")

    node_elapsed = time.time() - start
    logger.info("=== Nodes Complete (%.0fs) ===", node_elapsed)
    logger.info("  Lines parsed:        %d", stats["lines"])
    logger.info("  Persons:             %d", stats["persons"])
    logger.info("  Organizations:       %d", stats["organizations"])
    logger.info("  Events:              %d", stats["events"])
    logger.info("  Places captured:     %d", stats["places_captured"])
    logger.info("  Edges buffered:      %d", stats["edges_buffered"])
    logger.info("  Statement nodes:     %d", stats["stmt_nodes_parsed"])
    logger.info("  Skipped (notable):   %d", stats["skipped_notable"])
    logger.info("  Skipped (type):      %d", stats["skipped_type"])
    logger.info("  Skipped (existing):  %d", stats["skipped_existing"])
    logger.info("  Unknown P31 types:   %d", stats["unknown_type"])

    if unknown_types:
        logger.info("  Top unknown P31 types:")
        for p31, count in unknown_types.most_common(30):
            logger.info("    %s: %d", p31, count)

    # ── Resolve remaining birth/death place labels ────────────────────────
    # Some place entities may appear AFTER the persons that reference them.
    # Do a final pass to fill in any unresolved place names.
    if place_labels and not DRY_RUN:
        logger.info("=== Resolving birth/death place labels (%d places known) ===", len(place_labels))
        resolve_start = time.time()

        # Find persons with birth_place_id or death_place_id but no label
        cursor = db.aql.execute(f"""
            FOR p IN {PERSONS}
                FILTER (p.birth_place_id != null AND p.birth_place == null)
                    OR (p.death_place_id != null AND p.death_place == null)
                RETURN {{k: p._key, bp: p.birth_place_id, dp: p.death_place_id}}
        """)
        updates = []
        for row in cursor:
            update = {"_key": row["k"]}
            changed = False
            if row["bp"] and row["bp"] in place_labels:
                labels = place_labels[row["bp"]]
                update["birth_place"] = labels.get("it") or labels.get("en")
                changed = True
            if row["dp"] and row["dp"] in place_labels:
                labels = place_labels[row["dp"]]
                update["death_place"] = labels.get("it") or labels.get("en")
                changed = True
            if changed:
                updates.append(update)

        if updates:
            col = db.collection(PERSONS)
            resolved = 0
            for i in range(0, len(updates), BATCH_SIZE):
                batch = updates[i:i + BATCH_SIZE]
                resolved += _apply_doc_updates(col, batch, "place-labels")
            logger.info("  Resolved %d/%d place labels in %.0fs", resolved, len(updates), time.time() - resolve_start)

    # ── Seed award_counts from P166 targets (§1d / 3.5) ──────────────────
    # Some P166 targets are not themselves classed as Q618779/Q193622/Q4220917
    # (e.g. titles of nobility, honorific positions). Their statement count is
    # still the best available prestige signal, so fill award_counts from the
    # generic entity_statement_counts map for any P166 target we haven't
    # already scored.
    if p166_targets:
        seeded = 0
        for aq in p166_targets:
            if aq in award_counts:
                continue
            sc = entity_statement_counts.get(aq)
            if sc is not None:
                award_counts[aq] = sc
                seeded += 1
        if seeded:
            logger.info(
                "  Award prestige: %d P31-classed + %d P166-target-seeded = %d ranked QIDs",
                len(award_counts) - seeded,
                seeded,
                len(award_counts),
            )

    # ── Resolve rich person fields (awards, works, cause_of_death) ────────
    # At streaming time, award/work/cause target entities may not have been
    # seen yet, so labels were unresolved. Now that the whole dump has been
    # streamed, entity_labels and award_counts are complete — rebuild these
    # fields for every person we buffered and patch the DB.
    if persons_pending_rich and not DRY_RUN:
        logger.info(
            "=== Resolving rich person fields for %d persons ===",
            len(persons_pending_rich),
        )
        resolve_start = time.time()
        rich_updates = []
        for person_qid, pending in persons_pending_rich.items():
            cod_label = _resolve_label(pending["cod"], entity_labels) if pending["cod"] else None

            award_qids = pending["awards"]
            awards_count_val: int | None = len(award_qids) if award_qids else None
            top5: list[str] = []
            if award_qids:
                ranked = sorted(award_qids, key=lambda q: (-award_counts.get(q, 0), q))
                for aq in ranked:
                    label = _resolve_label(aq, entity_labels)
                    if label:
                        top5.append(label)
                    if len(top5) >= 5:
                        break

            works: list[str] = []
            for wq in pending["works"]:
                label = _resolve_label(wq, entity_labels)
                if label and label not in works:
                    works.append(label)
                if len(works) >= 10:
                    break

            update = {"_key": person_qid}
            if cod_label:
                update["cause_of_death"] = cod_label
            if awards_count_val is not None:
                update["awards_count"] = awards_count_val
            if top5:
                update["awards_top5"] = top5
            if works:
                update["notable_works"] = works

            # Re-resolve QID-valued rich fields with the now-complete
            # entity_labels. Streaming-time resolution missed labels for
            # entities flushed after the referencing person.
            for prop_id, qids in pending.get("qid_props", {}).items():
                if not qids:
                    continue
                field, kind = PERSON_QID_LABEL_PROPS[prop_id]
                if kind == "single":
                    for q in qids:
                        lbl = _resolve_label(q, entity_labels)
                        if lbl:
                            update[field] = lbl
                            break
                else:
                    seen: set[str] = set()
                    out: list[str] = []
                    for q in qids:
                        lbl = _resolve_label(q, entity_labels)
                        if lbl and lbl not in seen:
                            seen.add(lbl)
                            out.append(lbl)
                        if len(out) >= 10:
                            break
                    if out:
                        update[field] = out

            for prop_id, q in pending.get("place_props", {}).items():
                if not q:
                    continue
                field = PERSON_PLACE_LABEL_PROPS[prop_id]
                lbl = _resolve_label(q, entity_labels)
                if not lbl:
                    pl = place_labels.get(q)
                    if pl:
                        lbl = pl.get("it") or pl.get("en")
                if lbl:
                    update[field] = lbl
                    update[f"{field}_id"] = q

            if len(update) > 1:
                rich_updates.append(update)

        if rich_updates:
            col = db.collection(PERSONS)
            resolved = 0
            for i in range(0, len(rich_updates), BATCH_SIZE):
                batch = rich_updates[i:i + BATCH_SIZE]
                resolved += _apply_doc_updates(col, batch, "rich-fields")
            logger.info(
                "  Resolved rich fields for %d/%d persons in %.0fs",
                resolved,
                len(rich_updates),
                time.time() - resolve_start,
            )

    # ── Resolve org headquarters/country labels ──────────────────────────
    if orgs_pending_labels and not DRY_RUN:
        logger.info(
            "=== Resolving org headquarters/country labels for %d orgs ===",
            len(orgs_pending_labels),
        )
        resolve_start = time.time()
        org_updates = []
        for org_qid, pending in orgs_pending_labels.items():
            update = {"_key": org_qid}
            hq_label = _resolve_label(pending["hq"], entity_labels) if pending["hq"] else None
            c_label = _resolve_label(pending["country"], entity_labels) if pending["country"] else None
            if hq_label:
                update["headquarters"] = hq_label
            if c_label:
                update["country"] = c_label

            # Re-resolve industries/products/legal_form/etc.
            for prop_id, qids in pending.get("qid_props", {}).items():
                if not qids:
                    continue
                field, kind = ORG_QID_LABEL_PROPS[prop_id]
                if kind == "single":
                    for q in qids:
                        lbl = _resolve_label(q, entity_labels)
                        if lbl:
                            update[field] = lbl
                            break
                else:
                    seen: set[str] = set()
                    out: list[str] = []
                    for q in qids:
                        lbl = _resolve_label(q, entity_labels)
                        if lbl and lbl not in seen:
                            seen.add(lbl)
                            out.append(lbl)
                        if len(out) >= 10:
                            break
                    if out:
                        update[field] = out

            if len(update) > 1:
                org_updates.append(update)
        if org_updates:
            col = db.collection(ORGANIZATIONS)
            resolved = 0
            for i in range(0, len(org_updates), BATCH_SIZE):
                resolved += _apply_doc_updates(col, org_updates[i:i + BATCH_SIZE], "org-labels")
            logger.info(
                "  Resolved labels for %d/%d orgs in %.0fs",
                resolved,
                len(org_updates),
                time.time() - resolve_start,
            )

    # ── Resolve event location/country labels ────────────────────────────
    if events_pending_labels and not DRY_RUN:
        logger.info(
            "=== Resolving event location/country labels for %d events ===",
            len(events_pending_labels),
        )
        resolve_start = time.time()
        event_updates = []
        for event_qid, pending in events_pending_labels.items():
            update = {"_key": event_qid}
            loc_label = _resolve_label(pending["location"], entity_labels) if pending["location"] else None
            c_label = _resolve_label(pending["country"], entity_labels) if pending["country"] else None
            if loc_label:
                update["location"] = loc_label
            if c_label:
                update["country"] = c_label
            if len(update) > 1:
                event_updates.append(update)
        if event_updates:
            col = db.collection(EVENTS)
            resolved = 0
            for i in range(0, len(event_updates), BATCH_SIZE):
                resolved += _apply_doc_updates(col, event_updates[i:i + BATCH_SIZE], "event-labels")
            logger.info(
                "  Resolved labels for %d/%d events in %.0fs",
                resolved,
                len(event_updates),
                time.time() - resolve_start,
            )

    # ── Insert edges (all nodes now exist) ───────────────────────────────
    if all_edges and not SKIP_EDGES:
        logger.info("=== Inserting %d buffered edges ===", len(all_edges))
        edge_start = time.time()

        # Build existing QID set for orphan filtering, plus org-only set for
        # the org_structure endpoint-type check (both endpoints must be orgs).
        existing_qids: set[str] = set()
        org_qids: set[str] = set()
        if not DRY_RUN:
            for col_name in [PERSONS, ORGANIZATIONS, EVENTS]:
                cursor = db.aql.execute(f"FOR d IN {col_name} RETURN d._key")
                existing_qids.update(cursor)
            cursor = db.aql.execute(f"FOR d IN {ORGANIZATIONS} RETURN d._key")
            org_qids.update(cursor)
            logger.info("  %d nodes in DB for orphan check (%d orgs)", len(existing_qids), len(org_qids))

        # Filter and group by collection
        edge_batches = {RELATIONSHIPS: [], AFFILIATIONS: [], INVOLVEMENTS: [], ORG_STRUCTURE: []}
        edges_created = 0
        edges_orphaned = 0
        edges_qualified = 0

        for edge in all_edges:
            target_qid = edge["_to"].split("/")[1]
            source_qid = edge["_from"].split("/")[1]
            if target_qid not in existing_qids or source_qid not in existing_qids:
                edges_orphaned += 1
                continue
            collection = edge.pop("edge_collection")
            # org_structure: both endpoints must be organizations (§2b endpoint-type check)
            if collection == ORG_STRUCTURE:
                if target_qid not in org_qids or source_qid not in org_qids:
                    edges_orphaned += 1
                    continue
            # affiliations: source must NOT be an org (catches P127 org-owner case
            # where ORG_REVERSE_PROPERTIES emits persons/org_qid→orgs/entity — invalid)
            elif collection == AFFILIATIONS and source_qid in org_qids:
                edges_orphaned += 1
                continue
            if edge.get("start_date") or edge.get("end_date"):
                edges_qualified += 1
            edge_batches[collection].append(edge)
            edges_created += 1

            # Flush when batch is full
            if len(edge_batches[collection]) >= BATCH_SIZE:
                if not DRY_RUN:
                    batch_upsert(db, collection, edge_batches[collection],
                                 f"edges-{collection}-{edges_created}")
                edge_batches[collection].clear()

        # Flush remaining
        if not DRY_RUN:
            for col_name, batch in edge_batches.items():
                if batch:
                    batch_upsert(db, col_name, batch, f"edges-{col_name}-final")

        stats["edges_created"] = edges_created
        stats["edges_orphaned"] = edges_orphaned
        stats["qualified_edges"] = edges_qualified
        logger.info("  Edges created:     %d", edges_created)
        logger.info("  Edges w/ dates:    %d", edges_qualified)
        logger.info("  Edges orphaned:    %d", edges_orphaned)
        logger.info("  Edge insert time:  %.0fs", time.time() - edge_start)

    # ── Post-load orphan cleanup (for edges written during interim flushes) ──
    if edges_interim_written > 0 and not SKIP_EDGES and not DRY_RUN:
        logger.info(
            "=== Orphan cleanup for %d interim-flushed edges ===",
            edges_interim_written,
        )
        cleanup_start = time.time()
        removed = _cleanup_orphan_edges(db)
        logger.info(
            "  Total orphans removed: %d in %.0fs",
            removed,
            time.time() - cleanup_start,
        )

    return stats


# ── Pass 1: Identification ────────────────────────────────────────────────────

_REVERSE_CONTAINER_PROPERTIES = (
    set(ORG_REVERSE_PROPERTIES) | set(EVENT_REVERSE_PROPERTIES)
)


def _resolve_pass1_containers(
    containers: dict[str, tuple[set[str], set[str]]],
    target_persons: set[str],
    needed_refs: set[str],
) -> int:
    """Include containers linked to selected people, independent of dump order."""
    pending = [
        qid
        for qid, (person_targets, _related_refs) in containers.items()
        if qid in needed_refs or person_targets.intersection(target_persons)
    ]
    queued = set(pending)
    included: set[str] = set()

    while pending:
        qid = pending.pop()
        if qid in included:
            continue
        candidate = containers.get(qid)
        if candidate is None:
            continue
        included.add(qid)
        needed_refs.add(qid)
        for related_qid in candidate[1]:
            needed_refs.add(related_qid)
            if related_qid in containers and related_qid not in queued:
                queued.add(related_qid)
                pending.append(related_qid)
    return len(included)


def _json_claim_qids(claims: dict, property_ids: set[str]) -> set[str]:
    """Return non-deprecated QID values for the requested JSON claim keys."""
    qids: set[str] = set()
    for property_id in property_ids:
        claim_list = claims.get(property_id) or []
        if not isinstance(claim_list, list):
            continue
        for claim in claim_list:
            if not isinstance(claim, dict) or claim.get("rank") == "deprecated":
                continue
            value = _json_snak_value(claim.get("mainsnak") or {}, property_id)
            if isinstance(value, str) and value.startswith("Q"):
                qids.add(value)
    return qids


def _run_pass1_nt(stream, target_persons: set, needed_refs: set) -> int:
    """Select people and reverse-edge containers from an N-Triples stream."""
    start = time.time()

    current_qid: str | None = None
    current_p31: list[str] = []
    current_p27: list[str] = []
    current_edge_targets: list[str] = []
    current_property_targets: dict[str, set[str]] = {}
    current_direct_count = 0
    containers: dict[str, tuple[set[str], set[str]]] = {}
    lines = 0

    def flush_pass1(qid):
        if not qid:
            return
        if "Q5" in current_p31:
            if not person_matches_scope(current_p27):
                return
            if current_direct_count < PASS1_MIN_STATEMENTS:
                return
            target_persons.add(qid)
            needed_refs.update(current_edge_targets)
            return

        is_org = any(value in ORG_QIDS for value in current_p31)
        is_event = any(value in EVENT_QIDS for value in current_p31)
        if not is_org and not is_event:
            return
        reverse_properties = (
            set(ORG_REVERSE_PROPERTIES) if is_org else set(EVENT_REVERSE_PROPERTIES)
        )
        person_targets = {
            target
            for property_id in reverse_properties
            for target in current_property_targets.get(property_id, set())
        }
        related_properties = set(LABEL_REF_PROPERTIES)
        if is_org:
            related_properties.update(ORG_STRUCTURE_PROPERTIES)
        related_refs = {
            target
            for property_id in related_properties
            for target in current_property_targets.get(property_id, set())
        }
        if person_targets or related_refs:
            containers[qid] = (person_targets, related_refs)

    for line in stream:
        lines += 1
        parsed = parse_nt_line(line)
        if not parsed:
            continue

        subject, predicate, value, meta = parsed
        subj_qid = extract_qid(subject)
        if not subj_qid:
            continue

        if subj_qid != current_qid:
            flush_pass1(current_qid)
            current_qid = subj_qid
            current_p31 = []
            current_p27 = []
            current_edge_targets = []
            current_property_targets = {}
            current_direct_count = 0

        if predicate.startswith(WDT_DIRECT):
            current_direct_count += 1
            prop_id = predicate[len(WDT_DIRECT):]
            if prop_id == "P31":
                qid_val = extract_qid(value) if value.startswith("http") else None
                if qid_val:
                    current_p31.append(qid_val)
            elif prop_id == "P27":
                qid_val = extract_qid(value) if value.startswith("http") else None
                if qid_val:
                    current_p27.append(qid_val)
            elif (
                prop_id in EDGE_PROPERTIES
                or prop_id in LABEL_REF_PROPERTIES
                or prop_id in ORG_REVERSE_PROPERTIES
                or prop_id in ORG_STRUCTURE_PROPERTIES
                or prop_id in EVENT_REVERSE_PROPERTIES
            ):
                target = extract_qid(value) if WD_ENTITY in value else None
                if target:
                    current_property_targets.setdefault(prop_id, set()).add(target)
                    if prop_id in EDGE_PROPERTIES or prop_id in LABEL_REF_PROPERTIES:
                        current_edge_targets.append(target)

        if lines % 1_000_000 == 0:
            elapsed = time.time() - start
            logger.info(
                "  %dM lines | %d target persons | %d refs | %.0fs",
                lines // 1_000_000, len(target_persons), len(needed_refs), elapsed,
            )

    flush_pass1(current_qid)
    included = _resolve_pass1_containers(containers, target_persons, needed_refs)
    logger.info("  Reverse/reference containers included: %d", included)
    return lines


def _run_pass1_json(stream, target_persons: set, needed_refs: set) -> int:
    """Select people and reverse-edge containers from a Wikidata JSON stream."""
    start = time.time()
    entities = 0
    bad = 0
    skipped_prefilter = 0
    containers: dict[str, tuple[set[str], set[str]]] = {}
    reverse_markers = tuple(f'"{prop}"' for prop in _REVERSE_CONTAINER_PROPERTIES)

    for raw in stream:
        s = raw.strip()
        if not s or s in ("[", "]"):
            continue
        if s.endswith(","):
            s = s[:-1]
        entities += 1

        is_person_candidate = '"P31"' in s and '"Q5"' in s
        is_container_candidate = any(marker in s for marker in reverse_markers)
        if not is_person_candidate and not is_container_candidate:
            skipped_prefilter += 1
            if entities % 100_000 == 0:
                elapsed = time.time() - start
                logger.info(
                    "  %dK entities | %d persons | %d refs | prefilter-skip %d | %.0fs",
                    entities // 1000, len(target_persons), len(needed_refs),
                    skipped_prefilter, elapsed,
                )
            continue

        try:
            obj = json.loads(s)
        except json.JSONDecodeError as e:
            bad += 1
            if bad <= 5:
                logger.warning("_run_pass1_json: malformed line (%s)", e)
            continue

        qid = obj.get("id")
        if not isinstance(qid, str) or not qid.startswith("Q"):
            continue
        if obj.get("type") and obj["type"] != "item":
            continue

        claims = obj.get("claims") or {}

        p31_qids = _json_claim_qids(claims, {"P31"})
        is_person = "Q5" in p31_qids
        if not is_person:
            is_org = bool(p31_qids.intersection(ORG_QIDS))
            is_event = bool(p31_qids.intersection(EVENT_QIDS))
            if is_org or is_event:
                reverse_properties = (
                    set(ORG_REVERSE_PROPERTIES)
                    if is_org
                    else set(EVENT_REVERSE_PROPERTIES)
                )
                related_properties = set(LABEL_REF_PROPERTIES)
                if is_org:
                    related_properties.update(ORG_STRUCTURE_PROPERTIES)
                person_targets = _json_claim_qids(claims, reverse_properties)
                related_refs = _json_claim_qids(claims, related_properties)
                if person_targets or related_refs:
                    containers[qid] = (person_targets, related_refs)
            if entities % 100_000 == 0:
                elapsed = time.time() - start
                logger.info(
                    "  %dK entities | %d target persons | %d refs | %.0fs",
                    entities // 1000, len(target_persons), len(needed_refs), elapsed,
                )
            continue

        # Country scope needs P27; global scope includes people without P27.
        in_target_country = PERSON_SCOPE == "global"
        for c in claims.get("P27") or []:
            if in_target_country:
                break
            if not isinstance(c, dict) or c.get("rank") == "deprecated":
                continue
            v = _json_snak_value(c.get("mainsnak") or {}, "P27")
            if person_matches_scope([v]):
                in_target_country = True
                break
        if not in_target_country:
            if entities % 100_000 == 0:
                elapsed = time.time() - start
                logger.info(
                    "  %dK entities | %d target persons | %d refs | %.0fs",
                    entities // 1000, len(target_persons), len(needed_refs), elapsed,
                )
            continue

        # Notability shortcut — total non-deprecated direct statements.
        stmt_count = 0
        for claim_list in claims.values():
            if isinstance(claim_list, list):
                for c in claim_list:
                    if isinstance(c, dict) and c.get("rank") != "deprecated":
                        stmt_count += 1
        if stmt_count < PASS1_MIN_STATEMENTS:
            if entities % 100_000 == 0:
                elapsed = time.time() - start
                logger.info(
                    "  %dK entities | %d target persons | %d refs | %.0fs",
                    entities // 1000, len(target_persons), len(needed_refs), elapsed,
                )
            continue

        target_persons.add(qid)

        # Collect edge-target and label-ref QIDs from this kept person.
        for prop_id, claim_list in claims.items():
            if prop_id not in EDGE_PROPERTIES and prop_id not in LABEL_REF_PROPERTIES:
                continue
            if not isinstance(claim_list, list):
                continue
            for c in claim_list:
                if not isinstance(c, dict) or c.get("rank") == "deprecated":
                    continue
                v = _json_snak_value(c.get("mainsnak") or {}, prop_id)
                if isinstance(v, str) and v.startswith("Q"):
                    needed_refs.add(v)

        if entities % 100_000 == 0:
            elapsed = time.time() - start
            logger.info(
                "  %dK entities | %d persons | %d refs | prefilter-skip %d | %.0fs",
                entities // 1000, len(target_persons), len(needed_refs),
                skipped_prefilter, elapsed,
            )

    if bad:
        raise RuntimeError(
            f"Wikidata JSON pass 1 encountered {bad} malformed entity line(s)"
        )
    included = _resolve_pass1_containers(containers, target_persons, needed_refs)
    logger.info(
        "_run_pass1_json done: %d entities, %d prefilter-skipped (%.1f%%), "
        "%d reverse/reference containers included",
        entities, skipped_prefilter,
        (skipped_prefilter / entities * 100.0) if entities else 0.0,
        included,
    )
    return entities


def run_pass1(stream, output_dir: str, input_format: str = "nt") -> tuple[set, set]:
    """Pass 1: find selected persons and their referenced QIDs.

    Dispatches to the NT or JSON loop based on ``input_format``. Both paths
    apply the PASS1_MIN_STATEMENTS notability shortcut so low-statement
    persons are skipped before their edge targets enter ``needed_refs``.

    Writes target_persons.txt and needed_refs.txt to output_dir.
    Returns (target_persons, needed_refs) sets.
    """
    logger.info("=== Pass 1: Identifying selected persons ===")
    logger.info("  Input format:           %s", input_format)
    logger.info("  Notability threshold:   %d statements", PASS1_MIN_STATEMENTS)
    start = time.time()

    target_persons: set[str] = set()
    needed_refs: set[str] = set()

    if input_format == "json":
        processed = _run_pass1_json(stream, target_persons, needed_refs)
        unit = "entities"
    else:
        processed = _run_pass1_nt(stream, target_persons, needed_refs)
        unit = "lines"

    elapsed = time.time() - start
    logger.info("=== Pass 1 Complete (%.0fs) ===", elapsed)
    logger.info("  %s:          %d", unit.capitalize(), processed)
    logger.info("  Target persons: %d", len(target_persons))
    logger.info("  Needed refs:    %d", len(needed_refs))

    os.makedirs(output_dir, exist_ok=True)
    persons_path = os.path.join(output_dir, PASS1_PERSONS_FILE)
    refs_path = os.path.join(output_dir, "needed_refs.txt")

    with open(persons_path, "w") as f:
        for qid in sorted(target_persons):
            f.write(qid + "\n")
    logger.info("  Wrote %s", persons_path)

    with open(refs_path, "w") as f:
        for qid in sorted(needed_refs):
            f.write(qid + "\n")
    logger.info("  Wrote %s", refs_path)

    return target_persons, needed_refs


def load_pass1_files(output_dir: str) -> set:
    """Load QID files written by pass 1. Returns combined allowed_qids set."""
    persons_path = os.path.join(output_dir, PASS1_PERSONS_FILE)
    legacy_persons_path = os.path.join(output_dir, PASS1_LEGACY_PERSONS_FILE)
    if not os.path.exists(persons_path) and os.path.exists(legacy_persons_path):
        logger.warning("Using legacy pass-1 file %s; rerun pass 1 to write %s", legacy_persons_path, persons_path)
        persons_path = legacy_persons_path
    refs_path = os.path.join(output_dir, "needed_refs.txt")

    if not os.path.exists(persons_path) or not os.path.exists(refs_path):
        logger.error("Pass 1 output files not found in %s — run --pass1 first", output_dir)
        sys.exit(1)

    allowed: set[str] = set()
    with open(persons_path) as f:
        for line in f:
            qid = line.strip()
            if qid:
                allowed.add(qid)
    with open(refs_path) as f:
        for line in f:
            qid = line.strip()
            if qid:
                allowed.add(qid)

    logger.info("Loaded %d allowed QIDs from pass 1 files", len(allowed))
    return allowed


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Wikidata Dump Loader (NTriples or JSON)")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--pass1", action="store_true",
                            help="Pass 1: identify selected persons, write QID files")
    mode_group.add_argument("--pass2", action="store_true",
                            help="Pass 2: load entities from QID files into ArangoDB")
    parser.add_argument("--input-format", choices=["auto", "nt", "json"], default="auto",
                        help="Dump format. 'auto' dispatches on DUMP_FILE extension; "
                             "stdin defaults to 'nt' unless INPUT_FORMAT env is set.")
    parser.add_argument("--preflight", action="store_true",
                        help="Validate source and scope without reading the dump or touching ArangoDB")
    args = parser.parse_args()

    # DUMP_URL deliberately takes precedence so a detached run cannot silently
    # fall back to the developer machine's local truthy dump.
    source = DUMP_URL.strip() or DUMP_FILE
    use_stdin = not source or source == "-"
    dump_desc = "stdin" if use_stdin else source
    effective_format = detect_input_format(source, args.input_format)

    logger.info("=== Wikidata Dump Loader ===")
    logger.info("Mode:            %s", "pass1" if args.pass1 else "pass2" if args.pass2 else "single-pass")
    logger.info("Input:           %s", dump_desc)
    logger.info("Input format:    %s", effective_format)
    logger.info("Min statements:  %d", MIN_STATEMENTS)
    logger.info("Person scope:    %s", PERSON_SCOPE)
    logger.info("Batch size:      %d", BATCH_SIZE)
    logger.info("Dry run:         %s", DRY_RUN)

    try:
        metadata = validate_dump_source(source)
        person_matches_scope([])  # validates PERSON_SCOPE even for empty P27.
    except (FileNotFoundError, ValueError, OSError) as exc:
        logger.error("Invalid dump source or scope: %s", exc)
        sys.exit(1)

    logger.info("Source metadata: %s", json.dumps(metadata, sort_keys=True))
    if args.preflight:
        logger.info("Preflight complete; no dump data or database was touched")
        return

    start = time.time()

    if args.pass1:
        stream = open_dump_stream(source)
        run_pass1(stream, PASS1_OUTPUT_DIR, input_format=effective_format)

    elif args.pass2:
        allowed_qids = load_pass1_files(PASS1_OUTPUT_DIR)
        db = None if DRY_RUN else init_db()
        stream = open_dump_stream(source)
        stats = load_dump(stream, db, allowed_qids=allowed_qids, input_format=effective_format)
        elapsed = time.time() - start
        _log_final_stats(stats, elapsed)

    else:
        # Single-pass mode (for pre-filtered or small dumps)
        logger.info("Skip existing:   %s", SKIP_EXISTING)
        logger.info("Skip edges:      %s", SKIP_EDGES)
        db = None if DRY_RUN else init_db()
        stream = open_dump_stream(source)
        stats = load_dump(stream, db, allowed_qids=None, input_format=effective_format)
        elapsed = time.time() - start
        _log_final_stats(stats, elapsed)


def _log_final_stats(stats: dict, elapsed: float):
    logger.info("=== Load Complete ===")
    logger.info("  Persons:       %d", stats.get("persons", 0))
    logger.info("  Organizations: %d", stats.get("organizations", 0))
    logger.info("  Events:        %d", stats.get("events", 0))
    logger.info("  Places:        %d", stats.get("places_captured", 0))
    logger.info("  Edges:         %d", stats.get("edges_created", 0))
    logger.info("  Edges orphaned:%d", stats.get("edges_orphaned", 0))
    logger.info("  Total time:    %.0f minutes", elapsed / 60)


if __name__ == "__main__":
    main()
