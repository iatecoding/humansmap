"""Re-resolve label-derived fields on existing persons / organizations from a
local Wikidata dump, preferring English labels over Italian.

Why this exists
---------------
The original pass-2 loader resolved referenced QIDs to labels with Italian as
the primary and English as the fallback. That turned out to produce confusing
UI copy for many entities (awards, cause-of-death, notable works, etc.). The
label-resolution helper has since been flipped to EN-first in load_from_dump.py,
but the DB still holds values from the original IT-first pass.

This script reads the local dump (truthy NT by default), and for every person
and organization already in the DB re-resolves just the label-derived fields,
then issues a **merge update** (never a replace) so SEC-integrated fields,
edges, last_updated, and anything else the script doesn't touch are left
untouched.

It is idempotent and safe to re-run. It never writes edges, never deletes
docs, and never upserts new docs.

Usage (inside backend container)
--------------------------------
  # dry-run on a single QID, no DB writes
  docker compose exec -T backend python /app/scripts/refresh_en_labels_from_dump.py \
      --qid Q1744 --dry-run

  # spot-test against 100 random persons
  docker compose exec -T backend python /app/scripts/refresh_en_labels_from_dump.py \
      --collections persons --limit 100 --dry-run

  # full refresh (persons + orgs)
  docker compose exec -T backend python /app/scripts/refresh_en_labels_from_dump.py

Flags
-----
  --dump PATH         dump file (default: /data/wikidump/latest-truthy.nt.bz2)
  --collections C,..  subset of {persons, organizations} to refresh
  --qid Qxxxx         restrict to a single entity (repeatable via comma list)
  --limit N           only refresh the first N entities (after qid filter)
  --fields F,..       subset of refreshable fields; default = all
  --dry-run           log changes but don't write to DB
  --checkpoint PATH   JSON file where pass-A/pass-B output is cached; reusing
                      the same file skips completed passes on re-run

Refreshed fields
----------------
Persons:
  cause_of_death, awards_count, awards_top5, notable_works, field_of_work,
  religion, genres, instruments, academic_degree, noble_title, manner_of_death,
  sport, native_language, languages_spoken, conflicts, given_name, family_name,
  place_of_origin, residence, burial_place, work_location
Orgs:
  headquarters, country, industries, products, legal_form, stock_exchange,
  formation_place, location

The script never touches SEC-sourced fields (filings, directors, ...), edges,
images, occupations, nationalities, gender, dates, Wikipedia URLs, or
aliases.
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from collections import defaultdict

# Fast JSON parser if available — orjson is 2–3x faster on entity lines.
try:
    import orjson
    def _json_loads(s):
        if isinstance(s, str):
            return orjson.loads(s.encode("utf-8"))
        return orjson.loads(s)
except ImportError:
    orjson = None
    _json_loads = json.loads

# Wikidata JSON-dump lines start with `{"type":"item","id":"Qxxxxx",...` so we
# can extract the QID without parsing the full JSON. Saves us from calling
# json.loads on the ~99% of entities we don't care about.
_QID_RE = re.compile(rb'"id"\s*:\s*"(Q\d+)"')


def _fast_extract_qid(raw_bytes_or_str):
    """Extract the entity QID from a raw dump line (bytes or str) without
    parsing the JSON. Returns None if the line doesn't look like an item."""
    if isinstance(raw_bytes_or_str, str):
        raw = raw_bytes_or_str.encode("utf-8", errors="replace")
    else:
        raw = raw_bytes_or_str
    m = _QID_RE.search(raw)
    return m.group(1).decode("ascii") if m else None

sys.path.insert(0, "/app")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.database import init_db, PERSONS, ORGANIZATIONS
from app.config import settings

