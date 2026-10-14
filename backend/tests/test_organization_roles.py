"""Tests for the shared organization-role taxonomy."""

from datetime import date

from app.domain.organization_roles import (
    feed_role_label,
    public_role_phrase,
    relationship_has_ended,
)


TODAY = date(2026, 9, 4)


def test_relationship_has_ended_understands_complete_and_partial_dates():
    assert relationship_has_ended("2025-12-31", today=TODAY)
    assert relationship_has_ended("2025-00-00", today=TODAY)
    assert relationship_has_ended("2026-08-00", today=TODAY)
    assert relationship_has_ended("2026-09-03", today=TODAY)


def test_relationship_has_ended_is_conservative_for_incomplete_current_dates():
    assert not relationship_has_ended(None, today=TODAY)
    assert not relationship_has_ended("", today=TODAY)
    assert not relationship_has_ended("2026-00-00", today=TODAY)
    assert not relationship_has_ended("2026-09-00", today=TODAY)
    assert not relationship_has_ended("2026-09-04", today=TODAY)
    assert not relationship_has_ended("2027-01-01", today=TODAY)


def test_relationship_has_ended_rejects_malformed_dates():
    assert not relationship_has_ended("2020-not-a-date", today=TODAY)
    assert not relationship_has_ended("2020-13-01", today=TODAY)
    assert not relationship_has_ended("2020-02-31", today=TODAY)
    assert not relationship_has_ended("2020-00-01", today=TODAY)


def test_ceo_labels_distinguish_ended_appointments():
    assert feed_role_label("chief_executive") == "CEO"
    assert feed_role_label("chief_executive", "2020-00-00") == "Former CEO"
    assert public_role_phrase("chief_executive") == "chief executive"
    assert (
        public_role_phrase("chief_executive", "2020-00-00")
        == "a former chief executive"
    )


def test_non_temporal_role_labels_are_unchanged():
    assert feed_role_label("founder", "2020-00-00") == "Founder"
    assert public_role_phrase("founder", "2020-00-00") == "a founder"
    assert feed_role_label("unknown") is None
    assert public_role_phrase("unknown") is None
