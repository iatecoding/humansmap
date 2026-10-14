"""Pydantic models for data transfer between providers, ETL, and API."""

from __future__ import annotations

from datetime import datetime
from pydantic import BaseModel


class PersonData(BaseModel):
    """Normalized person data from any provider."""
    wikidata_id: str
    name_en: str
    name_it: str | None = None
    description_en: str | None = None
    description_it: str | None = None
    birth_date: str | None = None
    death_date: str | None = None
    image_url: str | None = None
    image_license: str | None = None
    image_license_url: str | None = None
    image_author: str | None = None
    image_credit: str | None = None
    image_source_url: str | None = None
    image_filename: str | None = None
    image_attribution_required: bool | None = None
    image_modified: bool | None = None
    image_metadata_retrieved_at: str | None = None
    image_status: str | None = None
    image_cached_path: str | None = None
    wikidata_statements_count: int | None = None
    occupations: list[str] = []
    nationalities: list[str] = []
    cause_of_death: str | None = None
    gender: str | None = None
    birth_place: str | None = None
    birth_place_id: str | None = None
    death_place: str | None = None
    death_place_id: str | None = None
    awards_count: int | None = None
    awards_top5: list[str] = []
    notable_works: list[str] = []
    sec_cik: str | None = None
    source: str = "wikidata_sparql"


class OrganizationData(BaseModel):
    wikidata_id: str
    name_en: str
    name_it: str | None = None
    description_en: str | None = None
    description_it: str | None = None
    type: str | None = None
    founded: str | None = None
    dissolved: str | None = None
    image_url: str | None = None        # logo from Wikidata P154
    image_license: str | None = None
    image_license_url: str | None = None
    image_author: str | None = None
    image_credit: str | None = None
    image_source_url: str | None = None
    image_filename: str | None = None
    image_attribution_required: bool | None = None
    image_modified: bool | None = None
    image_metadata_retrieved_at: str | None = None
    image_status: str | None = None
    image_cached_path: str | None = None
    wikidata_statements_count: int | None = None
    website: str | None = None
    headquarters: str | None = None
    headquarters_id: str | None = None
    employees_count: int | None = None
    country: str | None = None
    country_id: str | None = None
    sec_cik: str | None = None
    sec_ticker: str | None = None
    sec_exchange: str | None = None
    legal_name_en: str | None = None
    source: str = "wikidata_sparql"


class EventData(BaseModel):
    wikidata_id: str
    name_en: str
    name_it: str | None = None
    type: str | None = None
    date: str | None = None
    description_en: str | None = None
    description_it: str | None = None
    location: str | None = None
    location_id: str | None = None
    deaths: int | None = None
    country: str | None = None
    country_id: str | None = None
    source: str = "wikidata_sparql"


class EdgeData(BaseModel):
    """Normalized edge data from any provider."""
    from_id: str  # QID of source node
    to_id: str  # QID of target node
    edge_collection: str  # "relationships", "affiliations", or "involvements"
    type: str  # e.g. "spouse", "member_of", "victim"
    wikidata_property: str | None = None
    role: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    source: str = "wikidata_sparql"


class SyncMetadata(BaseModel):
    provider: str
    started_at: datetime
    completed_at: datetime | None = None
    persons_processed: int = 0
    edges_processed: int = 0
    images_processed: int = 0
    errors: list[str] = []
