#!/usr/bin/env python3
"""Marketing language-bridge network miner — read-only.

The concept: language communities are linked when *the same people* speak both.
Wikidata records a person's languages in ``persons.languages_spoken`` (P1412)
and, when present, ``persons.native_language`` (P103). Every person who speaks
two or more languages adds one unordered pair of language edges; the edge weight
is how many such people the two languages share. Node size is how many people
speak the language at all (within the multilingual population).

This is the linguistic analogue of the dual-nationality network: the world as a
web of bilingual speakers rather than nation states.

It never writes to ArangoDB. One bounded READ-ONLY pass pulls every person with
``LENGTH(languages_spoken) >= 1`` and ``wikidata_statements_count >= 30`` (the
statement floor has a persistent index, so it bounds the scan), then all
normalisation, pair counting and examples happen in Python. Language variants
and historical spellings are mapped to a canonical human name before pairing
(``American English`` -> ``English``, ``Brazilian Portuguese`` -> ``Portuguese``,
``Greek`` -> ``Modern Greek`` and so on), so the numbers are per *language*, and
the JSON keeps the strongest example people for every drawn edge.

Outputs (both written by default):

  langs.json   machine-readable payload: nodes, edges, clusters, bridges, scale
  langs.html   brand-styled card sheet (1600x1000) to screenshot to langs.png

Run it manually inside the backend container:

    docker compose exec -T backend python - --out-dir /tmp/langs \\
        < backend/scripts/marketing_langs.py
    docker compose exec -T backend cat /tmp/langs/langs.json \\
        > docs/marketing/langs/langs.json
    docker compose exec -T backend cat /tmp/langs/langs.html \\
        > docs/marketing/langs/langs.html

Then render (from scripts/marketing/):

    source env.sh
    node render-insight-cards.js ../../docs/marketing/langs/langs.html \\
        ../../docs/marketing/langs
    mv ../../docs/marketing/langs/card-01.png ../../docs/marketing/langs/langs.png

Flags:
    --out-dir PATH       output directory (default: docs/marketing/langs)
    --min-statements N   Wikidata-statement floor, the notability bound (default 30)
    --min-pair N         shared-people floor for an edge (default 25)
    --scan-limit N       hard safety cap on the single scan (default 400000)
    --node-floor N       minimum speakers for a language to be drawn (default 250)
    --max-nodes N        cap on languages kept by speaker count (default 70)
    --from-json PATH     re-render the HTML from an existing langs.json
    --no-html            emit only the JSON payload
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from collections import OrderedDict, defaultdict
from datetime import date
from pathlib import Path

# When run as a file inside the container this resolves to /app; when piped
# over stdin (``python -``) __file__ is "<stdin>" and /app is cwd, so fall back.
try:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
except (NameError, OSError):  # pragma: no cover - stdin invocation
    sys.path.insert(0, str(Path.cwd()))

from app.database import get_arango_client, get_db  # noqa: E402

DEFAULT_MIN_STATEMENTS = 30
DEFAULT_MIN_PAIR = 60
# The scan now keeps people with a *single* recorded language too (they are the
# denominator of the "everyone with this language recorded" reading), so the cap
# has to sit above that population: ~831k rows at the 30-statement floor. It is
# a safety valve for a lowered floor, not a sampling budget — if it ever binds,
# the count is truncated and the rates stop meaning anything.
DEFAULT_SCAN_LIMIT = 1_500_000
DEFAULT_NODE_FLOOR = 250
DEFAULT_MAX_NODES = 70
DEFAULT_MAX_EDGES = 4000

# The orange ramp (opacity/width) grows with the count; everything drawn is a
# real link. DRAW_EDGE_MIN matches --min-pair so the picture and the payload
# report the same edge set.
STRONG_EDGE = 1500
DRAW_EDGE_MIN = 60

# ---------------------------------------------------------------------------
# Language normalisation — variant / historical spellings -> canonical name.
# Applied per person *before* pairing, so counts are per language.
# ---------------------------------------------------------------------------

NORMALIZE: dict[str, str] = {
    "American English": "English",
    "British English": "English",
    "Australian English": "English",
    "Canadian English": "English",
    "Irish English": "English",
    "Scottish English": "English",
    "Old English": "English",
    "Middle English": "English",
    "Brazilian Portuguese": "Portuguese",
    "European Portuguese": "Portuguese",
    "Greek": "Modern Greek",
    "Swiss German": "German",
    "Austrian German": "German",
    "Standard High German": "German",
    "medieval Latin": "Latin",
    "Medieval Latin": "Latin",
    "Modern Hebrew": "Hebrew",
    "Nynorsk": "Norwegian",
    "Bokmål": "Norwegian",
    "Canadian French": "French",
    "Old French": "French",
    "Middle French": "French",
    "Standard Chinese": "Chinese",
    "Mandarin": "Chinese",
    "Mandarin Chinese": "Chinese",
    "Standard Taiwanese Mandarin": "Chinese",
    "Egyptian Arabic": "Arabic",
    "Moroccan Arabic": "Arabic",
    "Algerian Arabic": "Arabic",
    "Tunisian Arabic": "Arabic",
    "Levantine Arabic": "Arabic",
    "Gulf Arabic": "Arabic",
    "Iraqi Arabic": "Arabic",
    "Yemeni Arabic": "Arabic",
    "Sudanese Arabic": "Arabic",
    "Hejazi Arabic": "Arabic",
    "Flemish": "Dutch",
    "Ottoman Turkish": "Turkish",
    "Yue Chinese": "Cantonese",
    "Taiwanese Mandarin": "Chinese",
    # Croatian, Serbian, Bosnian and Montenegrin are the national standards of
    # one pluricentric language; Wikidata records them as four separate labels,
    # which drew four dots for one language (and four dots that all tie to each
    # other). Collapse them so the node is a language, not a standard.
    "Croatian": "Serbo-Croatian",
    "Serbian": "Serbo-Croatian",
    "Bosnian": "Serbo-Croatian",
    "Montenegrin": "Serbo-Croatian",
}

# ---------------------------------------------------------------------------
# Display groups — hand-curated reading frames. Several are language families,
# but some are regions or compound frames, because the record is thin per
# family (African, East & SE Asian, Isolates & other). The card says "group",
# never "family", so a regional frame never implies a genealogy that isn't
# there. The family a language belongs to is a separate, curatorial field used
# by the companion charts (see FAMILY_OF below). The edge data is entirely
# data-driven; every drawn language belongs to exactly one group.
# ---------------------------------------------------------------------------

CLUSTER_SPEC = [
    ("germanic", "Germanic"),
    ("romance", "Romance & Latin"),
    ("slavic", "Slavic"),
    ("greek", "Greek"),
    ("uralic_turkic", "Uralic & Turkic"),
    ("semitic", "Arabic & Semitic"),
    ("iranian", "Iranian"),
    ("indo_dravidian", "Indo-Aryan & Dravidian"),
    ("eastasia", "East & Southeast Asian"),
    ("celtic", "Celtic"),
    ("baltic", "Baltic"),
    ("african", "African"),
    ("other", "Isolates & other"),
]

CLUSTER_ORDER = [c[0] for c in CLUSTER_SPEC]
CLUSTER_LABEL = {c[0]: c[1] for c in CLUSTER_SPEC}

FAMILY: dict[str, str] = {}


def _fam(cluster: str, names: str) -> None:
    for n in names.split("|"):
        FAMILY[n.strip()] = cluster


_fam("germanic", "English|German|Dutch|Swedish|Danish|Norwegian|Icelandic|"
                 "Yiddish|Afrikaans|Low German|Luxembourgish|Scots|Faroese|"
                 "Pennsylvania German|Swiss German|Gothic")
_fam("romance", "French|Spanish|Italian|Portuguese|Catalan|Romanian|Galician|"
                "Occitan|Latin|Sardinian|Sicilian|Venetian|Neapolitan|Corsican|"
                "Aragonese|Asturian|Lombard|Piedmontese|Mirandese|Walloon|"
                "Romansh|Friulian|Emilian")
_fam("slavic", "Russian|Polish|Czech|Ukrainian|Slovak|Slovene|Serbo-Croatian|"
               "Bulgarian|Belarusian|Macedonian|Rusyn|"
               "Upper Sorbian|Lower Sorbian|Church Slavonic|Old Church Slavonic")
_fam("greek", "Modern Greek|Ancient Greek|Greek")
_fam("uralic_turkic", "Finnish|Estonian|Hungarian|Turkish|Azerbaijani|Tatar|"
                      "Kazakh|Uzbek|Turkmen|Kyrgyz|Bashkir|Chuvash|Uyghur|"
                      "Karelian|Sami|Livonian|Votic")
_fam("semitic", "Arabic|Hebrew|Maltese|Aramaic|Syriac|Coptic|"
                "Ancient Egyptian|Phoenician|Akkadian|Sumerian")
# Iranian (Persian, Kurdish, Pashto, …) and the South Asian families
# (Indo-Aryan and Dravidian) are separate groups: calling Hindi or Tamil
# "Iranian" was simply wrong, and both sit under one roof only geographically.
_fam("iranian", "Persian|Kurdish|Pashto|Balochi|Tajik|Dari|Avestan|Pahlavi|"
                "Old Persian|Middle Persian|Sogdian")
_fam("indo_dravidian", "Hindi|Urdu|Bangla|Bengali|Punjabi|Marathi|Sanskrit|"
                       "Nepali|Gujarati|Sinhala|Romani|Tamil|Telugu|Malayalam|"
                       "Kannada|Odia|Oriya|Assamese|Sindhi|Pali|Prakrit|"
                       "Bhojpuri|Sinhalese")
_fam("eastasia", "Chinese|Classical Chinese|Old Mandarin|Cantonese|Yue Chinese|"
                 "Japanese|Korean|Vietnamese|Thai|Lao|Indonesian|Malay|Tagalog|"
                 "Javanese|Taiwanese Hokkien|Hokkien|Burmese|Tibetan|Khmer|"
                 "Mongolian|Sundanese|Cebuano|Ilocano|Maori|Hawaiian|"
                 "Samoan|Tongan|Fijian")
_fam("celtic", "Irish|Welsh|Breton|Scottish Gaelic|Manx|Cornish|Scots Gaelic")
_fam("baltic", "Lithuanian|Latvian|Old Prussian")
_fam("african", "Yoruba|Igbo|Swahili|Hausa|Amharic|Tigrinya|Zulu|Xhosa|Shona|"
                "Wolof|Fula|Somali|Oromo|Berber|Kabyle|Tuareg|Twi|Ewe|Fon|"
                "Kongo|Lingala|Sango|Nubian|Krio|Nigerian Pidgin|Malagasy")
_fam("other", "Basque|Georgian|Esperanto|Quechua|Nahuatl|Maya|Inuit|Navajo|"
              "Haida|Ainu|Haitian Creole|Tok Pisin|Sranan Tongo|"
              "Papamiento|Volapuk|Ido|Interlingua|Lojban|"
              "American Sign Language|French Sign Language|"
              "British Sign Language|Sign language|"
              # Armenian and Albanian are independent Indo-European branches:
              # neither is Greek, and pairing them with Greek implied a kinship
              # the record does not support. They live in the residual frame.
              "Armenian|Albanian")

# ---------------------------------------------------------------------------
# Genealogical family — a *separate*, hand-curated field for the companion
# charts (the "family or contact?" split). This is not a Wikidata property and
# not the same as the ring groups: the ring uses broader reading frames, this
# uses the linguistic branch each language belongs to. Where a language is a
# creole, a constructed language or an isolate, the branch is named as such so
# the split is never silently mislabelled. Only the drawn languages need an
# entry; anything missing falls back to its ring group so nothing is lost.
# ---------------------------------------------------------------------------

FAMILY_OF: dict[str, str] = {}


def _branch(branch: str, names: str) -> None:
    for n in names.split("|"):
        FAMILY_OF[n.strip()] = branch


_branch("Germanic", "English|German|Dutch|Swedish|Danish|Norwegian|Icelandic|"
                    "Yiddish|Afrikaans|Low German|Luxembourgish|Scots|Faroese|"
                    "Gothic")
_branch("Romance", "French|Spanish|Italian|Portuguese|Catalan|Romanian|"
                   "Galician|Occitan|Latin|Sardinian|Sicilian|Venetian|"
                   "Neapolitan|Corsican|Aragonese|Asturian|Lombard|Mirandese|"
                   "Walloon|Romansh|Friulian")
_branch("Slavic", "Russian|Polish|Czech|Ukrainian|Slovak|Slovene|"
                  "Serbo-Croatian|Bulgarian|Belarusian|Macedonian|Rusyn|"
                  "Upper Sorbian|Lower Sorbian|Church Slavonic|"
                  "Old Church Slavonic")
_branch("Hellenic", "Modern Greek|Ancient Greek|Greek")
_branch("Armenian", "Armenian")
_branch("Albanian", "Albanian")
_branch("Baltic", "Lithuanian|Latvian|Old Prussian")
_branch("Celtic", "Irish|Welsh|Breton|Scottish Gaelic|Manx|Cornish|Scots Gaelic")
_branch("Iranian", "Persian|Kurdish|Pashto|Balochi|Tajik|Dari|Avestan|Pahlavi|"
                   "Old Persian|Middle Persian|Sogdian")
_branch("Indo-Aryan", "Hindi|Urdu|Bangla|Bengali|Punjabi|Marathi|Sanskrit|"
                      "Nepali|Gujarati|Sinhala|Sinhalese|Romani|Odia|Oriya|"
                      "Assamese|Sindhi|Pali|Prakrit|Bhojpuri")
_branch("Dravidian", "Tamil|Telugu|Malayalam|Kannada")
_branch("Uralic", "Finnish|Estonian|Hungarian|Karelian|Sami|Livonian|Votic")
_branch("Turkic", "Turkish|Azerbaijani|Tatar|Kazakh|Uzbek|Turkmen|Kyrgyz|"
                  "Bashkir|Chuvash|Uyghur")
_branch("Semitic", "Arabic|Hebrew|Maltese|Aramaic|Syriac|"
                   "Ancient Egyptian|Phoenician|Akkadian|Sumerian")
_branch("Berber", "Berber|Kabyle|Tuareg")
_branch("Chadic", "Hausa")
_branch("Niger-Congo", "Yoruba|Igbo|Swahili|Zulu|Xhosa|Shona|Wolof|Fula|"
                       "Kongo|Lingala|Sango|Twi|Ewe|Fon|Malagasy|Krio")
_branch("Creole", "Nigerian Pidgin|Haitian Creole|Tok Pisin|Sranan Tongo|"
                  "Papamiento")
_branch("Sinitic", "Chinese|Cantonese|Classical Chinese|Old Mandarin")
_branch("Japonic", "Japanese")
_branch("Koreanic", "Korean")
_branch("Kra-Dai", "Thai|Lao")
_branch("Austroasiatic", "Vietnamese|Khmer")
_branch("Austronesian", "Indonesian|Malay|Tagalog|Javanese|Cebuano|Ilocano|"
                        "Sundanese|Maori|Hawaiian|Samoan|Tongan|Fijian|"
                        "Taiwanese Hokkien|Hokkien")
_branch("Sino-Tibetan", "Burmese|Tibetan")
_branch("Kartvelian", "Georgian")
_branch("Isolate", "Basque|Ainu|Haida|Navajo|Inuit|Maya|Nahuatl|Quechua")
_branch("Constructed", "Esperanto|Volapuk|Ido|Interlingua|Lojban")
_branch("Sign language", "American Sign Language|French Sign Language|"
                         "British Sign Language|Sign language")


# Languages that are historical/classical rather than living community
# languages. Drawn hollow so they never read as a living community, and
# excluded from headline "tightest bridge" candidacy.
HISTORICAL = {"Latin", "Ancient Greek", "Classical Chinese", "Old Mandarin",
              "Sanskrit", "Ottoman Turkish", "Old Church Slavonic",
              "Church Slavonic", "Avestan", "Pahlavi", "Old Persian",
              "Middle Persian", "Sogdian", "Gothic", "Old English",
              "Middle English", "Old French", "Middle French",
              "Ancient Egyptian", "Akkadian", "Sumerian", "Phoenician",
              "Aramaic", "Syriac", "Pali", "Prakrit", "Old Prussian"}

# ---------------------------------------------------------------------------
# Rendering geometry (card units; the PNG renders at 2x)
# ---------------------------------------------------------------------------

CARD_W = 1600
CARD_H = 1000
NET_W = 1020          # chord-diagram panel, left of the reading key
NET_H = 740
NODE_R_MIN = 4.6
NODE_R_MAX = 23.0
MAX_ANNOT = 7
MAX_LANGS_PER_ANNOT = 2  # diversity cap so no language hogs the callouts

# Radial ring geometry, in card units of arc length.
DOT_GAP = 6.0         # arc room between neighbouring language dots
FAMILY_GAP = 20.0     # arc room between language families
LABEL_SIZE = 10.6      # language label size
FAMILY_SIZE = 11.0    # family label size (renderer shrinks it to fit the arc)
# Arc reserved for a group name. Deliberately smaller than FAMILY_SIZE: the
# renderer shrinks a name to fit whatever arc it gets, so reserving room for
# every long name at full size (the old behaviour) inflated the whole ring.
FAMILY_RESERVE = 8.0
CHORD_BOW = 0.30      # how far chord control points are pulled to the centre

# Categorical family palette: ten accessible, well-separated hues that sit on
# the warm off-white. None of them is the brand orange, which stays reserved
# for the strongest-bridge highlight. Every colour here appears in the legend.
FAMILY_COLOR = {
    "germanic": "#2f6f9f",           # steel blue
    "romance": "#c15a86",            # rose
    "slavic": "#7f8a2e",             # olive
    "greek": "#1f8f8a",              # teal
    "uralic_turkic": "#5a5aa8",      # indigo-violet
    "semitic": "#9a3f9a",            # plum
    "iranian": "#a02030",            # crimson
    "indo_dravidian": "#7b4ea3",     # violet
    "eastasia": "#b8912c",           # gold
    "celtic": "#3f7a4a",             # forest green
    "baltic": "#8a6d3b",             # bronze
    "african": "#a0522d",            # sienna
    "other": "#6e6259",              # warm taupe
}

# Short group names for the rim arcs. They are *faithful* abbreviations: a rim
# label never drops half of a compound group's name (the old "E. ASIA" claimed
# East Asia for a group that also holds Southeast Asia). If a name is too long
# for its arc the renderer shrinks the type, it does not shorten the claim.
FAMILY_SHORT = {
    "germanic": "GERMANIC",
    "romance": "ROMANCE & LATIN",
    "slavic": "SLAVIC",
    "greek": "GREEK",
    "uralic_turkic": "URALIC & TURKIC",
    "semitic": "ARABIC & SEMITIC",
    "iranian": "IRANIAN",
    "indo_dravidian": "INDO-ARYAN & DRAVIDIAN",
    "eastasia": "EAST & SOUTHEAST ASIAN",
    "celtic": "CELTIC",
    "baltic": "BALTIC",
    "african": "AFRICAN",
    "other": "ISOLATES & OTHER",
}

# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------

LANGS_AQL = """
FOR p IN persons
  FILTER p.wikidata_statements_count >= @min_statements
  LET set = UNIQUE(APPEND(
     (p.native_language != null ? [p.native_language] : []),
     (p.languages_spoken != null ? p.languages_spoken : [])
  ))
  FILTER LENGTH(set) >= 1
  LIMIT @scan_limit
  RETURN {n: p.name_en, q: p.wikidata_id,
          s: p.wikidata_statements_count, ns: set}
