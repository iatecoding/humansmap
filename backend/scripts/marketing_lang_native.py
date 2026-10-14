#!/usr/bin/env python3
"""Marketing native-language miner — read-only, emits the "native or learned?"
card: how each of the world's most relevant languages is *held*.

The concept: the same reading as the gender heatmap, applied to languages.
Wikidata records a person's mother tongue in ``persons.native_language`` (P103,
single) and every language they speak in ``persons.languages_spoken`` (P1412,
multi, unordered).

**Both counts come from one consistent population** — the people who have *both*
a recorded mother tongue *and* a non-empty ``languages_spoken`` list (P103 is
recorded for only ~129k people, P1412 for ~825k, so dividing the two would be a
coverage artifact). For each language L, among those people:

  * ``native[L]``     = people with ``native_language == L``.
  * ``additional[L]`` = people with L in ``languages_spoken`` and a *different*
    recorded mother tongue — i.e. a language learned later.
  * ``holders[L]``    = ``native[L] + additional[L]``; the card's bar is the
    ``native`` / ``additional`` split of the holders (100%-stacked).

So the share answers a real question: *among people whose mother tongue is
recorded and who know L, is L their mother tongue or a language learned later?*

Names are normalised per person *before* counting (American/British English ->
English, Brazilian Portuguese -> Portuguese, Egyptian/Moroccan/... Arabic ->
Arabic, Mandarin/Standard Chinese -> Chinese, ...), so the numbers are per
*language*, not per Wikidata label. The map is a copy of
``backend/scripts/marketing_langs.py``'s ``NORMALIZE`` (extended), and the card
groups languages with the same ``FAMILY`` / ``CLUSTER_SPEC`` reading frames.

It never writes to ArangoDB. One bounded READ-ONLY pass pulls every person at
or above the statement floor (30, a persistent index, so the scan is bounded);
the full result is streamed and aggregated in Python, so memory stays flat.

Outputs (both written by default):

  langnative.json   machine-readable payload: counts, selection, headline
  langnative.html   brand-styled card sheet (1600x1000) to screenshot

Run it manually inside the backend container:

    docker compose exec -T backend python - --out-dir /tmp/langnative \\
        < backend/scripts/marketing_lang_native.py
    docker compose exec -T backend cat /tmp/langnative/langnative.json \\
        > docs/marketing/langnative/langnative.json
    docker compose exec -T backend cat /tmp/langnative/langnative.html \\
        > docs/marketing/langnative/langnative.html

Then render (from scripts/marketing/):

    source env.sh
    node render-insight-cards.js ../../docs/marketing/langnative/langnative.html \\
        /tmp/opencode/langnative
    # review /tmp/opencode/langnative/card-01.png, then
    cp /tmp/opencode/langnative/card-01.png \\
        ../../docs/marketing/langnative/langnative.png

Flags:
    --out-dir PATH       output directory (default: docs/marketing/langnative)
    --min-statements N   Wikidata-statement floor, the notability bound (default 30)
    --scan-limit N       hard safety cap on the single scan (default 1200000)
    --from-json PATH     re-render from an existing langnative.json (no DB)
    --no-html            emit only the JSON payload
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, OrderedDict
from datetime import date
from pathlib import Path

# When run as a file inside the container this resolves to /app; when piped
# over stdin (``python -``) __file__ is "<stdin>" and /app is cwd, so fall back.
try:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
except (NameError, OSError):  # pragma: no cover - stdin invocation
    sys.path.insert(0, str(Path.cwd()))

DEFAULT_MIN_STATEMENTS = 30
DEFAULT_SCAN_LIMIT = 1_200_000

# ---------------------------------------------------------------------------
# Language normalisation — a copy of marketing_langs.NORMALIZE, extended with
# the regional / spelling variants the native (P103) field actually uses.
# ---------------------------------------------------------------------------

NORMALIZE: dict[str, str] = {
    "American English": "English",
    "British English": "English",
    "Australian English": "English",
    "Canadian English": "English",
    "Irish English": "English",
    "Scottish English": "English",
    "New Zealand English": "English",
    "African American English": "English",
    "Old English": "English",
    "Middle English": "English",
    "Brazilian Portuguese": "Portuguese",
    "European Portuguese": "Portuguese",
    "Greek": "Modern Greek",
    "Swiss German": "German",
    "Austrian German": "German",
    "Swiss Standard German": "German",
    "Standard High German": "German",
    "medieval Latin": "Latin",
    "Medieval Latin": "Latin",
    "renaissance Latin": "Latin",
    "Ecclesiastical Latin": "Latin",
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
    "Taiwanese Mandarin": "Chinese",
    "Putonghua": "Chinese",
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
    "Palestinian Arabic": "Arabic",
    "Flemish": "Dutch",
    "Flemish Dutch": "Dutch",
    "Ottoman Turkish": "Turkish",
    "Yue Chinese": "Cantonese",
    "Hong Kong Cantonese": "Cantonese",
    "Bengali": "Bangla",
    "Oriya": "Odia",
    "Peninsular Spanish": "Spanish",
    "Mexican Spanish": "Spanish",
    # One pluricentric language, four Wikidata labels (see marketing_langs.py):
    # shown once so the national standards are not read as four languages.
    "Croatian": "Serbo-Croatian",
    "Serbian": "Serbo-Croatian",
    "Bosnian": "Serbo-Croatian",
    "Montenegrin": "Serbo-Croatian",
}

# Display groups (the langs card's reading frames): mostly language families,
# a few regional because the data is thin per family. Labels say "group".
CLUSTER_SPEC = [
    ("germanic", "Germanic"),
    ("romance", "Romance"),
    ("slavic", "Slavic"),
    ("uralic_turkic", "Uralic & Turkic"),
    ("greek", "Greek"),
    ("semitic", "Arabic & Semitic"),
    ("iranian", "Iranian"),
    ("indo_dravidian", "Indo-Aryan & Dravidian"),
    ("eastasia", "East & Southeast Asian"),
    ("celtic", "Celtic"),
    ("baltic", "Baltic"),
    ("african", "African"),
    ("other", "Americas & Oceania"),
]
CLUSTER_ORDER = [c[0] for c in CLUSTER_SPEC]
CLUSTER_LABEL = {c[0]: c[1] for c in CLUSTER_SPEC}

FAMILY: dict[str, str] = {}


def _fam(cluster: str, names: str) -> None:
    for n in names.split("|"):
        FAMILY[n.strip()] = cluster


_fam("germanic", "English|German|Dutch|Swedish|Danish|Norwegian|Icelandic|"
                 "Yiddish|Afrikaans|Low German|Luxembourgish|Scots|Faroese|"
                 "Pennsylvania German|Middle High German|Swiss German|Gothic")
_fam("romance", "French|Spanish|Italian|Portuguese|Catalan|Romanian|Galician|"
                "Occitan|Latin|Sardinian|Sicilian|Venetian|Neapolitan|Corsican|"
                "Aragonese|Asturian|Lombard|Piedmontese|Mirandese|Walloon|"
                "Romansh|Friulian|Emilian")
_fam("slavic", "Russian|Polish|Czech|Ukrainian|Slovak|Slovene|Croatian|"
               "Serbian|Bulgarian|Belarusian|Bosnian|Macedonian|Rusyn|"
               "Upper Sorbian|Lower Sorbian|Sorbian|Church Slavonic|"
               "Old Church Slavonic|Montenegrin|Serbo-Croatian|Old East Slavic")
_fam("greek", "Modern Greek|Ancient Greek|Greek")
_fam("uralic_turkic", "Finnish|Estonian|Hungarian|Turkish|Azerbaijani|Tatar|"
                      "Kazakh|Uzbek|Turkmen|Kyrgyz|Bashkir|Chuvash|Uyghur|"
                      "Karelian|Sami|Northern Sami|Livonian|Votic")
_fam("semitic", "Arabic|Hebrew|Maltese|Aramaic|Syriac|Coptic|"
                "Ancient Egyptian|Phoenician|Akkadian|Sumerian")
_fam("iranian", "Persian|Kurdish|Pashto|Balochi|Tajik|Dari|Avestan|Pahlavi|"
                "Old Persian|Middle Persian|Sogdian")
_fam("indo_dravidian", "Hindi|Urdu|Bangla|Bengali|Punjabi|Marathi|Sanskrit|"
                       "Nepali|Gujarati|Sinhala|Sinhalese|Romani|Tamil|Telugu|"
                       "Malayalam|Kannada|Odia|Oriya|Assamese|Sindhi|Pali|"
                       "Prakrit|Bhojpuri")
_fam("eastasia", "Chinese|Classical Chinese|Old Mandarin|Cantonese|Yue Chinese|"
                 "Japanese|Korean|Vietnamese|Thai|Lao|Indonesian|Malay|Tagalog|"
                 "Filipino|Javanese|Taiwanese Hokkien|Hokkien|Burmese|Tibetan|"
                 "Khmer|Mongolian|Sundanese|Cebuano|Ilocano|Maori|Māori|"
                 "Hawaiian|Samoan|Tongan|Fijian")
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
              # Pacific / Oceanian languages: langs.py files these under East &
              # SE Asia, but the card reads them as Oceania. This call runs last,
              # so it wins and they are drawn under "Americas & Oceania".
              "Maori|Māori|Hawaiian|Samoan|Tongan|Fijian")

# Historical / classical languages: ~0 native speakers by construction, so they
# are excluded from the ranking (and named in the card's caveat).
HISTORICAL = {"Latin", "Ancient Greek", "Classical Chinese", "Old Mandarin",
              "Sanskrit", "Ottoman Turkish", "Old Church Slavonic",
              "Church Slavonic", "Old East Slavic", "Middle English",
              "Old English", "Old French", "Middle French", "medieval Latin",
              "Medieval Latin", "renaissance Latin", "Ecclesiastical Latin",
              "medieval Italian", "Old Spanish", "Old Catalan", "Old Occitan",
              "Middle High German", "medieval Greek", "Akkadian", "Sumerian",
              "Ancient Egyptian", "Phoenician", "Aramaic", "Syriac", "Coptic",
              "Pali", "Prakrit", "Gothic"}

# Constructed languages: no traditional native community; excluded like the
# historical ones so they cannot distort the native/learned story.
CONSTRUCTED = {"Esperanto", "Volapuk", "Ido", "Interlingua", "Lojban"}

# The "other" group spans isolates *and* constructed *and* indigenous/creole
# languages. For the Americas & Oceania slot we want the indigenous/creole
# ones, so the selection reads from this list only.
INDIGENOUS = {"Quechua", "Nahuatl", "Maya", "Inuit", "Navajo", "Haida", "Ainu",
              "Haitian Creole", "Tok Pisin", "Sranan Tongo", "Papamiento",
              "Maori", "Māori", "Hawaiian", "Samoan", "Tongan", "Fijian"}

# Split the indigenous/creole set so the card keeps both continents rather than
# two languages from the same one.
OCEANIA = {"Maori", "Māori", "Hawaiian", "Samoan", "Tongan", "Fijian",
           "Tok Pisin"}
AMERICAS = {"Quechua", "Nahuatl", "Maya", "Inuit", "Navajo", "Haida",
            "Haitian Creole", "Sranan Tongo", "Papamiento"}

# ---------------------------------------------------------------------------
# Selection — the card cannot draw 1,500 languages. Take each group's biggest
# languages (holders = native + additional) down to a floor, so every region
# appears, not just Europe. Quotas are the number of languages kept per group.
# ---------------------------------------------------------------------------

QUOTA = [
    ("germanic", 3),
    ("romance", 4),
    ("slavic", 3),
    ("uralic_turkic", 3),
    ("greek", 1),
    ("semitic", 2),
    ("iranian", 1),
    ("indo_dravidian", 3),
    ("eastasia", 4),
    ("african", 5),
    ("other", 2),
]

HOLDER_FLOOR = 50        # smallest drawn language must have at least this many holders
OTHER_FLOOR = 4          # the Americas & Oceania pair is thin in this population
PANEL_FLOOR = 250        # holders needed to enter the "most learned/native" lists
MIN_STORE = 3            # languages with fewer holders than this stay out of the JSON

# ---------------------------------------------------------------------------
# Query — one bounded, read-only pass; aggregate in Python.
# ---------------------------------------------------------------------------

LANGNATIVE_AQL = """
FOR p IN persons
  FILTER p.wikidata_statements_count >= @min_statements
    AND p.native_language != null AND LENGTH(p.languages_spoken) > 0
  LIMIT @scan_limit
  RETURN {nat: p.native_language, spoken: p.languages_spoken}
