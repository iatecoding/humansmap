"""Unit tests for backend/scripts/sec/fetch_form4_bulk.py helper functions.

Covers pure logic only — no network or DB access required.

Run inside the Docker container:
    docker compose exec backend pytest tests/test_fetch_form4_bulk.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow import without the app on PYTHONPATH
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.sec.fetch_form4_bulk import (
    edge_key_insider,
    edge_key_role,
    first_initial,
    last_name,
    name_canonicals,
    normalize_name,
    parse_sec_name,
    parse_wd_name,
    parse_quarter_override,
    parse_sec_date,
    slugify_role,
    split_relationship,
    tier1_match,
)


# ---------------------------------------------------------------------------
# normalize_name
# ---------------------------------------------------------------------------


def test_normalize_name_strips_mr():
    assert normalize_name("Mr. Timothy Cook") == "TIMOTHY COOK"


def test_normalize_name_strips_dr():
    assert normalize_name("DR. Jane Doe") == "JANE DOE"


def test_normalize_name_strips_jr():
    assert normalize_name("WARREN BUFFETT JR.") == "WARREN BUFFETT"


def test_normalize_name_strips_sr():
    assert normalize_name("John Smith Sr.") == "JOHN SMITH"


def test_normalize_name_strips_iii():
    assert normalize_name("William Gates III") == "WILLIAM GATES"


def test_normalize_name_strips_middle_initial():
    assert normalize_name("Timothy D. Cook") == "TIMOTHY COOK"


def test_normalize_name_strips_middle_initial_no_period():
    # Middle initial without period — we only strip X. pattern
    result = normalize_name("Warren E. Buffett")
    assert "E" not in result.split()
    assert "WARREN" in result
    assert "BUFFETT" in result


def test_normalize_name_unicode_nfkc():
    # Fullwidth characters should be normalised
    assert normalize_name("Ｅｌｏｎ Ｍｕｓｋ") == "ELON MUSK"


def test_normalize_name_uppercase():
    assert normalize_name("elon musk") == "ELON MUSK"


def test_normalize_name_collapses_whitespace():
    assert normalize_name("  Tim   Cook  ") == "TIM COOK"


def test_normalize_name_empty():
    assert normalize_name("") == ""


def test_normalize_name_none():
    assert normalize_name(None) == ""


def test_normalize_name_only_titles():
    # All tokens are titles — result should be empty or near-empty
    result = normalize_name("Mr. Jr.")
    # After stripping, nothing useful should remain
    assert "MR" not in result
    assert "JR" not in result


# ---------------------------------------------------------------------------
# last_name / first_initial
# ---------------------------------------------------------------------------


def test_last_name_two_parts():
    assert last_name("TIMOTHY COOK") == "COOK"


def test_last_name_one_part():
    assert last_name("MUSK") == "MUSK"


def test_last_name_three_parts():
    assert last_name("JOHN JAMES SMITH") == "SMITH"


def test_last_name_empty():
    assert last_name("") == ""


def test_first_initial_normal():
    assert first_initial("TIMOTHY COOK") == "T"


def test_first_initial_empty():
    assert first_initial("") == ""


def test_first_initial_single_char():
    assert first_initial("A") == "A"


# ---------------------------------------------------------------------------
# parse_sec_date — DD-MON-YYYY format (§2.4 quirk)
# ---------------------------------------------------------------------------


def test_parse_sec_date_standard():
    assert parse_sec_date("15-JAN-2025") == "2025-01-15"


def test_parse_sec_date_lowercase():
    assert parse_sec_date("01-dec-2003") == "2003-12-01"


def test_parse_sec_date_leading_zero_day():
    assert parse_sec_date("01-FEB-2020") == "2020-02-01"


def test_parse_sec_date_december():
    assert parse_sec_date("31-DEC-1999") == "1999-12-31"


def test_parse_sec_date_bad_format_returns_none():
    assert parse_sec_date("2025-01-15") is None


def test_parse_sec_date_empty_returns_none():
    assert parse_sec_date("") is None


def test_parse_sec_date_none_returns_none():
    assert parse_sec_date(None) is None


def test_parse_sec_date_garbage():
    assert parse_sec_date("not-a-date") is None


def test_parse_sec_date_whitespace_stripped():
    assert parse_sec_date("  15-JAN-2025  ") == "2025-01-15"


# ---------------------------------------------------------------------------
# split_relationship — comma-separated RPTOWNER_RELATIONSHIP (§2.4 quirk)
# ---------------------------------------------------------------------------


def test_split_relationship_single():
    assert split_relationship("Director") == ["director"]


def test_split_relationship_two_tokens():
    assert split_relationship("Director,Officer") == ["director", "officer"]


def test_split_relationship_three_tokens():
    assert split_relationship("Director, Officer, Other") == ["director", "officer", "other"]


def test_split_relationship_ten_percent_owner():
    assert split_relationship("TenPercentOwner") == ["tenpercentowner"]


def test_split_relationship_mixed_case():
    assert split_relationship("DIRECTOR,OFFICER") == ["director", "officer"]


def test_split_relationship_empty():
    assert split_relationship("") == []


def test_split_relationship_none():
    assert split_relationship(None) == []


def test_split_relationship_spaces_around_tokens():
    result = split_relationship("  Director  ,  Officer  ")
    assert result == ["director", "officer"]


# ---------------------------------------------------------------------------
# edge_key_insider — deterministic
# ---------------------------------------------------------------------------


def test_edge_key_insider_format():
    key = edge_key_insider("Q312097", "Q312", "0001234567-23-000001", 0)
    assert key == "Q312097__Q312__0001234567-23-000001__0"


def test_edge_key_insider_deterministic():
    k1 = edge_key_insider("Q312097", "Q312", "0001234567-23-000001", 3)
    k2 = edge_key_insider("Q312097", "Q312", "0001234567-23-000001", 3)
    assert k1 == k2


def test_edge_key_insider_seq_disambiguates():
    k1 = edge_key_insider("Q312097", "Q312", "ACC", 0)
    k2 = edge_key_insider("Q312097", "Q312", "ACC", 1)
    assert k1 != k2


def test_edge_key_insider_acc_disambiguates():
    k1 = edge_key_insider("Q312097", "Q312", "ACC1", 0)
    k2 = edge_key_insider("Q312097", "Q312", "ACC2", 0)
    assert k1 != k2


# ---------------------------------------------------------------------------
# edge_key_role — deterministic, sanitises unsafe chars
# ---------------------------------------------------------------------------


def test_edge_key_role_format():
    key = edge_key_role("Q312097", "Q312", "director_of")
    assert key == "Q312097__Q312__director_of"


def test_edge_key_role_deterministic():
    k1 = edge_key_role("Q312097", "Q312", "officer_of")
    k2 = edge_key_role("Q312097", "Q312", "officer_of")
    assert k1 == k2


def test_edge_key_role_sanitises_unsafe_chars():
    # Spaces and special chars must be replaced with underscores
    key = edge_key_role("Q312097", "Q312", "chief executive officer")
    assert " " not in key
    assert key.startswith("Q312097__Q312__")


def test_edge_key_role_different_roles_differ():
    k1 = edge_key_role("Q312097", "Q312", "officer_of")
    k2 = edge_key_role("Q312097", "Q312", "director_of")
    assert k1 != k2


# ---------------------------------------------------------------------------
# slugify_role
# ---------------------------------------------------------------------------


def test_slugify_role_simple():
    assert slugify_role("Chief Executive Officer") == "chief_executive_officer"


def test_slugify_role_ampersand():
    result = slugify_role("EVP & CFO")
    assert "&" not in result
    assert " " not in result


def test_slugify_role_empty():
    assert slugify_role("") == ""


# ---------------------------------------------------------------------------
# parse_sec_name / parse_wd_name
# ---------------------------------------------------------------------------


def test_parse_sec_name_basic():
    assert parse_sec_name("COOK TIMOTHY D") == ("COOK", "TIMOTHY")


def test_parse_sec_name_with_comma():
    assert parse_sec_name("COOK, TIMOTHY D.") == ("COOK", "TIMOTHY")


def test_parse_sec_name_with_suffix():
    assert parse_sec_name("BUFFETT WARREN E JR") == ("BUFFETT", "WARREN")


def test_parse_sec_name_empty():
    assert parse_sec_name("") == ("", "")


def test_parse_sec_name_single_word():
    assert parse_sec_name("MUSK") == ("", "")


def test_parse_wd_name_basic():
    assert parse_wd_name("Tim Cook") == ("COOK", "TIM")


def test_parse_wd_name_middle_initial():
    assert parse_wd_name("Warren E. Buffett") == ("BUFFETT", "WARREN")


def test_parse_wd_name_three_tokens():
    assert parse_wd_name("Mary Jane Watson") == ("WATSON", "MARY")


def test_parse_wd_name_single_token():
    assert parse_wd_name("Musk") == ("", "")


# ---------------------------------------------------------------------------
# tier1_match — exact, initial, ambiguous, no match
# ---------------------------------------------------------------------------


def _cand(qid: str, name: str) -> dict:
    return {"person_qid": qid, "person_name_en": name, "source": "P108"}


def test_tier1_match_exact_sec_format():
    # SEC format: "LAST FIRST MIDDLE" vs WD "First Last"
    candidates = [_cand("Q312097", "Tim Cook")]
    qid, method, confidence = tier1_match("COOK TIMOTHY D", candidates)
    # "Timothy" != "Tim" so this is an initial-level match in structured parse
    assert qid == "Q312097"
    assert confidence == "initial"


def test_tier1_match_exact_same_first_name():
    candidates = [_cand("Q312097", "Timothy Cook")]
    qid, method, confidence = tier1_match("COOK TIMOTHY", candidates)
    assert qid == "Q312097"
    assert method == "wd_p108_scoped"
    assert confidence == "exact"


def test_tier1_match_sec_with_comma():
    candidates = [_cand("Q312097", "Timothy Cook")]
    qid, _, confidence = tier1_match("COOK, TIMOTHY D.", candidates)
    assert qid == "Q312097"
    assert confidence == "exact"


def test_tier1_match_sec_with_suffix():
    candidates = [_cand("Q76", "Warren Buffett")]
    qid, _, confidence = tier1_match("BUFFETT WARREN E JR", candidates)
    assert qid == "Q76"
    assert confidence == "exact"


def test_tier1_match_initial_unique():
    candidates = [
        _cand("Q312097", "Timothy Cook"),
        _cand("Q9999", "Tom Brown"),
    ]
    # SEC has only initial: "COOK T" — unique last+initial in pool → initial match
    qid, method, confidence = tier1_match("COOK T", candidates)
    assert qid == "Q312097"
    assert method == "wd_p108_initial_scoped"
    assert confidence == "initial"


def test_tier1_match_initial_ambiguous_no_match():
    """Two candidates share last name + first initial — must not match."""
    candidates = [
        _cand("Q1", "Thomas Cook"),
        _cand("Q2", "Timothy Cook"),
    ]
    qid, method, confidence = tier1_match("COOK T", candidates)
    assert qid is None
    assert method is None
    assert confidence is None


def test_tier1_match_no_match():
    candidates = [_cand("Q312097", "Tim Cook")]
    qid, _, _ = tier1_match("RANDOM JANE", candidates)
    assert qid is None


def test_tier1_match_empty_candidates():
    qid, _, _ = tier1_match("COOK TIMOTHY", [])
    assert qid is None


# ---------------------------------------------------------------------------
# Diacritic stripping in name parsers
# ---------------------------------------------------------------------------


def test_parse_sec_name_strips_diacritics():
    assert parse_sec_name("OLIVÁN JAVIER") == ("OLIVAN", "JAVIER")


def test_parse_wd_name_strips_diacritics():
    assert parse_wd_name("Javier Oliván") == ("OLIVAN", "JAVIER")


def test_tier1_match_diacritic_insensitive():
    candidates = [{"person_qid": "Q1", "person_name_en": "Javier Oliván", "source": "P108"}]
    qid, method, confidence = tier1_match("OLIVAN JAVIER", candidates)
    assert qid == "Q1"
    assert confidence == "exact"


# ---------------------------------------------------------------------------
# name_canonicals + tier-c (nickname-scoped) match
# ---------------------------------------------------------------------------


def test_name_canonicals_nickname_to_canonical():
    assert "TIMOTHY" in name_canonicals("TIM")
    assert "WILLIAM" in name_canonicals("BILL")
    assert "ROBERT" in name_canonicals("BOB")


def test_name_canonicals_unknown_returns_self():
    assert name_canonicals("ZZTOP") == {"ZZTOP"}


def test_name_canonicals_empty():
    assert name_canonicals("") == set()


def test_name_canonicals_intersect_for_compatible_pair():
    assert name_canonicals("TIM") & name_canonicals("TIMOTHY") == {"TIMOTHY"}
    assert name_canonicals("BILL") & name_canonicals("WILLIAM") == {"WILLIAM"}
    assert name_canonicals("ALICE") & name_canonicals("BOB") == set()


def test_tier1_match_nickname_billy_to_william():
    """EDGAR 'BILLY' vs Wikidata 'William' — different initials, tier-b can't fire."""
    candidates = [{"person_qid": "Q1", "person_name_en": "William Smith", "source": "P108"}]
    qid, method, confidence = tier1_match("SMITH BILLY", candidates)
    assert qid == "Q1"
    assert method == "wd_p108_nickname_scoped"
    assert confidence == "nickname"