"""


def _rows(db, aql: str, bind: dict | None = None) -> list:
    try:
        return list(db.aql.execute(aql, bind_vars=bind or {}))
    except Exception as exc:  # noqa: BLE001 - a marketing job must not crash
        print(f"[warn] query failed: {exc}", file=sys.stderr)
        return []


# ---------------------------------------------------------------------------
# Collection + aggregation (all in Python, one bounded DB pass)
# ---------------------------------------------------------------------------


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _clean_langs(row: dict) -> list[str]:
    raw = _as_list(row.get("ns"))
    out = []
    for l in raw:
        if not isinstance(l, str):
            continue
        l = l.strip()
        if not l:
            continue
        out.append(NORMALIZE.get(l, l))
    return sorted(set(out))


def collect(min_statements: int, min_pair: int, scan_limit: int,
            max_nodes: int, max_edges: int) -> dict:
    db = get_db(get_arango_client(request_timeout=900))
    total_persons = _rows(db, "RETURN LENGTH(persons)")

    # Stream the scan instead of materialising it: at the 30-statement floor the
    # language population is ~831k people and only 114k of them are multilingual.
    # Monolinguals never form a pair, so they only need a denominator.
    people: list[tuple] = []                       # (name, qid, stmts, langs)
    speakers_all: dict[str, int] = defaultdict(int)
    n_with_language = 0
    n_monolingual = 0
    try:
        cursor = db.aql.execute(LANGS_AQL, bind_vars={
            "min_statements": min_statements, "scan_limit": scan_limit})
        for row in cursor:
            langs = _clean_langs(row)
            if not langs:
                continue
            n_with_language += 1
            for l in langs:
                speakers_all[l] += 1
            if len(langs) < 2:
                n_monolingual += 1
                continue
            try:
                stmts = int(row.get("s") or 0)
            except (TypeError, ValueError):
                stmts = 0
            name = row.get("n") or row.get("q") or "Unknown"
            people.append((name, row.get("q") or None, stmts, langs))
    except Exception as exc:  # noqa: BLE001 - a marketing job must not crash
        print(f"[warn] language scan failed: {exc}", file=sys.stderr)

    if not people:
        raise SystemExit("language scan returned nothing; check the DB")

    # --- pass 1: speaker counts and pair counts ----------------------------
    speakers: dict[str, int] = defaultdict(int)
    pair_count: dict[tuple, int] = defaultdict(int)
    for _n, _q, _s, langs in people:
        for l in langs:
            speakers[l] += 1
        for i in range(len(langs)):
            for j in range(i + 1, len(langs)):
                pair_count[(langs[i], langs[j])] += 1

    # --- pick drawn languages ---------------------------------------------
    ranked = sorted(speakers.items(), key=lambda kv: (-kv[1], kv[0]))
    node_rows = [OrderedDict(lang=l, speakers=s,
                             speakers_all=speakers_all.get(l, s))
                 for l, s in ranked[:max_nodes]]
    kept = {n["lang"] for n in node_rows}

    # --- pass 2: examples only for qualifying edges -----------------------
    qualifying = {p for p, c in pair_count.items()
                  if c >= min_pair and p[0] in kept and p[1] in kept}
    examples: dict[tuple, list] = defaultdict(list)
    for name, _q, stmts, langs in people:
        for i in range(len(langs)):
            for j in range(i + 1, len(langs)):
                p = (langs[i], langs[j])
                if p not in qualifying:
                    continue
                ex = examples[p]
                if len(ex) < 8:
                    ex.append((stmts, name))
                elif stmts > ex[0][0]:
                    ex.sort()
                    ex[0] = (stmts, name)

    edges = []
    for p in sorted(qualifying, key=lambda p: -pair_count[p]):
        ex = sorted(examples.get(p, []), key=lambda t: -t[0])[:4]
        edges.append(OrderedDict(a=p[0], b=p[1], count=pair_count[p],
                                 examples=[name for _s, name in ex]))
    edges = edges[:max_edges]

    drawn = {n["lang"] for n in node_rows}
    edges = [e for e in edges if e["a"] in drawn and e["b"] in drawn]
    nodes = [n for n in node_rows if n["lang"] in drawn]

    return OrderedDict(
        generated=date.today().isoformat(),
        concept="language_bridge_network",
        thresholds=OrderedDict(
            min_statements=min_statements,
            min_pair=min_pair,
            scan_limit=scan_limit,
            strong_edge=STRONG_EDGE,
            draw_edge_min=DRAW_EDGE_MIN,
        ),
        scale=OrderedDict(
            total_persons=int(total_persons[0]) if total_persons else 0,
            multilingua_people=len(people),
            people_with_language=n_with_language,
            monolingual_people=n_monolingual,
            languages_total=len(speakers),
            nodes_drawn=len(nodes),
            edges_drawn=len(edges),
            clusters_drawn=0,
            people_3plus=sum(1 for _n, _q, _s, l in people if len(l) >= 3),
            people_4plus=sum(1 for _n, _q, _s, l in people if len(l) >= 4),
            max_pair=max((e["count"] for e in edges), default=0),
            min_drawn_pair=min((e["count"] for e in edges), default=0),
        ),
        nodes=nodes,
        edges=edges,
    )


# ---------------------------------------------------------------------------
# Analysis: clusters, radii, bridges
# ---------------------------------------------------------------------------


def _pretty_lang(lang: str) -> str:
    return lang


def analyze(payload: dict, node_floor: int) -> dict:
    sp = {n["lang"]: n["speakers"] for n in payload["nodes"]}
    # Everyone with the language recorded, not only the multilinguals. This is
    # the denominator of the companion "of everyone recorded with X" reading;
    # it falls back to the multilingual count for payloads cached before it
    # existed.
    sp_all = {n["lang"]: n.get("speakers_all", n["speakers"])
              for n in payload["nodes"]}
    kept = {l: s for l, s in sp.items() if s >= node_floor}
    if not kept:
        raise SystemExit("no languages above --node-floor; lower the floor")
    payload["nodes"] = [OrderedDict(lang=l, speakers=s)
                        for l, s in sorted(kept.items(), key=lambda kv: (-kv[1], kv[0]))]
    drawn = set(kept)
    edges = [e for e in payload["edges"]
             if e["a"] in drawn and e["b"] in drawn]
    payload["edges"] = edges

    degree: dict[str, int] = defaultdict(int)
    for e in edges:
        degree[e["a"]] += 1
        degree[e["b"]] += 1

    smin, smax = min(kept.values()), max(kept.values())

    def radius(s: int) -> float:
        if smax <= smin:
            return NODE_R_MIN + 6
        t = (s - smin) / (smax - smin)
        return NODE_R_MIN + (NODE_R_MAX - NODE_R_MIN) * math.sqrt(max(0.0, t))

    def clus(lang: str) -> str:
        return FAMILY.get(lang, "other")

    nodes = []
    for lang, s in sorted(kept.items(), key=lambda kv: (-kv[1], kv[0])):
        cid = clus(lang)
        nodes.append(OrderedDict(
            lang=lang, speakers=s,
            speakers_all=sp_all.get(lang, s), cluster=cid,
            cluster_label=CLUSTER_LABEL[cid],
            branch=FAMILY_OF.get(lang, CLUSTER_LABEL[cid]),
            historical=lang in HISTORICAL,
            degree=degree.get(lang, 0), radius=round(radius(s), 2),
        ))
    payload["nodes"] = nodes

    # --- clusters in shelf order ------------------------------------------
    clusters = []
    for cid, label in CLUSTER_SPEC:
        members = [n for n in nodes if n["cluster"] == cid]
        if not members:
            continue
        clusters.append(OrderedDict(
            id=cid, label=label,
            members=[m["lang"] for m in members],
            people=sum(m["speakers"] for m in members),
            languages=len(members),
            median_speakers=sorted(m["speakers"] for m in members)[len(members) // 2],
        ))
    payload["clusters"] = clusters

    # --- bridges: biggest and tightest co-speaker ties --------------------
    # Two directed shares per edge: of the people recorded speaking A, how many
    # also speak B, and the reverse. They are wildly asymmetric (97% of Catalan
    # speakers also speak Spanish, 14% the other way), and the old single
    # "share of the smaller group" hid that behind a phrase nobody could parse.
    def share_of(count: int, speakers: int) -> float:
        return count / speakers if speakers else 0.0

    for e in edges:
        sa = kept.get(e["a"], 0)
        sb = kept.get(e["b"], 0)
        e["share_a"] = round(share_of(e["count"], sa), 4)   # of A's speakers
        e["share_b"] = round(share_of(e["count"], sb), 4)   # of B's speakers
        # ``share`` is the smaller-group coefficient, kept for ordering.
        e["share"] = max(e["share_a"], e["share_b"])
        e["cluster_a"] = clus(e["a"])
        e["cluster_b"] = clus(e["b"])
        e["cross_cluster"] = e["cluster_a"] != e["cluster_b"]
        e["branch_a"] = FAMILY_OF.get(e["a"], CLUSTER_LABEL.get(e["cluster_a"], ""))
        e["branch_b"] = FAMILY_OF.get(e["b"], CLUSTER_LABEL.get(e["cluster_b"], ""))
        e["cross_branch"] = e["branch_a"] != e["branch_b"]

    by_count = sorted(edges, key=lambda e: -e["count"])
    payload["top_pairs"] = [
        OrderedDict(a=e["a"], b=e["b"], count=e["count"], share=e["share"],
                    share_a=e["share_a"], share_b=e["share_b"],
                    examples=e["examples"][:3], cross_cluster=e["cross_cluster"])
        for e in by_count[:30]
    ]

    # Annotated callouts: the strongest bridges, preferring links between two
    # different families and capping each language/cluster so one hub cannot
    # take over the callouts.
    counts: dict[str, int] = defaultdict(int)
    cluster_counts: dict[str, int] = defaultdict(int)
    annot = []
    chosen: set = set()

    def _take(e: dict) -> None:
        counts[e["a"]] += 1
        counts[e["b"]] += 1
        for c in {e["cluster_a"], e["cluster_b"]}:
            cluster_counts[c] += 1
        annot.append(e)
        chosen.add(frozenset((e["a"], e["b"])))

    for e in by_count:
        if counts[e["a"]] >= MAX_LANGS_PER_ANNOT or counts[e["b"]] >= MAX_LANGS_PER_ANNOT:
            continue
        if e["cross_cluster"] and (cluster_counts[e["cluster_a"]] >= 2
                                   or cluster_counts[e["cluster_b"]] >= 2):
            continue
        _take(e)
        if len(annot) >= MAX_ANNOT:
            break
    # If the cross-family pass came up short, top up with the remaining ties
    # (same caps, so one hub still cannot take over the callouts).
    if len(annot) < MAX_ANNOT:
        for e in by_count:
            if frozenset((e["a"], e["b"])) in chosen:
                continue
            if counts[e["a"]] >= MAX_LANGS_PER_ANNOT or counts[e["b"]] >= MAX_LANGS_PER_ANNOT:
                continue
            if (cluster_counts[e["cluster_a"]] >= 2
                    or cluster_counts[e["cluster_b"]] >= 2):
                continue
            _take(e)
            if len(annot) >= MAX_ANNOT:
                break
    payload["featured_bridges"] = [
        OrderedDict(a=e["a"], b=e["b"], count=e["count"], share=e["share"],
                    share_a=e["share_a"], share_b=e["share_b"],
                    examples=e["examples"][:3], cross_cluster=e["cross_cluster"])
        for e in annot
    ]

    # Tightest ties (high share) among reasonably sized languages.
    tight = [e for e in edges
             if min(kept.get(e["a"], 0), kept.get(e["b"], 0)) >= 2000
             and e["count"] >= 1000]
    tight.sort(key=lambda e: (-e["share"], -e["count"]))
    payload["tightest"] = [
        OrderedDict(a=e["a"], b=e["b"], count=e["count"], share=e["share"],
                    share_a=e["share_a"], share_b=e["share_b"],
                    examples=e["examples"][:3], historical=(
                        e["a"] in HISTORICAL or e["b"] in HISTORICAL))
        for e in tight[:12]
    ]

    payload["scale"]["nodes_drawn"] = len(nodes)
    payload["scale"]["edges_drawn"] = len(edges)
    payload["scale"]["clusters_drawn"] = len(clusters)
    payload["scale"]["max_pair"] = by_count[0]["count"] if by_count else 0
    payload["scale"]["min_drawn_pair"] = by_count[-1]["count"] if by_count else 0
    payload["thresholds"]["node_floor"] = node_floor
    return payload


# ---------------------------------------------------------------------------
# Layout: languages grouped into family arcs on one ring
# ---------------------------------------------------------------------------


def _esc(value) -> str:
    return (str(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _n(value) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "0"


def _ang_dist(a: float, b: float) -> float:
    d = abs(a - b) % (2 * math.pi)
    return min(d, 2 * math.pi - d)


def _ring_order(ids: list, weight: dict) -> list:
    """Order the families around the ring so heavy bridges are short.

    A greedy chain (always step to the family sharing the most speakers) is
    polished with circular 2-opt. Ten families, so this is cheap and exact
    enough; the objective is the weighted angular span of every bridge.
    """
    if len(ids) <= 2:
        return list(ids)
    remaining = set(ids)
    order = [ids[0]]
    remaining.discard(ids[0])
    while remaining:
        last = order[-1]
        nxt = max(remaining,
                  key=lambda x: (weight.get(tuple(sorted((last, x))), 0.0), x))
        order.append(nxt)
        remaining.discard(nxt)

    def cost(seq: list) -> float:
        n = len(seq)
        total = 0.0
        for i in range(n):
            for j in range(i + 1, n):
                w = weight.get(tuple(sorted((seq[i], seq[j]))), 0.0)
                if w:
                    total += w * min(j - i, n - (j - i))
        return total

    improved = True
    while improved:
        improved = False
        for i in range(1, len(order) - 1):
            for j in range(i + 1, len(order)):
                cand = order[:i] + order[i:j + 1][::-1] + order[j + 1:]
                if cost(cand) < cost(order) - 1e-9:
                    order = cand
                    improved = True
    return order


def _lang_chain(members: list, edges: list, nodes: dict) -> list:
    """Chain a family's languages so strongly linked ones sit side by side."""
    if len(members) <= 1:
        return list(members)
    w: dict = defaultdict(float)
    for e in edges:
        if e["a"] in members and e["b"] in members:
            w[frozenset((e["a"], e["b"]))] += e["count"]
    start = max(members, key=lambda m: (nodes[m]["speakers"], m))
    order = [start]
    remaining = set(members) - {start}
    while remaining:
        last = order[-1]
        nxt = max(remaining, key=lambda m: (w.get(frozenset((last, m)), 0.0),
                                            nodes[m]["speakers"], m))
        order.append(nxt)
        remaining.discard(nxt)
    return order


