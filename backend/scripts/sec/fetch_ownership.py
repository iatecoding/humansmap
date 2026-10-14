"""Fetch SC 13D/G beneficial ownership filings and write org_structure edges.

§10.5.b of the project design notes.

What it does
------------
1. Loads org CIK → QID pairs from sec_cik_map (entity_kind == "org").
   Person filers (~1% of SC 13D universe) are intentionally out of scope —
   they would require widening the affiliations collection and add complexity
   for negligible yield.
2. Builds an in-memory CIK→QID index for fast subject-company lookups.
3. For each mapped filer CIK, walks filings.recent + filings.files[] (the same
   paginated approach as fetch_material_events.py), reusing cached submissions
   JSON from §10.4.a.
4. Filters rows to form types: SC 13D, SC 13D/A, SC 13G, SC 13G/A.
5. Fetches the primary document from EDGAR Archives (XML or HTML).
6. Parses two fields from the primary document:
   - SUBJECT-COMPANY CIK  — identifies the issuer whose shares are being held
   - Percent of class      — the beneficial ownership stake
   If either cannot be extracted, or stake < 5%, the filing is skipped.
7. Looks up the subject CIK in the CIK→QID index.  If not mapped, skips
   (pure-additive policy — no new nodes created).
8. Upserts a beneficial_owner_of edge into org_structure (Org→Org).

Known gaps (intentional, not bugs)
-----------------------------------
- Person filers: ~1% of SC 13D filings are from natural persons.  These are
  dropped here because they would require Person→Org edges in affiliations,
  which is a separate concern.  The net omission is very small.
- Parse failures: silently skipped with a warning.  No retry or LLM fallback.
- Amendment linking: each amendment version is stored as a standalone edge doc
  keyed by its own accession.  Canonical selection (latest version wins) is
  done at query time; no superseded_by backpointer is written here.

Usage (user runs these — do NOT invoke inside this script)
----------------------------------------------------------
    # Dry run (no DB writes):
    docker compose exec backend python -m scripts.sec.fetch_ownership --dry-run --limit 5

    # Full run:
    docker compose exec backend python -m scripts.sec.fetch_ownership

    # Force re-fetch of submissions JSONs (ignore on-disk cache):
    docker compose exec backend python -m scripts.sec.fetch_ownership --refresh

    # Start fresh, ignoring checkpoint:
    docker compose exec backend python -m scripts.sec.fetch_ownership --no-resume

Flags
-----
--limit N      Cap number of CIKs processed (0 = no limit).
--dry-run      Parse filings but skip all DB writes.
--verbose      Set log level to DEBUG.
--refresh      Ignore on-disk submissions cache; re-fetch from SEC EDGAR.
--resume       Skip already-processed CIKs via checkpoint (default ON).
--no-resume    Disable checkpoint; process all CIKs.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# lxml is available inside the container via requirements.txt
try:
    from lxml import etree as _etree
    _LXML_AVAILABLE = True
except ImportError:  # pragma: no cover
    _LXML_AVAILABLE = False

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.config import settings  # noqa: E402
from app.database import (  # noqa: E402
    ORGANIZATIONS,
    ORG_STRUCTURE,
    SEC_CIK_MAP,
    ensure_collections,
    ensure_database,
    get_arango_client,
)
from app.providers.sec_edgar import SecEdgarProvider  # noqa: E402

logger = logging.getLogger("fetch_ownership")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CACHE_SUBDIR = "sparql_cache"
CHECKPOINT_FILENAME = "ownership_checkpoint.json"

# Form types we care about
_13DG_FORMS = frozenset({"SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A"})

# Base URL for paginated filings.files[] entries
_SUBMISSIONS_FILES_BASE = "https://data.sec.gov/submissions/"

# Minimum beneficial ownership stake to record (per SEC definition)
_MIN_STAKE_PCT = 5.0

# ---------------------------------------------------------------------------
# Pure helpers — easy to unit-test
# ---------------------------------------------------------------------------


def classify_13_form(form: str) -> tuple[str, bool] | None:
    """Classify a form type string as a 13D/G form.

    Returns ``(canonical_form_type, is_amendment)`` for recognised SC 13D/G
    forms, or ``None`` for everything else.

    ``canonical_form_type`` is the base form without the ``/A`` suffix:
    - ``"SC 13D"`` for both ``SC 13D`` and ``SC 13D/A``
    - ``"SC 13G"`` for both ``SC 13G`` and ``SC 13G/A``

    >>> classify_13_form("SC 13D")
    ('SC 13D', False)
    >>> classify_13_form("SC 13D/A")
    ('SC 13D', True)
    >>> classify_13_form("SC 13G")
    ('SC 13G', False)
    >>> classify_13_form("SC 13G/A")
    ('SC 13G', True)
    >>> classify_13_form("8-K") is None
    True
    >>> classify_13_form("") is None
    True
    """
    if not form:
        return None
    is_amendment = form.endswith("/A")
    base = form[:-2] if is_amendment else form
    if base in ("SC 13D", "SC 13G"):
        return (base, is_amendment)
    return None


def parse_stake_pct(doc_text: str, content_type: str = "text/html") -> float | None:
    """Extract the "Percent of class" value from an SC 13D/G primary document.

    Tries two strategies in order:

    1. **XML tag** — looks for ``<percentOfClass>`` (case-insensitive element
       name) anywhere in the document.  Works for modern EDGAR XML submissions.
    2. **HTML/text regex fallback** — scans for a number immediately following
       any variant of "Percent of class" or "Percent of Class represented by".
       Handles common HTML table layouts and plain-text cover pages.

    Returns the stake as a ``float`` (e.g. ``7.5``), or ``None`` if not found.

    Args:
        doc_text:     Raw text of the primary document (decoded as UTF-8 or
                      latin-1 — the caller is responsible for decoding).
        content_type: Hint; if it contains ``"xml"`` the XML path is tried
                      first.  Otherwise both paths are tried (XML first, then
                      regex).

    >>> parse_stake_pct('<percentOfClass>7.5</percentOfClass>', 'text/xml')
    7.5
    >>> parse_stake_pct('<PercentOfClass>12.3</PercentOfClass>', 'text/xml')
    12.3
    >>> html = '<tr><td>Percent of class</td><td>12.3%</td></tr>'
    >>> parse_stake_pct(html, 'text/html')
    12.3
    >>> parse_stake_pct('no stake here', 'text/html') is None
    True
    """
    # Strategy 1: XML element <percentOfClass> (case-insensitive local name)
    if _LXML_AVAILABLE:
        try:
            # Use lxml's recover mode — SC 13D XML often has a full SGML
            # header (EDGAR text-format header block) before the XML payload.
            # We strip the preamble by finding the first '<' that looks like
            # an XML tag.
            xml_start = _find_xml_start(doc_text)
            if xml_start >= 0:
                snippet = doc_text[xml_start:]
                root = _etree.fromstring(
                    snippet.encode("utf-8", errors="replace"),
                    parser=_etree.XMLParser(recover=True),
                )
                for elem in root.iter():
                    local = _etree.QName(elem).localname.lower() if elem.tag and not callable(elem.tag) else ""
                    if local == "percentofclass" and elem.text:
                        try:
                            return float(elem.text.strip().rstrip("%"))
                        except ValueError:
                            pass
        except Exception:
            pass  # XML parse failed — fall through to regex

    # Strategy 2: regex on the raw text (HTML or plain-text cover page)
    return _parse_stake_pct_regex(doc_text)


def _find_xml_start(text: str) -> int:
    """Return index of the first XML-looking '<' character, or -1."""
    idx = text.find("<?xml")
    if idx >= 0:
        return idx
    # Some SC 13D XML files start with a bare root tag after the EDGAR header
    idx = text.find("<ownershipDocument")
    if idx >= 0:
        return idx
    idx = text.find("<sc13")
    if idx >= 0:
        return idx
    # Generic: first < followed by a letter (not SGML <!DOCTYPE / <!-- etc.)
    for i, ch in enumerate(text):
        if ch == "<" and i + 1 < len(text) and text[i + 1].isalpha():
            return i
    return -1


def _parse_stake_pct_regex(text: str) -> float | None:
    """Regex-based stake extraction for HTML and plain-text documents."""
    # Pattern 1: "Percent of class represented by amount in Row (11): 4.9%"
    # Pattern 2: "<td>Percent of class</td><td>12.3%</td>"
    # Pattern 3: "Percent of Class: 7.5%" (various header formats)
    patterns = [
        # SEC cover pages commonly label the value as Row (11). Match the row
        # number explicitly so it cannot be mistaken for the percentage.
        r'(?i)percent\s+of\s+class\s+represented\s+by\s+amount\s+in\s+row\s*\(\d+\)\s*:\s*(\d+\.?\d*)\s*%',
        # Explicit "percent of class" label followed (after noise) by a number + optional %
        r'(?i)percent\s+of\s+class(?:\s+represented\s+by[^%\d]{0,80})?[^%\d]{0,60}(\d+\.?\d*)\s*%',
        # Fallback: just "percent" + number in the same vicinity (less specific)
        r'(?i)(?:percent(?:age)?\s+of\s+(?:the\s+)?class)[^\d]{0,80}(\d+\.?\d*)',
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                continue
    return None


def parse_subject_cik(doc_text: str) -> str | None:
    """Extract the SUBJECT-COMPANY CIK from an SC 13D/G primary document.

    Tries three patterns in priority order:

    1. **XML tag** — ``<CIK>0000320193</CIK>`` inside a ``<SUBJECT-COMPANY>``
       or similar block (EDGAR structured XML header).
    2. **SEC SGML header style** — multi-line block beginning with
       ``SUBJECT COMPANY:`` followed by ``CENTRAL INDEX KEY: <digits>`` within
       the next ~20 lines.
    3. **Inline CIK= pattern** — ``CIK=0000320193`` anywhere in the text
       (legacy EDGAR HTML filing headers).

    Returns the CIK zero-padded to 10 digits, or ``None`` if not extractable.

    >>> parse_subject_cik('<SUBJECT-COMPANY><CIK>0000320193</CIK>')
    '0000320193'
    >>> parse_subject_cik('CIK: 320193')
    '0000320193'
    >>> parse_subject_cik('no cik here') is None
    True
    """
    # Pattern 1: XML-style <SUBJECT-COMPANY>...<CIK>...</CIK>
    # The tag is typically upper-cased in EDGAR format files
    m = re.search(
        r'<SUBJECT[- ]COMPANY>.*?<CIK>\s*(\d+)\s*</CIK>',
        doc_text,
        re.IGNORECASE | re.DOTALL,
    )
    if m:
        return str(int(m.group(1))).zfill(10)

    # Pattern 2: SEC SGML plain-text header style
    # "SUBJECT COMPANY:" appears as a section header; "CENTRAL INDEX KEY: nnnn"
    # follows within ~20 lines.
    m = re.search(
        r'SUBJECT\s+COMPANY\s*:.*?CENTRAL\s+INDEX\s+KEY\s*:\s*(\d+)',
        doc_text,
        re.IGNORECASE | re.DOTALL,
    )
    if m:
        return str(int(m.group(1))).zfill(10)

    # Pattern 3: CIK= inline (legacy HTML header, e.g. in <META> tags or URL params)
    m = re.search(r'\bCIK\s*[=:]\s*(\d{1,10})\b', doc_text, re.IGNORECASE)
    if m:
        return str(int(m.group(1))).zfill(10)

    return None


def build_edge_key(accession_nodash: str, filer_qid: str, subject_qid: str) -> str:
    """Build a stable, unique edge _key for an ownership edge.

    Format: ``{accession_nodash}_{filer_qid}_{subject_qid}``

    Using the accession number (which uniquely identifies a single filing
    version) plus both endpoint QIDs makes the key stable across re-runs and
    collision-free even if the same accession somehow referenced multiple
    subject companies (which is not valid per SEC rules, but defensive coding
    costs nothing here).

    >>> build_edge_key("000119312524123456", "Q312", "Q380")
    '000119312524123456_Q312_Q380'
    """
    return f"{accession_nodash}_{filer_qid}_{subject_qid}"


def build_filing_url(cik: str, accession: str, primary_doc: str) -> str:
    """Build the EDGAR filing URL for a given CIK, accession, and document.

    Mirrors the identical function in fetch_material_events.py.

    Args:
        cik:         10-digit zero-padded CIK string, e.g. "0000320193".
        accession:   Raw accession number with dashes, e.g. "0001193125-24-198765".
        primary_doc: Filename of the primary document, e.g. "sc13d.xml".

    Returns:
        Full EDGAR URL, e.g.:
        https://www.sec.gov/Archives/edgar/data/320193/000119312524198765/sc13d.xml

    >>> build_filing_url("0000320193", "0001193125-24-198765", "sc13d.xml")
    'https://www.sec.gov/Archives/edgar/data/320193/000119312524198765/sc13d.xml'
    """
    cik_int = int(cik)  # strips leading zeros
    accession_nodash = accession.replace("-", "")
    return (
        f"https://www.sec.gov/Archives/edgar/data/{cik_int}"
        f"/{accession_nodash}/{primary_doc}"
    )


# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------


def checkpoint_path() -> Path:
    return Path(settings.sec_data_dir) / CACHE_SUBDIR / CHECKPOINT_FILENAME


def load_checkpoint() -> set[str]:
    """Return the set of already-processed CIKs."""
    p = checkpoint_path()
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_bytes())
        return set(data.get("processed_ciks", []))
    except Exception as exc:
        logger.warning("Could not read checkpoint file (%s); starting fresh", exc)
        return set()


def save_checkpoint(processed: set[str]) -> None:
    p = checkpoint_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "processed_ciks": sorted(processed),
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Filings iteration helpers (mirrors fetch_material_events.py)
# ---------------------------------------------------------------------------


def iter_filings_rows(filings_recent: dict) -> Iterable[dict]:
    """Zip column-oriented filings.recent dict into per-filing dicts.

    Pads shorter columns with empty strings so callers can unconditionally
    access all keys.  Mirrors the identical function in fetch_material_events.
    """
    accessions = filings_recent.get("accessionNumber", [])
    dates = filings_recent.get("filingDate", [])
    forms = filings_recent.get("form", [])
    primary_docs = filings_recent.get("primaryDocument", [])

    max_len = max(
        [len(accessions), len(dates), len(forms), len(primary_docs)],
        default=0,
    )

    def _pad(lst: list, length: int) -> list:
        return lst + [""] * (length - len(lst))

    accessions = _pad(list(accessions), max_len)
    dates = _pad(list(dates), max_len)
    forms = _pad(list(forms), max_len)
    primary_docs = _pad(list(primary_docs), max_len)

    for acc, date, form, doc in zip(accessions, dates, forms, primary_docs, strict=True):
        yield {
            "accessionNumber": acc,
            "filingDate": date,
            "form": form,
            "primaryDocument": doc,
        }


def iter_extra_filings(
    submissions: dict[str, Any],
    provider: SecEdgarProvider,
) -> Iterable[dict]:
    """Iterate additional filings pages referenced in filings.files[].

    Mirrors the identical function in fetch_material_events.py.
    """
    files_list: list[dict] = (
        submissions.get("filings", {}).get("files") or []
    )
    for file_entry in files_list:
        name = file_entry.get("name", "")
        if not name:
            continue
        url = f"{_SUBMISSIONS_FILES_BASE}{name}"
        logger.debug("Fetching additional filings page: %s", url)
        try:
            body = provider._get(url)
            page: dict = json.loads(body)
        except Exception as exc:
            logger.warning("Could not fetch %s: %s", url, exc)
            continue
        yield from iter_filings_rows(page)


# ---------------------------------------------------------------------------
# ArangoDB helpers
# ---------------------------------------------------------------------------


def load_org_ciks(db) -> list[tuple[str, str]]:
    """Return list of (cik, wikidata_id) for all org entries in sec_cik_map."""
    aql = f"""
    FOR doc IN {SEC_CIK_MAP}
      FILTER doc.entity_kind == "org"
      FILTER doc.wikidata_id != null
      RETURN [doc._key, doc.wikidata_id]
    """
    cursor = db.aql.execute(aql)
    return [tuple(row) for row in cursor]


def upsert_edge(db, edge_doc: dict, dry_run: bool) -> None:
    """Upsert a beneficial_owner_of edge into org_structure (overwrite: true)."""
    if dry_run:
        logger.debug(
            "DRY RUN: would upsert %s/%s (%s → %s stake=%.1f%%)",
            ORG_STRUCTURE,
            edge_doc["_key"],
            edge_doc.get("_from"),
            edge_doc.get("_to"),
            edge_doc.get("stake_pct", 0.0),
        )
        return

    aql = f"""
    INSERT @doc INTO {ORG_STRUCTURE}
      OPTIONS {{ overwrite: true }}
    """
    db.aql.execute(aql, bind_vars={"doc": edge_doc})


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------


def run(args: argparse.Namespace) -> int:
    """Execute the fetch_ownership pipeline. Returns exit code (0 = success)."""

    # --- Connect to DB -------------------------------------------------------
    client = get_arango_client()
    db = ensure_database(client)
    ensure_collections(db)

    # --- Load CIK → QID mapping from sec_cik_map ----------------------------
    logger.info("Loading org CIKs from %s …", SEC_CIK_MAP)
    cik_qid_pairs: list[tuple[str, str]] = load_org_ciks(db)
    logger.info("Found %d org CIK mappings in %s", len(cik_qid_pairs), SEC_CIK_MAP)

    if not cik_qid_pairs:
        logger.error(
            "No org CIKs found in %s. Run build_cik_map.py first.", SEC_CIK_MAP
        )
        return 1

    # Build CIK→QID index for fast subject-company lookup
    cik_to_qid: dict[str, str] = {cik: qid for cik, qid in cik_qid_pairs}

    # --- Optionally cap the list --------------------------------------------
    if args.limit:
        cik_qid_pairs = cik_qid_pairs[: args.limit]
        logger.info("Limiting to %d CIKs (--limit)", args.limit)

    # --- Load checkpoint ----------------------------------------------------
    processed_ciks: set[str] = set()
    if args.resume:
        processed_ciks = load_checkpoint()
        if processed_ciks:
            logger.info(
                "Checkpoint: %d CIKs already processed (will skip)",
                len(processed_ciks),
            )

    # --- Instantiate provider once ------------------------------------------
    provider = SecEdgarProvider()

    # --- Stats counters -----------------------------------------------------
    stats: dict[str, int] = {
        "total": len(cik_qid_pairs),
        "skipped_checkpoint": 0,
        "http_errors": 0,
        "filings_examined": 0,
        "13dg_found": 0,
        "subject_cik_not_mapped": 0,
        "parse_failures": 0,
        "stake_below_min": 0,
        "edges_upserted": 0,
        "dry_run_would_upsert": 0,
    }
    form_type_counts: dict[str, int] = {}

    # --- Per-CIK fetch loop -------------------------------------------------
    for idx, (cik, wikidata_id) in enumerate(cik_qid_pairs, start=1):
        if args.resume and cik in processed_ciks:
            stats["skipped_checkpoint"] += 1
            logger.debug("CIK %s (%s): skipping (checkpoint)", cik, wikidata_id)
            continue

        logger.info(
            "[%d/%d] Processing SC 13D/G for CIK %s (org=%s) …",
            idx, len(cik_qid_pairs), cik, wikidata_id,
        )

        # --- Optionally bust the submissions JSON cache ----------------------
        if args.refresh:
            import hashlib as _hl
            url = f"https://data.sec.gov/submissions/CIK{str(cik).zfill(10)}.json"
            cache_key = _hl.sha256(url.encode()).hexdigest()
            cache_entry = Path(settings.sec_data_dir) / "cache" / cache_key
            if cache_entry.exists():
                cache_entry.unlink()
                logger.debug("Cleared submissions cache for CIK %s", cik)

        # --- Fetch submissions JSON -----------------------------------------
        try:
            submissions = provider.fetch_submissions_for_cik(cik)
        except Exception as exc:
            logger.warning("CIK %s (%s): HTTP/IO error — %s", cik, wikidata_id, exc)
            stats["http_errors"] += 1
            continue

        now_iso = datetime.now(timezone.utc).isoformat()

        # --- Iterate filings.recent + filings.files[] -----------------------
        filings_recent = submissions.get("filings", {}).get("recent", {})
        recent_rows = list(iter_filings_rows(filings_recent))
        extra_rows = list(iter_extra_filings(submissions, provider))
        all_rows = recent_rows + extra_rows

        stats["filings_examined"] += len(all_rows)

        cik_edges = 0

        for row in all_rows:
            form = row.get("form", "")

            # Filter to SC 13D/G forms only
            classified = classify_13_form(form)
            if classified is None:
                continue

            canonical_form, is_amendment = classified
            stats["13dg_found"] += 1

            accession = row.get("accessionNumber", "")
            if not accession:
                stats["parse_failures"] += 1
                logger.debug("CIK %s: missing accessionNumber in row — skipping", cik)
                continue

            accession_nodash = accession.replace("-", "")
            primary_doc = row.get("primaryDocument", "")
            filing_date = row.get("filingDate", "")

            if not primary_doc:
                stats["parse_failures"] += 1
                logger.debug(
                    "CIK %s: no primaryDocument for %s — skipping", cik, accession
                )
                continue

            filing_url = build_filing_url(cik, accession, primary_doc)

            # --- Fetch the primary document ---------------------------------
            try:
                doc_bytes = provider._get(filing_url)
            except Exception as exc:
                logger.warning(
                    "CIK %s: failed to fetch primary doc %s — %s",
                    cik, filing_url, exc,
                )
                stats["parse_failures"] += 1
                continue

            # Decode — EDGAR files are typically UTF-8 or latin-1
            try:
                doc_text = doc_bytes.decode("utf-8")
            except UnicodeDecodeError:
                try:
                    doc_text = doc_bytes.decode("latin-1")
                except UnicodeDecodeError:
                    logger.debug("CIK %s: cannot decode doc %s — skipping", cik, filing_url)
                    stats["parse_failures"] += 1
                    continue

            # Determine content type hint (xml vs html) from filename
            lower_doc = primary_doc.lower()
            if lower_doc.endswith(".xml"):
                content_type = "text/xml"
            else:
                content_type = "text/html"

            # --- Parse stake_pct -------------------------------------------
            stake_pct = parse_stake_pct(doc_text, content_type)
            if stake_pct is None:
                logger.debug(
                    "CIK %s: could not parse stake_pct from %s — skipping",
                    cik, filing_url,
                )
                stats["parse_failures"] += 1
                continue

            if stake_pct < _MIN_STAKE_PCT:
                logger.debug(
                    "CIK %s: stake %.2f%% < %.1f%% minimum — skipping",
                    cik, stake_pct, _MIN_STAKE_PCT,
                )
                stats["stake_below_min"] += 1
                continue

            # --- Parse subject CIK -----------------------------------------
            subject_cik = parse_subject_cik(doc_text)
            if subject_cik is None:
                logger.debug(
                    "CIK %s: could not parse subject CIK from %s — skipping",
                    cik, filing_url,
                )
                stats["parse_failures"] += 1
                continue

            # --- Look up subject CIK in our mapping -------------------------
            subject_qid = cik_to_qid.get(subject_cik)
            if subject_qid is None:
                logger.debug(
                    "CIK %s: subject CIK %s not in sec_cik_map — skipping",
                    cik, subject_cik,
                )
                stats["subject_cik_not_mapped"] += 1
                continue

            # Skip self-referential edges (shouldn't happen in practice)
            if subject_qid == wikidata_id:
                logger.debug(
                    "CIK %s: subject == filer (%s) — skipping self-edge",
                    cik, wikidata_id,
                )
                stats["parse_failures"] += 1
                continue

            # --- Build edge document ----------------------------------------
            edge_key = build_edge_key(accession_nodash, wikidata_id, subject_qid)
            edge_doc: dict[str, Any] = {
                "_key": edge_key,
                "_from": f"{ORGANIZATIONS}/{wikidata_id}",
                "_to": f"{ORGANIZATIONS}/{subject_qid}",
                "type": "beneficial_owner_of",
                "stake_pct": stake_pct,
                "filing_date": filing_date,
                "form_type": canonical_form,
                "is_amendment": is_amendment,
                "accession": accession,
                "filing_url": filing_url,
                "source": "sec_13dg",
                "last_synced": now_iso,
            }

            # --- Upsert (or dry-run log) -------------------------------------
            if args.dry_run:
                stats["dry_run_would_upsert"] += 1
                logger.debug(
                    "DRY RUN: %s/%s form=%s date=%s stake=%.1f%% %s→%s",
                    ORG_STRUCTURE, edge_key,
                    canonical_form, filing_date, stake_pct,
                    wikidata_id, subject_qid,
                )
            else:
                try:
                    upsert_edge(db, edge_doc, dry_run=False)
                    stats["edges_upserted"] += 1
                    cik_edges += 1
                except Exception as exc:
                    logger.warning(
                        "CIK %s: failed to upsert edge %s — %s",
                        cik, edge_key, exc,
                    )

            # Track per-form-type stats (regardless of dry_run)
            form_type_counts[canonical_form] = (
                form_type_counts.get(canonical_form, 0) + 1
            )

        logger.info(
            "CIK %s: %d filings examined, %d 13D/G found, %d edges written",
            cik, len(all_rows), stats["13dg_found"], cik_edges,
        )

        # --- Mark as processed and save checkpoint --------------------------
        processed_ciks.add(cik)
        if not args.dry_run:
            save_checkpoint(processed_ciks)

    # --- Summary ------------------------------------------------------------
    print("")
    print("=" * 60)
    print("  fetch_ownership SUMMARY")
    print("=" * 60)
    print(f"  Total CIKs in mapping        : {stats['total']:>8,}")
    print(f"  Skipped (checkpoint)         : {stats['skipped_checkpoint']:>8,}")
    print(f"  HTTP/IO errors               : {stats['http_errors']:>8,}")
    print(f"  Filings examined             : {stats['filings_examined']:>8,}")
    print(f"  SC 13D/G filings found       : {stats['13dg_found']:>8,}")
    print(f"  Parse failures               : {stats['parse_failures']:>8,}")
    print(f"  Stake < {_MIN_STAKE_PCT:.0f}% (skipped)       : {stats['stake_below_min']:>8,}")
    print(f"  Subject CIK not mapped       : {stats['subject_cik_not_mapped']:>8,}")
    if args.dry_run:
        print(f"  Would upsert (dry run)       : {stats['dry_run_would_upsert']:>8,}")
        print("  MODE: DRY RUN (no DB writes)")
    else:
        print(f"  Edges upserted               : {stats['edges_upserted']:>8,}")
    print("")
    print("  Form-type breakdown (SC 13D/G found):")
    for ft, count in sorted(form_type_counts.items(), key=lambda x: -x[1]):
        print(f"    {ft:<36}: {count:>5,}")
    print("=" * 60)

    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Fetch SC 13D/G beneficial ownership filings from SEC EDGAR and "
            "persist beneficial_owner_of edges into org_structure. §10.5.b."
        )
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        metavar="N",
        help="Process only the first N CIKs (0 = no limit).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse filings but skip all DB writes.",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Set log level to DEBUG.",
    )
    p.add_argument(
        "--refresh",
        action="store_true",
        help="Ignore on-disk submissions cache; re-fetch from SEC EDGAR.",
    )
    resume_group = p.add_mutually_exclusive_group()
    resume_group.add_argument(
        "--resume",
        dest="resume",
        action="store_true",
        default=True,
        help="Skip already-processed CIKs via checkpoint (default ON).",
    )
    resume_group.add_argument(
        "--no-resume",
        dest="resume",
        action="store_false",
        help="Disable checkpoint; process all CIKs.",
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