def test_tier1_match_nickname_bob_to_robert():
    candidates = [{"person_qid": "Q2", "person_name_en": "Robert Jones", "source": "P108"}]
    qid, method, confidence = tier1_match("JONES BOB", candidates)
    assert qid == "Q2"
    assert method == "wd_p108_nickname_scoped"
    assert confidence == "nickname"


def test_tier1_match_nickname_ambiguous_no_match():
    """An exact match wins before the nickname tier is considered."""
    candidates = [
        {"person_qid": "Q1", "person_name_en": "William Smith", "source": "P108"},
        {"person_qid": "Q2", "person_name_en": "Bill Smith", "source": "P108"},
    ]
    qid, method, confidence = tier1_match("SMITH WILLIAM", candidates)
    # Tier-a exact matching has priority over tier-c nickname matching.
    assert qid == "Q1"
    assert method == "wd_p108_scoped"
    assert confidence == "exact"


def test_tier1_match_nickname_unrelated_no_match():
    candidates = [{"person_qid": "Q1", "person_name_en": "Robert Smith", "source": "P108"}]
    qid, _, _ = tier1_match("SMITH ALICE", candidates)
    assert qid is None


def test_tier1_match_nickname_does_not_override_exact():
    """If exact tier matches, nickname tier must not run / override."""
    candidates = [
        {"person_qid": "Q1", "person_name_en": "Timothy Cook", "source": "P108"},
        # Decoy that would also nickname-match if tier-a were skipped:
        {"person_qid": "Q2", "person_name_en": "Tim Cook", "source": "P108"},
    ]
    qid, method, confidence = tier1_match("COOK TIMOTHY", candidates)
    # Tier-a returns first exact match (Q1). Tier-c never runs.
    assert qid == "Q1"
    assert confidence == "exact"


