"""Tests for candidate statement-context reconciliation."""

from scripts.reconcile_candidate_edge_context import edge_identity


def test_forward_person_edge_identity_preserves_original_direction():
    collection, key, subject, target = edge_identity(1, 26, 2)

    assert collection == "relationships"
    assert key == "Q1_Q2_spouse_P26"
    assert subject == "Q1"
    assert target == "Q2"


def test_reverse_org_edge_identity_preserves_wikidata_direction():
    collection, key, subject, target = edge_identity(312, 169, 42)

    assert collection == "affiliations"
    assert key == "Q42_Q312_chief_executive_P169"
    assert subject == "Q312"
    assert target == "Q42"


def test_org_structure_identity_is_not_reversed():
    collection, key, subject, target = edge_identity(10, 749, 20)

    assert collection == "org_structure"
    assert key == "Q10_Q20_parent_of_P749"
    assert subject == "Q10"
    assert target == "Q20"