def _pt(cx: float, cy: float, ang: float, r: float) -> tuple:
    return (cx + r * math.cos(ang), cy + r * math.sin(ang))


def _label_width(text: str, size: float) -> float:
    return len(text) * 0.56 * size


def _arc(cx: float, cy: float, r: float, a0: float, a1: float) -> str:
    x0, y0 = _pt(cx, cy, a0, r)
    x1, y1 = _pt(cx, cy, a1, r)
    delta = a1 - a0
    large = 1 if abs(delta) > math.pi else 0
    sweep = 1 if delta >= 0 else 0
    return (f"M{x0:.2f},{y0:.2f} A{r:.2f},{r:.2f} 0 {large} {sweep} "
            f"{x1:.2f},{y1:.2f}")


def build_layout(payload: dict) -> dict:
    """Place the languages on a ring, grouped into contiguous family arcs."""
    nodes = {n["lang"]: n for n in payload["nodes"]}
    edges = payload["edges"]
    clusters = [c for c in payload["clusters"]
                if any(m in nodes for m in c["members"])]
    cids = [c["id"] for c in clusters]

    fam_weight: dict = defaultdict(float)
    for e in edges:
        if e["cluster_a"] != e["cluster_b"]:
            fam_weight[tuple(sorted((e["cluster_a"], e["cluster_b"])))] += e["count"]
    fam_order = _ring_order(cids, fam_weight)

    chains = {c["id"]: _lang_chain([m for m in c["members"] if m in nodes],
                                   edges, nodes)
              for c in clusters}
    nfam = len(fam_order)

    label_need = {
        cid: len(FAMILY_SHORT.get(cid, "")) * 0.80 * FAMILY_RESERVE + 6.0
        for cid in fam_order
    }

    def geometry(gap: float, fam_gap: float):
        sizes = {}
        for _cid, seq in chains.items():
            for lang in seq:
                sizes[lang] = 2.0 * nodes[lang]["radius"] + gap
        # Give every group at least enough arc for its rim label, so textPath
        # never clips the small groups (Celtic, Baltic, …).
        group_size = {}
        for cid in fam_order:
            node_sum = sum(sizes[l] for l in chains[cid])
            group_size[cid] = max(node_sum, label_need.get(cid, 0.0))
        total = sum(group_size.values()) + fam_gap * nfam
        r = total / (2.0 * math.pi)
        angles = {}
        spans = {}
        cursor = -math.pi / 2.0
        for cid in fam_order:
            start = cursor
            seq = chains[cid]
            node_sum = sum(sizes[l] for l in seq) or 1.0
            extra = max(0.0, group_size[cid] - node_sum) / max(1, len(seq))
            for lang in seq:
                width = 2.0 * math.pi * (sizes[lang] + extra) / total
                angles[lang] = cursor + width / 2.0
                cursor += width
            spans[cid] = (start, cursor)
            cursor += 2.0 * math.pi * fam_gap / total
        return r, angles, spans

    def cost(chains_try: dict) -> float:
        _r, angles, _spans = geometry(DOT_GAP, FAMILY_GAP)
        total = 0.0
        for e in edges:
            a = angles.get(e["a"])
            b = angles.get(e["b"])
            if a is not None and b is not None:
                total += e["count"] * _ang_dist(a, b)
        return total

    best = cost(chains)
    for _pass in range(6):
        improved = False
        for cid in fam_order:
            base = chains[cid]
            if len(base) <= 1:
                continue
            options = [base, base[::-1]]
            options += [base[k:] + base[:k] for k in range(1, len(base))]
            pick, pick_cost = base, best
            for opt in options:
                chains[cid] = opt
                c = cost(chains)
                if c < pick_cost - 1e-6:
                    pick, pick_cost = opt, c
            chains[cid] = pick
            if pick_cost < best - 1e-6:
                best = pick_cost
                improved = True
        if not improved:
            break

    # Fit the ring inside the panel. Two things stick out past the dots: the
    # language labels (which run outward along each radius) and the group names
    # (which sit on their own arc). Only the labels near the top and bottom eat
    # *vertical* room, so the fit is angle-aware — a long label on the 3 o'clock
    # side costs nothing here. Each group also gets its own arc radius, hugging
    # its own labels instead of forcing one global radius on every group.
    def family_radii(rr, spans_try) -> dict:
        out = {}
        for cid in fam_order:
            reach = max(rr + nodes[l]["radius"] + 5.0 + _label_width(l, LABEL_SIZE)
                        for l in chains[cid])
            out[cid] = reach + 10.0
        return out

    def fits(rr, angles_try, spans_try, fam_r) -> bool:
        head_v = NET_H / 2.0 - 8.0
        head_h = NET_W / 2.0 - 10.0
        for l, a in angles_try.items():
            reach = rr + nodes[l]["radius"] + 5.0 + _label_width(l, LABEL_SIZE)
            if abs(math.sin(a)) * reach + 8.0 > head_v:
                return False
            if abs(math.cos(a)) * reach + 6.0 > head_h:
                return False
        for cid in fam_order:
            a0, a1 = spans_try[cid]
            mid = (a0 + a1) / 2.0
            worst = max(abs(math.sin(a0)), abs(math.sin(a1)), abs(math.sin(mid)))
            worstc = max(abs(math.cos(a0)), abs(math.cos(a1)), abs(math.cos(mid)))
            if worst * fam_r[cid] + FAMILY_SIZE + 10.0 > head_v:
                return False
            if worstc * fam_r[cid] + 4.0 > head_h:
                return False
        return True

    geom = None
    for gap, fam_gap in ((DOT_GAP, FAMILY_GAP), (5.0, 17.0), (4.0, 14.0),
                         (3.0, 11.0), (2.5, 9.0), (2.0, 7.0), (1.5, 6.0),
                         (1.2, 5.0), (0.8, 4.0), (0.4, 3.0)):
        r, angles, spans = geometry(gap, fam_gap)
        fam_r = family_radii(r, spans)
        geom = (r, angles, spans, fam_r)
        if fits(r, angles, spans, fam_r):
            break
    r, angles, spans, fam_r = geom
    r_fam = max(fam_r.values())

    cx, cy = NET_W / 2.0, NET_H / 2.0
    lay_nodes = {}
    for lang in nodes:
        x, y = _pt(cx, cy, angles[lang], r)
        lay_nodes[lang] = {"angle": angles[lang], "x": x, "y": y,
                           "r": nodes[lang]["radius"]}

    families = []
    for cid in fam_order:
        cl = next(c for c in clusters if c["id"] == cid)
        a0, a1 = spans[cid]
        this_r = fam_r[cid]
        arc_len = this_r * (a1 - a0)
        short = FAMILY_SHORT.get(cid, cl["label"].upper())
        size = FAMILY_SIZE
        # 0.92 per glyph is a deliberately honest estimate for tracked uppercase
        # in DM Sans; with 0.80 the long compound names ran past the end of their
        # arc and collided with the neighbouring label.
        width = len(short) * 0.92 * size
        avail = arc_len - 16.0
        if width > avail:
            size = max(6.8, size * avail / width)
        families.append({
            "id": cid, "label": cl["label"], "short": short,
            "color": FAMILY_COLOR.get(cid, "#8a8278"),
            "span": (a0, a1), "mid": (a0 + a1) / 2.0, "r": this_r,
            "size": round(size, 2),
            "members": [m for m in chains[cid] if m in lay_nodes],
        })

    return {"cx": cx, "cy": cy, "R": r, "R_fam": r_fam,
            "nodes": lay_nodes, "families": families, "fam_order": fam_order}