def test_tier1_match_nickname_uses_variants():
    """Nickname tier must walk name_variants[], not just person_name_en."""
    candidates = [{
        "person_qid": "Q3",
        "person_name_en": "Tim Cook",
        "name_variants": ["Tim Cook", "Timothy Cook", "Timothy Donald Cook"],
        "source": "P108",
    }]
    # SEC "TIMOTHY" matches "Timothy Cook" variant via tier-a (exact). Confirm path.
    qid, _, confidence = tier1_match("COOK TIMOTHY D", candidates)
    assert qid == "Q3"
    assert confidence == "exact"


def test_tier1_match_empty_name():
    candidates = [_cand("Q312097", "Tim Cook")]
    qid, _, _ = tier1_match("", candidates)
    assert qid is None


def test_tier1_match_prefers_exact_over_initial():
    """If an exact match exists it must win, even if an initial match also exists."""
    candidates = [
        _cand("Q1", "Timothy Cook"),   # exact match (Timothy == Timothy)
        _cand("Q2", "Tony Cook"),      # would be an initial match (T. Cook)
    ]
    qid, _, confidence = tier1_match("COOK TIMOTHY", candidates)
    assert qid == "Q1"
    assert confidence == "exact"


def test_tier1_match_rejects_corporate_reporter():
    # Single-word-ish corporate reporters should not match a person
    candidates = [_cand("Q312097", "Tim Cook")]
    qid, _, _ = tier1_match("BERKSHIRE HATHAWAY INC", candidates)
    # "INC" is 3 chars, not stripped; parses as (BERKSHIRE, HATHAWAY)
    # — no candidate has that combo, so no match. Safe.
    assert qid is None