"""

# Companion scan of the same statement floor, for the coverage denominators the
# card states (total / has a mother tongue / has languages spoken / has both).
COVERAGE_AQL = """
FOR p IN persons
  FILTER p.wikidata_statements_count >= @min_statements
  COLLECT AGGREGATE
    total = COUNT(1),
    native = SUM(p.native_language != null ? 1 : 0),
    spoken = SUM(LENGTH(p.languages_spoken) > 0 ? 1 : 0),
    both = SUM((p.native_language != null AND LENGTH(p.languages_spoken) > 0) ? 1 : 0)
  RETURN {total: total, native: native, spoken: spoken, both: both}
"""


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _norm(name):
    if not isinstance(name, str):
        return None
    name = name.strip()
    if not name:
        return None
    return NORMALIZE.get(name, name)


# ---------------------------------------------------------------------------
# Collection — stream the single pass and aggregate in Python
# ---------------------------------------------------------------------------


def collect(min_statements: int, scan_limit: int) -> dict:
    # Lazy import so --from-json can re-render a cached payload without the
    # ArangoDB driver.
    from app.database import get_arango_client, get_db  # noqa: E402

    db = get_db(get_arango_client(request_timeout=1800))

    # Coverage denominators (the card states these): how many people have a
    # mother tongue, languages spoken, or both, at the same statement floor.
    cov: dict = {}
    try:
        cov_rows = list(db.aql.execute(
            COVERAGE_AQL, bind_vars={"min_statements": min_statements}))
        cov = cov_rows[0] if cov_rows else {}
    except Exception as exc:  # noqa: BLE001 - a marketing job must not crash
        print(f"[warn] coverage query failed: {exc}", file=sys.stderr)

    # The metric pass: only people with BOTH a recorded mother tongue and a
    # non-empty languages_spoken list, so native and additional divide the same
    # population.
    native: Counter = Counter()
    additional: Counter = Counter()
    dist: Counter = Counter()
    population = 0

    cursor = db.aql.execute(
        LANGNATIVE_AQL,
        bind_vars={"min_statements": min_statements, "scan_limit": scan_limit},
        batch_size=20000,
    )
    for row in cursor:
        population += 1
        nat = _norm(row.get("nat"))
        spoken = set()
        for s in _as_list(row.get("spoken")):
            n = _norm(s)
            if n:
                spoken.add(n)
        langs = set(spoken)
        if nat is not None:
            langs.add(nat)
        dist[len(langs)] += 1
        for l in langs:
            if l == nat:
                native[l] += 1
            else:
                additional[l] += 1

    holders = {l: native[l] + additional[l]
               for l in set(native) | set(additional)}
    counts = {l: [native[l], additional[l]]
              for l, h in holders.items() if h >= MIN_STORE}

    total = int(cov.get("total") or 0) or population
    n_native = int(cov.get("native") or 0)
    n_spoken = int(cov.get("spoken") or 0)
    n_both = int(cov.get("both") or 0) or population

    payload = OrderedDict(
        generated=date.today().isoformat(),
        concept="native_vs_additional_languages",
        params=OrderedDict(
            min_statements=min_statements,
            scan_limit=scan_limit,
            holder_floor=HOLDER_FLOOR,
            other_floor=OTHER_FLOOR,
            panel_floor=PANEL_FLOOR,
            min_store=MIN_STORE,
            quota=[[g, q] for g, q in QUOTA],
        ),
        scale=OrderedDict(
            total_people=total,
            people_with_native=n_native,
            people_with_spoken=n_spoken,
            people_with_both=n_both,
            people_other=total - n_native - n_spoken + n_both,
            population=population,
            languages_total=len(holders),
            languages_stored=len(counts),
            languages_drawn=0,
            languages_historical=len(HISTORICAL),
            languages_constructed=len(CONSTRUCTED),
            distinct_languages=[[int(k), v] for k, v in sorted(dist.items())],
        ),
        counts=counts,
    )
    derive(payload)
    return payload


# ---------------------------------------------------------------------------
# Derive — selection, groups, headline, panels (pure; also runs for --from-json)
# ---------------------------------------------------------------------------


def _lang_rows(payload: dict) -> list:
    rows = []
    for lang, vals in payload.get("counts", {}).items():
        native, additional = int(vals[0]), int(vals[1])
        holders = native + additional
        if holders <= 0:
            continue
        rows.append(OrderedDict(
            lang=lang,
            native=native,
            additional=additional,
            holders=holders,
            native_share=native / holders,
            group=FAMILY.get(lang, "other"),
            group_label=CLUSTER_LABEL.get(FAMILY.get(lang, "other"), "Americas & Oceania"),
        ))
    return rows


def select(rows: list) -> list[str]:
    """Each group's biggest languages by holders, down to the floor.

    The Americas & Oceania slot is deliberately thin in this population, so its
    indigenous/creole languages get a lower floor (OTHER_FLOOR) instead of the
    standard HOLDER_FLOOR — keeping every region on the card.
    """
    def keeps(r: dict) -> bool:
        if r["lang"] in HISTORICAL or r["lang"] in CONSTRUCTED:
            return False
        if r["group"] == "other" and r["lang"] in INDIGENOUS:
            return r["holders"] >= OTHER_FLOOR
        return r["holders"] >= HOLDER_FLOOR

    candidates = [r for r in rows if keeps(r)]
    by_group: dict[str, list] = {}
    for r in candidates:
        by_group.setdefault(r["group"], []).append(r)
    for g in by_group:
        by_group[g].sort(key=lambda r: (-r["holders"], r["lang"]))

    chosen: list[str] = []
    seen: set = set()
    for gid, quota in QUOTA:
        pool = by_group.get(gid, [])
        if gid == "other":
            pool = [r for r in pool if r["lang"] in INDIGENOUS]
            if quota >= 2:
                picks = []
                for region in (AMERICAS, OCEANIA):
                    sub = [r for r in pool if r["lang"] in region]
                    if sub:
                        picks.append(sub[0])
                picks += [r for r in pool if r not in picks]
                pool = picks
        for r in pool[:quota]:
            if r["lang"] not in seen:
                chosen.append(r["lang"])
                seen.add(r["lang"])
    return chosen


def derive(payload: dict) -> dict:
    rows = _lang_rows(payload)
    by_name = {r["lang"]: r for r in rows}
    chosen = select(rows)

    # group order: most holders first, ties broken by the fixed group order
    prio = {g: i for i, g in enumerate(CLUSTER_ORDER)}
    group_total: dict[str, int] = {}
    group_members: dict[str, list] = {}
    for lang in chosen:
        r = by_name[lang]
        g = r["group"]
        group_total[g] = group_total.get(g, 0) + r["holders"]
        group_members.setdefault(g, []).append(r)
    order = sorted(group_total, key=lambda g: (-group_total[g], prio.get(g, 99)))

    groups = []
    for g in order:
        members = sorted(group_members[g], key=lambda r: (-r["holders"], r["lang"]))
        groups.append(OrderedDict(
            id=g,
            label=CLUSTER_LABEL.get(g, g),
            holders=group_total[g],
            members=members,
        ))

    drawn = [r for g in groups for r in g["members"]]
    total_holders = sum(r["holders"] for r in drawn)
    total_native = sum(r["native"] for r in drawn)

    big = max(drawn, key=lambda r: r["holders"])
    learned_pool = [r for r in drawn if r["holders"] >= PANEL_FLOOR]
    most_learned = sorted(learned_pool,
                          key=lambda r: (r["native_share"], -r["holders"]))[:6]
    most_native = sorted(learned_pool,
                         key=lambda r: (-r["native_share"], -r["holders"]))[:6]

    payload["languages"] = sorted(
        [OrderedDict(
            lang=r["lang"], native=r["native"], additional=r["additional"],
            holders=r["holders"],
            native_share=round(r["native_share"], 4),
            group=r["group"], group_label=r["group_label"],
        ) for r in drawn],
        key=lambda r: (-r["holders"], r["lang"]),
    )
    payload["groups"] = [
        OrderedDict(
            id=g["id"], label=g["label"], holders=g["holders"],
            members=[m["lang"] for m in g["members"]],
        )
        for g in groups
    ]
    payload["headline"] = OrderedDict(
        biggest=big["lang"],
        biggest_holders=big["holders"],
        biggest_native=big["native"],
        biggest_native_share=round(big["native_share"], 4),
        most_native=most_native[0]["lang"] if most_native else None,
        most_native_share=round(most_native[0]["native_share"], 4) if most_native else None,
        most_native_holders=most_native[0]["holders"] if most_native else 0,
        drawn=len(drawn),
        groups=len(groups),
        floor=HOLDER_FLOOR,
        panel_floor=PANEL_FLOOR,
        total_holders=total_holders,
        total_native=total_native,
        overall_native_share=round(total_native / total_holders, 4) if total_holders else 0.0,
    )
    payload["most_learned"] = [
        OrderedDict(lang=r["lang"], holders=r["holders"], native=r["native"],
                    additional=r["additional"],
                    native_share=round(r["native_share"], 4),
                    additional_share=round(1 - r["native_share"], 4))
        for r in most_learned
    ]
    payload["most_native"] = [
        OrderedDict(lang=r["lang"], holders=r["holders"], native=r["native"],
                    additional=r["additional"],
                    native_share=round(r["native_share"], 4))
        for r in most_native
    ]
    payload["scale"]["languages_drawn"] = len(drawn)
    payload["scale"]["groups_drawn"] = len(groups)
    payload["scale"]["holders_drawn"] = total_holders
    payload["scale"]["smallest_drawn"] = min((r["holders"] for r in drawn), default=0)
    return payload


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _esc(value) -> str:
    return (str(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _n(value) -> str:
    try:
        return f"{int(round(float(value))):,}"
    except (TypeError, ValueError):
        return "0"


def _pct(value, digits: int = 0) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.{digits}f}%"


def _bar_path(x: float, y: float, w: float, h: float,
              rl: float, rr: float) -> str:
    """A rounded rectangle with independent left / right corner radii."""
    if w <= 0.01:
        return ""
    rl = max(0.0, min(rl, w / 2.0, h / 2.0))
    rr = max(0.0, min(rr, w / 2.0, h / 2.0))
    d = [f"M{x + rl:.2f} {y:.2f}", f"L{x + w - rr:.2f} {y:.2f}"]
    if rr:
        d.append(f"A{rr:.2f} {rr:.2f} 0 0 1 {x + w:.2f} {y + rr:.2f}")
    d.append(f"L{x + w:.2f} {y + h - rr:.2f}")
    if rr:
        d.append(f"A{rr:.2f} {rr:.2f} 0 0 1 {x + w - rr:.2f} {y + h:.2f}")
    d.append(f"L{x + rl:.2f} {y + h:.2f}")
    if rl:
        d.append(f"A{rl:.2f} {rl:.2f} 0 0 1 {x:.2f} {y + h - rl:.2f}")
    d.append(f"L{x:.2f} {y + rl:.2f}")
    if rl:
        d.append(f"A{rl:.2f} {rl:.2f} 0 0 1 {x + rl:.2f} {y:.2f}")
    d.append("Z")
    return " ".join(d)


# ---------------------------------------------------------------------------
# Chart — one 100%-stacked bar per language, grouped by family
# ---------------------------------------------------------------------------

CHART_W = 1060.0
LABEL_W = 166.0
BAR_X = 174.0
BAR_W = 716.0
PCT_X = 964.0
HOLD_X = 1060.0
ROW_H = 18.2
GROUP_H = 16.0
COLHEAD_H = 18.0
BAR_H = 11.6
BAR_R = 2.6
NATIVE = "#bf6b28"
ADDITIONAL = "#1f7d78"
TRACK = "#ece8de"


def build_chart(payload: dict) -> str:
    groups = payload["groups"]
    by = {r["lang"]: r for r in payload["languages"]}
    height = COLHEAD_H + sum(GROUP_H + ROW_H * len(g["members"])
                             for g in groups) + 6.0

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {CHART_W:.0f} '
        f'{height:.1f}" width="{CHART_W:.0f}" height="{height:.1f}" role="img" '
        f'aria-label="Share of each language\'s speakers held as a native '
        f'mother tongue versus a language spoken later">'
    ]

    # --- 50% reference line (majority-native threshold) --------------------
    x50 = BAR_X + BAR_W * 0.5
    parts.append(
        f'<line x1="{x50:.1f}" y1="{COLHEAD_H:.1f}" x2="{x50:.1f}" '
        f'y2="{height - 6:.1f}" stroke="rgba(28,21,16,.20)" stroke-width="1" '
        f'stroke-dasharray="2.5 3"/>'
    )
    parts.append(
        f'<text x="{x50:.1f}" y="9.5" class="tick" text-anchor="middle">50%</text>'
    )
    # --- column headers ----------------------------------------------------
    parts.append(
        f'<text x="{PCT_X:.0f}" y="13" class="colhead" text-anchor="end">native</text>'
    )
    parts.append(
        f'<text x="{HOLD_X:.0f}" y="13" class="colhead" text-anchor="end">holders</text>'
    )
    parts.append(
        f'<text x="{BAR_X:.0f}" y="13" class="colhead" text-anchor="start">'
        f'share of holders &rarr;</text>'
    )

    y = COLHEAD_H
    for g in groups:
        gy = y + GROUP_H
        parts.append(
            f'<line x1="0" y1="{y + 0.5:.1f}" x2="{CHART_W:.0f}" y2="{y + 0.5:.1f}" '
            f'stroke="rgba(28,21,16,.10)" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="0" y="{y + GROUP_H / 2 + 3.4:.1f}" class="group" '
            f'text-anchor="start">{_esc(g["label"])}</text>'
        )
        y = gy
        for name in g["members"]:
            r = by[name]
            cy = y + ROW_H / 2.0
            bar_y = cy - BAR_H / 2.0
            nw = BAR_W * r["native_share"]
            aw = BAR_W - nw
            tip = (f'{_esc(r["lang"])}: {_n(r["holders"])} people hold it \u00b7 '
                   f'{_pct(r["native_share"], 1)} as a mother tongue '
                   f'({_n(r["native"])}) \u00b7 {_n(r["additional"])} additional')
            row = [f'<g><title>{tip}</title>']
            row.append(
                f'<text x="{LABEL_W - 8:.0f}" y="{cy + 3.9:.1f}" class="lang" '
                f'text-anchor="end">{_esc(r["lang"])}</text>'
            )
            row.append(
                f'<path d="{_bar_path(BAR_X, bar_y, BAR_W, BAR_H, BAR_R, BAR_R)}" '
                f'fill="{TRACK}"/>'
            )
            if aw < 1.0:
                row.append(
                    f'<path d="{_bar_path(BAR_X, bar_y, nw, BAR_H, BAR_R, BAR_R)}" '
                    f'fill="{NATIVE}"/>'
                )
            elif nw < 1.0:
                row.append(
                    f'<path d="{_bar_path(BAR_X, bar_y, BAR_W, BAR_H, BAR_R, BAR_R)}" '
                    f'fill="{ADDITIONAL}"/>'
                )
            else:
                row.append(
                    f'<path d="{_bar_path(BAR_X, bar_y, nw, BAR_H, BAR_R, 0)}" '
                    f'fill="{NATIVE}"/>'
                )
                row.append(
                    f'<path d="{_bar_path(BAR_X + nw, bar_y, aw, BAR_H, 0, BAR_R)}" '
                    f'fill="{ADDITIONAL}"/>'
                )
            row.append(
                f'<text x="{PCT_X:.0f}" y="{cy + 3.9:.1f}" class="pct" '
                f'text-anchor="end">{_pct(r["native_share"])}</text>'
            )
            row.append(
                f'<text x="{HOLD_X:.0f}" y="{cy + 3.8:.1f}" class="hold" '
                f'text-anchor="end">{_n(r["holders"])}</text>'
            )
            row.append('</g>')
            parts.append("".join(row))
            y += ROW_H
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Card HTML
# ---------------------------------------------------------------------------

HTML_HEAD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>HumansMap — native or learned?</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;800&family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:#f9f8f4; --text:#1c1510;
    --chrome:#1b4d8e;          /* chrome only: wordmark, eyebrow, headline emphasis */
    --native:#bf6b28;          /* data: recorded mother tongue (native_language) */
    --additional:#1f7d78;      /* data: spoken but not recorded as native */
    --muted:#8a8278;           /* structure: rules, labels, counts */
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
  h1 { font:800 35px/1.08 var(--display); margin:9px 0 0; letter-spacing:-.012em;
    max-width:64ch; }
  h1 em { font-style:normal; color:var(--chrome); }
  .deck { font:400 13.4px/1.44 var(--body); color:var(--muted); margin:7px 0 0;
    max-width:1512px; }
  .deck b { color:var(--text); font-weight:700; }
  .body { display:flex; gap:30px; margin-top:9px; flex:1; min-height:0; }
  .chart { flex:0 0 1060px; width:1060px; display:flex; flex-direction:column;
    min-height:0; }
  .chart svg { display:block; width:1060px; height:auto; overflow:visible; }
  .lang { font:600 11.4px/1 var(--body); fill:var(--text); }
  .group { font:700 9.2px/1 var(--body); letter-spacing:.17em; text-transform:uppercase;
    fill:var(--muted); }
  .colhead { font:700 8.8px/1 var(--body); letter-spacing:.13em; text-transform:uppercase;
    fill:var(--muted); }
  .tick { font:500 8.6px/1 var(--body); fill:var(--muted); }
  .pct { font:700 11.4px/1 var(--body); fill:var(--native); }
  .hold { font:500 10.4px/1 var(--body); fill:var(--muted);
    font-variant-numeric:tabular-nums; }
  .panel { flex:1 1 auto; min-width:0; display:flex; flex-direction:column; }
  .plabel { font:700 10.5px/1 var(--body); letter-spacing:.17em; text-transform:uppercase;
    color:var(--muted); }
  .legend { margin-top:7px; display:flex; flex-direction:column; gap:6px; }
  .lg { display:flex; align-items:flex-start; gap:8px; font:400 11.6px/1.34 var(--body);
    color:var(--text); }
  .sw { flex:0 0 auto; width:22px; height:11px; border-radius:3px; margin-top:1px; }
  .sw.nat { background:var(--native); }
  .sw.add { background:var(--additional); }
  .lgnote { font:400 10.8px/1.4 var(--body); color:var(--muted); margin-top:2px; }
  .block { border-top:2px solid var(--text); padding-top:8px; margin-top:13px; }
  .finding { border-top:2px solid var(--text); padding-top:8px; margin-top:13px;
    display:flex; gap:13px; align-items:baseline; }
  .fb { font:800 47px/0.9 var(--display); color:var(--native); letter-spacing:-.02em; }
  .fb small { font:700 12px/1 var(--body); letter-spacing:.04em; text-transform:uppercase;
    color:var(--muted); display:block; margin-top:4px; }
  .ft { font:400 11.8px/1.42 var(--body); color:var(--text); }
  .ft b { color:var(--text); font-weight:700; }
  .ft em { font-style:normal; color:var(--native); font-weight:700; }
  ul.rank { list-style:none; margin:7px 0 0; padding:0; }
  ul.rank li { display:grid; grid-template-columns:118px 1fr 40px 60px; gap:8px;
    align-items:center; padding:4.2px 0; border-bottom:1px solid var(--line); }
  .rank .nm { font:600 11.8px/1.15 var(--body); }
  .rank .mb { height:8px; border-radius:4px; background:var(--surface); overflow:hidden; }
  .rank .mb i { display:block; height:100%; border-radius:4px; }
  .rank .pv { font:700 11.8px/1 var(--body); text-align:right;
    font-variant-numeric:tabular-nums; }
  .rank .pn { font:400 10.2px/1 var(--body); color:var(--muted); text-align:right;
    font-variant-numeric:tabular-nums; }
  .caveat { font:400 10.7px/1.42 var(--body); color:var(--muted); margin-top:auto;
    padding-top:9px; border-top:1px solid var(--line); }
  .caveat b { color:var(--text); }
  .card footer { margin-top:8px; padding-top:6px; display:flex; justify-content:space-between;
    align-items:baseline; font:400 12.3px/1.4 var(--body); color:var(--muted); }
</style></head>
<body>"""