def _cubic_point(p0: tuple, c1: tuple, c2: tuple, p1: tuple, t: float) -> tuple:
    mt = 1.0 - t
    x = (mt ** 3 * p0[0] + 3 * mt * mt * t * c1[0]
         + 3 * mt * t * t * c2[0] + t ** 3 * p1[0])
    y = (mt ** 3 * p0[1] + 3 * mt * mt * t * c1[1]
         + 3 * mt * t * t * c2[1] + t ** 3 * p1[1])
    return (x, y)


def _bridge_list(payload: dict) -> list:
    """The strongest bridges shown in the panel and highlighted on the ring."""
    return list(payload.get("top_pairs", []))[:5]


def build_scene(payload: dict, lay: dict) -> str:
    nodes = {n["lang"]: n for n in payload["nodes"]}
    layout = lay["nodes"]
    edges = [e for e in payload["edges"] if e["a"] in layout and e["b"] in layout]
    cx, cy, R = lay["cx"], lay["cy"], lay["R"]
    r_fam = lay["R_fam"]

    counts = [e["count"] for e in edges] or [1]
    log_min = math.log10(max(1, min(counts)))
    log_max = math.log10(max(1, max(counts)))

    def strength(count: int) -> float:
        if log_max - log_min < 1e-6:
            return 1.0
        t = (math.log10(max(1, count)) - log_min) / (log_max - log_min)
        return min(1.0, max(0.0, t))

    def chord_d(e: dict):
        x1, y1 = layout[e["a"]]["x"], layout[e["a"]]["y"]
        x2, y2 = layout[e["b"]]["x"], layout[e["b"]]["y"]
        c1 = (x1 + (cx - x1) * CHORD_BOW, y1 + (cy - y1) * CHORD_BOW)
        c2 = (x2 + (cx - x2) * CHORD_BOW, y2 + (cy - y2) * CHORD_BOW)
        d = (f"M{x1:.2f},{y1:.2f} C{c1[0]:.2f},{c1[1]:.2f} "
             f"{c2[0]:.2f},{c2[1]:.2f} {x2:.2f},{y2:.2f}")
        return d, (x1, y1), c1, c2, (x2, y2)

    def within_d(e: dict, radius: float) -> str:
        a1 = layout[e["a"]]["angle"]
        a2 = layout[e["b"]]["angle"]
        delta = (a2 - a1) % (2 * math.pi)
        if delta > math.pi:
            delta -= 2 * math.pi
        large = 1 if abs(delta) > math.pi else 0
        sweep = 1 if delta >= 0 else 0
        x1, y1 = _pt(cx, cy, a1, radius)
        x2, y2 = _pt(cx, cy, a2, radius)
        return (f"M{x1:.2f},{y1:.2f} A{radius:.2f},{radius:.2f} 0 {large} "
                f"{sweep} {x2:.2f},{y2:.2f}")

    within = sorted([e for e in edges if not e["cross_cluster"]],
                    key=lambda e: e["count"])
    cross = sorted([e for e in edges if e["cross_cluster"]],
                   key=lambda e: e["count"])

    # --- defs: dot grid, one blended gradient per cross-family chord, and the
    # arc paths that carry the family names around the rim ------------------
    defs = ['<pattern id="dots" width="22" height="22" patternUnits="userSpaceOnUse">'
            '<circle cx="1.4" cy="1.4" r="1.1" fill="rgba(28,21,16,.06)"/></pattern>']
    gradients = {}
    for i, e in enumerate(cross):
        x1, y1 = layout[e["a"]]["x"], layout[e["a"]]["y"]
        x2, y2 = layout[e["b"]]["x"], layout[e["b"]]["y"]
        ca = FAMILY_COLOR.get(e["cluster_a"], "#8a8278")
        cb = FAMILY_COLOR.get(e["cluster_b"], "#8a8278")
        gid = f"chordgrad{i}"
        gradients[(e["a"], e["b"])] = gid
        defs.append(
            f'<linearGradient id="{gid}" gradientUnits="userSpaceOnUse" '
            f'x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}">'
            f'<stop offset="0" stop-color="{ca}"/>'
            f'<stop offset=".16" stop-color="{ca}"/>'
            f'<stop offset=".84" stop-color="{cb}"/>'
            f'<stop offset="1" stop-color="{cb}"/></linearGradient>')
    for fam in lay["families"]:
        a0, a1 = fam["span"]
        if math.sin(fam["mid"]) < 0:      # top half reads left-to-right
            pa, pb, sweep = a0, a1, 1
        else:                             # bottom half: reverse to stay upright
            pa, pb, sweep = a1, a0, 0
        fr = fam.get("r", r_fam)
        x0, y0 = _pt(cx, cy, pa, fr)
        x1, y1 = _pt(cx, cy, pb, fr)
        defs.append(
            f'<path id="famarc-{fam["id"]}" d="M{x0:.2f},{y0:.2f} '
            f'A{fr:.2f},{fr:.2f} 0 0 {sweep} {x1:.2f},{y1:.2f}"/>')

    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {NET_W} {NET_H}" '
        f'width="{NET_W}" height="{NET_H}" role="img" '
        f'aria-label="Languages arranged in a ring by group, joined by chords '
        f'for the people who speak them in pairs">')
    parts.append("<defs>" + "".join(defs) + "</defs>")
    parts.append(f'<rect x="0" y="0" width="{NET_W}" height="{NET_H}" '
                 f'fill="url(#dots)"/>')

    # --- family arcs on the rim -------------------------------------------
    for fam in lay["families"]:
        a0, a1 = fam["span"]
        parts.append(
            f'<path d="{_arc(cx, cy, R, a0, a1)}" fill="none" '
            f'stroke="{fam["color"]}" stroke-opacity="0.16" stroke-width="11" '
            f'stroke-linecap="round"><title>{_esc(fam["label"])}: '
            f'{len(fam["members"])} languages drawn</title></path>')

    drawn: dict = {}
    # --- within-family ties reinforce their own arc ------------------------
    for e in within:
        t = strength(e["count"])
        radius = R - 10.0 - 4.0 * t
        d = within_d(e, radius)
        a1 = layout[e["a"]]["angle"]
        a2 = layout[e["b"]]["angle"]
        delta = (a2 - a1) % (2 * math.pi)
        if delta > math.pi:
            delta -= 2 * math.pi
        drawn[(e["a"], e["b"])] = (d, 0.7 + 2.3 * t,
                                   ("arc", a1 + delta / 2.0, radius))
        color = FAMILY_COLOR.get(e["cluster_a"], "#8a8278")
        parts.append(
            f'<path d="{d}" fill="none" stroke="{color}" '
            f'stroke-opacity="{0.22 + 0.55 * t ** 0.85:.3f}" '
            f'stroke-width="{0.8 + 2.6 * t ** 0.85:.2f}" stroke-linecap="round"><title>'
            f'{_esc(e["a"])} \u2014 {_esc(e["b"])}: {_n(e["count"])} people '
            f'speak both \u00b7 same family</title></path>')

    # --- cross-family chords: colour blends the two families ---------------
    for e in cross:
        t = strength(e["count"])
        d, p0, c1, c2, p1 = chord_d(e)
        width = 0.4 + 3.2 * t ** 1.35
        drawn[(e["a"], e["b"])] = (d, width, ("cubic", p0, c1, c2, p1))
        la = CLUSTER_LABEL.get(e["cluster_a"], e["cluster_a"])
        lb = CLUSTER_LABEL.get(e["cluster_b"], e["cluster_b"])
        parts.append(
            f'<path d="{d}" fill="none" '
            f'stroke="url(#{gradients[(e["a"], e["b"])]})" '
            f'stroke-opacity="{0.07 + 0.62 * t ** 1.7:.3f}" '
            f'stroke-width="{width:.2f}" stroke-linecap="round"><title>'
            f'{_esc(e["a"])} \u2014 {_esc(e["b"])}: {_n(e["count"])} people '
            f'speak both \u00b7 {_esc(la)} \u2194 {_esc(lb)}</title></path>')

    # --- the strongest bridges: a soft orange halo (listed in the panel) ---
    for b in _bridge_list(payload):
        key = (b["a"], b["b"])
        if key not in drawn:
            continue
        d, width, _ctrl = drawn[key]
        parts.append(
            f'<path d="{d}" fill="none" stroke="#bf6b28" stroke-opacity="0.22" '
            f'stroke-width="{width + 7.5:.2f}" stroke-linecap="round"/>')

    # --- language dots (size is the data) ----------------------------------
    for lang in sorted(layout, key=lambda m: layout[m]["r"]):
        nd = layout[lang]
        n = nodes[lang]
        color = FAMILY_COLOR.get(n["cluster"], "#8a8278")
        # Historical and classical languages are drawn hollow: they are not
        # living communities, and mixing them in as solid dots was reading as
        # a claim about today's speakers.
        hist = bool(n.get("historical"))
        fill = "#f9f8f4" if hist else color
        stroke = color if hist else "#f9f8f4"
        sw = 2.1 if hist else 1.7
        kind = "historical &middot; " if hist else ""
        parts.append(
            f'<circle cx="{nd["x"]:.1f}" cy="{nd["y"]:.1f}" r="{nd["r"]:.1f}" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}"><title>'
            f'{_esc(lang)}: {_n(n["speakers"])} multilingual speakers \u00b7 '
            f'{kind}{_esc(n["cluster_label"])}</title></circle>')

    # --- language labels, radial so 60 never collide -----------------------
    for lang in sorted(layout, key=lambda m: -nodes[m]["speakers"]):
        nd = layout[lang]
        a = nd["angle"]
        lx, ly = _pt(cx, cy, a, R + nd["r"] + 5.0)
        deg = math.degrees(a)
        if math.cos(a) >= -1e-9:
            anchor, rot = "start", deg
        else:
            anchor, rot = "end", deg + 180.0
        parts.append(
            f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="{anchor}" '
            f'font-size="{LABEL_SIZE}" '
            f'dominant-baseline="middle" class="nlabel" '
            f'transform="rotate({rot:.1f} {lx:.1f} {ly:.1f})">'
            f'{_esc(lang)}</text>')

    # --- family names follow their arc -------------------------------------
    for fam in lay["families"]:
        parts.append(
            f'<text class="family-label" font-size="{fam["size"]}" '
            f'text-anchor="middle"><title>{_esc(fam["label"])}</title>'
            f'<textPath href="#famarc-{fam["id"]}" startOffset="50%">'
            f'{_esc(fam["short"])}</textPath></text>')

    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Card HTML
