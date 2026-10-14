"""Download and parse SEC EDGAR Form 3/4/5 quarterly TSV zips.

§10.3.a of the project design notes.

What it does
------------
1. Loads mapped CIKs from sec_cik_map → {cik: {wikidata_id, entity_kind}}.
2. Loads Tier-0 person candidates from sec_person_candidates → {org_qid: [{person_qid, person_name_en, source}]}.
3. Determines quarterly ZIP URLs from --since-year through current year (Q1–Q4).
4. Downloads each ZIP to {sec_data_dir}/form4/YYYYqQ_form345.zip.
5. Parses in-memory (no disk extraction): SUBMISSION.tsv, REPORTINGOWNER.tsv, NONDERIV_TRANS.tsv.
6. Filters filings to mapped ISSUERCIK set.
7. Name-matches reporters against person candidates (Tier-1: exact + last-name/first-initial scoped).
8. Writes insider_of / officer_of / director_of / beneficial_owner_of edges to the affiliations
   collection. Unmatched reporters are stored on organizations.sec_unmatched_reporters[] (cap 200).
9. Checkpoints after each quarter so partial runs are resumable.

Usage (user runs these — do NOT invoke inside this script)
----------------------------------------------------------
    # Smoke test — dry run, 5 CIKs, 2023+:
    docker compose exec backend python -m scripts.sec.fetch_form4_bulk --dry-run --since-year 2023 --limit-ciks 5

    # Full dry run:
    docker compose exec backend python -m scripts.sec.fetch_form4_bulk --dry-run

    # Full run:
    docker compose exec backend python -m scripts.sec.fetch_form4_bulk

    # Targeted quarter rerun after schema drift:
    docker compose exec backend python -m scripts.sec.fetch_form4_bulk --quarters 2024q3 --dry-run

Flags
-----
--since-year YYYY   First year to process (default: settings.sec_form4_start_year = 2010).
--limit-ciks N      Cap number of CIKs processed (0 = no limit; useful for smoke tests).
--dry-run           Parse everything but skip DB writes.
--refresh           Ignore ZIP cache, re-download all quarters.
--verbose           Set log level to DEBUG.
--quarters Q        Comma-separated quarter overrides, e.g. "2024q1,2024q2".

Disk estimate
-------------
ZIPs are ~30–50 MB each. With 4 quarters × 16 years (2010–2026) that is ~256–320 ZIPs or
~8–16 GB total download. Retained parsed NDJSON is ~10× smaller (~800 MB–1.6 GB).
After parsing, ZIPs may be deleted to reclaim space. The script keeps them by default.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.config import settings  # noqa: E402
from app.database import (  # noqa: E402
    AFFILIATIONS,
    ORGANIZATIONS,
    SEC_CIK_MAP,
    SEC_PERSON_CANDIDATES,
    ensure_collections,
    ensure_database,
    get_arango_client,
)
from scripts.sec._match import (  # noqa: E402
    name_canonicals,
    parse_sec_name,
    parse_wd_name,
    tier1_match,
    _strip_diacritics,
    _TITLE_RE,
    _SUFFIX_TOKENS,
    _NICKNAMES,
    _NAME_TO_CANONICALS,
)

logger = logging.getLogger("fetch_form4_bulk")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# ZIP URL pattern per §2.4 (static files — polite requests, not token-bucket)
ZIP_URL_PATTERN = (
    "https://www.sec.gov/files/structureddata/data/"
    "insider-transactions-data-sets/{year}q{q}_form345.zip"
)

FORM4_SUBDIR = "form4"
CHECKPOINT_FILENAME = "checkpoint.json"
UNMATCHED_CAP = 200  # max entries per org in sec_unmatched_reporters
BATCH_SIZE = 500

# Polite sleep between sequential downloads (§6 note: 2 parallel max — we go sequential)
DOWNLOAD_SLEEP_SECONDS = 0.5

# Relationship tokens → edge types (Form 3 / Form 4 role field)
REL_TOKEN_TO_EDGE: dict[str, str] = {
    "director": "director_of",
    "officer": "officer_of",
    "tenpercentowner": "beneficial_owner_of",
    # "other" intentionally omitted — too vague
}

# Remove middle initials that are a lone letter (possibly followed by a period)
_MIDDLE_INITIAL_RE = re.compile(r"\s+[A-Z]\.\s+", re.IGNORECASE)

# Slug-safe characters (for edge _key)
_SLUG_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_\-]")

# ---------------------------------------------------------------------------
# Pure helpers — name normalisation, date parsing, key derivation
# ---------------------------------------------------------------------------


def normalize_name(raw: str) -> str:
    """Normalize a person name for Tier-1 matching.

    Steps: Unicode NFKC → strip titles/suffixes → strip middle initials →
    collapse whitespace → uppercase.

    >>> normalize_name("Mr. Timothy D. Cook")
    'TIMOTHY COOK'
    >>> normalize_name("WARREN E. BUFFETT JR.")
    'WARREN BUFFETT'
    >>> normalize_name("DR. JANE A. DOE III")
    'JANE DOE'
    >>> normalize_name("Elon Musk")
    'ELON MUSK'
    """
    s = unicodedata.normalize("NFKC", raw or "")
    s = _TITLE_RE.sub(" ", s)
    # Strip punctuation/commas left by suffix removal
    s = s.replace(",", " ")
    # Remove middle initials (space + single letter + optional period + space)
    s = re.sub(r"\s+[A-Z]\.\s+", " ", s, flags=re.IGNORECASE)
    # Also remove trailing single-letter initial at end (e.g. "John A")
    s = re.sub(r"\s+[A-Z]\.?\s*$", "", s, flags=re.IGNORECASE)
    # Collapse whitespace and uppercase
    return " ".join(s.split()).upper()


def last_name(normalized: str) -> str:
    """Return the last token of a normalized name (Wikidata-style: "First Last").

    >>> last_name('TIMOTHY COOK')
    'COOK'
    >>> last_name('WARREN BUFFETT')
    'BUFFETT'
    >>> last_name('MUSK')
    'MUSK'
    """
    parts = normalized.split()
    return parts[-1] if parts else ""


def first_initial(normalized: str) -> str:
    """Return the first character of the normalized name, or empty string.

    >>> first_initial('TIMOTHY COOK')
    'T'
    >>> first_initial('')
    ''
    """
    return normalized[0] if normalized else ""


def parse_sec_date(date_str: str) -> str | None:
    """Parse DD-MON-YYYY (§2.4 quirk) to ISO 8601 date string YYYY-MM-DD.

    Returns None on failure so callers can handle gracefully.

    >>> parse_sec_date("15-JAN-2025")
    '2025-01-15'
    >>> parse_sec_date("01-DEC-2003")
    '2003-12-01'
    >>> parse_sec_date("bad") is None
    True
    >>> parse_sec_date("") is None
    True
    """
    if not date_str or not date_str.strip():
        return None
    try:
        dt = datetime.strptime(date_str.strip(), "%d-%b-%Y")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        logger.debug("Unrecognised date format: %r", date_str)
        return None


def split_relationship(rptowner_relationship: str) -> list[str]:
    """Split comma-separated RPTOWNER_RELATIONSHIP tokens (§2.4 quirk).

    Returns lowercase tokens; 'other' is kept — callers decide whether to use it.

    >>> split_relationship("Director,Officer")
    ['director', 'officer']
    >>> split_relationship("TenPercentOwner")
    ['tenpercentowner']
    >>> split_relationship("Director, Officer, Other")
    ['director', 'officer', 'other']
    >>> split_relationship("")
    []
    """
    if not rptowner_relationship:
        return []
    return [t.strip().lower() for t in rptowner_relationship.split(",") if t.strip()]


def edge_key_insider(person_qid: str, org_qid: str, acc: str, trans_seq: int) -> str:
    """Deterministic _key for an insider_of edge: one per transaction.

    >>> edge_key_insider('Q312097', 'Q312', '0001234567-23-000001', 0)
    'Q312097__Q312__0001234567-23-000001__0'
    """
    return f"{person_qid}__{org_qid}__{acc}__{trans_seq}"


def edge_key_role(person_qid: str, org_qid: str, role_slug: str) -> str:
    """Deterministic _key for officer_of / director_of / beneficial_owner_of.

    _key chars must be [A-Za-z0-9_-] in ArangoDB, so we sanitise role_slug.

    >>> edge_key_role('Q312097', 'Q312', 'director_of')
    'Q312097__Q312__director_of'
    """
    safe_slug = _SLUG_UNSAFE_RE.sub("_", role_slug)
    return f"{person_qid}__{org_qid}__{safe_slug}"


def slugify_role(raw_role: str) -> str:
    """Convert an OFFICER_TITLE string to a URL/key-safe slug.

    >>> slugify_role("Chief Executive Officer")
    'chief_executive_officer'
    >>> slugify_role("EVP & CFO")
    'evp___cfo'
    """
    s = raw_role.strip().lower()
    return _SLUG_UNSAFE_RE.sub("_", s)


# ---------------------------------------------------------------------------
# ZIP URL / quarter helpers
# ---------------------------------------------------------------------------


def quarter_url(year: int, q: int) -> str:
    return ZIP_URL_PATTERN.format(year=year, q=q)


def quarter_key(year: int, q: int) -> str:
    return f"{year}q{q}"


def enumerate_quarters(since_year: int, until_year: int) -> list[tuple[int, int]]:
    """Return all (year, q) tuples from since_year Q1 through until_year Q4.

    The caller is responsible for skipping future quarters by checking 404s.
    """
    quarters = []
    for year in range(since_year, until_year + 1):
        for q in range(1, 5):
            quarters.append((year, q))
    return quarters


def parse_quarter_override(override: str) -> list[tuple[int, int]]:
    """Parse --quarters arg like '2024q1,2024q2' → [(2024, 1), (2024, 2)].

    >>> parse_quarter_override('2024q1,2024q2')
    [(2024, 1), (2024, 2)]
    >>> parse_quarter_override('2023q4')
    [(2023, 4)]
    """
    result = []
    for token in override.split(","):
        token = token.strip().lower()
        if not token:
            continue
        m = re.fullmatch(r"(\d{4})q([1-4])", token)
        if not m:
            raise ValueError(f"Invalid quarter spec: {token!r}. Expected YYYYqQ (e.g. 2024q3)")
        result.append((int(m.group(1)), int(m.group(2))))
    return result


# ---------------------------------------------------------------------------
# Download helpers
# ---------------------------------------------------------------------------


def zip_local_path(year: int, q: int) -> Path:
    return Path(settings.sec_data_dir) / FORM4_SUBDIR / f"{year}q{q}_form345.zip"


def download_zip(
    url: str,
    dest: Path,
    user_agent: str,
    refresh: bool = False,
) -> tuple[bool, str]:
    """Download a quarterly ZIP to dest. Returns (success, status).

    status is one of: "downloaded", "cached", "404", "error".
    Polite 0.5s sleep is applied by the caller between successive downloads.
    """
    if dest.exists() and dest.stat().st_size > 0 and not refresh:
        logger.info("Reusing cached ZIP: %s", dest.name)
        return True, "cached"

    logger.info("Downloading %s …", url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            dest.write_bytes(resp.read())
        logger.info("Saved %s (%.1f MB)", dest.name, dest.stat().st_size / 1e6)
        return True, "downloaded"
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            logger.debug("404 for %s — quarter not yet published or gap", url)
            return False, "404"
        logger.error("HTTP %d downloading %s: %s", exc.code, url, exc)
        return False, "error"
    except Exception as exc:
        logger.error("Error downloading %s: %s", url, exc)
        return False, "error"


# ---------------------------------------------------------------------------
# TSV parsing helpers
# ---------------------------------------------------------------------------


def _open_tsv_from_zip(zf: zipfile.ZipFile, name: str) -> csv.DictReader | None:
    """Return a DictReader for a TSV file inside the ZIP, or None if absent."""
    # Names may be mixed-case inside the ZIP
    name_lower = name.lower()
    for member in zf.namelist():
        if member.lower() == name_lower or member.lower().endswith("/" + name_lower):
            raw = zf.read(member)
            # Try UTF-8; fall back to latin-1 (SEC TSVs are occasionally latin-1)
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = raw.decode("latin-1")
            return csv.DictReader(
                io.StringIO(text),
                delimiter="\t",
                quoting=csv.QUOTE_NONE,
            )
    logger.warning("File %s not found in ZIP", name)
    return None


def parse_zip_in_memory(
    zip_path: Path,
    mapped_ciks: set[str],
) -> dict:
    """Parse a single quarterly ZIP in-memory.

    Returns a dict with keys:
      submissions      : {acc: row_dict}       — filtered to mapped CIKs
      reporters        : {acc: [row_dict, ...]} — from REPORTINGOWNER
      transactions     : {acc: [row_dict, ...]} — from NONDERIV_TRANS

    Only submissions whose ISSUERCIK is in mapped_ciks are kept.
    """
    result: dict[str, Any] = {
        "submissions": {},
        "reporters": defaultdict(list),
        "transactions": defaultdict(list),
    }

    if not zip_path.exists():
        logger.error("ZIP file not found: %s", zip_path)
        return result

    logger.debug("Parsing ZIP: %s", zip_path.name)

    with zipfile.ZipFile(zip_path, "r") as zf:
        # --- SUBMISSION.tsv -------------------------------------------------
        sub_reader = _open_tsv_from_zip(zf, "SUBMISSION.tsv")
        if sub_reader:
            for row in sub_reader:
                cik_raw = (row.get("ISSUERCIK") or "").strip()
                if not cik_raw:
                    continue
                cik_padded = cik_raw.zfill(10)
                if cik_padded not in mapped_ciks:
                    continue
                acc = (row.get("ACCESSION_NUMBER") or "").strip()
                if acc:
                    row["_cik_padded"] = cik_padded
                    result["submissions"][acc] = row

        logger.debug("Submissions kept: %d", len(result["submissions"]))

        if not result["submissions"]:
            # Nothing relevant in this ZIP — skip the other files
            return result

        kept_accs = set(result["submissions"].keys())

        # --- REPORTINGOWNER.tsv ---------------------------------------------
        ro_reader = _open_tsv_from_zip(zf, "REPORTINGOWNER.tsv")
        if ro_reader:
            for row in ro_reader:
                acc = (row.get("ACCESSION_NUMBER") or "").strip()
                if acc in kept_accs:
                    result["reporters"][acc].append(row)

        # --- NONDERIV_TRANS.tsv ---------------------------------------------
        nt_reader = _open_tsv_from_zip(zf, "NONDERIV_TRANS.tsv")
        if nt_reader:
            for row in nt_reader:
                acc = (row.get("ACCESSION_NUMBER") or "").strip()
                if acc in kept_accs:
                    result["transactions"][acc].append(row)

    return result


# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------


def checkpoint_path() -> Path:
    return Path(settings.sec_data_dir) / FORM4_SUBDIR / CHECKPOINT_FILENAME


def load_checkpoint() -> set[str]:
    p = checkpoint_path()
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_bytes())
        return set(data.get("processed_quarters", []))
    except Exception as exc:
        logger.warning("Could not read checkpoint (%s); starting fresh", exc)
        return set()


def save_checkpoint(processed: set[str], dry_run: bool = False) -> None:
    if dry_run:
        return
    p = checkpoint_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "processed_quarters": sorted(processed),
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# ArangoDB helpers
# ---------------------------------------------------------------------------


def load_cik_map(db) -> dict[str, dict]:
    """Return {padded_cik: {wikidata_id, entity_kind}} from sec_cik_map."""
    aql = f"""
    FOR doc IN {SEC_CIK_MAP}
      RETURN {{cik: doc._key, wikidata_id: doc.wikidata_id, entity_kind: doc.entity_kind}}
    """
    cursor = db.aql.execute(aql)
    return {row["cik"]: row for row in cursor if row.get("cik")}


def load_person_candidates(db) -> dict[str, list[dict]]:
    """Return {org_qid: [{person_qid, person_name_en, name_variants, source}, ...]}."""
    aql = f"""
    FOR doc IN {SEC_PERSON_CANDIDATES}
      RETURN {{
        org_qid: doc.org_qid,
        person_qid: doc.person_qid,
        person_name_en: doc.person_name_en,
        name_variants: doc.name_variants,
        source: doc.source
      }}
    """
    cursor = db.aql.execute(aql)
    result: dict[str, list[dict]] = defaultdict(list)
    for row in cursor:
        if row.get("org_qid") and row.get("person_qid"):
            result[row["org_qid"]].append(row)
    return result


def upsert_edges_batch(db, edges: list[dict], dry_run: bool) -> None:
    if dry_run:
        logger.debug("DRY RUN: would upsert %d edges", len(edges))
        return
    # Use overwriteMode: "update" to merge (not replace) on duplicate _key
    aql = f"""
    FOR doc IN @batch
      INSERT doc INTO {AFFILIATIONS}
      OPTIONS {{ overwriteMode: "update" }}
    """
    db.aql.execute(aql, bind_vars={"batch": edges})


def append_unmatched_reporters_batch(
    db,
    updates: list[tuple[str, dict]],
    dry_run: bool,
) -> None:
    """Append unmatched reporter entries to organizations.sec_unmatched_reporters[].

    Each update is (org_qid, entry_dict). Caps array at UNMATCHED_CAP by keeping
    the most recent entries (the appended ones win; we trim the head).
    """
    if dry_run:
        logger.debug("DRY RUN: would append %d unmatched reporter entries", len(updates))
        return
    # Group by org_qid so we do one UPDATE per org
    by_org: dict[str, list[dict]] = defaultdict(list)
    for org_qid, entry in updates:
        by_org[org_qid].append(entry)

    for org_qid, entries in by_org.items():
        aql = f"""
        LET doc = DOCUMENT("{ORGANIZATIONS}/{org_qid}")
        LET existing = (doc != null AND doc.sec_unmatched_reporters != null)
          ? doc.sec_unmatched_reporters : []
        LET combined = APPEND(existing, @entries)
        LET capped = LENGTH(combined) > {UNMATCHED_CAP}
          ? SLICE(combined, LENGTH(combined) - {UNMATCHED_CAP})
          : combined
        UPDATE @org_qid WITH {{sec_unmatched_reporters: capped}} IN {ORGANIZATIONS}
          OPTIONS {{ignoreErrors: true}}
        """
        try:
            db.aql.execute(aql, bind_vars={"org_qid": org_qid, "entries": entries})
        except Exception as exc:
            logger.warning("Could not update unmatched reporters for %s: %s", org_qid, exc)


def update_last_seen_batch(
    db,
    updates: list[tuple[str, str, str]],
    dry_run: bool,
) -> None:
    """Update last_seen on existing officer_of / director_of edges.

    updates = [(person_qid, org_qid, date_iso), ...]
    Updates the max(current last_seen, date_iso) for each (person, org) affiliation.
    """
    if dry_run:
        return
    for person_qid, org_qid, date_iso in updates:
        aql = f"""
        FOR edge IN {AFFILIATIONS}
          FILTER edge._from == CONCAT("{ORGANIZATIONS}/", @person_qid) OR
                 edge._from == CONCAT("persons/", @person_qid)
          FILTER edge._to == CONCAT("{ORGANIZATIONS}/", @org_qid)
          FILTER edge.type IN ["officer_of", "director_of"]
          UPDATE edge WITH {{
            last_seen: (edge.last_seen == null OR edge.last_seen < @date_iso) ? @date_iso : edge.last_seen
          }} IN {AFFILIATIONS}
          OPTIONS {{ignoreErrors: true}}
        """
        try:
            db.aql.execute(
                aql,
                bind_vars={"person_qid": person_qid, "org_qid": org_qid, "date_iso": date_iso},
            )
        except Exception as exc:
            logger.debug("last_seen update skipped for (%s, %s): %s", person_qid, org_qid, exc)


# ---------------------------------------------------------------------------
# Edge builders
# ---------------------------------------------------------------------------


def build_insider_edge(
    person_qid: str,
    org_qid: str,
    acc: str,
    trans_row: dict,
    trans_seq: int,
    form_type: str,
    match_method: str,
    match_confidence: str,
    prev_acc: str | None,
) -> dict:
    """Build an insider_of affiliations edge document."""
    trans_date = parse_sec_date(trans_row.get("TRANS_DATE", ""))
    try:
        shares = float(trans_row.get("TRANS_SHARES") or 0)
    except (ValueError, TypeError):
        shares = None
    try:
        price = float(trans_row.get("TRANS_PRICEPERSHARE") or 0)
    except (ValueError, TypeError):
        price = None
    direction = (trans_row.get("TRANS_ACQUIRED_DISP_CD") or "").strip().upper() or None

    edge: dict = {
        "_key": edge_key_insider(person_qid, org_qid, acc, trans_seq),
        "_from": f"persons/{person_qid}",
        "_to": f"organizations/{org_qid}",
        "type": "insider_of",
        "transaction_date": trans_date,
        "shares": shares,
        "price_usd": price,
        "direction": direction,
        "accession": acc,
        "form_type": form_type,
        "match_method": match_method,
        "match_confidence": match_confidence,
    }
    if prev_acc:
        edge["supersedes_accession"] = prev_acc
    return edge


def build_role_edge(
    edge_type: str,
    person_qid: str,
    org_qid: str,
    filing_date: str | None,
    role: str | None,
    acc: str,
    match_method: str,
    match_confidence: str,
    is_amendment: bool,
    prev_acc: str | None,
) -> dict:
    """Build an officer_of / director_of / beneficial_owner_of edge document."""
    role_slug = slugify_role(role or edge_type)
    edge: dict = {
        "_key": edge_key_role(person_qid, org_qid, role_slug),
        "_from": f"persons/{person_qid}",
        "_to": f"organizations/{org_qid}",
        "type": edge_type,
        "start_date": filing_date,
        "end_date": None,
        "last_seen": filing_date,
        "source_accession": acc,
        "match_method": match_method,
        "match_confidence": match_confidence,
    }
    if edge_type == "officer_of" and role:
        edge["role"] = role
    if is_amendment and prev_acc:
        edge["supersedes_accession"] = prev_acc
    return edge


# ---------------------------------------------------------------------------
# Per-quarter processing
# ---------------------------------------------------------------------------


def process_quarter(
    year: int,
    q: int,
    zip_path: Path,
    cik_map: dict[str, dict],
    person_candidates: dict[str, list[dict]],
    limit_ciks: int,
    dry_run: bool,
    db,
    stats: dict,
) -> None:
    """Parse one quarterly ZIP and write edges to ArangoDB."""
    mapped_ciks = set(cik_map.keys())
    if limit_ciks:
        # Pick the N CIKs whose org has the most Tier-0 candidates — gives
        # smoke tests meaningful match-rate signal instead of hash-order roulette.
        ranked = sorted(
            mapped_ciks,
            key=lambda c: len(person_candidates.get(cik_map[c]["wikidata_id"], [])),
            reverse=True,
        )
        mapped_ciks = set(ranked[:limit_ciks])
        logger.info(
            "limit-ciks %d: selected CIKs (by candidate-count desc): %s",
            limit_ciks,
            ", ".join(sorted(mapped_ciks)),
        )

    parsed = parse_zip_in_memory(zip_path, mapped_ciks)
    submissions = parsed["submissions"]
    reporters_index = parsed["reporters"]
    transactions_index = parsed["transactions"]

    stats["total_submissions_parsed"] += len(submissions)
    logger.info(
        "%dq%d: %d submissions matched mapped CIKs",
        year, q, len(submissions),
    )

    edges_to_write: list[dict] = []
    unmatched_to_write: list[tuple[str, dict]] = []
    last_seen_updates: list[tuple[str, str, str]] = []

    for acc, sub_row in submissions.items():
        cik_padded = sub_row["_cik_padded"]
        org_info = cik_map.get(cik_padded)
        if not org_info:
            continue
        org_qid = org_info["wikidata_id"]
        form_type_raw = (sub_row.get("DOCUMENT_TYPE") or "").strip()
        is_amendment = form_type_raw.endswith("/A")
        prev_acc = (sub_row.get("PREVIOUS_ACCESSION_NUMBER") or "").strip() or None

        filing_date = parse_sec_date(sub_row.get("PERIOD_OF_REPORT") or sub_row.get("FILED_AS_OF_DATE") or "")

        reporter_rows = reporters_index.get(acc, [])
        if not reporter_rows:
            stats["no_reporter_rows"] = stats.get("no_reporter_rows", 0) + 1
            continue

        trans_rows = transactions_index.get(acc, [])

        for reporter in reporter_rows:
            sec_name = (reporter.get("RPTOWNERNAME") or "").strip()

            # Tier-1 match
            candidates = person_candidates.get(org_qid, [])
            person_qid, match_method, match_confidence = tier1_match(sec_name, candidates)

            if person_qid is None:
                # Store unmatched reporter as denormalized org-level data
                stats["unmatched"] += 1
                stats.setdefault("unmatched_samples", []).append(
                    f"{sec_name!r} @ {org_qid} (cands={len(candidates)})"
                )
                unmatched_to_write.append(
                    (
                        org_qid,
                        {
                            "reporter_name": sec_name,
                            "accession": acc,
                            "form_type": form_type_raw,
                            "filing_date": filing_date,
                            "cik": cik_padded,
                        },
                    )
                )
                continue

            # Determine relationship tokens
            rel_raw = (reporter.get("RPTOWNER_RELATIONSHIP") or "").strip()
            rel_tokens = split_relationship(rel_raw)

            # --- Form 3 (initial statement): produce role edges -------------
            if form_type_raw in ("3", "3/A"):
                for token in rel_tokens:
                    edge_type = REL_TOKEN_TO_EDGE.get(token)
                    if not edge_type:
                        continue  # skip "other"
                    officer_title = (reporter.get("OFFICER_TITLE") or "").strip() or None
                    role = officer_title if edge_type == "officer_of" else edge_type

                    edge = build_role_edge(
                        edge_type=edge_type,
                        person_qid=person_qid,
                        org_qid=org_qid,
                        filing_date=filing_date,
                        role=role,
                        acc=acc,
                        match_method=match_method,
                        match_confidence=match_confidence,
                        is_amendment=is_amendment,
                        prev_acc=prev_acc,
                    )
                    edges_to_write.append(edge)

                    edge_type_key = f"edges_{edge_type}"
                    stats[edge_type_key] = stats.get(edge_type_key, 0) + 1

            # --- Form 4 / 5 (transactions): produce insider_of edges --------
            elif form_type_raw in ("4", "4/A", "5", "5/A"):
                # Also produce role edges from RPTOWNER_RELATIONSHIP
                for token in rel_tokens:
                    edge_type = REL_TOKEN_TO_EDGE.get(token)
                    if not edge_type:
                        continue
                    officer_title = (reporter.get("OFFICER_TITLE") or "").strip() or None
                    role = officer_title if edge_type == "officer_of" else edge_type
                    edge = build_role_edge(
                        edge_type=edge_type,
                        person_qid=person_qid,
                        org_qid=org_qid,
                        filing_date=filing_date,
                        role=role,
                        acc=acc,
                        match_method=match_method,
                        match_confidence=match_confidence,
                        is_amendment=is_amendment,
                        prev_acc=prev_acc,
                    )
                    edges_to_write.append(edge)
                    stats[f"edges_{edge_type}"] = stats.get(f"edges_{edge_type}", 0) + 1

                # Transaction rows → insider_of edges
                for seq, trans_row in enumerate(trans_rows):
                    edge = build_insider_edge(
                        person_qid=person_qid,
                        org_qid=org_qid,
                        acc=acc,
                        trans_row=trans_row,
                        trans_seq=seq,
                        form_type=form_type_raw,
                        match_method=match_method,
                        match_confidence=match_confidence,
                        prev_acc=prev_acc,
                    )
                    edges_to_write.append(edge)
                    stats["edges_insider_of"] = stats.get("edges_insider_of", 0) + 1

                    # §2.5 last_seen update
                    if edge.get("transaction_date"):
                        last_seen_updates.append(
                            (person_qid, org_qid, edge["transaction_date"])
                        )

            # Track match stats
            if match_confidence == "exact":
                stats["match_exact"] += 1
            else:
                stats["match_initial"] += 1

    # --- Batch write edges --------------------------------------------------
    batch_sleep_s = getattr(settings, "sec_batch_sleep_ms", 50) / 1000.0
    for i in range(0, len(edges_to_write), BATCH_SIZE):
        chunk = edges_to_write[i : i + BATCH_SIZE]
        upsert_edges_batch(db, chunk, dry_run)
        if not dry_run and i + BATCH_SIZE < len(edges_to_write):
            time.sleep(batch_sleep_s)

    # --- Unmatched reporter updates -----------------------------------------
    if unmatched_to_write:
        append_unmatched_reporters_batch(db, unmatched_to_write, dry_run)
        stats["unmatched_appended"] += len(unmatched_to_write)

    # --- last_seen updates ---------------------------------------------------
    if last_seen_updates:
        update_last_seen_batch(db, last_seen_updates, dry_run)

    logger.info(
        "%dq%d: wrote %d edges, %d unmatched",
        year, q, len(edges_to_write), len(unmatched_to_write),
    )


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------


def run(args: argparse.Namespace) -> int:
    """Execute the Form 4 bulk load. Returns exit code (0 = success)."""
    t_start = time.monotonic()

    # --- Connect to DB -------------------------------------------------------
    client = get_arango_client()
    db = ensure_database(client)
    ensure_collections(db)

    # --- Load CIK map --------------------------------------------------------
    logger.info("Loading CIK map from %s …", SEC_CIK_MAP)
    cik_map = load_cik_map(db)
    logger.info("Loaded %d mapped CIKs", len(cik_map))
    if not cik_map:
        logger.error("sec_cik_map is empty. Run build_cik_map.py first.")
        return 1

    # --- Load person candidates ----------------------------------------------
    logger.info("Loading person candidates from %s …", SEC_PERSON_CANDIDATES)
    person_candidates = load_person_candidates(db)
    logger.info("Loaded candidates for %d orgs", len(person_candidates))
    if not person_candidates:
        logger.warning(
            "sec_person_candidates is empty — all reporters will be unmatched. "
            "Run build_person_candidates.py first."
        )

    # --- Determine quarters --------------------------------------------------
    current_year = datetime.now(timezone.utc).year
    since_year = args.since_year

    if args.quarters:
        try:
            quarter_list = parse_quarter_override(args.quarters)
        except ValueError as exc:
            logger.error("Invalid --quarters argument: %s", exc)
            return 1
    else:
        quarter_list = enumerate_quarters(since_year, current_year)

    logger.info(
        "Processing %d quarter(s) from %dq1 onwards",
        len(quarter_list), since_year,
    )

    # --- Load checkpoint -----------------------------------------------------
    processed_quarters: set[str] = load_checkpoint()
    logger.info("Checkpoint: %d quarters already processed", len(processed_quarters))

    # --- Per-quarter download + parse loop -----------------------------------
    stats: dict = {
        "quarters_processed": 0,
        "quarters_404": 0,
        "quarters_cached": 0,
        "quarters_downloaded": 0,
        "quarters_error": 0,
        "total_submissions_parsed": 0,
        "match_exact": 0,
        "match_initial": 0,
        "unmatched": 0,
        "unmatched_appended": 0,
    }

    user_agent = settings.sec_user_agent

    for year, q in quarter_list:
        qkey = quarter_key(year, q)

        if qkey in processed_quarters and not args.refresh:
            logger.info("Skipping %s (checkpoint)", qkey)
            stats["quarters_cached"] += 1
            continue

        url = quarter_url(year, q)
        dest = zip_local_path(year, q)

        ok, status = download_zip(url, dest, user_agent, refresh=args.refresh)

        if not ok:
            if status == "404":
                stats["quarters_404"] += 1
            else:
                stats["quarters_error"] += 1
                logger.warning("Skipping %s due to download error", qkey)
            # Don't checkpoint failed quarters so they can be retried
            # Sleep briefly before next attempt
            time.sleep(DOWNLOAD_SLEEP_SECONDS)
            continue

        if status == "downloaded":
            stats["quarters_downloaded"] += 1
            time.sleep(DOWNLOAD_SLEEP_SECONDS)
        elif status == "cached":
            stats["quarters_cached"] += 1

        # Parse this quarter
        process_quarter(
            year=year,
            q=q,
            zip_path=dest,
            cik_map=cik_map,
            person_candidates=person_candidates,
            limit_ciks=args.limit_ciks,
            dry_run=args.dry_run,
            db=db,
            stats=stats,
        )

        stats["quarters_processed"] += 1
        processed_quarters.add(qkey)
        save_checkpoint(processed_quarters, dry_run=args.dry_run)

    # --- Summary -------------------------------------------------------------
    elapsed = time.monotonic() - t_start
    total_matched = stats["match_exact"] + stats["match_initial"]
    total_reporters = total_matched + stats["unmatched"]
    match_pct = (total_matched / total_reporters * 100) if total_reporters else 0.0

    print("")
    print("=" * 62)
    print("  fetch_form4_bulk SUMMARY")
    print("=" * 62)
    print(f"  Quarters processed          : {stats['quarters_processed']:>8,}")
    print(f"  Quarters 404 (skipped)      : {stats['quarters_404']:>8,}")
    print(f"  Quarters cached (reused)    : {stats['quarters_cached']:>8,}")
    print(f"  Quarters newly downloaded   : {stats['quarters_downloaded']:>8,}")
    print(f"  Quarters error              : {stats['quarters_error']:>8,}")
    print(f"  Total submissions parsed    : {stats['total_submissions_parsed']:>8,}")
    print(f"  Reporter match rate         : {match_pct:>7.1f}%")
    print(f"    Exact matches             : {stats['match_exact']:>8,}")
    print(f"    Initial matches           : {stats['match_initial']:>8,}")
    print(f"    Unmatched                 : {stats['unmatched']:>8,}")
    print(f"  Unmatched appended to orgs  : {stats['unmatched_appended']:>8,}")
    print("  Edges by type:")
    for et in ("insider_of", "officer_of", "director_of", "beneficial_owner_of"):
        key = f"edges_{et}"
        print(f"    {et:<28}: {stats.get(key, 0):>8,}")
    print(f"  Total runtime               : {elapsed:>7.1f}s")
    if args.dry_run:
        print("  MODE: DRY RUN (no DB writes)")
    print("=" * 62)

    samples = stats.get("unmatched_samples", [])
    if samples:
        import random
        show = random.sample(samples, min(20, len(samples)))
        print("")
        print("  Sample of 20 unmatched reporter names (to audit):")
        for s in show:
            print(f"    - {s}")

    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Download SEC EDGAR Form 3/4/5 quarterly TSV ZIPs, parse "
            "SUBMISSION + REPORTINGOWNER + NONDERIV_TRANS, and write "
            "insider_of / officer_of / director_of / beneficial_owner_of "
            "edges to the affiliations collection."
        )
    )
    p.add_argument(
        "--since-year",
        type=int,
        default=settings.sec_form4_start_year,
        metavar="YYYY",
        help=(
            f"First year to process (default: {settings.sec_form4_start_year} "
            "from SEC_FORM4_START_YEAR)."
        ),
    )
    p.add_argument(
        "--limit-ciks",
        type=int,
        default=0,
        metavar="N",
        help="Cap number of CIKs processed per quarter (0 = no limit; for smoke tests).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse everything but skip all DB writes.",
    )
    p.add_argument(
        "--refresh",
        action="store_true",
        help="Ignore ZIP cache and re-download all quarters.",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Set log level to DEBUG.",
    )
    p.add_argument(
        "--quarters",
        default="",
        metavar="Q",
        help="Comma-separated quarter overrides, e.g. '2024q1,2024q3'. Skips --since-year logic.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    sys.exit(run(args))


if __name__ == "__main__":
    main()
