"""Unit tests for Phase 3 connections logic in person_page.py.

Pure-function tests — no database, no network, no Redis required.
All DB calls are replaced with mock data.
"""

import pytest

from app.utils.slug import slugify
from app.routers.person_page import (
    _ORGANIZATION_CONNECTIONS_AQL,
    _ORGANIZATION_EDGE_TYPES,
    _build_body_fragment,
    _build_head_fragment,
    _build_connections_nav,
    _build_organization_connections_section,
    _connections_hash,
    _group_organization_connections,
    _meta_description,
    _organization_connections_hash,
    _relationship_period,
    _render_page,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_conn(qid: str, name_en: str = None, name_it: str = None,
               edge_type: str = None, statements: int = 0) -> dict:
    return {
        "qid": qid,
        "name_en": name_en,
        "name_it": name_it,
        "statements": statements,
        "edge_type": edge_type,
    }


# ---------------------------------------------------------------------------
# _build_connections_nav — empty input
# ---------------------------------------------------------------------------

def test_empty_connections_returns_empty_string():
    """Zero connections → no <nav> block at all."""
    result = _build_connections_nav([])
    assert result == ""


def test_none_list_treated_as_empty():
    """_build_connections_nav([]) never raises; None guard is upstream."""
    assert _build_connections_nav([]) == ""


# ---------------------------------------------------------------------------
# _build_connections_nav — nav structure
# ---------------------------------------------------------------------------

def test_nav_present_when_connections_exist():
    conns = [_make_conn("Q100", name_en="Alice Example", edge_type="colleague")]
    result = _build_connections_nav(conns)
    assert '<nav aria-label="Connected people">' in result
    assert "<h2>Connections</h2>" in result
    assert "<ul>" in result
    assert "</ul>" in result
    assert "</nav>" in result


def test_one_connection_one_li():
    conns = [_make_conn("Q100", name_en="Alice Example", edge_type="colleague")]
    result = _build_connections_nav(conns)
    assert result.count("<li>") == 1


def test_multiple_connections_multiple_li():
    conns = [
        _make_conn("Q100", name_en="Alice Example", edge_type="spouse"),
        _make_conn("Q200", name_en="Bob Sample", edge_type="colleague"),
        _make_conn("Q300", name_en="Carol Test", edge_type=None),
    ]
    result = _build_connections_nav(conns)
    assert result.count("<li>") == 3


# ---------------------------------------------------------------------------
# _build_connections_nav — slug composition
# ---------------------------------------------------------------------------

def test_slug_in_href_matches_slugify():
    """The href must use slugify() output."""
    name = "François Mitterrand"
    expected_slug = slugify(name)
    conns = [_make_conn("Q12345", name_en=name)]
    result = _build_connections_nav(conns)
    assert f'/person/Q12345-{expected_slug}"' in result


def test_qid_only_href_when_slug_empty():
    """Pure CJK name → slug is empty → href is /person/{qid} without a dash."""
    conns = [_make_conn("Q999", name_en="日本語")]
    result = _build_connections_nav(conns)
    assert '/person/Q999"' in result
    # Must not produce /person/Q999-
    assert "/person/Q999-" not in result


def test_href_uses_name_en_over_name_it():
    """name_en takes priority over name_it for display and slug."""
    conns = [_make_conn("Q42", name_en="English Name", name_it="Nome Italiano",
                        edge_type=None)]
    result = _build_connections_nav(conns)
    assert "English Name" in result
    assert "Nome Italiano" not in result


def test_fallback_to_name_it_when_no_name_en():
    conns = [_make_conn("Q42", name_it="Nome Italiano")]
    result = _build_connections_nav(conns)
    assert "Nome Italiano" in result


def test_fallback_to_qid_when_no_name():
    conns = [_make_conn("Q42")]
    result = _build_connections_nav(conns)
    assert "Q42" in result


# ---------------------------------------------------------------------------
# _build_connections_nav — label formatting
# ---------------------------------------------------------------------------

def test_edge_type_snake_case_rendered_title_case():
    """'member_of_parliament' → 'Member Of Parliament' after snake-to-title."""
    conns = [_make_conn("Q55", name_en="Test Person", edge_type="member_of_parliament")]
    result = _build_connections_nav(conns)
    assert "Member Of Parliament" in result


def test_edge_type_simple_word_rendered():
    conns = [_make_conn("Q55", name_en="Test Person", edge_type="spouse")]
    result = _build_connections_nav(conns)
    assert "Spouse" in result


def test_edge_type_with_em_dash_separator():
    """Label must be separated from the name by ' — ' (em-dash with spaces)."""
    conns = [_make_conn("Q55", name_en="Test Person", edge_type="colleague")]
    result = _build_connections_nav(conns)
    assert " \u2014 Colleague" in result


def test_no_label_no_em_dash():
    """When edge_type is None/empty, no em-dash or label is rendered."""
    conns = [_make_conn("Q55", name_en="No Label Person", edge_type=None)]
    result = _build_connections_nav(conns)
    assert "\u2014" not in result


def test_no_label_empty_string():
    conns = [_make_conn("Q55", name_en="No Label Person", edge_type="")]
    result = _build_connections_nav(conns)
    assert "\u2014" not in result


# ---------------------------------------------------------------------------
# _build_connections_nav — HTML escaping
# ---------------------------------------------------------------------------

def test_name_is_html_escaped():
    """XSS attempt in name is escaped."""
    conns = [_make_conn("Q99", name_en='<script>alert("xss")</script>')]
    result = _build_connections_nav(conns)
    assert "<script>" not in result
    assert "&lt;script&gt;" in result


def test_edge_type_is_html_escaped():
    conns = [_make_conn("Q99", name_en="Safe Name", edge_type='<b>bold</b>')]
    result = _build_connections_nav(conns)
    assert "<b>" not in result


def test_href_is_html_escaped():
    """QID is always Q\\d+, but ensure href attr is quoted safely."""
    conns = [_make_conn("Q1", name_en="Test")]
    result = _build_connections_nav(conns)
    # href attribute must be inside double quotes
    assert 'href="/person/' in result


# ---------------------------------------------------------------------------
# _build_connections_nav — rel="related"
# ---------------------------------------------------------------------------

def test_rel_related_attribute_present():
    conns = [_make_conn("Q1", name_en="Somebody")]
    result = _build_connections_nav(conns)
    assert 'rel="related"' in result


# ---------------------------------------------------------------------------
# Self-loop filtering (done at AQL level; test the nav doesn't render it
# even if it somehow slips through with matching qid)
# ---------------------------------------------------------------------------

def test_nav_renders_all_items_passed_to_it():
    """_build_connections_nav renders what it's given — self-loop guard is
    at the AQL query level.  If zero items passed in, empty string is returned."""
    # Pass empty list simulating AQL already filtered self-loop
    assert _build_connections_nav([]) == ""

    # Pass a non-empty list — it renders
    conns = [_make_conn("Q1", name_en="Someone")]
    assert _build_connections_nav(conns) != ""


# ---------------------------------------------------------------------------
# _connections_hash — determinism and sensitivity
# ---------------------------------------------------------------------------

def test_connections_hash_deterministic():
    """Same connections list always produces the same hash."""
    conns = [
        _make_conn("Q1", name_en="A", edge_type="spouse"),
        _make_conn("Q2", name_en="B", edge_type="colleague"),
    ]
    h1 = _connections_hash(conns)
    h2 = _connections_hash(conns)
    assert h1 == h2


def test_connections_hash_changes_on_new_edge():
    """Adding a connection changes the hash."""
    conns = [_make_conn("Q1", edge_type="spouse")]
    h1 = _connections_hash(conns)
    conns2 = conns + [_make_conn("Q2", edge_type="colleague")]
    h2 = _connections_hash(conns2)
    assert h1 != h2


def test_connections_hash_changes_on_edge_type_change():
    """Changing edge_type for the same QID changes the hash."""
    conns_a = [_make_conn("Q1", edge_type="spouse")]
    conns_b = [_make_conn("Q1", edge_type="colleague")]
    assert _connections_hash(conns_a) != _connections_hash(conns_b)


def test_connections_hash_empty_is_stable():
    assert _connections_hash([]) == _connections_hash([])


def test_connections_hash_is_16_chars():
    conns = [_make_conn("Q1", edge_type="spouse")]
    h = _connections_hash(conns)
    assert len(h) == 16


# ---------------------------------------------------------------------------
# Dedupe: multiple edges to same QID — nav renders each qid once
# (dedupe happens in AQL via COLLECT; test that if nav receives a deduped
#  list it renders correctly)
# ---------------------------------------------------------------------------

def test_no_duplicate_qids_in_deduped_list():
    """After AQL COLLECT dedup, each QID appears once in the rendered nav."""
    conns = [
        _make_conn("Q10", name_en="Person A", edge_type="spouse"),
        _make_conn("Q20", name_en="Person B", edge_type="colleague"),
    ]
    result = _build_connections_nav(conns)
    # Q10 appears exactly once as an anchor
    assert result.count("/person/Q10") == 1
    assert result.count("/person/Q20") == 1


# ---------------------------------------------------------------------------
# Server-rendered profile content
# ---------------------------------------------------------------------------

def test_body_keeps_real_person_facts_and_connection_links_without_template_copy():
    person = {
        "description_en": "Example actor and director.",
        "occupations": ["actor"],
        "nationalities": ["Italian"],
    }
    conns = [
        _make_conn("Q100", name_en="Alice Example", edge_type="colleague"),
        _make_conn("Q200", name_en="Bob Example", edge_type="spouse"),
    ]
    result = _build_body_fragment(person, "Test Person", "Q1", False, conns)
    assert "Example actor and director." in result
    assert "public relationship network" not in result
    assert "Alice Example" in result


def test_person_metadata_uses_real_description_and_non_templated_title():
    person = {"description_en": "An example biography."}
    description = _meta_description(person)
    head = _build_head_fragment(
        person,
        "Example Person",
        description,
        "https://humansmap.com/person/Q1-example-person",
        "https://humansmap.com/og/Q1.png",
        "Q1",
        False,
    )
    assert description == "An example biography."
    assert "<title>Example Person | humansmap.com</title>" in head
    assert "connections and relationship network" not in head


def test_rendered_profile_survives_vue_mount_target():
    """The durable profile must follow #app rather than be replaced by Vue."""
    shell = """<!doctype html><html><head>
<title>Homepage</title>
<meta name="description" content="Homepage description">
</head><body><div id="app"></div></body></html>"""

    rendered = _render_page(
        shell,
        "<title>Profile</title>",
        '<main id="seo-content"><h1>Profile</h1></main>',
    )

    assert rendered.index('<div id="app"></div>') < rendered.index('id="seo-content"')
    assert rendered.index('id="seo-content"') < rendered.index("</body>")
    assert "<title>Homepage</title>" not in rendered


def _make_org_row(
    qid: str,
    name_en: str = None,
    name_it: str = None,
    edge_type: str = "founder",
    statements: int = 0,
    start_date: str = None,
    end_date: str = None,
) -> dict:
    return {
        "qid": qid,
        "name_en": name_en,
        "name_it": name_it,
        "statements": statements,
        "edge_type": edge_type,
        "start_date": start_date,
        "end_date": end_date,
    }


# ---------------------------------------------------------------------------
# Server-rendered organization relationships (Phase 4)
# ---------------------------------------------------------------------------

def test_organization_query_is_wikidata_only_and_excludes_sec_relationships():
    assert "wikidata" in _ORGANIZATION_CONNECTIONS_AQL.lower()
    assert set(_ORGANIZATION_EDGE_TYPES).isdisjoint({
        "holds_shares_of",
        "insider_of",
        "director_of",
        "officer_of",
        "beneficial_owner_of",
    })


def test_group_organization_connections_deduplicates_roles_and_suppresses_employee():
    rows = [
        _make_org_row("Q1", "Acme", "Acme IT", "founder", 200, "2015-00-00"),
        _make_org_row("Q1", "Acme", "Acme IT", "founder", 200, "2015-00-00"),
        _make_org_row("Q1", "Acme", "Acme IT", "owner", 200),
        _make_org_row("Q1", "Acme", "Acme IT", "employee", 200),
        _make_org_row("Q2", "University", edge_type="educated_at", statements=100),
    ]

    grouped = _group_organization_connections(rows)

    assert [item["qid"] for item in grouped] == ["Q1", "Q2"]
    assert [rel["edge_type"] for rel in grouped[0]["relationships"]] == [
        "founder",
        "owner",
    ]


def test_relationship_period_handles_partial_wikidata_dates():
    assert _relationship_period({
        "start_date": "2015-00-00",
        "end_date": "2019-00-00",
    }) == "2015–2019"
    assert _relationship_period({"start_date": "2002-00-00"}) == "from 2002"
    assert _relationship_period({"end_date": "2018-00-00"}) == "until 2018"
    assert _relationship_period({}) == ""


def test_organization_section_has_canonical_links_explanations_and_escaping():
    grouped = _group_organization_connections([
        _make_org_row("Q478214", "Tesla & <script>", edge_type="founder"),
        _make_org_row("Q478214", "Tesla & <script>", edge_type="owner"),
        _make_org_row("Q123", "University of Somewhere", edge_type="educated_at"),
    ])

    result = _build_organization_connections_section("Elon &amp; Co", grouped)

    assert '<section aria-labelledby="organization-connections">' in result
    assert '<h2 id="organization-connections">Organizations and public roles</h2>' in result
    assert "These connections are inferred from roles and affiliations recorded in the graph." in result
    assert '/organization/Q478214-tesla-script"' in result
    assert "The graph records Elon &amp; Co as a founder and an owner of" in result
    assert "The graph connects Elon &amp; Co to" in result
    assert "through education" in result
    assert "<script>" not in result
    assert "Tesla &amp; &lt;script&gt;" in result


def test_organization_connections_hash_is_deterministic_and_sensitive():
    grouped = _group_organization_connections([
        _make_org_row("Q1", "Acme", edge_type="founder"),
    ])
    changed = _group_organization_connections([
        _make_org_row("Q1", "Acme", edge_type="owner"),
    ])

    assert _organization_connections_hash(grouped) == _organization_connections_hash(grouped)
    assert _organization_connections_hash(grouped) != _organization_connections_hash(changed)
    assert len(_organization_connections_hash(grouped)) == 16


def test_body_includes_organization_section_and_person_connections():
    organizations = _group_organization_connections([
        _make_org_row("Q1", "Acme", edge_type="founder"),
    ])
    people = [_make_conn("Q2", name_en="Alice Example", edge_type="colleague")]

    result = _build_body_fragment(
        {"description_en": "A public biography."},
        "Test Person",
        "Q9",
        False,
        people,
        organization_connections=organizations,
    )

    assert "Organizations and public roles" in result
    assert "/organization/Q1-acme" in result
    assert "Alice Example" in result
    assert result.index("Organizations and public roles") < result.index("Connections")


def test_organization_section_marks_ended_ceo_as_former():
    grouped = _group_organization_connections([
        _make_org_row(
            "Q1",
            "Acme",
            edge_type="chief_executive",
            end_date="2020-00-00",
        ),
    ])

    result = _build_organization_connections_section("Alice", grouped)

    assert "as a former chief executive (until 2020) of" in result
    assert "as chief executive" not in result


def test_group_organization_connections_prefers_active_ceo_appointment():
    grouped = _group_organization_connections([
        _make_org_row(
            "Q1",
            "Acme",
            edge_type="chief_executive",
            start_date="2010-00-00",
            end_date="2020-00-00",
        ),
        _make_org_row(
            "Q1",
            "Acme",
            edge_type="chief_executive",
            start_date="2024-00-00",
        ),
    ])

    assert grouped[0]["relationships"][0]["end_date"] is None


def test_entity_render_removes_homepage_noscript_copy():
    shell = """<!doctype html><html><head><title>Homepage</title></head><body>
<div id="app"></div><noscript><main><h1>Homepage H1</h1></main></noscript></body></html>"""
    rendered = _render_page(shell, "<title>Profile</title>", '<main id="seo-content"><h1>Person H1</h1></main>')
    assert "<noscript>" not in rendered
    assert "Homepage H1" not in rendered
    assert rendered.count("<h1>") == 1


def test_profile_body_renders_image_attribution_and_enrichment():
    person = {
        "image_source_url": "https://commons.wikimedia.org/wiki/File:Portrait.jpg",
        "image_license": "CC BY-SA 4.0",
        "image_license_url": "https://creativecommons.org/licenses/by-sa/4.0/",
        "image_author": "Jane Doe",
        "awards_count": 3,
        "awards_top5": ["Award One", "Award Two"],
        "notable_works": ["A Notable Work"],
    }
    body = _build_body_fragment(person, "Example Person", "Q1", True)
    assert "Image by Jane Doe" in body
    assert "CC BY-SA 4.0" in body
    assert "resized and converted by Humans Map" in body
    assert "Award One" in body
    assert "And 1 more" in body
    assert "A Notable Work" in body
