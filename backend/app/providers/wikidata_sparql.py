"""Wikidata SPARQL API provider for fetching Italian public figures."""

import asyncio
import json
import logging
import urllib.parse
import urllib.request
from urllib.error import HTTPError

from app.config import settings
from app.models.schemas import PersonData, OrganizationData, EventData, EdgeData
from app.providers.base import DataProvider

logger = logging.getLogger(__name__)

# Wikidata occupation QIDs to query
OCCUPATION_CATEGORIES = {
    "Q82955": "politician",
    "Q43845": "businessperson",
    "Q16533": "judge",
    "Q40348": "lawyer",
    "Q1930187": "journalist",
    "Q47064": "military_person",
    "Q2159907": "criminal",
    "Q33999": "actor",
    "Q177220": "singer",
    "Q36834": "composer",
}

# Wikidata property IDs for relationship extraction
FAMILY_PROPERTIES = {
    "P26": "spouse",
    "P22": "parent",  # father
    "P25": "parent",  # mother
    "P40": "child",
    "P3373": "sibling",
}

# Phase 4.1: new person↔person relationship types (§2a)
PERSON_RELATIONSHIP_PROPERTIES = {
    "P451": "unmarried_partner",
    "P184": "doctoral_advisor",
    "P185": "doctoral_student",
    "P1327": "professional_collaborator",
}

AFFILIATION_PROPERTIES = {
    "P108": "employee",
    "P39": "position_held",
    "P102": "member_of",
    "P463": "member_of",
}

INVOLVEMENT_PROPERTIES = {
    "P1344": "participant",
    "P793": "significant_event",
}

# Phase 4.3: org→org structure edge properties (§2b).
# P127 (owned by) and P1830 (owns) must only emit when the counterpart is also
# an org — the endpoint-type filter is applied in fetch_org_structure_edges().
ORG_STRUCTURE_PROPERTIES = {
    "P749": "parent_of",      # child org → parent org
    "P355": "subsidiary_of",  # parent org → subsidiary org
    "P127": "owned_by",       # org → owner-org (org-owner only)
    "P1830": "owns",          # org → owned-org (org-org only)
}


def _sparql_query_sync(sparql: str, endpoint: str, user_agent: str) -> list[dict]:
    """Execute a SPARQL GET query using stdlib urllib. Returns bindings list."""
    params = urllib.parse.urlencode({"query": sparql, "format": "json"})
    url = f"{endpoint}?{params}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": user_agent, "Accept": "application/sparql-results+json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    return data["results"]["bindings"]


