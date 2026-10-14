"""Build graph edges from normalized Wikidata dump entities.

The dump parsers normalize both N-Triples and JSON input into the same entity
shape. This module owns the property taxonomy and turns that normalized shape
into ArangoDB edge documents, independently from streaming and batch writes.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from datetime import datetime, timezone

from app.database import (
    AFFILIATIONS,
    EVENTS,
    INVOLVEMENTS,
    ORGANIZATIONS,
    ORG_STRUCTURE,
    PERSONS,
    RELATIONSHIPS,
)
from app.domain.organization_roles import WIKIDATA_ORGANIZATION_PROPERTY_ROLES

EdgeDefinition = tuple[str, str]
PropertyMap = Mapping[str, EdgeDefinition]
EndpointBuilder = Callable[[str, str, str], tuple[str, str]]


EDGE_PROPERTIES: dict[str, EdgeDefinition] = {
    # Family and other person-to-person relationships.
    "P26": (RELATIONSHIPS, "spouse"),
    "P22": (RELATIONSHIPS, "parent"),
    "P25": (RELATIONSHIPS, "parent"),
    "P40": (RELATIONSHIPS, "child"),
    "P3373": (RELATIONSHIPS, "sibling"),
    "P1038": (RELATIONSHIPS, "relative"),
    "P451": (RELATIONSHIPS, "unmarried_partner"),
    "P184": (RELATIONSHIPS, "doctoral_advisor"),
    "P185": (RELATIONSHIPS, "doctoral_student"),
    "P1327": (RELATIONSHIPS, "professional_collaborator"),
    # Person-to-organization affiliations.
    "P108": (AFFILIATIONS, "employee"),
    "P39": (AFFILIATIONS, "position_held"),
    "P102": (AFFILIATIONS, "member_of"),
    "P463": (AFFILIATIONS, "member_of"),
    "P69": (AFFILIATIONS, "educated_at"),
    "P54": (AFFILIATIONS, "sports_team_member"),
    # Person-to-event involvements.
    "P1344": (INVOLVEMENTS, "participant"),
    "P793": (INVOLVEMENTS, "significant_event"),
}

# These properties live on organizations but point to people. Their graph
# direction is inverted below to preserve the person-to-organization schema.
ORG_REVERSE_PROPERTIES: dict[str, EdgeDefinition] = {
    property_id: (AFFILIATIONS, role)
    for property_id, role in WIKIDATA_ORGANIZATION_PROPERTY_ROLES.items()
}

ORG_STRUCTURE_PROPERTIES: dict[str, EdgeDefinition] = {
    "P749": (ORG_STRUCTURE, "parent_of"),       # child -> parent
    "P355": (ORG_STRUCTURE, "subsidiary_of"),  # parent -> child
    "P127": (ORG_STRUCTURE, "owned_by"),        # owned -> owner
    "P1830": (ORG_STRUCTURE, "owns"),           # owner -> owned
}

EVENT_REVERSE_PROPERTIES: dict[str, EdgeDefinition] = {
    "P710": (INVOLVEMENTS, "participant"),
}


def parse_date(value: str | None) -> str | None:
    """Extract a clean date from a Wikidata dateTime literal."""
    if not value:
        return None
    return value.split("T", 1)[0]


def sanitize_key(key: str) -> str:
    """Sanitize a string for use as an ArangoDB document key."""
    return "".join(c if c.isalnum() or c in "_-:." else "_" for c in key)


def _qualified_statements(entity: dict, property_map: PropertyMap) -> dict[str, list[dict]]:
    qualified: dict[str, list[dict]] = {}
    for statement_key, statement in entity["stmt_nodes"].items():
        property_id = statement.get("prop")
        value = statement.get("value")
        if not property_id or not value or property_id not in property_map:
            continue

        qualifiers = statement.get("quals", {})
        qualified.setdefault(property_id, []).append({
            "value": value,
            "statement_guid": statement.get("statement_guid") or statement_key,
            "rank": statement.get("rank"),
            "start_date": parse_date(qualifiers.get("P580")),
            "end_date": parse_date(qualifiers.get("P582")),
            "point_in_time": parse_date(qualifiers.get("P585")),
            "named_as": qualifiers.get("P1810"),
            "ordinal": qualifiers.get("P1545"),
        })
    return qualified


def _document_key(handle: str) -> str:
    return handle.rsplit("/", 1)[-1]


def _build_property_edges(
    subject_qid: str,
    entity: dict,
    property_map: PropertyMap,
    endpoint_builder: EndpointBuilder,
    *,
    include_statement_context: bool,
) -> list[dict]:
    """Build qualified edges first, then fill gaps from direct properties."""
    edges: list[dict] = []
    handled: set[tuple[str, str]] = set()
    now = datetime.now(timezone.utc).isoformat()

    for property_id, statements in _qualified_statements(entity, property_map).items():
        collection, edge_type = property_map[property_id]
        target_counts: Counter[str] = Counter()

        for statement in statements:
            target_qid = statement["value"]
            if not target_qid.startswith("Q"):
                continue

            target_counts[target_qid] += 1
            count = target_counts[target_qid]
            suffix = f"_{count}" if count > 1 else ""
            from_handle, to_handle = endpoint_builder(subject_qid, target_qid, collection)
            key = sanitize_key(
                f"{_document_key(from_handle)}_{_document_key(to_handle)}_"
                f"{edge_type}_{property_id}{suffix}"
            )
            edge = {
                "_key": key,
                "_from": from_handle,
                "_to": to_handle,
                "type": edge_type,
                "wikidata_property": property_id,
                "statement_guid": statement["statement_guid"],
                "rank": statement["rank"],
                "original_subject": subject_qid,
                "original_object": target_qid,
                "start_date": statement["start_date"],
                "end_date": statement["end_date"],
                "point_in_time": statement["point_in_time"],
                "source": "wikidata_dump",
                "last_updated": now,
                "edge_collection": collection,
            }
            if include_statement_context:
                edge["named_as"] = statement.get("named_as")
                edge["ordinal"] = statement.get("ordinal")
            edges.append(edge)
            handled.add((property_id, target_qid))

    for property_id, (collection, edge_type) in property_map.items():
        for target_qid in entity["props"].get(property_id, []):
            if not target_qid.startswith("Q") or (property_id, target_qid) in handled:
                continue

            from_handle, to_handle = endpoint_builder(subject_qid, target_qid, collection)
            key = sanitize_key(
                f"{_document_key(from_handle)}_{_document_key(to_handle)}_"
                f"{edge_type}_{property_id}"
            )
            edges.append({
                "_key": key,
                "_from": from_handle,
                "_to": to_handle,
                "type": edge_type,
                "wikidata_property": property_id,
                "statement_guid": None,
                "rank": None,
                "original_subject": subject_qid,
                "original_object": target_qid,
                "start_date": None,
                "end_date": None,
                "point_in_time": None,
                "source": "wikidata_dump",
                "last_updated": now,
                "edge_collection": collection,
            })

    return edges


def _person_endpoints(person_qid: str, target_qid: str, collection: str) -> tuple[str, str]:
    target_collections = {
        RELATIONSHIPS: PERSONS,
        AFFILIATIONS: ORGANIZATIONS,
        INVOLVEMENTS: EVENTS,
    }
    target_collection = target_collections.get(collection, PERSONS)
    return f"{PERSONS}/{person_qid}", f"{target_collection}/{target_qid}"


def build_edge_docs(qid: str, entity: dict) -> list[dict]:
    """Build person-originating relationship, affiliation, and event edges."""
    return _build_property_edges(
        qid,
        entity,
        EDGE_PROPERTIES,
        _person_endpoints,
        include_statement_context=True,
    )


def build_reverse_edge_docs(
    entity_qid: str,
    entity: dict,
    target_collection: str,
    reverse_props_map: PropertyMap,
) -> list[dict]:
    """Build person edges from properties declared on an organization or event."""

    def reverse_endpoints(_: str, person_qid: str, __: str) -> tuple[str, str]:
        return f"{PERSONS}/{person_qid}", f"{target_collection}/{entity_qid}"

    return _build_property_edges(
        entity_qid,
        entity,
        reverse_props_map,
        reverse_endpoints,
        include_statement_context=True,
    )


def build_org_structure_edge_docs(org_qid: str, entity: dict) -> list[dict]:
    """Build organization-to-organization ownership and hierarchy edges."""

    def organization_endpoints(subject_qid: str, target_qid: str, _: str) -> tuple[str, str]:
        return f"{ORGANIZATIONS}/{subject_qid}", f"{ORGANIZATIONS}/{target_qid}"

    return _build_property_edges(
        org_qid,
        entity,
        ORG_STRUCTURE_PROPERTIES,
        organization_endpoints,
        include_statement_context=False,
    )
