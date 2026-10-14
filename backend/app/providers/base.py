"""Abstract base class for data providers."""

from abc import ABC, abstractmethod

from app.models.schemas import PersonData, OrganizationData, EventData, EdgeData


class DataProvider(ABC):
    """All data providers implement this interface.

    The ETL orchestrator consumes normalized entities and edges
    regardless of which provider produced them.
    """

    @abstractmethod
    async def fetch_persons(self, **filters) -> list[PersonData]:
        """Fetch person entities, optionally filtered."""
        ...

    @abstractmethod
    async def fetch_organizations(self, person_ids: list[str]) -> list[OrganizationData]:
        """Fetch organizations related to the given persons."""
        ...

    @abstractmethod
    async def fetch_events(self, person_ids: list[str]) -> list[EventData]:
        """Fetch events related to the given persons."""
        ...

    @abstractmethod
    async def fetch_relationships(self, person_ids: list[str]) -> list[EdgeData]:
        """Fetch all edges (relationships, affiliations, involvements) for given persons."""
        ...

    @abstractmethod
    def get_provider_name(self) -> str:
        """Return a unique identifier for this provider (e.g. 'wikidata_sparql')."""
        ...