# ---------------------------------------------------------------------------
# parse_quarter_override
# ---------------------------------------------------------------------------


def test_parse_quarter_override_single():
    assert parse_quarter_override("2023q4") == [(2023, 4)]


def test_parse_quarter_override_multiple():
    assert parse_quarter_override("2024q1,2024q2") == [(2024, 1), (2024, 2)]


def test_parse_quarter_override_uppercase():
    assert parse_quarter_override("2024Q3") == [(2024, 3)]


def test_parse_quarter_override_whitespace():
    assert parse_quarter_override(" 2024q1 , 2024q2 ") == [(2024, 1), (2024, 2)]


def test_parse_quarter_override_invalid_raises():
    import pytest

    with pytest.raises(ValueError):
        parse_quarter_override("2024q5")  # Q5 does not exist


def test_parse_quarter_override_bad_format_raises():
    import pytest

    with pytest.raises(ValueError):
        parse_quarter_override("Q3-2024")


# ---------------------------------------------------------------------------
# tier1_match — name_variants support
# ---------------------------------------------------------------------------


def _cand_with_variants(qid: str, name: str, variants: list[str]) -> dict:
    return {"person_qid": qid, "person_name_en": name, "name_variants": variants, "source": "P108"}


def test_tier1_match_via_alt_label_variant():
    """Match must succeed via an altLabel variant even when person_name_en alone would miss.

    SEC reports "GORE ALBERT" → parse_sec_name → ('GORE', 'ALBERT').
    Candidate has person_name_en="Al Gore" (wouldn't match 'ALBERT') but
    name_variants includes "Albert Gore" which parses to ('GORE', 'ALBERT') → exact match.
    """
    candidates = [_cand_with_variants("Q76", "Al Gore", ["Albert Gore", "Al Gore"])]
    qid, method, confidence = tier1_match("GORE ALBERT", candidates)
    assert qid == "Q76"
    assert method == "wd_p108_scoped"
    assert confidence == "exact"


def test_tier1_match_fallback_to_person_name_en_when_no_variants():
    """Candidate without name_variants falls back to person_name_en (older doc format)."""
    # Dict without name_variants key at all
    candidates = [{"person_qid": "Q312097", "person_name_en": "Tim Cook", "source": "P108"}]
    qid, method, confidence = tier1_match("COOK TIM", candidates)
    assert qid == "Q312097"
    assert confidence == "exact"


def test_tier1_match_initial_via_variant():
    """Initial match must also work through a variant."""
    candidates = [_cand_with_variants("Q76", "Al Gore", ["Albert Gore", "Al Gore"])]
    # SEC reports only "GORE A" — initial match via "Albert Gore"
    qid, method, confidence = tier1_match("GORE A", candidates)
    assert qid == "Q76"
    assert method == "wd_p108_initial_scoped"
    assert confidence == "initial"


def test_tier1_match_no_false_positive_via_variant():
    """Having many variants must not accidentally match an unrelated SEC name."""
    candidates = [_cand_with_variants("Q76", "Al Gore", ["Albert Gore", "Al Gore"])]
    qid, _, _ = tier1_match("SMITH JOHN", candidates)
    assert qid is None
