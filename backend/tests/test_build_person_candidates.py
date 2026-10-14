"""Unit tests for backend/scripts/sec/build_person_candidates.py helper functions.

Covers pure logic only — no network or DB access required.

Run inside the Docker container:
    docker compose exec backend pytest tests/test_build_person_candidates.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow import without the app on PYTHONPATH
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.sec.build_person_candidates import (
    batch_hash,
    batch_list,
    build_candidate_docs,
    collapse_source,
    parse_sparql_rows,
    uri_to_qid,
)

WD = "http://www.wikidata.org/entity/"


# ---------------------------------------------------------------------------
# Helpers for building fake SPARQL rows
# ---------------------------------------------------------------------------


def _row(
    person_qid: str,
    org_qid: str,
    source: str,
    label: str = "",
    alt_label: str = "",
) -> dict:
    row: dict = {
        "person": {"value": f"{WD}{person_qid}"},
        "org": {"value": f"{WD}{org_qid}"},
        "source": {"value": source},
        "personLabel": {"value": label or person_qid},
    }
    if alt_label:
        row["personAltLabel"] = {"value": alt_label}
    return row


# ---------------------------------------------------------------------------
# batch_list
# ---------------------------------------------------------------------------


def test_batch_list_even_split():
    result = batch_list([1, 2, 3, 4], 2)
    assert result == [[1, 2], [3, 4]]


def test_batch_list_uneven_split():
    result = batch_list([1, 2, 3, 4, 5], 2)
    assert result == [[1, 2], [3, 4], [5]]


def test_batch_list_empty():
    assert batch_list([], 10) == []


def test_batch_list_single_item():
    assert batch_list(["Q312"], 50) == [["Q312"]]


def test_batch_list_size_larger_than_list():
    items = ["Q1", "Q2", "Q3"]
    assert batch_list(items, 100) == [items]


def test_batch_list_size_equals_list_length():
    items = ["Q1", "Q2"]
    assert batch_list(items, 2) == [["Q1", "Q2"]]


# ---------------------------------------------------------------------------
# batch_hash — order independence
# ---------------------------------------------------------------------------


def test_batch_hash_deterministic():
    assert batch_hash(["Q312", "Q380"]) == batch_hash(["Q312", "Q380"])


def test_batch_hash_order_independent():
    """Sorting before hashing means order must not matter."""
    assert batch_hash(["Q380", "Q312"]) == batch_hash(["Q312", "Q380"])


def test_batch_hash_different_inputs_differ():
    assert batch_hash(["Q312"]) != batch_hash(["Q380"])


def test_batch_hash_length():
    """SHA-256 hex is always 64 chars."""
    assert len(batch_hash(["Q312"])) == 64


def test_batch_hash_three_qids_order_independent():
    qids = ["Q999", "Q1", "Q500"]
    import itertools

    first = batch_hash(qids)
    for perm in itertools.permutations(qids):
        assert batch_hash(list(perm)) == first


# ---------------------------------------------------------------------------
# collapse_source
# ---------------------------------------------------------------------------


def test_collapse_source_single():
    assert collapse_source({"P108"}) == "P108"


def test_collapse_source_single_p39():
    assert collapse_source({"P39_P642"}) == "P39_P642"


def test_collapse_source_both_alphabetical():
    # P108 < P39_P642 alphabetically
    assert collapse_source({"P39_P642", "P108"}) == "P108+P39_P642"


def test_collapse_source_both_regardless_of_input_order():
    assert collapse_source({"P108", "P39_P642"}) == collapse_source({"P39_P642", "P108"})


# ---------------------------------------------------------------------------
# parse_sparql_rows — additive filter
# ---------------------------------------------------------------------------


def test_parse_drops_unknown_person():
    """Persons not in known_person_qids must be dropped."""
    rows = [_row("Q999999", "Q312", "P108", "Unknown Person")]
    result = parse_sparql_rows(rows, known_person_qids={"Q312097"})  # Q999999 not in set
    assert len(result) == 0


def test_parse_keeps_known_person():
    rows = [_row("Q312097", "Q312", "P108", "Tim Cook")]
    result = parse_sparql_rows(rows, known_person_qids={"Q312097"})
    assert ("Q312", "Q312097") in result


def test_parse_label_extracted():
    rows = [_row("Q312097", "Q312", "P108", "Tim Cook")]
    result = parse_sparql_rows(rows, known_person_qids={"Q312097"})
    assert result[("Q312", "Q312097")]["person_name_en"] == "Tim Cook"


def test_parse_source_stored():
    rows = [_row("Q312097", "Q312", "P108")]
    result = parse_sparql_rows(rows, known_person_qids={"Q312097"})
    assert "P108" in result[("Q312", "Q312097")]["sources"]


def test_parse_skips_missing_person_uri():
    rows = [{"person": {"value": ""}, "org": {"value": f"{WD}Q312"}, "source": {"value": "P108"}}]
    result = parse_sparql_rows(rows, known_person_qids={"Q312097"})
    assert len(result) == 0


def test_parse_skips_missing_org_uri():
    rows = [{"person": {"value": f"{WD}Q312097"}, "org": {"value": ""}, "source": {"value": "P108"}}]
    result = parse_sparql_rows(rows, known_person_qids={"Q312097"})
    assert len(result) == 0


# ---------------------------------------------------------------------------
# parse_sparql_rows — source collapse
# ---------------------------------------------------------------------------


def test_parse_collapses_two_sources_for_same_pair():
    """Same (org, person) via P108 and P39_P642 → one entry with both sources."""
    rows = [
        _row("Q312097", "Q312", "P108", "Tim Cook"),
        _row("Q312097", "Q312", "P39_P642", "Tim Cook"),
    ]
    result = parse_sparql_rows(rows, known_person_qids={"Q312097"})
    assert len(result) == 1
    sources = result[("Q312", "Q312097")]["sources"]
    assert sources == {"P108", "P39_P642"}


def test_parse_two_persons_for_same_org():
    rows = [
        _row("Q312097", "Q312", "P108", "Tim Cook"),
        _row("Q76", "Q312", "P108", "Barack Obama"),
    ]
    result = parse_sparql_rows(rows, known_person_qids={"Q312097", "Q76"})
    assert len(result) == 2
    assert ("Q312", "Q312097") in result
    assert ("Q312", "Q76") in result


def test_parse_same_person_different_orgs():
    rows = [
        _row("Q312097", "Q312", "P108"),   # Apple
        _row("Q312097", "Q380", "P108"),   # Another org
    ]
    result = parse_sparql_rows(rows, known_person_qids={"Q312097"})
    assert len(result) == 2
    assert ("Q312", "Q312097") in result
    assert ("Q380", "Q312097") in result


# ---------------------------------------------------------------------------
# build_candidate_docs — _key, source collapse, last_synced
# ---------------------------------------------------------------------------


def test_build_candidate_docs_key_format():
    pair_data = {
        ("Q312", "Q312097"): {
            "org_qid": "Q312",
            "person_qid": "Q312097",
            "person_name_en": "Tim Cook",
            "sources": {"P108"},
        }
    }
    docs = build_candidate_docs(pair_data, "2026-04-17T00:00:00+00:00")
    assert len(docs) == 1
    assert docs[0]["_key"] == "Q312_Q312097"


def test_build_candidate_docs_source_joined():
    pair_data = {
        ("Q312", "Q312097"): {
            "org_qid": "Q312",
            "person_qid": "Q312097",
            "person_name_en": "Tim Cook",
            "sources": {"P108", "P39_P642"},
        }
    }
    docs = build_candidate_docs(pair_data, "2026-04-17T00:00:00+00:00")
    assert docs[0]["source"] == "P108+P39_P642"


def test_build_candidate_docs_last_synced():
    pair_data = {
        ("Q312", "Q312097"): {
            "org_qid": "Q312",
            "person_qid": "Q312097",
            "person_name_en": "",
            "sources": {"P108"},
        }
    }
    ts = "2026-04-17T12:00:00+00:00"
    docs = build_candidate_docs(pair_data, ts)
    assert docs[0]["last_synced"] == ts


def test_build_candidate_docs_empty_sources():
    pair_data = {
        ("Q312", "Q312097"): {
            "org_qid": "Q312",
            "person_qid": "Q312097",
            "person_name_en": "Tim Cook",
            "sources": set(),
        }
    }
    docs = build_candidate_docs(pair_data, "2026-04-17T00:00:00+00:00")
    assert docs[0]["source"] == ""


# ---------------------------------------------------------------------------
# uri_to_qid (also used in this module, confirm it works)
# ---------------------------------------------------------------------------


def test_uri_to_qid_full_uri():
    assert uri_to_qid(f"{WD}Q312") == "Q312"


def test_uri_to_qid_already_qid():
    assert uri_to_qid("Q312") == "Q312"


def test_uri_to_qid_none():
    assert uri_to_qid(None) is None


def test_uri_to_qid_empty():
    assert uri_to_qid("") is None


# ---------------------------------------------------------------------------
# parse_sparql_rows — new source properties (P488, P3320, P169, P1037)
# ---------------------------------------------------------------------------


def test_parse_p488_chairperson():
    """P488 (chairperson) row must be captured with correct source."""
    rows = [_row("Q100", "Q312", "P488", "Alice Chair")]
    result = parse_sparql_rows(rows, known_person_qids={"Q100"})
    assert ("Q312", "Q100") in result
    assert "P488" in result[("Q312", "Q100")]["sources"]


def test_parse_p3320_board_member():
    """P3320 (board member) row must be captured with correct source."""
    rows = [_row("Q200", "Q312", "P3320", "Bob Board")]
    result = parse_sparql_rows(rows, known_person_qids={"Q200"})
    assert ("Q312", "Q200") in result
    assert "P3320" in result[("Q312", "Q200")]["sources"]


def test_parse_p169_ceo():
    """P169 (CEO — subject is org, object is person) must be captured."""
    rows = [_row("Q300", "Q312", "P169", "Carol CEO")]
    result = parse_sparql_rows(rows, known_person_qids={"Q300"})
    assert ("Q312", "Q300") in result
    assert "P169" in result[("Q312", "Q300")]["sources"]


def test_parse_p1037_director_manager():
    """P1037 (director / manager) row must be captured with correct source."""
    rows = [_row("Q400", "Q312", "P1037", "Dave Director")]
    result = parse_sparql_rows(rows, known_person_qids={"Q400"})
    assert ("Q312", "Q400") in result
    assert "P1037" in result[("Q312", "Q400")]["sources"]


# ---------------------------------------------------------------------------
# parse_sparql_rows — altLabel / name_variants collapsing
# ---------------------------------------------------------------------------


def test_parse_alt_label_collected():
    """A row with personAltLabel must add the variant to name_variants."""
    rows = [_row("Q76", "Q312", "P108", "Albert Gore", alt_label="Al Gore")]
    result = parse_sparql_rows(rows, known_person_qids={"Q76"})
    pair = result[("Q312", "Q76")]
    assert "Al Gore" in pair["name_variants"]


def test_parse_canonical_label_in_variants():
    """The canonical personLabel must always appear in name_variants."""
    rows = [_row("Q76", "Q312", "P108", "Albert Gore")]
    result = parse_sparql_rows(rows, known_person_qids={"Q76"})
    pair = result[("Q312", "Q76")]
    assert "Albert Gore" in pair["name_variants"]


def test_parse_two_rows_same_pair_different_alt_labels():
    """Two rows for the same (org, person) with different altLabels → both variants."""
    rows = [
        _row("Q76", "Q312", "P108", "Albert Gore", alt_label="Al Gore"),
        _row("Q76", "Q312", "P108", "Albert Gore", alt_label="Al Albert Gore"),
    ]
    result = parse_sparql_rows(rows, known_person_qids={"Q76"})
    pair = result[("Q312", "Q76")]
    assert "Al Gore" in pair["name_variants"]
    assert "Al Albert Gore" in pair["name_variants"]
    assert "Albert Gore" in pair["name_variants"]


def test_parse_case_insensitive_dedup_preserves_first_seen():
    """'Albert Gore' and 'albert gore' are the same variant — keep first-seen casing."""
    rows = [
        _row("Q76", "Q312", "P108", "Albert Gore", alt_label="Albert Gore"),
        _row("Q76", "Q312", "P108", "Albert Gore", alt_label="albert gore"),
    ]
    result = parse_sparql_rows(rows, known_person_qids={"Q76"})
    pair = result[("Q312", "Q76")]
    # Both lowercase and title-case should NOT both be in the set
    lower_count = sum(1 for v in pair["name_variants"] if v.lower() == "albert gore")
    assert lower_count == 1  # deduplicated to exactly one entry


# ---------------------------------------------------------------------------
# build_candidate_docs — name_variants emitted as sorted list
# ---------------------------------------------------------------------------


def test_build_candidate_docs_name_variants_sorted_list():
    """name_variants must be emitted as a sorted list."""
    pair_data = {
        ("Q312", "Q76"): {
            "org_qid": "Q312",
            "person_qid": "Q76",
            "person_name_en": "Albert Gore",
            "sources": {"P108"},
            "name_variants": {"Al Gore", "Albert Gore", "Al Albert Gore"},
        }
    }
    docs = build_candidate_docs(pair_data, "2026-04-17T00:00:00+00:00")
    assert len(docs) == 1
    variants = docs[0]["name_variants"]
    assert isinstance(variants, list)
    assert variants == sorted(variants)


def test_build_candidate_docs_name_variants_empty_set():
    """Empty name_variants set must produce an empty list (not absent key)."""
    pair_data = {
        ("Q312", "Q312097"): {
            "org_qid": "Q312",
            "person_qid": "Q312097",
            "person_name_en": "Tim Cook",
            "sources": {"P108"},
            "name_variants": set(),
        }
    }
    docs = build_candidate_docs(pair_data, "2026-04-17T00:00:00+00:00")
    assert "name_variants" in docs[0]
    assert docs[0]["name_variants"] == []


def test_build_candidate_docs_missing_name_variants_key():
    """Older pair dicts without name_variants must not crash — emit empty list."""
    pair_data = {
        ("Q312", "Q312097"): {
            "org_qid": "Q312",
            "person_qid": "Q312097",
            "person_name_en": "Tim Cook",
            "sources": {"P108"},
            # name_variants intentionally absent (legacy dict)
        }
    }
    docs = build_candidate_docs(pair_data, "2026-04-17T00:00:00+00:00")
    assert "name_variants" in docs[0]
    assert docs[0]["name_variants"] == []


# ---------------------------------------------------------------------------
# parse_sparql_rows — comma-joined altLabel splitting
# ---------------------------------------------------------------------------


def test_parse_comma_joined_alt_label_splits_into_separate_variants():
    """A comma-joined altLabel blob must be split into individual variants.

    Wikidata's wikibase:label SERVICE returns skos:altLabel as a single
    comma-joined string (e.g. "Reid G. Hoffman, Reid Garrett Hoffman").
    parse_sparql_rows must split on ", " so each piece becomes a usable
    matching surface.
    """
    rows = [
        _row(
            "Q517",
            "Q380",
            "P108",
            label="Reid Hoffman",
            alt_label="Reid G. Hoffman, Reid Garrett Hoffman",
        )
    ]
    result = parse_sparql_rows(rows, known_person_qids={"Q517"})
    docs = build_candidate_docs(result, "2026-04-17T00:00:00+00:00")
    assert len(docs) == 1
    variants = docs[0]["name_variants"]
    assert sorted(variants) == ["Reid G. Hoffman", "Reid Garrett Hoffman", "Reid Hoffman"]


def test_parse_comma_joined_alt_label_deduplicates_across_rows():
    """A variant piece that appears in two rows' altLabel blobs must be deduplicated.

    Two rows for the same (org, person) pair where "Reid G. Hoffman" appears in
    both altLabel strings must result in only one occurrence in name_variants.
    """
    rows = [
        _row(
            "Q517",
            "Q380",
            "P108",
            label="Reid Hoffman",
            alt_label="Reid G. Hoffman, Reid Garrett Hoffman",
        ),
        _row(
            "Q517",
            "Q380",
            "P39_P642",
            label="Reid Hoffman",
            alt_label="Reid G. Hoffman, LinkedIn founder",
        ),
    ]
    result = parse_sparql_rows(rows, known_person_qids={"Q517"})
    docs = build_candidate_docs(result, "2026-04-17T00:00:00+00:00")
    assert len(docs) == 1
    variants = docs[0]["name_variants"]
    reid_g_count = sum(1 for v in variants if v == "Reid G. Hoffman")
    assert reid_g_count == 1
