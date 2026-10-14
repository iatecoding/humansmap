"""Seed the small deterministic dataset used by GitHub Actions integration tests.

This is intentionally separate from production ingestion.  It supplies enough
shape and variety for endpoint contracts (including the trivia minimum of 20
people in one nationality) without downloading the public Wikidata dump.
"""

from __future__ import annotations

from app.database import (
    AFFILIATIONS,
    EVENTS,
    INVOLVEMENTS,
    ORGANIZATIONS,
    PERSONS,
    RELATIONSHIPS,
    init_db,
)


def seed() -> None:
    db = init_db()

    # Keep fixtures inside the same identifier contract enforced by the API:
    # Wikidata IDs are a Q followed exclusively by decimal digits. These high
    # deterministic values live only in the isolated CI database.
    person_qid = lambda index: f"Q990000{index:03d}"
    organization_qid = "Q990000101"
    event_qid = "Q990000201"

    persons = [
        {
            "_key": person_qid(index),
            "name_en": f"CI Test Person {index:02d}",
            "name_it": f"Persona CI Test {index:02d}",
            "description_en": f"A deterministic CI test subject, number {index:02d}.",
            "description_it": f"Soggetto di test CI deterministico numero {index:02d}.",
            "occupations": ["actor" if index <= 10 else "politician"],
            "primary_occupation": "actor" if index <= 10 else "politician",
            "nationalities": ["US"],
            "gender": "male" if index % 2 else "female",
            "birth_date": f"198{index % 10}-01-01",
            "death_date": None,
            "wikidata_statements_count": 100 + index,
        }
        for index in range(1, 21)
    ]
    organizations = [{
        "_key": organization_qid,
        "name_en": "CI Test Organization",
        "name_it": "Organizzazione CI Test",
        "type": "company",
        "country_id": "Q30",
        "wikidata_statements_count": 100,
    }]
    events = [{
        "_key": event_qid,
        "name_en": "CI Test Event",
        "name_it": "Evento CI Test",
        "type": "event",
        "date": "2024-01-01",
    }]

    for collection_name, documents in (
        (PERSONS, persons),
        (ORGANIZATIONS, organizations),
        (EVENTS, events),
    ):
        collection = db.collection(collection_name)
        collection.insert_many(documents, overwrite=True)

    db.collection(AFFILIATIONS).insert_many([
        {
            "_key": f"{person_qid(index)}_{organization_qid}",
            "_from": f"persons/{person_qid(index)}",
            "_to": f"organizations/{organization_qid}",
            "type": "member_of",
            "role": "member",
        }
        for index in range(1, 21)
    ], overwrite=True)
    db.collection(RELATIONSHIPS).insert({
        "_key": f"{person_qid(1)}_{person_qid(2)}_spouse",
        "_from": f"persons/{person_qid(1)}",
        "_to": f"persons/{person_qid(2)}",
        "type": "spouse",
    }, overwrite=True)
    db.collection(INVOLVEMENTS).insert({
        "_key": f"{event_qid}_{organization_qid}",
        "_from": f"events/{event_qid}",
        "_to": f"organizations/{organization_qid}",
        "type": "filer",
    }, overwrite=True)


if __name__ == "__main__":
    seed()