# Reuse dump parsing + property maps + doc-builder helpers from the loader so we
# stay bug-for-bug consistent with how the original load resolved these fields.
from scripts.load_from_dump import (  # noqa: E402
    open_dump_stream,
    parse_nt_line,
    extract_qid,
    parse_json_entity,
    iter_json_entities,
    detect_input_format,
    WD_ENTITY,
    WDT_DIRECT,
    RDFS_LABEL,
    LABEL_REF_PROPERTIES,
    PERSON_QID_LABEL_PROPS,
    PERSON_PLACE_LABEL_PROPS,
    ORG_QID_LABEL_PROPS,
    _resolve_label,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("refresh_labels")


# Person fields we refresh. Keys are the doc field names; values are
# (prop_id, kind). kind is "single" / "multi" for generic QID-label fields,
# or a tagged tuple for the special-cased awards/COD/notable-works trio that
# the original builder gates on rich_fields_min_statements.
PERSON_LABEL_FIELDS: dict[str, tuple[str, str]] = {
    field: (prop_id, kind)
    for prop_id, (field, kind) in PERSON_QID_LABEL_PROPS.items()
}

# Place-ish fields (residence, burial_place, work_location) — stored with a
# companion `<field>_id` that we also update.
PERSON_PLACE_FIELDS: dict[str, str] = {
    field: prop_id for prop_id, field in PERSON_PLACE_LABEL_PROPS.items()
}

# Rich-field trio (gated on direct_count ≥ rich_fields_min_statements in the
# original builder). We don't know direct_count here because we're not
# rebuilding the whole entity — but since these fields are already populated
# in the DB, whoever was above threshold then is still above threshold now,
# so we re-resolve if-and-only-if the field currently has a value.
RICH_FIELDS = {"cause_of_death", "awards_top5", "awards_count", "notable_works"}

ORG_LABEL_FIELDS: dict[str, tuple[str, str]] = {
    field: (prop_id, kind)
    for prop_id, (field, kind) in ORG_QID_LABEL_PROPS.items()
}
# Org single-QID fields that aren't in ORG_QID_LABEL_PROPS: headquarters (P159),
# country (P17). These have companion `_id` fields in the doc.
ORG_PLACE_FIELDS: dict[str, str] = {
    "headquarters": "P159",
    "country": "P17",
}

# Union of properties we need values for on DB subjects, for either direct
# field re-resolution OR award ranking. Pass-A captures these.
REFRESH_PROPS_PERSON = (
    {"P509", "P166", "P800"}
    | set(PERSON_QID_LABEL_PROPS.keys())
    | set(PERSON_PLACE_LABEL_PROPS.keys())
)
REFRESH_PROPS_ORG = (
    {"P159", "P17"}
    | set(ORG_QID_LABEL_PROPS.keys())
)
ALL_REFRESH_PROPS = REFRESH_PROPS_PERSON | REFRESH_PROPS_ORG


# ── Pass A: collect prop values for DB subjects ──────────────────────────────

def pass_a(dump_path: str, db_qids: set[str], fmt: str) -> tuple[dict, set[str], set[str]]:
    if fmt == "json":
        return _pass_a_json(dump_path, db_qids)
    return _pass_a_nt(dump_path, db_qids)


def _pass_a_json(dump_path: str, db_qids: set[str]) -> tuple[dict, set[str], set[str]]:
    """JSON-dump pass A. Each line is one complete entity.

    Fast-path: we extract the QID via regex on the raw line and skip the full
    json.loads() for the ~99% of entities not in db_qids. This drops per-line
    cost from ~150µs (full JSON parse of a big entity) to ~1µs (regex match).
    """
    subj_props: dict[str, dict[str, list[str]]] = {}
    needed_refs: set[str] = set()
    award_refs: set[str] = set()

    t0 = time.time()
    t_last = t0
    seen_last = 0
    seen = 0
    matched_parsed = 0
    log.info("pass-A(json): starting stream of %s (target DB QIDs: %d) "
             "[orjson=%s]", dump_path, len(db_qids), bool(orjson))
    stream = open_dump_stream(dump_path)
    for line in stream:
        seen += 1
        if seen % 500_000 == 0:
            now = time.time()
            rate_recent = (seen - seen_last) / max(now - t_last, 1e-6)
            rate_avg = seen / max(now - t0, 1e-6)
            log.info("pass-A(json): %7dk entities | %d subj matched | %d refs | "
                     "recent %.0f ent/s | avg %.0f ent/s | elapsed %.0fs",
                     seen // 1000, len(subj_props), len(needed_refs),
                     rate_recent, rate_avg, now - t0)
            t_last = now
            seen_last = seen

        qid = _fast_extract_qid(line)
        if qid is None or qid not in db_qids:
            continue

        # Strip trailing comma so orjson/json.loads is happy.
        s = line.strip()
        if s.endswith(","):
            s = s[:-1]
        if not s or s in ("[", "]"):
            continue
        try:
            obj = _json_loads(s)
        except Exception as e:
            log.warning("pass-A: skip malformed line for %s (%s)", qid, e)
            continue

        _, entity = parse_json_entity(obj)
        if entity is None:
            continue
        matched_parsed += 1

        props_out: dict[str, list[str]] = {}
        for prop_id, values in entity["props"].items():
            if prop_id not in ALL_REFRESH_PROPS:
                continue
            props_out[prop_id] = list(values)
            for v in values:
                if isinstance(v, str) and v.startswith("Q"):
                    needed_refs.add(v)
                    if prop_id == "P166":
                        award_refs.add(v)
        subj_props[qid] = props_out

    log.info("pass-A(json) done: %d entities scanned (%d fully parsed), "
             "%d subjects matched, %d refs (%d awards), %.0fs (%.0f ent/s avg)",
             seen, matched_parsed, len(subj_props), len(needed_refs), len(award_refs),
             time.time() - t0, seen / max(time.time() - t0, 1e-6))
    return subj_props, needed_refs, award_refs


def _pass_a_nt(dump_path: str, db_qids: set[str]) -> tuple[dict, set[str], set[str]]:
    """Stream the dump; for every triple whose subject is in db_qids and whose
    predicate is a refresh-relevant WDT property, capture the value.

    Returns:
      subj_props:        qid → {prop_id → [value, ...]} (values are QIDs or
                         dateTime/string literals for the few non-QID refresh props)
      needed_refs:       every QID value we'll need to resolve to a label
      award_refs:        subset of needed_refs that were P166 values; pass-B
                         needs per-award direct_count for ranking
    """
    subj_props: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    needed_refs: set[str] = set()
    award_refs: set[str] = set()

    t0 = time.time()
    lines = 0
    hits = 0
    stream = open_dump_stream(dump_path)
    for line in stream:
        lines += 1
        if lines % 50_000_000 == 0:
            log.info("pass-A: %dM lines, %d hits, %.0fs", lines // 1_000_000, hits, time.time() - t0)

        parsed = parse_nt_line(line)
        if parsed is None:
            continue
        subject, predicate, obj, _meta = parsed

        if not predicate.startswith(WDT_DIRECT):
            continue
        prop_id = predicate[len(WDT_DIRECT):]
        if prop_id not in ALL_REFRESH_PROPS:
            continue

        subj_qid = extract_qid(subject)
        if subj_qid is None or subj_qid not in db_qids:
            continue

        ref_qid = extract_qid(obj) if obj.startswith(WD_ENTITY) or obj.startswith("http") else None
        if ref_qid is None and isinstance(obj, str) and obj.startswith("Q"):
            ref_qid = obj if obj[1:].isdigit() else None

        value = ref_qid if ref_qid else obj
        subj_props[subj_qid][prop_id].append(value)
        hits += 1

        if ref_qid:
            needed_refs.add(ref_qid)
            if prop_id == "P166":
                award_refs.add(ref_qid)

    log.info("pass-A done: %d lines, %d hits, %d subjects, %d refs (%d awards), %.0fs",
             lines, hits, len(subj_props), len(needed_refs), len(award_refs), time.time() - t0)
    return dict(subj_props), needed_refs, award_refs


# ── Pass B: labels for needed_refs + direct-count for award_refs ─────────────

def pass_b(dump_path: str, needed_refs: set[str], award_refs: set[str], fmt: str) -> tuple[dict, dict]:
    if fmt == "json":
        return _pass_b_json(dump_path, needed_refs, award_refs)
    return _pass_b_nt(dump_path, needed_refs, award_refs)


def _pass_b_json(dump_path: str, needed_refs: set[str], award_refs: set[str]) -> tuple[dict, dict]:
    """JSON-dump pass B. Read labels straight from the entity object, and
    use the claim-list-length sum as the award-ranking proxy (equivalent to
    direct_count in the NT path)."""
    entity_labels: dict[str, dict[str, str]] = {}
    award_counts: dict[str, int] = {}

    t0 = time.time()
    t_last = t0
    seen_last = 0
    seen = 0
    log.info("pass-B(json): starting stream of %s (target refs: %d labels, %d award-counts) "
             "[orjson=%s]", dump_path, len(needed_refs), len(award_refs), bool(orjson))
    interesting = needed_refs | award_refs
    stream = open_dump_stream(dump_path)
    for line in stream:
        seen += 1
        if seen % 500_000 == 0:
            now = time.time()
            rate_recent = (seen - seen_last) / max(now - t_last, 1e-6)
            rate_avg = seen / max(now - t0, 1e-6)
            pct_labels = 100.0 * len(entity_labels) / max(len(needed_refs), 1)
            log.info("pass-B(json): %7dk entities | labels %d/%d (%.1f%%) | "
                     "award-counts %d/%d | recent %.0f ent/s | avg %.0f ent/s | "
                     "elapsed %.0fs",
                     seen // 1000, len(entity_labels), len(needed_refs), pct_labels,
                     len(award_counts), len(award_refs), rate_recent, rate_avg, now - t0)
            t_last = now
            seen_last = seen

        qid = _fast_extract_qid(line)
        if qid is None or qid not in interesting:
            continue

        s = line.strip()
        if s.endswith(","):
            s = s[:-1]
        if not s or s in ("[", "]"):
            continue
        try:
            obj = _json_loads(s)
        except Exception as e:
            log.warning("pass-B: skip malformed line for %s (%s)", qid, e)
            continue

        if qid in needed_refs:
            labels = obj.get("labels") or {}
            slot: dict[str, str] = {}
            for lang in ("en", "it"):
                blk = labels.get(lang)
                if isinstance(blk, dict):
                    v = blk.get("value")
                    if isinstance(v, str) and v:
                        slot[lang] = v
            if slot:
                entity_labels[qid] = slot

        if qid in award_refs:
            claims = obj.get("claims") or {}
            total = 0
            for claim_list in claims.values():
                if isinstance(claim_list, list):
                    total += len(claim_list)
            award_counts[qid] = total

    log.info("pass-B(json) done: %d entities scanned, %d labels for %d refs, "
             "%d award stmt-counts, %.0fs",
             seen, sum(len(v) for v in entity_labels.values()),
             len(entity_labels), len(award_counts), time.time() - t0)
    return entity_labels, award_counts


def _pass_b_nt(dump_path: str, needed_refs: set[str], award_refs: set[str]) -> tuple[dict, dict]:
    """Stream the dump again.

    For every subject QID in needed_refs: capture EN and IT rdfs:label values.
    For every subject QID in award_refs: count wdt: direct-statement triples
    (used as the award-prestige proxy for ranking awards_top5).
    """
    entity_labels: dict[str, dict[str, str]] = {}
    award_counts: dict[str, int] = defaultdict(int)

    t0 = time.time()
    lines = 0
    label_hits = 0
    count_hits = 0
    stream = open_dump_stream(dump_path)
    for line in stream:
        lines += 1
        if lines % 50_000_000 == 0:
            log.info("pass-B: %dM lines, %d labels, %d award-stmts, %.0fs",
                     lines // 1_000_000, label_hits, count_hits, time.time() - t0)

        parsed = parse_nt_line(line)
        if parsed is None:
            continue
        subject, predicate, obj, meta = parsed

        subj_qid = extract_qid(subject)
        if subj_qid is None:
            continue

        if predicate == RDFS_LABEL and meta in ("en", "it") and subj_qid in needed_refs:
            slot = entity_labels.setdefault(subj_qid, {})
            # First occurrence wins — the truthy dump shouldn't emit duplicates,
            # but belt-and-braces.
            slot.setdefault(meta, obj)
            label_hits += 1
        elif predicate.startswith(WDT_DIRECT) and subj_qid in award_refs:
            award_counts[subj_qid] += 1
            count_hits += 1

    log.info("pass-B done: %d lines, %d labels for %d refs, %d award-stmts, %.0fs",
             lines, label_hits, len(entity_labels), count_hits, time.time() - t0)
    return entity_labels, dict(award_counts)


# ── Doc updater ──────────────────────────────────────────────────────────────

def _resolve_multi(qids: list[str], entity_labels: dict, cap: int = 10) -> list[str]:
    out, seen = [], set()
    for q in qids:
        if not (isinstance(q, str) and q.startswith("Q")):
            continue
        lbl = _resolve_label(q, entity_labels)
        if lbl and lbl not in seen:
            seen.add(lbl)
            out.append(lbl)
        if len(out) >= cap:
            break
    return out


def _resolve_single(qids: list[str], entity_labels: dict) -> str | None:
    for q in qids:
        if isinstance(q, str) and q.startswith("Q"):
            lbl = _resolve_label(q, entity_labels)
            if lbl:
                return lbl
    return None


def build_person_update(
    qid: str,
    existing: dict,
    props: dict[str, list[str]],
    entity_labels: dict,
    award_counts: dict,
    selected_fields: set[str] | None,
) -> dict:
    """Compute the merge patch for a person doc. Only includes fields whose
    resolved value differs from the current stored value; this keeps the DB
    traffic minimal and `last_updated` stable when nothing changed.
    """
    patch: dict = {}

    def maybe_set(field: str, new_val):
        if selected_fields is not None and field not in selected_fields:
            return
        old = existing.get(field)
        if new_val != old:
            patch[field] = new_val

    # Generic QID-label fields (field_of_work, religion, genres, ...)
    for field, (prop_id, kind) in PERSON_LABEL_FIELDS.items():
        qids = props.get(prop_id, [])
        if kind == "single":
            maybe_set(field, _resolve_single(qids, entity_labels))
        else:
            new = _resolve_multi(qids, entity_labels, cap=10)
            # Match the builder's shape: empty list is fine (mirrors builder).
            maybe_set(field, new)

    # Place-like fields: residence, burial_place, work_location — plus their _id
    for field, prop_id in PERSON_PLACE_FIELDS.items():
        qids = [q for q in props.get(prop_id, []) if isinstance(q, str) and q.startswith("Q")]
        if not qids:
            # Don't clobber existing _id if we have no evidence from the dump
            # (keeps the field null if it was null, leaves it alone otherwise).
            continue
        q = qids[0]
        lbl = _resolve_label(q, entity_labels)
        maybe_set(field, lbl)
        maybe_set(f"{field}_id", q)

    # Rich trio — only if the field is currently populated (proxy for having
    # crossed the rich-fields threshold during the original build).
    if existing.get("cause_of_death") is not None or any(
        existing.get(k) for k in ("awards_top5", "awards_count", "notable_works")
    ):
        # cause_of_death
        cod_qids = [q for q in props.get("P509", []) if isinstance(q, str) and q.startswith("Q")]
        maybe_set("cause_of_death", _resolve_single(cod_qids, entity_labels))

        # awards: recompute count + top-5 ordered by award direct_count
        award_qids = [q for q in props.get("P166", []) if isinstance(q, str) and q.startswith("Q")]
        if award_qids:
            maybe_set("awards_count", len(award_qids))
            ranked = sorted(award_qids, key=lambda q: (-award_counts.get(q, 0), q))
            top5: list[str] = []
            for aq in ranked:
                lbl = _resolve_label(aq, entity_labels)
                if lbl:
                    top5.append(lbl)
                if len(top5) >= 5:
                    break
            maybe_set("awards_top5", top5)
        else:
            maybe_set("awards_count", None)
            maybe_set("awards_top5", [])

        # notable_works (cap 10)
        works = [q for q in props.get("P800", []) if isinstance(q, str) and q.startswith("Q")]
        maybe_set("notable_works", _resolve_multi(works, entity_labels, cap=10))

    return patch


def build_org_update(
    qid: str,
    existing: dict,
    props: dict[str, list[str]],
    entity_labels: dict,
    selected_fields: set[str] | None,
) -> dict:
    patch: dict = {}

    def maybe_set(field: str, new_val):
        if selected_fields is not None and field not in selected_fields:
            return
        old = existing.get(field)
        if new_val != old:
            patch[field] = new_val

    for field, (prop_id, kind) in ORG_LABEL_FIELDS.items():
        qids = props.get(prop_id, [])
        if kind == "single":
            maybe_set(field, _resolve_single(qids, entity_labels))
        else:
            maybe_set(field, _resolve_multi(qids, entity_labels, cap=10))

    for field, prop_id in ORG_PLACE_FIELDS.items():
        qids = [q for q in props.get(prop_id, []) if isinstance(q, str) and q.startswith("Q")]
        if not qids:
            continue
        q = qids[0]
        maybe_set(field, _resolve_label(q, entity_labels))
        maybe_set(f"{field}_id", q)

    return patch


# ── Main ─────────────────────────────────────────────────────────────────────

def load_db_qids(db, collection_name: str, qid_filter: set[str] | None, limit: int | None) -> list[str]:
    """Return sorted list of QIDs present in the given collection, optionally
    intersected with qid_filter and truncated to `limit`.
    """
    t0 = time.time()
    log.info("loading QIDs from %s...", collection_name)
    aql = f"FOR d IN {collection_name} RETURN d._key"
    cursor = db.aql.execute(aql, batch_size=10_000)
    qids = []
    for q in cursor:
        if qid_filter is not None and q not in qid_filter:
            continue
        qids.append(q)
        if limit and len(qids) >= limit:
            break
        if len(qids) % 100_000 == 0:
            log.info("  %s: loaded %d QIDs (%.0fs)", collection_name, len(qids), time.time() - t0)
    log.info("loaded %d QIDs from %s in %.0fs", len(qids), collection_name, time.time() - t0)
    return qids


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dump", default=os.getenv("DUMP_FILE", "/data/wikidump/latest-truthy.nt.bz2"),
                    help="path to Wikidata dump inside the container "
                         "(.nt[.bz2|.gz] or .json[.gz]); auto-detected by extension")
    ap.add_argument("--format", default="auto", choices=["auto", "nt", "json"],
                    help="dump format override; default auto-detects from filename")
    ap.add_argument("--collections", default="persons,organizations",
                    help="comma-separated: persons,organizations")
    ap.add_argument("--qid", default=None, help="comma-separated QID whitelist")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--fields", default=None,
                    help="comma-separated field whitelist (default: all)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--checkpoint", default=None,
                    help="JSON cache of pass-A/pass-B results; reuses if present")
    ap.add_argument("--batch-size", type=int, default=500)
    args = ap.parse_args()

    collections = {c.strip() for c in args.collections.split(",") if c.strip()}
    if not collections <= {"persons", "organizations"}:
        ap.error("--collections must be a subset of persons,organizations")

    qid_filter = None
    if args.qid:
        qid_filter = {q.strip() for q in args.qid.split(",") if q.strip()}

    selected_fields = None
    if args.fields:
        selected_fields = {f.strip() for f in args.fields.split(",") if f.strip()}

    db = init_db()

    # ── Collect DB QIDs ────────────────────────────────────────────────────
    person_qids: list[str] = []
    org_qids: list[str] = []
    if "persons" in collections:
        person_qids = load_db_qids(db, PERSONS, qid_filter, args.limit)
        log.info("persons to scan: %d", len(person_qids))
    if "organizations" in collections:
        org_qids = load_db_qids(db, ORGANIZATIONS, qid_filter, args.limit)
        log.info("orgs to scan: %d", len(org_qids))

    db_qids: set[str] = set(person_qids) | set(org_qids)
    if not db_qids:
        log.warning("no QIDs to refresh; exiting")
        return

    # ── Pass-A / Pass-B (with optional checkpoint) ─────────────────────────
    ckpt = None
    if args.checkpoint and os.path.exists(args.checkpoint):
        log.info("loading checkpoint %s", args.checkpoint)
        with open(args.checkpoint, "r") as f:
            ckpt = json.load(f)

    if ckpt:
        subj_props = {k: dict(v) for k, v in ckpt["subj_props"].items()}
        needed_refs = set(ckpt["needed_refs"])
        award_refs = set(ckpt["award_refs"])
        entity_labels = ckpt["entity_labels"]
        award_counts = ckpt["award_counts"]
        log.info("checkpoint loaded: %d subj_props, %d needed_refs, %d entity_labels",
                 len(subj_props), len(needed_refs), len(entity_labels))
    else:
        fmt = detect_input_format(args.dump, args.format)
        log.info("dump format: %s (%s)", fmt, args.dump)
        subj_props, needed_refs, award_refs = pass_a(args.dump, db_qids, fmt)
        entity_labels, award_counts = pass_b(args.dump, needed_refs, award_refs, fmt)
        if args.checkpoint:
            t0 = time.time()
            log.info("writing checkpoint %s...", args.checkpoint)
            try:
                with open(args.checkpoint, "w") as f:
                    json.dump({
                        "subj_props": subj_props,
                        "needed_refs": sorted(needed_refs),
                        "award_refs": sorted(award_refs),
                        "entity_labels": entity_labels,
                        "award_counts": award_counts,
                    }, f)
                log.info("checkpoint written (%.1f MB, %.0fs)",
                         os.path.getsize(args.checkpoint) / 1e6, time.time() - t0)
            except OSError as e:
                # Don't abort the whole run just because we can't persist the
                # cache — the DB update phase still has everything it needs
                # in memory.
                log.warning("checkpoint write failed (%s); continuing with "
                            "in-memory data, updates will still be applied", e)

    # ── Apply updates ──────────────────────────────────────────────────────
    persons = db.collection(PERSONS)
    orgs = db.collection(ORGANIZATIONS)

    stats = {"persons_scanned": 0, "persons_changed": 0,
             "orgs_scanned": 0, "orgs_changed": 0}

    def flush(coll, batch):
        if not batch:
            return
        if args.dry_run:
            for p in batch:
                log.info("[DRY] %s.update %s -> %s", coll.name, p["_key"],
                         {k: v for k, v in p.items() if k != "_key"})
            return
        coll.update_many(batch, merge=True, keep_none=True, silent=True)

    if person_qids:
        log.info("applying patches to %d persons (batch_size=%d)...", len(person_qids), args.batch_size)
        t0 = time.time()
        t_last = t0
        batch = []
        for i, qid in enumerate(person_qids, 1):
            existing = persons.get(qid)
            if existing is None:
                continue
            stats["persons_scanned"] += 1
            props = subj_props.get(qid, {})
            patch = build_person_update(
                qid, existing, props, entity_labels, award_counts, selected_fields
            )
            if patch:
                patch["_key"] = qid
                batch.append(patch)
                stats["persons_changed"] += 1
            if len(batch) >= args.batch_size:
                flush(persons, batch)
                batch = []
            if i % 10_000 == 0:
                now = time.time()
                log.info("  persons: %d/%d scanned, %d changed, %.0f docs/s (recent)",
                         i, len(person_qids), stats["persons_changed"],
                         10_000 / max(now - t_last, 1e-6))
                t_last = now
        flush(persons, batch)
        log.info("persons done: %d scanned, %d changed, %.0fs",
                 stats["persons_scanned"], stats["persons_changed"], time.time() - t0)

    if org_qids:
        log.info("applying patches to %d orgs (batch_size=%d)...", len(org_qids), args.batch_size)
        t0 = time.time()
        batch = []
        for qid in org_qids:
            existing = orgs.get(qid)
            if existing is None:
                continue
            stats["orgs_scanned"] += 1
            props = subj_props.get(qid, {})
            patch = build_org_update(qid, existing, props, entity_labels, selected_fields)
            if patch:
                patch["_key"] = qid
                batch.append(patch)
                stats["orgs_changed"] += 1
            if len(batch) >= args.batch_size:
                flush(orgs, batch)
                batch = []
        flush(orgs, batch)
        log.info("orgs done: %d scanned, %d changed, %.0fs",
                 stats["orgs_scanned"], stats["orgs_changed"], time.time() - t0)

    log.info("done: %s%s", stats, " (DRY-RUN)" if args.dry_run else "")


if __name__ == "__main__":
    main()