# ---------------------------------------------------------------------------

HTML_HEAD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>HumansMap — the language-bridge network</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;800&family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:#f9f8f4; --text:#1c1510;
    --chrome:#1b4d8e;          /* chrome only: wordmark, eyebrow, headline emphasis */
    --data:#bf6b28;            /* data highlight only: strongest bridges */
    --structure:#8a8278;       /* structure: dot grid, neutral marks */
    --surface:#ece8de; --line:rgba(28,21,16,.10);
    --display:'Playfair Display', Georgia, serif; --body:'DM Sans', system-ui, sans-serif;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:#d9d4c9; font-family:var(--body); padding:32px; }
  .card { width:1600px; height:1000px; background:var(--bg); color:var(--text);
    padding:30px 44px 16px; display:flex; flex-direction:column; position:relative;
    overflow:hidden; }
  .card header { display:flex; justify-content:space-between; align-items:baseline; }
  .eyebrow { font:700 15px/1 var(--body); letter-spacing:.2em; text-transform:uppercase;
    color:var(--chrome); }
  .brand { font:700 27px/1 var(--display); color:var(--chrome); }
  h1 { font:800 35px/1.06 var(--display); margin:9px 0 0; letter-spacing:-.012em;
    max-width:62ch; }
  h1 em { font-style:normal; color:var(--chrome); }
  .deck { font:400 13.6px/1.42 var(--body); color:var(--structure); margin:7px 0 0;
    max-width:1512px; }
  .deck b { color:var(--text); font-weight:700; }
  .stage { margin-top:6px; flex:0 0 740px; display:flex; gap:34px; }
  .chart { flex:0 0 1020px; width:1020px; height:740px; }
  .chart svg { display:block; width:1020px; height:740px; overflow:visible; }
  .nlabel { font-family:var(--body); font-weight:500; fill:var(--text); paint-order:stroke;
    stroke:#f9f8f4; stroke-width:2.8px; stroke-linejoin:round; }
  .family-label { font-family:var(--body); font-weight:700; letter-spacing:.15em;
    fill:var(--structure); paint-order:stroke; stroke:#f9f8f4; stroke-width:3.4px; }
  .badge { font:700 9.6px/1 var(--body); fill:#fff; }
  .keys { flex:1 1 auto; min-width:0; display:flex; flex-direction:column; }
  .plabel { font:700 11px/1 var(--body); letter-spacing:.16em; text-transform:uppercase;
    color:var(--structure); margin-bottom:9px; }
  .read { display:flex; flex-direction:column; gap:6px; font:400 12.2px/1.35 var(--body);
    color:var(--structure); }
  .read .row { display:flex; align-items:center; gap:10px; }
  .read b { color:var(--text); }
  .swatch { flex:0 0 auto; }
  .famdot { flex:0 0 auto; width:11px; height:11px; border-radius:3px;
    display:inline-block; }
  .badge-dot { flex:0 0 auto; width:17px; height:17px; border-radius:50%;
    background:var(--data); color:#fff; font:700 9.6px/17px var(--body);
    text-align:center; }
  .famgrid { display:grid; grid-template-columns:1fr 1fr; gap:4px 16px;
    margin:10px 0 3px; }
  .fam { display:flex; align-items:center; gap:8px; font:500 11.4px/1.2 var(--body);
    color:var(--text); }
  .stats { display:flex; gap:16px; margin-top:11px; padding-top:9px;
    border-top:1px solid var(--line); }
  .stat { flex:1 1 0; }
  .stat b { display:block; font:800 19px/1 var(--display); color:var(--text); }
  .stat span { display:block; font:500 9.5px/1.25 var(--body); color:var(--structure);
    text-transform:uppercase; letter-spacing:.06em; margin-top:3px; }
  .bridges { list-style:none; margin:2px 0 0; padding:0; }
  .bridges li { padding:6px 0 5px; border-bottom:1px solid var(--line); }
  .bhead { display:flex; align-items:baseline; gap:9px; }
  .bnum { flex:0 0 auto; width:16px; height:16px; border-radius:50%;
    background:var(--data); color:#fff; font:700 9.6px/16px var(--body);
    text-align:center; }
  .bname { font:600 12.8px/1.25 var(--body); flex:1 1 auto; }
  .bname b { color:var(--text); }
  .bcount { font:800 14px/1 var(--display); color:var(--data); white-space:nowrap; }
  .bmeta { font:400 10.4px/1.3 var(--body); color:var(--structure);
    margin:3px 0 0 25px; }
  .caveat { font:400 11px/1.4 var(--body); color:var(--structure); margin-top:auto;
    padding-top:9px; border-top:1px solid var(--line); }
  .caveat b { color:var(--text); }
  .card footer { margin-top:8px; display:flex; justify-content:space-between;
    align-items:baseline; font:400 12.3px/1.4 var(--body); color:var(--structure); }
</style></head>
<body>"""


def _directed_shares(e: dict) -> tuple:
    """Order a pair so the higher-overlap direction is named first.

    Returns (hi, lo, share_of_hi, share_of_lo) where hi is the language whose
    recorded speakers are the more likely to speak the other one.
    """
    sa, sb = e.get("share_a", 0.0), e.get("share_b", 0.0)
    if sa >= sb:
        return e["a"], e["b"], sa, sb
    return e["b"], e["a"], sb, sa


def render_html(payload: dict) -> str:
    lay = build_layout(payload)
    scene = build_scene(payload, lay)

    scale = payload["scale"]
    thresholds = payload["thresholds"]
    shown = _bridge_list(payload)

    biggest = shown[0] if shown else None
    tight = [t for t in payload.get("tightest", []) if not t["historical"]]
    headlines = tight[0] if tight else None

    headline = "Languages, linked by the people who speak more than one."

    deck = (
        f"Every language Wikidata records for the <b>{_n(scale['multilingua_people'])}</b> "
        f"people who speak <b>two or more languages</b>. Two languages are linked when "
        f"the same person speaks both. The <b>{_n(scale['nodes_drawn'])}</b> "
        f"best-recorded languages are set out in a ring and coloured by "
        f"<b>reading frame</b>; dot size is how many people speak a language here, "
        f"chord thickness how many speak both. The biggest single bridge is "
        f"<b>{_esc(biggest['a'])}&ndash;{_esc(biggest['b'])}</b> "
        f"({_n(biggest['count'])} people speak both)."
        if biggest else ""
    )
    if headlines:
        hi, lo, hi_s, lo_s = _directed_shares(headlines)
        deck += (
            f" The tightest pair is <b>{_esc(hi)}&ndash;{_esc(lo)}</b>: "
            f"{round(100 * hi_s)}% of recorded {_esc(hi)} speakers also speak "
            f"{_esc(lo)}."
        )

    def bridge_li(e: dict, idx: int) -> str:
        hi, lo, hi_s, lo_s = _directed_shares(e)
        return (
            f'<li><div class="bhead"><span class="bnum">{idx}</span>'
            f'<span class="bname"><b>{_esc(hi)}</b> &middot; {_esc(lo)}</span>'
            f'<span class="bcount">{_n(e["count"])}</span></div>'
            f'<div class="bmeta">{round(100 * hi_s)}% of {_esc(hi)} speakers also '
            f'speak {_esc(lo)} &middot; {round(100 * lo_s)}% the other way</div></li>'
        )

    caveat = (
        "<b>Read with care.</b> Only people whose languages are recorded \u2014 a "
        "coverage that skews to well-documented figures and to Europe, so this is "
        "partly a map of Wikipedia. Percentages are of the people in this record, "
        "not of all living speakers. The coloured frames are hand-set reading "
        "frames, not a genealogy read off the data."
    )

    germanic = FAMILY_COLOR["germanic"]
    romance = FAMILY_COLOR["romance"]
    legend = (
        '<div class="read">'
        '<div class="row"><svg class="swatch" width="74" height="20">'
        '<circle cx="6" cy="10" r="2.6" fill="#8a8278"/>'
        '<circle cx="18" cy="10" r="4.4" fill="#8a8278"/>'
        '<circle cx="32" cy="10" r="6.6" fill="#8a8278"/>'
        '<circle cx="56" cy="10" r="7.4" fill="none" stroke="#8a8278" '
        'stroke-width="1.8"/></svg>'
        '<span>Each dot is a language; its <b>size</b> = people who speak it; a '
        '<b>hollow</b> dot is historical or classical</span></div>'
        '<div class="row"><svg class="swatch" width="58" height="16">'
        '<defs><linearGradient id="lg-chord" x1="0" y1="0" x2="1" y2="0">'
        f'<stop offset="0" stop-color="{germanic}"/>'
        f'<stop offset="1" stop-color="{romance}"/></linearGradient></defs>'
        '<path d="M2,12 C20,2 38,2 56,12" fill="none" stroke="url(#lg-chord)" '
        'stroke-width="3"/></svg>'
        '<span>A chord links two languages; <b>thickness</b> = people who '
        'speak both</span></div>'
        f'<div class="row"><span class="famdot" style="background:{germanic}"></span>'
        '<span>Colour = <b>language group</b> (key below); a chord blends the two '
        'groups it links</span></div>'
        '<div class="row"><svg class="swatch" width="58" height="18">'
        '<line x1="2" y1="15" x2="56" y2="15" stroke="#c9c1b4" stroke-width="1.4"/>'
        '<path d="M9,15 A19,10 0 0 1 49,15" fill="none" stroke="#8a8278" '
        'stroke-width="2.2" stroke-linecap="round"/></svg>'
        '<span>A tie inside one <b>group</b> hugs the rim; a chord '
        'across the middle links two different groups</span></div>'
        '<div class="row"><svg class="swatch" width="58" height="14">'
        '<path d="M2,7 C20,2 38,2 56,7" fill="none" stroke="#bf6b28" '
        'stroke-opacity="0.28" stroke-width="8" stroke-linecap="round"/>'
        '<path d="M2,7 C20,2 38,2 56,7" fill="none" stroke="#bf6b28" '
        'stroke-width="2.2" stroke-linecap="round"/></svg>'
        '<span>An orange halo marks one of the <b>strongest bridges</b> '
        '(listed at right)</span></div>'
        '</div>'
    )

    famgrid = "".join(
        f'<div class="fam"><span class="famdot" style="background:{fam["color"]}">'
        f'</span><span>{_esc(fam["label"])}</span></div>'
        for fam in lay["families"])

    stats = (
        f'<div class="stat"><b>{_n(scale["multilingua_people"])}</b>'
        '<span>multilingual people</span></div>'
        f'<div class="stat"><b>{_n(scale["languages_total"])}</b>'
        '<span>languages recorded</span></div>'
        f'<div class="stat"><b>{_n(scale["edges_drawn"])}</b>'
        '<span>linked pairs</span></div>'
        f'<div class="stat"><b>{_n(scale["clusters_drawn"])}</b>'
        '<span>groups</span></div>'
    )

    html = f"""<section class="card">
  <header>
    <span class="eyebrow">The language bridge</span>
    <span class="brand">Humans&nbsp;Map</span>
  </header>
  <h1>{headline}</h1>
  <p class="deck">{deck}</p>
  <div class="stage">
    <div class="chart">{scene}</div>
    <aside class="keys">
      <div class="plabel">How to read it</div>
      {legend}
      <div class="famgrid">{famgrid}</div>
      <div class="stats">{stats}</div>
      <div class="plabel" style="margin-top:12px">Strongest bridges &mdash; people who speak both</div>
      <ul class="bridges">{''.join(bridge_li(e, i) for i, e in enumerate(shown, start=1))}</ul>
      <div class="caveat">{caveat}</div>
    </aside>
  </div>
  <footer>
    <span>Data: Wikidata (CC BY-SA) &middot; humansmap.com</span>
    <span>{_n(scale['nodes_drawn'])} languages &middot; {_n(scale['edges_drawn'])} linked pairs &middot; &ge;{_n(thresholds['min_pair'])} people per pair</span>
  </footer>
</section>"""

    return HTML_HEAD + html + "\n</body></html>\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default="docs/marketing/langs")
    parser.add_argument("--min-statements", type=int, default=DEFAULT_MIN_STATEMENTS)
    parser.add_argument("--min-pair", type=int, default=DEFAULT_MIN_PAIR)
    parser.add_argument("--scan-limit", type=int, default=DEFAULT_SCAN_LIMIT)
    parser.add_argument("--node-floor", type=int, default=DEFAULT_NODE_FLOOR)
    parser.add_argument("--max-nodes", type=int, default=DEFAULT_MAX_NODES)
    parser.add_argument("--from-json", default=None)
    parser.add_argument("--no-html", action="store_true")
    args = parser.parse_args()

    if args.from_json:
        payload = json.loads(Path(args.from_json).read_text())
        for key in ("clusters", "featured_bridges", "top_pairs", "tightest"):
            payload.pop(key, None)
        # Honour the node floor the cache was built with, so a --from-json
        # re-render draws exactly the same set (otherwise the CLI default
        # silently drops the smaller languages again).
        args.node_floor = payload.get("thresholds", {}).get("node_floor", args.node_floor)
    else:
        payload = collect(args.min_statements, args.min_pair, args.scan_limit,
                          args.max_nodes, DEFAULT_MAX_EDGES)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    analyze(payload, args.node_floor)

    if not args.from_json:
        json_path = out_dir / "langs.json"
        json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        print(f"wrote {json_path}")

    if not args.no_html:
        html_path = out_dir / "langs.html"
        html_path.write_text(render_html(payload))
        print(f"wrote {html_path}")

    summary = {
        "thresholds": payload["thresholds"],
        "scale": payload["scale"],
        "clusters": [
            {"id": c["id"], "label": c["label"], "languages": c["languages"],
             "people": c["people"]}
            for c in payload["clusters"]
        ],
        "top_pairs": [
            {"a": e["a"], "b": e["b"], "count": e["count"], "share": e["share"]}
            for e in payload["top_pairs"][:15]
        ],
        "tightest": payload["tightest"][:8],
        "featured": [
            {"a": e["a"], "b": e["b"], "count": e["count"]}
            for e in payload["featured_bridges"]
        ],
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
