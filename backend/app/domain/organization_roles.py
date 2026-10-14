"""Canonical organization-role taxonomy and temporal presentation rules."""

from __future__ import annotations

import re
from datetime import date

# Wikidata properties declared on organizations whose values are people.
# Ingestion reverses these into the graph's canonical person-to-organization edge.
WIKIDATA_ORGANIZATION_PROPERTY_ROLES: dict[str, str] = {
    "P169": "chief_executive",
    "P3320": "board_member",
    "P112": "founder",
    "P488": "chairperson",
    "P127": "owner",
}

PUBLIC_ORGANIZATION_ROLE_TYPES: tuple[str, ...] = (
    "founder",
    "owner",
    "chief_executive",
    "chairperson",
    "board_member",
    "employee",
    "member_of",
    "sports_team_member",
    "educated_at",
)

PUBLIC_ORGANIZATION_ROLE_PRIORITY: dict[str, int] = {
    role: priority
    for priority, role in enumerate(PUBLIC_ORGANIZATION_ROLE_TYPES)
}

PUBLIC_ORGANIZATION_ROLE_PHRASES: dict[str, str] = {
    "founder": "a founder",
    "owner": "an owner",
    "chief_executive": "chief executive",
    "chairperson": "chairperson",
    "board_member": "a board member",
    "employee": "an employee",
    "member_of": "a member",
    "sports_team_member": "a team member",
}

SPECIFIC_PUBLIC_ORGANIZATION_ROLES: frozenset[str] = frozenset({
    "founder",
    "owner",
    "chief_executive",
    "chairperson",
    "board_member",
})

FEED_ORGANIZATION_ROLE_LABELS: dict[str, str | None] = {
    "founder": "Founder",
    "chief_executive": "CEO",
    "chairperson": "Chair",
    "director_of": "Director",
    "board_member": "Board member",
    "officer_of": "Officer",
    "owner": "Owner",
    "beneficial_owner_of": "Owner",
    "holds_shares_of": "Shareholder",
    "insider_of": None,
    "employee": None,
}

FEED_ORGANIZATION_ROLE_PRIORITY: tuple[str, ...] = (
    "Founder",
    "CEO",
    "Former CEO",
    "Chair",
    "Director",
    "Board member",
    "Officer",
    "Owner",
    "Shareholder",
)

_PARTIAL_DATE_RE = re.compile(
    r"^(?P<year>\d{4})(?:-(?P<month>\d{2})(?:-(?P<day>\d{2}))?)?(?:T.*)?$"
)


def relationship_has_ended(end_date: object, *, today: date | None = None) -> bool:
    """Return True only when an end date is unambiguously in the past.

    Wikidata frequently uses partial dates such as 2018-00-00. A past year or
    month is enough to establish that a role ended; an unknown day in the
    current month is deliberately treated as inconclusive.
    """
    match = _PARTIAL_DATE_RE.match(str(end_date or ""))
    if not match or match.group("year") == "0000":
        return False

    current = today or date.today()
    year = int(match.group("year"))
    month = int(match.group("month") or 0)
    day = int(match.group("day") or 0)

    if month not in range(13) or (month == 0 and day != 0):
        return False
    if day:
        try:
            date(year, month, day)
        except ValueError:
            return False

    if year != current.year:
        return year < current.year

    if month == 0:
        return False
    if month != current.month:
        return month < current.month

    return day != 0 and day < current.day


def public_role_phrase(edge_type: str, end_date: object = None) -> str | None:
    """Return factual English profile wording for an organization role."""
    if edge_type == "chief_executive" and relationship_has_ended(end_date):
        return "a former chief executive"
    return PUBLIC_ORGANIZATION_ROLE_PHRASES.get(edge_type)


def feed_role_label(edge_type: str, end_date: object = None) -> str | None:
    """Return the compact feed label, distinguishing ended CEO appointments."""
    label = FEED_ORGANIZATION_ROLE_LABELS.get(edge_type)
    if label == "CEO" and relationship_has_ended(end_date):
        return "Former CEO"
    return label
