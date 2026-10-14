"""Unit tests for backend/app/utils/seo.py — is_thin_stub predicate.

Pure-function tests — no database, no network, no Redis required.
"""

import pytest

from app.utils.seo import is_thin_stub


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_person(
    image_cached_path=None,
    description_en=None,
    description_it=None,
) -> dict:
    return {
        "image_cached_path": image_cached_path,
        "description_en": description_en,
        "description_it": description_it,
    }


# ---------------------------------------------------------------------------
# Core stub detection
# ---------------------------------------------------------------------------

def test_all_empty_zero_connections_is_stub():
    """All fields empty + 0 connections → stub."""
    p = _make_person()
    assert is_thin_stub(p, 0) is True


def test_all_empty_one_connection_is_stub():
    """All fields empty + 1 connection → stub (<2 threshold)."""
    p = _make_person()
    assert is_thin_stub(p, 1) is True


def test_all_empty_two_connections_not_stub():
    """All fields empty + 2 connections → NOT a stub (threshold is <2)."""
    p = _make_person()
    assert is_thin_stub(p, 2) is False


def test_all_empty_many_connections_not_stub():
    """All fields empty + many connections → NOT a stub."""
    p = _make_person()
    assert is_thin_stub(p, 25) is False


# ---------------------------------------------------------------------------
# Photo presence cancels stub
# ---------------------------------------------------------------------------

def test_has_photo_not_stub():
    """Has photo, no descriptions, 0 connections → NOT a stub."""
    p = _make_person(image_cached_path="/images/Q1.jpg")
    assert is_thin_stub(p, 0) is False


def test_has_photo_with_connections_not_stub():
    """Has photo, 1 connection → NOT a stub (photo alone is enough)."""
    p = _make_person(image_cached_path="/images/Q1.jpg")
    assert is_thin_stub(p, 1) is False


# ---------------------------------------------------------------------------
# description_en presence cancels stub
# ---------------------------------------------------------------------------

def test_has_description_en_not_stub():
    """Has description_en, no photo, no desc_it, 0 connections → NOT a stub."""
    p = _make_person(description_en="A famous person.")
    assert is_thin_stub(p, 0) is False


def test_empty_string_description_en_is_stub():
    """description_en == '' (empty string) counts as absent → still a stub."""
    p = _make_person(description_en="")
    assert is_thin_stub(p, 0) is True


# ---------------------------------------------------------------------------
# description_it presence cancels stub
# ---------------------------------------------------------------------------

def test_has_description_it_not_stub():
    """Has description_it, no photo, no desc_en, 0 connections → NOT a stub."""
    p = _make_person(description_it="Una persona famosa.")
    assert is_thin_stub(p, 0) is False


def test_empty_string_description_it_is_stub():
    """description_it == '' (empty string) counts as absent → still a stub."""
    p = _make_person(description_it="")
    assert is_thin_stub(p, 0) is True


# ---------------------------------------------------------------------------
# None vs missing key handling
# ---------------------------------------------------------------------------

def test_none_fields_treated_as_absent():
    """None values for all fields + 0 connections → stub."""
    p = {"image_cached_path": None, "description_en": None, "description_it": None}
    assert is_thin_stub(p, 0) is True


def test_missing_keys_treated_as_absent():
    """Missing keys (empty dict) + 0 connections → stub."""
    assert is_thin_stub({}, 0) is True


# ---------------------------------------------------------------------------
# Combinations
# ---------------------------------------------------------------------------

def test_only_photo_cancels_stub_regardless_of_connections():
    """Photo present with 0 connections → NOT a stub."""
    p = _make_person(image_cached_path="x.jpg")
    assert is_thin_stub(p, 0) is False


def test_photo_and_desc_and_connections_not_stub():
    """Rich page → definitely NOT a stub."""
    p = _make_person(
        image_cached_path="/images/Q1.jpg",
        description_en="Rich description.",
        description_it="Descrizione ricca.",
    )
    assert is_thin_stub(p, 10) is False


# ---------------------------------------------------------------------------
# Sitemap filter — mock person list
# ---------------------------------------------------------------------------

def test_sitemap_filter_excludes_stubs():
    """Filtering a mixed list keeps only non-stubs."""
    persons = [
        # Rich page — should be included
        {"qid": "Q1", "image_cached_path": "/img/q1.jpg", "description_en": "Desc",
         "description_it": None, "conn_count": 5},
        # Stub — should be excluded (no photo, no desc, 1 connection)
        {"qid": "Q2", "image_cached_path": None, "description_en": None,
         "description_it": None, "conn_count": 1},
        # Has description_it — should be included
        {"qid": "Q3", "image_cached_path": None, "description_en": None,
         "description_it": "Una persona.", "conn_count": 0},
        # Has 2 connections but no content — NOT a stub
        {"qid": "Q4", "image_cached_path": None, "description_en": None,
         "description_it": None, "conn_count": 2},
    ]

    included = [
        row["qid"] for row in persons
        if not is_thin_stub(row, row.get("conn_count") or 0)
    ]
    excluded = [
        row["qid"] for row in persons
        if is_thin_stub(row, row.get("conn_count") or 0)
    ]

    assert included == ["Q1", "Q3", "Q4"]
    assert excluded == ["Q2"]


def test_sitemap_filter_all_stubs():
    """All stubs → empty included list."""
    persons = [
        {"qid": f"Q{i}", "image_cached_path": None, "description_en": None,
         "description_it": None, "conn_count": 0}
        for i in range(5)
    ]
    included = [
        row["qid"] for row in persons
        if not is_thin_stub(row, row.get("conn_count") or 0)
    ]
    assert included == []


def test_sitemap_filter_no_stubs():
    """All rich pages → all included."""
    persons = [
        {"qid": "Q1", "image_cached_path": "/img/q1.jpg", "description_en": "Desc",
         "description_it": None, "conn_count": 5},
        {"qid": "Q2", "image_cached_path": None, "description_en": None,
         "description_it": None, "conn_count": 3},
    ]
    included = [
        row["qid"] for row in persons
        if not is_thin_stub(row, row.get("conn_count") or 0)
    ]
    assert included == ["Q1", "Q2"]