class WikidataSPARQLProvider(DataProvider):
    async def _query(self, sparql: str) -> list[dict]:
        """Execute a SPARQL query in a thread (urllib is blocking) and return bindings."""
        return await asyncio.to_thread(
            _sparql_query_sync,
            sparql,
            settings.sparql_endpoint,
            settings.sparql_user_agent,
        )

    def _val(self, binding: dict, key: str) -> str | None:
        """Extract a value from a SPARQL binding, or None."""
        if key in binding and "value" in binding[key]:
            return binding[key]["value"]
        return None

    def _qid(self, uri: str | None) -> str | None:
        """Extract QID from a Wikidata entity URI."""
        if uri and "entity/" in uri:
            return uri.split("entity/")[-1]
        return None

    async def _fetch_award_statement_counts(self, award_qids: list[str]) -> dict[str, int]:
        """Fetch wikibase:statements counts for a list of award QIDs.

        Used to rank awards_top5 by prominence (§1d / 3.5): the award entity
        with the most Wikidata statements is treated as most notable.
        Returns {qid: statement_count}.
        """
        if not award_qids:
            return {}
        counts: dict[str, int] = {}
        batch_size = 50
        for i in range(0, len(award_qids), batch_size):
            batch = award_qids[i : i + batch_size]
            values = " ".join(f"wd:{q}" for q in batch)
            sparql = f"""
            SELECT ?award ?stmtCount WHERE {{
              VALUES ?award {{ {values} }}
              ?award wikibase:statements ?stmtCount .
            }}
            """
            try:
                results = await self._query(sparql)
                for row in results:
                    qid = self._qid(self._val(row, "award"))
                    count = self._val(row, "stmtCount")
                    if qid and count:
                        counts[qid] = int(count)
            except Exception as e:
                logger.warning("Failed to fetch award statement counts: %s", e)
            await asyncio.sleep(settings.sparql_delay_seconds)
        return counts

    async def _fetch_person_rich_fields(
        self, person_qids: list[str]
    ) -> dict[str, dict]:
        """Fetch awards (P166) and notable works (P800) for a batch of persons.

        Returns {qid: {"award_qids": [...], "work_labels": [...]}}.
        These are fetched separately because SPARQL label service doesn't handle
        multi-valued entity properties well in a single SELECT.

        Award ranking (awards_top5) by statement count is applied here:
        statement counts for all encountered award QIDs are fetched in one batch.
        """
        if not person_qids:
            return {}

        values = " ".join(f"wd:{q}" for q in person_qids)
        rich: dict[str, dict] = {
            q: {"award_qids": [], "work_labels": []} for q in person_qids
        }

        # P166 — awards (collect QIDs, resolve labels + statement counts for ranking)
        awards_sparql = f"""
        SELECT ?person ?award ?awardLabel WHERE {{
          VALUES ?person {{ {values} }}
          ?person wdt:P166 ?award .
          FILTER(STRSTARTS(STR(?award), "http://www.wikidata.org/entity/Q"))
          SERVICE wikibase:label {{
            bd:serviceParam wikibase:language "en,it" .
          }}
        }}
        """
        # {person_qid: {award_qid: label}}
        person_awards: dict[str, dict[str, str]] = {}
        try:
            results = await self._query(awards_sparql)
            for row in results:
                p_qid = self._qid(self._val(row, "person"))
                a_qid = self._qid(self._val(row, "award"))
                a_label = self._val(row, "awardLabel") or a_qid
                if p_qid and a_qid:
                    person_awards.setdefault(p_qid, {})[a_qid] = a_label
        except Exception as e:
            logger.warning("Failed to fetch P166 awards: %s", e)
        await asyncio.sleep(settings.sparql_delay_seconds)

        # Fetch statement counts for all unique award QIDs (for ranking)
        all_award_qids = list({q for awards in person_awards.values() for q in awards})
        award_counts = await self._fetch_award_statement_counts(all_award_qids)

        for p_qid, awards_map in person_awards.items():
            if p_qid not in rich:
                continue
            rich[p_qid]["award_qids"] = list(awards_map.keys())
            # Sort by statement count DESC, take top 5
            sorted_awards = sorted(
                awards_map.items(),
                key=lambda kv: award_counts.get(kv[0], 0),
                reverse=True,
            )
            rich[p_qid]["awards_top5"] = [label for _, label in sorted_awards[:5]]

        # P800 — notable works (cap 10, resolve labels)
        works_sparql = f"""
        SELECT ?person ?work ?workLabel WHERE {{
          VALUES ?person {{ {values} }}
          ?person wdt:P800 ?work .
          FILTER(STRSTARTS(STR(?work), "http://www.wikidata.org/entity/Q"))
          SERVICE wikibase:label {{
            bd:serviceParam wikibase:language "en,it" .
          }}
        }}
        LIMIT 500
        """
        person_works: dict[str, list[str]] = {}
        try:
            results = await self._query(works_sparql)
            for row in results:
                p_qid = self._qid(self._val(row, "person"))
                label = self._val(row, "workLabel")
                if p_qid and label:
                    bucket = person_works.setdefault(p_qid, [])
                    if label not in bucket and len(bucket) < 10:
                        bucket.append(label)
        except Exception as e:
            logger.warning("Failed to fetch P800 notable works: %s", e)
        await asyncio.sleep(settings.sparql_delay_seconds)

        for p_qid, works in person_works.items():
            if p_qid in rich:
                rich[p_qid]["work_labels"] = works

        return rich

    async def fetch_persons(self, **filters) -> list[PersonData]:
        """Fetch Italian persons by occupation categories.

        Phase 5.1: also fetches awards (P166) and notable_works (P800) for persons
        meeting the rich_fields_min_statements gate (default 50 statements).
        Award top-5 is ranked by the award entity's own statement count.
        """
        occupation_qids = filters.get("occupation_qids", list(OCCUPATION_CATEGORIES.keys()))
        all_persons: dict[str, PersonData] = {}

        for occ_qid in occupation_qids:
            occ_label = OCCUPATION_CATEGORIES.get(occ_qid, "unknown")
            logger.info("Fetching Italian %s (occupation %s)...", occ_label, occ_qid)

            sparql = f"""
            SELECT ?person ?personLabel ?personDescription ?descriptionIt
                   ?birthDate ?deathDate ?image ?stmtCount
                   ?nameItLabel ?personLabelIt ?causeOfDeathLabel ?genderLabel
            WHERE {{
              ?person wdt:P27 wd:Q38 .
              ?person wdt:P106 wd:{occ_qid} .
              ?person wikibase:statements ?stmtCount .
              FILTER(?stmtCount >= {settings.notability_min_statements})
              OPTIONAL {{ ?person wdt:P569 ?birthDate . }}
              OPTIONAL {{ ?person wdt:P570 ?deathDate . }}
              OPTIONAL {{ ?person wdt:P18 ?image . }}
              OPTIONAL {{ ?person wdt:P1559 ?nameIt . }}
              OPTIONAL {{ ?person wdt:P509 ?causeOfDeath . }}
              OPTIONAL {{ ?person wdt:P21 ?gender . }}
              OPTIONAL {{ ?person rdfs:label ?personLabelIt . FILTER(LANG(?personLabelIt) = "it") }}
              OPTIONAL {{ ?person schema:description ?descriptionIt . FILTER(LANG(?descriptionIt) = "it") }}
              SERVICE wikibase:label {{
                bd:serviceParam wikibase:language "en,it" .
              }}
            }}
            LIMIT 1000
            """

            try:
                results = await self._query(sparql)
            except Exception as e:
                logger.error("SPARQL query failed for %s: %s", occ_qid, e)
                await asyncio.sleep(settings.sparql_delay_seconds)
                continue

            for row in results:
                uri = self._val(row, "person")
                qid = self._qid(uri)
                if not qid or qid in all_persons:
                    continue

                person = PersonData(
                    wikidata_id=qid,
                    name_en=self._val(row, "personLabel") or qid,
                    name_it=self._val(row, "nameItLabel") or self._val(row, "personLabelIt"),
                    description_en=self._val(row, "personDescription"),
                    description_it=self._val(row, "descriptionIt"),
                    birth_date=self._val(row, "birthDate"),
                    death_date=self._val(row, "deathDate"),
                    image_url=self._val(row, "image"),
                    wikidata_statements_count=int(self._val(row, "stmtCount") or 0),
                    occupations=[occ_label],
                    nationalities=["Italian"],
                    cause_of_death=self._val(row, "causeOfDeathLabel"),
                    gender=self._val(row, "genderLabel"),
                    source=self.get_provider_name(),
                )
                all_persons[qid] = person

            logger.info("Got %d total persons so far (after %s)", len(all_persons), occ_label)
            await asyncio.sleep(settings.sparql_delay_seconds)

        # Fetch rich fields (P166 awards, P800 notable_works) for persons meeting
        # the statements gate — same threshold as the dump loader (§1a / 3.1).
        rich_threshold = settings.rich_fields_min_statements
        rich_qids = [
            qid for qid, p in all_persons.items()
            if (p.wikidata_statements_count or 0) >= rich_threshold
        ]
        if rich_qids:
            logger.info(
                "Fetching rich fields (awards/works) for %d persons (≥%d statements)",
                len(rich_qids), rich_threshold,
            )
            batch_size = 50
            for i in range(0, len(rich_qids), batch_size):
                batch = rich_qids[i : i + batch_size]
                try:
                    rich_data = await self._fetch_person_rich_fields(batch)
                    for qid, rdata in rich_data.items():
                        if qid not in all_persons:
                            continue
                        person = all_persons[qid]
                        award_qids = rdata.get("award_qids", [])
                        if award_qids:
                            person.awards_count = len(award_qids)
                            person.awards_top5 = rdata.get("awards_top5", [])
                        works = rdata.get("work_labels", [])
                        if works:
                            person.notable_works = works
                except Exception as e:
                    logger.warning("Failed to fetch rich fields batch: %s", e)

        return list(all_persons.values())

    async def fetch_organizations(self, person_ids: list[str]) -> list[OrganizationData]:
        """Fetch organizations (parties, employers) linked to persons.

        Phase 5.1: now also fetches P856 (website), P159 (headquarters + QID),
        P1128 (employees_count), P17 (country + QID) for each org (§1b / 3.2).
        """
        orgs: dict[str, OrganizationData] = {}

        # Batch person IDs to avoid huge queries
        batch_size = 50
        for i in range(0, len(person_ids), batch_size):
            batch = person_ids[i : i + batch_size]
            values = " ".join(f"wd:{qid}" for qid in batch)

            sparql = f"""
            SELECT DISTINCT ?org ?orgLabel ?orgLabelIt ?orgDescription ?orgDescriptionIt
                   ?inception ?dissolved ?website ?hq ?hqLabel ?employees ?country ?countryLabel
            WHERE {{
              VALUES ?person {{ {values} }}
              {{
                ?person wdt:P102 ?org .
              }} UNION {{
                ?person wdt:P108 ?org .
              }} UNION {{
                ?person wdt:P463 ?org .
              }}
              OPTIONAL {{ ?org wdt:P571 ?inception . }}
              OPTIONAL {{ ?org wdt:P576 ?dissolved . }}
              OPTIONAL {{ ?org wdt:P856 ?website . }}
              OPTIONAL {{ ?org wdt:P159 ?hq . }}
              OPTIONAL {{ ?org wdt:P1128 ?employees . }}
              OPTIONAL {{ ?org wdt:P17 ?country . }}
              OPTIONAL {{ ?org rdfs:label ?orgLabelIt . FILTER(LANG(?orgLabelIt) = "it") }}
              OPTIONAL {{ ?org schema:description ?orgDescriptionIt . FILTER(LANG(?orgDescriptionIt) = "it") }}
              SERVICE wikibase:label {{
                bd:serviceParam wikibase:language "en,it" .
              }}
            }}
            """

            try:
                results = await self._query(sparql)
                for row in results:
                    uri = self._val(row, "org")
                    qid = self._qid(uri)
                    if not qid or qid in orgs:
                        continue
                    # Parse employees_count safely
                    employees_raw = self._val(row, "employees")
                    employees_count: int | None = None
                    if employees_raw:
                        try:
                            employees_count = int(float(employees_raw))
                        except (ValueError, TypeError):
                            pass
                    orgs[qid] = OrganizationData(
                        wikidata_id=qid,
                        name_en=self._val(row, "orgLabel") or qid,
                        name_it=self._val(row, "orgLabelIt"),
                        description_en=self._val(row, "orgDescription"),
                        description_it=self._val(row, "orgDescriptionIt"),
                        type="organization",
                        founded=self._val(row, "inception"),
                        dissolved=self._val(row, "dissolved"),
                        website=self._val(row, "website"),
                        headquarters=self._val(row, "hqLabel"),
                        headquarters_id=self._qid(self._val(row, "hq")),
                        employees_count=employees_count,
                        country=self._val(row, "countryLabel"),
                        country_id=self._qid(self._val(row, "country")),
                        source=self.get_provider_name(),
                    )
            except Exception as e:
                logger.error("Failed to fetch organizations batch: %s", e)

            await asyncio.sleep(settings.sparql_delay_seconds)

            # Also fetch P39 (position_held) targets — offices like "Presidente del Consiglio"
            pos_sparql = f"""
            SELECT DISTINCT ?org ?orgLabel ?orgLabelIt ?orgDescription ?orgDescriptionIt WHERE {{
              VALUES ?person {{ {values} }}
              ?person wdt:P39 ?org .
              OPTIONAL {{ ?org rdfs:label ?orgLabelIt . FILTER(LANG(?orgLabelIt) = "it") }}
              OPTIONAL {{ ?org schema:description ?orgDescriptionIt . FILTER(LANG(?orgDescriptionIt) = "it") }}
              SERVICE wikibase:label {{
                bd:serviceParam wikibase:language "en,it" .
              }}
            }}
            """
            try:
                pos_results = await self._query(pos_sparql)
                for row in pos_results:
                    uri = self._val(row, "org")
                    qid = self._qid(uri)
                    if not qid or qid in orgs:
                        continue
                    orgs[qid] = OrganizationData(
                        wikidata_id=qid,
                        name_en=self._val(row, "orgLabel") or qid,
                        name_it=self._val(row, "orgLabelIt"),
                        description_en=self._val(row, "orgDescription"),
                        description_it=self._val(row, "orgDescriptionIt"),
                        type="position",
                        source=self.get_provider_name(),
                    )
            except Exception as e:
                logger.error("Failed to fetch positions batch: %s", e)

            await asyncio.sleep(settings.sparql_delay_seconds)

        return list(orgs.values())

    async def fetch_events(self, person_ids: list[str]) -> list[EventData]:
        """Fetch events linked to persons.

        Phase 5.1: now also fetches P276 (location + QID), P1120 (deaths),
        P495 (country + QID) for each event (§1c / 3.3).
        """
        events: dict[str, EventData] = {}

        batch_size = 50
        for i in range(0, len(person_ids), batch_size):
            batch = person_ids[i : i + batch_size]
            values = " ".join(f"wd:{qid}" for qid in batch)

            sparql = f"""
            SELECT DISTINCT ?event ?eventLabel ?eventLabelIt ?eventDescription ?eventDescriptionIt
                   ?date ?location ?locationLabel ?deaths ?country ?countryLabel
            WHERE {{
              VALUES ?person {{ {values} }}
              {{
                ?person wdt:P1344 ?event .
              }} UNION {{
                ?person wdt:P793 ?event .
              }}
              OPTIONAL {{ ?event wdt:P585 ?date . }}
              OPTIONAL {{ ?event wdt:P276 ?location . }}
              OPTIONAL {{ ?event wdt:P1120 ?deaths . }}
              OPTIONAL {{ ?event wdt:P495 ?country . }}
              OPTIONAL {{ ?event rdfs:label ?eventLabelIt . FILTER(LANG(?eventLabelIt) = "it") }}
              OPTIONAL {{ ?event schema:description ?eventDescriptionIt . FILTER(LANG(?eventDescriptionIt) = "it") }}
              SERVICE wikibase:label {{
                bd:serviceParam wikibase:language "en,it" .
              }}
            }}
            """

            try:
                results = await self._query(sparql)
                for row in results:
                    uri = self._val(row, "event")
                    qid = self._qid(uri)
                    if not qid or qid in events:
                        continue
                    # Parse deaths safely
                    deaths_raw = self._val(row, "deaths")
                    deaths: int | None = None
                    if deaths_raw:
                        try:
                            deaths = int(float(deaths_raw))
                        except (ValueError, TypeError):
                            pass
                    events[qid] = EventData(
                        wikidata_id=qid,
                        name_en=self._val(row, "eventLabel") or qid,
                        name_it=self._val(row, "eventLabelIt"),
                        type="event",
                        date=self._val(row, "date"),
                        description_en=self._val(row, "eventDescription"),
                        description_it=self._val(row, "eventDescriptionIt"),
                        location=self._val(row, "locationLabel"),
                        location_id=self._qid(self._val(row, "location")),
                        deaths=deaths,
                        country=self._val(row, "countryLabel"),
                        country_id=self._qid(self._val(row, "country")),
                        source=self.get_provider_name(),
                    )
            except Exception as e:
                logger.error("Failed to fetch events batch: %s", e)

            await asyncio.sleep(settings.sparql_delay_seconds)

        return list(events.values())

    async def fetch_relationships(self, person_ids: list[str]) -> list[EdgeData]:
        """Fetch all edges (family, affiliations, involvements) for given persons.

        Phase 5.1: also fetches the 4 new person↔person relationship types from §2a:
          P451 (unmarried_partner), P184 (doctoral_advisor),
          P185 (doctoral_student), P1327 (professional_collaborator).
        These join FAMILY_PROPERTIES in the `relationships` collection.
        """
        edges: list[EdgeData] = []

        # All person→person properties (family + new phase-4 types)
        all_person_person_props = {**FAMILY_PROPERTIES, **PERSON_RELATIONSHIP_PROPERTIES}

        batch_size = 50
        for i in range(0, len(person_ids), batch_size):
            batch = person_ids[i : i + batch_size]
            values = " ".join(f"wd:{qid}" for qid in batch)

            # Person → person relationships (family + new types)
            # Note: the nationality filter (?related wdt:P27 wd:Q38) is kept only for
            # family properties to match original behavior; for the new academic/professional
            # types we skip it since doctoral_advisor, doctoral_student, professional_collaborator
            # are useful regardless of nationality. The dump loader also doesn't filter.
            for prop_id, rel_type in FAMILY_PROPERTIES.items():
                sparql = f"""
                SELECT ?person ?related WHERE {{
                  VALUES ?person {{ {values} }}
                  ?person wdt:{prop_id} ?related .
                  ?related wdt:P31 wd:Q5 .
                }}
                """
                try:
                    results = await self._query(sparql)
                    for row in results:
                        from_qid = self._qid(self._val(row, "person"))
                        to_qid = self._qid(self._val(row, "related"))
                        if from_qid and to_qid:
                            edges.append(EdgeData(
                                from_id=from_qid,
                                to_id=to_qid,
                                edge_collection="relationships",
                                type=rel_type,
                                wikidata_property=prop_id,
                                source=self.get_provider_name(),
                            ))
                except Exception as e:
                    logger.error("Failed to fetch %s edges: %s", prop_id, e)

                await asyncio.sleep(settings.sparql_delay_seconds)

            # New person↔person types (§2a / 4.1) — no nationality filter
            for prop_id, rel_type in PERSON_RELATIONSHIP_PROPERTIES.items():
                sparql = f"""
                SELECT ?person ?related WHERE {{
                  VALUES ?person {{ {values} }}
                  ?person wdt:{prop_id} ?related .
                  ?related wdt:P31 wd:Q5 .
                }}
                """
                try:
                    results = await self._query(sparql)
                    for row in results:
                        from_qid = self._qid(self._val(row, "person"))
                        to_qid = self._qid(self._val(row, "related"))
                        if from_qid and to_qid:
                            edges.append(EdgeData(
                                from_id=from_qid,
                                to_id=to_qid,
                                edge_collection="relationships",
                                type=rel_type,
                                wikidata_property=prop_id,
                                source=self.get_provider_name(),
                            ))
                except Exception as e:
                    logger.error("Failed to fetch %s edges: %s", prop_id, e)

                await asyncio.sleep(settings.sparql_delay_seconds)

            # Affiliations (person → organization)
            for prop_id, aff_type in AFFILIATION_PROPERTIES.items():
                sparql = f"""
                SELECT ?person ?org WHERE {{
                  VALUES ?person {{ {values} }}
                  ?person wdt:{prop_id} ?org .
                }}
                """
                try:
                    results = await self._query(sparql)
                    for row in results:
                        from_qid = self._qid(self._val(row, "person"))
                        to_qid = self._qid(self._val(row, "org"))
                        if from_qid and to_qid:
                            edges.append(EdgeData(
                                from_id=from_qid,
                                to_id=to_qid,
                                edge_collection="affiliations",
                                type=aff_type,
                                wikidata_property=prop_id,
                                source=self.get_provider_name(),
                            ))
                except Exception as e:
                    logger.error("Failed to fetch %s edges: %s", prop_id, e)

                await asyncio.sleep(settings.sparql_delay_seconds)

            # Involvements (person → event)
            for prop_id, inv_type in INVOLVEMENT_PROPERTIES.items():
                sparql = f"""
                SELECT ?person ?event WHERE {{
                  VALUES ?person {{ {values} }}
                  ?person wdt:{prop_id} ?event .
                }}
                """
                try:
                    results = await self._query(sparql)
                    for row in results:
                        from_qid = self._qid(self._val(row, "person"))
                        to_qid = self._qid(self._val(row, "event"))
                        if from_qid and to_qid:
                            edges.append(EdgeData(
                                from_id=from_qid,
                                to_id=to_qid,
                                edge_collection="involvements",
                                type=inv_type,
                                wikidata_property=prop_id,
                                source=self.get_provider_name(),
                            ))
                except Exception as e:
                    logger.error("Failed to fetch %s edges: %s", prop_id, e)

                await asyncio.sleep(settings.sparql_delay_seconds)

        return edges

    async def fetch_org_structure_edges(self, org_ids: list[str]) -> list[EdgeData]:
        """Fetch org→org structure edges (P749/P355/P127/P1830) for given org QIDs.

        Phase 5.1 — §2b / 4.3 SPARQL parity.

        The endpoint-type filter (only emit when the counterpart is also an org)
        is enforced via a SPARQL FILTER on the counterpart's P31 value.  We
        restrict to counterparts that are NOT instances of Q5 (human) — a simpler
        proxy than enumerating all org P31 QIDs, and safe because the main reason
        to exclude is person-owned orgs.
        """
        if not org_ids:
            return []

        edges: list[EdgeData] = []
        batch_size = 50

        for i in range(0, len(org_ids), batch_size):
            batch = org_ids[i : i + batch_size]
            values = " ".join(f"wd:{q}" for q in batch)

            for prop_id, rel_type in ORG_STRUCTURE_PROPERTIES.items():
                # FILTER: counterpart must not be a human (Q5) — proxy for org-only check.
                # This matches the dump loader's endpoint-type branch which skips edges
                # where the other side is in the persons collection.
                sparql = f"""
                SELECT ?org ?counterpart WHERE {{
                  VALUES ?org {{ {values} }}
                  ?org wdt:{prop_id} ?counterpart .
                  FILTER(STRSTARTS(STR(?counterpart), "http://www.wikidata.org/entity/Q"))
                  FILTER NOT EXISTS {{ ?counterpart wdt:P31 wd:Q5 . }}
                }}
                """
                try:
                    results = await self._query(sparql)
                    for row in results:
                        from_qid = self._qid(self._val(row, "org"))
                        to_qid = self._qid(self._val(row, "counterpart"))
                        if from_qid and to_qid:
                            edges.append(EdgeData(
                                from_id=from_qid,
                                to_id=to_qid,
                                edge_collection="org_structure",
                                type=rel_type,
                                wikidata_property=prop_id,
                                source=self.get_provider_name(),
                            ))
                except Exception as e:
                    logger.error(
                        "Failed to fetch org_structure %s edges: %s", prop_id, e
                    )

                await asyncio.sleep(settings.sparql_delay_seconds)

        return edges

    def get_provider_name(self) -> str:
        return "wikidata_sparql"
