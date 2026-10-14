import scripts.incremental_update as updater


class FakeCollection:
    def __init__(self, existing=()):
        self.existing = set(existing)
        self.writes = []

    def has(self, qid):
        return qid in self.existing

    def insert(self, doc):
        self.writes.append(("insert", doc))

    def update(self, doc):
        self.writes.append(("update", doc))


class FakeDb:
    def __init__(self, *, persons=(), organizations=(), events=()):
        self.collections = {
            updater.PERSONS: FakeCollection(persons),
            updater.ORGANIZATIONS: FakeCollection(organizations),
            updater.EVENTS: FakeCollection(events),
        }

    def collection(self, name):
        return self.collections[name]


def test_org_fetch_excludes_human_targets(monkeypatch):
    captured = {}
    monkeypatch.setattr(updater, "_sparql_get", lambda query: captured.setdefault("query", query) and [])
    updater.fetch_orgs_for_persons(["Q22686"])
    assert "FILTER NOT EXISTS { ?org wdt:P31 wd:Q5 . }" in captured["query"]


def test_event_fetch_requires_event_type(monkeypatch):
    captured = {}
    monkeypatch.setattr(updater, "_sparql_get", lambda query: captured.setdefault("query", query) and [])
    updater.fetch_events_for_persons(["Q22686"])
    assert "?event wdt:P31/wdt:P279* wd:Q1656682 ." in captured["query"]


def test_upsert_org_refuses_cross_collection_duplicate():
    db = FakeDb(persons={"Q5593"})
    updater.upsert_org(db, "Q5593", {})
    assert db.collection(updater.ORGANIZATIONS).writes == []


def test_upsert_event_refuses_cross_collection_duplicate():
    db = FakeDb(organizations={"Q7983"})
    updater.upsert_event(db, "Q7983", {})
    assert db.collection(updater.EVENTS).writes == []
