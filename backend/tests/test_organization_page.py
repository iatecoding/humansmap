"""Pure-function tests for the server-rendered organization SEO page."""

from app.routers.organization_page import (
    _build_body,
    _build_head,
    _build_jsonld,
    _meta_description,
    _organization_path,
    _relationship_label,
)


def _organization() -> dict:
    return {
        "_key": "Q1",
        "name_en": "Example Holdings",
        "description_en": "Example holding company.",
        "country": "Italy",
        "employees_count": 12,
        "website": "https://example.com",
    }


def test_organization_path_uses_a_slugged_canonical_url():
    assert _organization_path("Q1", "Example Holdings") == "/organization/Q1-example-holdings"


def test_structure_labels_reflect_edge_direction():
    assert _relationship_label("parent_of", True) == "Parent organization"
    assert _relationship_label("parent_of", False) == "Subsidiary"
    assert _relationship_label("owned_by", True) == "Owner"


def test_body_contains_real_internal_links_for_structure_and_people():
    body = _build_body(
        _organization(),
        [{
            "qid": "Q2",
            "name_en": "Example Subsidiary",
            "edge_type": "subsidiary_of",
            "outgoing": True,
        }],
        [{
            "qid": "Q3",
            "name_en": "Example Founder",
            "edge_type": "founder",
            "role": None,
        }],
        people_count=1,
        structure_count=1,
    )

    assert '/organization/Q2-example-subsidiary' in body
    assert '/person/Q3-example-founder' in body
    assert "Ownership and structure" in body
    assert "People associated with this organization" in body
    # The copied source description stays visible but does not dominate snippets.
    assert 'data-nosnippet' in body


def test_meta_description_leads_with_the_network_value():
    description = _meta_description(_organization(), people_count=3, structure_count=2)
    assert description.startswith("Explore Example Holdings&#x27;s ownership, people")
    assert "3 associated people" in description


def test_jsonld_identifies_the_canonical_organization():
    markup = _build_jsonld(
        _organization(),
        "https://humansmap.com/organization/Q1-example-holdings",
        [],
    )
    assert '"@type": "Organization"' in markup
    assert '"url": "https://humansmap.com/organization/Q1-example-holdings"' in markup
    assert 'https://www.wikidata.org/wiki/Q1' in markup


def test_substantive_organization_head_is_indexable():
    head = _build_head(
        _organization(),
        "https://humansmap.com/organization/Q1-example-holdings",
        "Example holding company.",
        [],
    )
    assert 'name="robots" content="noindex' not in head


def test_thin_organization_head_is_noindex():
    head = _build_head(
        _organization(),
        "https://humansmap.com/organization/Q1-example-holdings",
        "Example holding company.",
        [],
        noindex=True,
    )
    assert '<meta name="robots" content="noindex, follow">' in head


def test_thin_organization_helper_distinguishes_empty_stubs():
    from app.utils.seo import is_thin_organization

    assert is_thin_organization({"description_en": None, "description_it": None}, 1) is False
    assert is_thin_organization({"description_en": None, "description_it": None}, 0) is True


def test_organization_image_has_visible_and_structured_license_attribution():
    org = {
        **_organization(),
        "image_cached_path": "/app/images/Q1.jpg",
        "image_source_url": "https://commons.wikimedia.org/wiki/File:Example.jpg",
        "image_license": "CC BY-SA 4.0",
        "image_license_url": "https://creativecommons.org/licenses/by-sa/4.0/",
        "image_author": "Example Photographer",
    }
    body = _build_body(org, [], [], people_count=0, structure_count=0)
    jsonld = _build_jsonld(
        org, "https://humansmap.com/organization/Q1-example-holdings", []
    )
    assert "Image by Example Photographer" in body
    assert "CC BY-SA 4.0" in body
    assert '"license": "https://creativecommons.org/licenses/by-sa/4.0/"' in jsonld
    assert '"creator": {"@type": "Person", "name": "Example Photographer"}' in jsonld