def _rank_rows(items: list, mode: str) -> str:
    out = []
    color = "var(--additional)" if mode == "learned" else "var(--native)"
    for r in items:
        share = (1 - r["native_share"]) if mode == "learned" else r["native_share"]
        out.append(
            f'<li><span class="nm">{_esc(r["lang"])}</span>'
            f'<span class="mb"><i style="width:{max(1.5, share * 100):.1f}%;'
            f'background:{color}"></i></span>'
            f'<span class="pv">{_pct(share)}</span>'
            f'<span class="pn">{_n(r["holders"])}</span></li>'
        )
    return "".join(out)


def render_html(payload: dict) -> str:
    head = payload["headline"]
    scale = payload["scale"]
    params = payload["params"]
    chart = build_chart(payload)

    loc = {r["lang"]: r for r in payload["languages"]}

    def pc(lang: str, digits: int = 0) -> str:
        r = loc.get(lang)
        return _pct(r["native_share"], digits) if r else "n/a"

    big = head["biggest"]
    bh = head["biggest_holders"]
    mn = head["most_native"]
    n_langs = head["drawn"]
    floor = head["floor"]
    both_n = scale["people_with_both"]

    # The "learned" contrast for the headline: the most-learned drawn languages,
    # skipping the creole (Nigerian Pidgin) so the examples are normal languages.
    learned_list = [r["lang"] for r in payload["most_learned"]
                    if r["lang"] != "Nigerian Pidgin"]
    ll = learned_list[0] if learned_list else mn
    tail = learned_list[1:4]

    def fmt_list(langs: list) -> str:
        return ", ".join(f"{_esc(l)} {pc(l)}" for l in langs)

    tail_txt = f", {fmt_list(tail)}" if tail else ""

    # The Americas & Oceania group is tiny in this population; name its samples.
    other_group = next((g for g in payload["groups"] if g["id"] == "other"), None)
    tiny = ", ".join(
        f'{_esc(m)} {_n(loc[m]["holders"])}'
        for m in (other_group["members"] if other_group else [])
        if m in loc
    )

    h1 = (f'{_esc(big)} is a mother tongue to <em>{pc(big)}</em> of the people '
          f'recorded speaking it &mdash; {_esc(ll)} to only {pc(ll)}.')

    deck = (
        f"Every bar is <b>100% of the people</b> in one consistent population: the "
        f"<b>{_n(both_n)}</b> people with <b>both</b> a recorded mother tongue "
        f"(<code>native_language</code>, P103) and recorded languages spoken "
        f"(<code>languages_spoken</code>, P1412). <b>Native</b> is that recorded mother "
        f"tongue; <b>additional</b> is a language spoken with a different recorded mother "
        f"tongue. Most languages come out mostly native &mdash; <b>{_esc(big)}</b> "
        f"{pc(big)}, French {pc('French')}, German {pc('German')}, Dutch {pc('Dutch')} "
        f"&mdash; but a few are mostly learned: <b>{_esc(ll)}</b> is only "
        f"<b>{pc(ll)} native</b>{tail_txt}. <code>languages_spoken</code> is unordered, "
        f"so this is a native-vs-additional split, not acquisition order; coverage skews "
        f"to well-documented, largely European figures."
    )

    legend = (
        '<div class="legend">'
        '<div class="lg"><span class="sw nat"></span><span><b>Native</b> &mdash; '
        'recorded as their mother tongue (<code>native_language</code>, P103)</span></div>'
        '<div class="lg"><span class="sw add"></span><span><b>Additional</b> &mdash; '
        'spoken, with a different recorded mother tongue (<code>languages_spoken</code>, '
        'P1412)</span></div>'
        '<div class="lgnote">Every bar is the same length \u2014 100% of that '
        'language\u2019s holders <em>in this population</em>. The dashed line marks 50%; '
        'the number at the far right is the holder total.</div>'
        '</div>'
    )

    finding = (
        '<div class="finding">'
        f'<div class="fb">{pc(big)}<small>native</small></div>'
        f'<div class="ft"><b>{_esc(big)}</b> is a mother tongue to '
        f'<b>{_n(head["biggest_native"])}</b> of its <b>{_n(bh)}</b> recorded holders. '
        f'The clearest exceptions are {fmt_list(tail) if tail else _esc(ll)}.</div>'
        '</div>'
    )

    learned = _rank_rows(payload["most_learned"], "learned")
    native = _rank_rows(payload["most_native"], "native")

    caveat = (
        "<b>Read with care.</b> The analysis is limited to the "
        f"<b>{_n(both_n)}</b> people with <b>both</b> a recorded mother tongue (P103) "
        f"and recorded languages spoken (P1412) \u2014 of {_n(scale['total_people'])} "
        f"people with &ge;{_n(params['min_statements'])} statements, only "
        f"{_n(scale['people_with_native'])} record a mother tongue and "
        f"{_n(scale['people_with_spoken'])} record languages spoken. "
        "<code>languages_spoken</code> is unordered, so this is a native-vs-additional "
        "split, not acquisition order. Across these "
        f"{n_langs} languages, <b>{_pct(head['overall_native_share'])}</b> of holders are "
        "recorded native. Coverage skews to well-documented, largely European figures; "
        "historical (Latin, Ancient Greek) and constructed (Esperanto) languages are "
        f"excluded. Most drawn languages have {_n(floor)}+ holders, but the Americas & "
        f"Oceania group rests on tiny samples ({tiny})."
    )

    card = f"""<section class="card">
  <header>
    <span class="eyebrow">Native or learned?</span>
    <span class="brand">Humans&nbsp;Map</span>
  </header>
  <h1>{h1}</h1>
  <p class="deck">{deck}</p>
  <div class="body">
    <div class="chart">{chart}</div>
    <aside class="panel">
      <div class="plabel">How to read it</div>
      {legend}
      {finding}
      <div class="block">
        <div class="plabel">Most learned &mdash; a different recorded mother tongue</div>
        <ul class="rank">{learned}</ul>
      </div>
      <div class="block">
        <div class="plabel">Most native &mdash; recorded mother tongues</div>
        <ul class="rank">{native}</ul>
      </div>
      <div class="caveat">{caveat}</div>
    </aside>
  </div>
  <footer>
    <span>Data: Wikidata (CC BY-SA) &middot; humansmap.com</span>
  </footer>
</section>"""

    return HTML_HEAD + card + "\n</body></html>\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default="docs/marketing/langnative")
    parser.add_argument("--min-statements", type=int, default=DEFAULT_MIN_STATEMENTS)
    parser.add_argument("--scan-limit", type=int, default=DEFAULT_SCAN_LIMIT)
    parser.add_argument("--from-json", default=None,
                        help="re-render from an existing langnative.json (no DB)")
    parser.add_argument("--no-html", action="store_true")
    args = parser.parse_args()

    if args.from_json:
        payload = json.loads(Path(args.from_json).read_text())
        print(f"[cache] loaded {args.from_json} (no DB access)")
        derive(payload)
    else:
        payload = collect(args.min_statements, args.scan_limit)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / "langnative.json"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {json_path}")

    if not args.no_html:
        html_path = out_dir / "langnative.html"
        html_path.write_text(render_html(payload))
        print(f"wrote {html_path}")

    head = payload["headline"]
    scale = payload["scale"]
    summary = {
        "population_with_both": scale["people_with_both"],
        "total_people": scale["total_people"],
        "people_with_native": scale["people_with_native"],
        "people_with_spoken": scale["people_with_spoken"],
        "languages_drawn": head["drawn"],
        "groups": head["groups"],
        "floor": head["floor"],
        "smallest_drawn": scale["smallest_drawn"],
        "headline": f"{head['biggest']} {_pct(head['biggest_native_share'], 1)} native",
        "biggest": f"{head['biggest']} {head['biggest_holders']} "
                   f"({_pct(head['biggest_native_share'], 1)} native)",
        "most_native": f"{head['most_native']} "
                       f"{_pct(head['most_native_share'], 1)} native",
        "native_shares": {
            r["lang"]: {"native_share": _pct(r["native_share"], 1),
                        "holders": r["holders"],
                        "native": r["native"], "additional": r["additional"]}
            for r in payload["languages"]
        },
        "most_learned": [
            {"lang": r["lang"], "additional_share": _pct(r["additional_share"], 1),
             "holders": r["holders"]}
            for r in payload["most_learned"]
        ],
        "most_native_list": [
            {"lang": r["lang"], "native_share": _pct(r["native_share"], 1),
             "holders": r["holders"]}
            for r in payload["most_native"]
        ],
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
