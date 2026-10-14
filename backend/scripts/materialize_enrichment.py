"""Disk-backed late-label enrichment for the global candidate materializer."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable

from app.config import settings
from app.database import EVENTS, ORGANIZATIONS, PERSONS
from scripts.load_from_dump import (
    ORG_QID_LABEL_PROPS,
    PERSON_PLACE_LABEL_PROPS,
    PERSON_QID_LABEL_PROPS,
    best_label,
)

ENRICHMENT_FLUSH_SIZE = 10_000
SQLITE_IN_LIMIT = 900


class EnrichmentStore:
    """Keep labels/pending references on disk and patch Arango in bulk."""

    def __init__(self, path: Path):
        self.path = path
        for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
            if candidate.exists():
                candidate.unlink()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA temp_store=FILE")
        self.db.execute("PRAGMA cache_size=-32768")
        self.db.executescript(
            """
            CREATE TABLE labels (
                qid INTEGER PRIMARY KEY,
                en TEXT,
                it TEXT,
                statements INTEGER NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE pending (
                qid INTEGER PRIMARY KEY,
                kind TEXT NOT NULL,
                direct_count INTEGER NOT NULL,
                payload TEXT NOT NULL
            ) WITHOUT ROWID;
            """
        )
        self._labels: list[tuple[int, str | None, str | None, int]] = []
        self._pending: list[tuple[int, str, int, str]] = []

    @staticmethod
    def _number(qid: str | None) -> int | None:
        if isinstance(qid, str) and qid.startswith("Q") and qid[1:].isdigit():
            return int(qid[1:])
        return None

    def record_entity(self, qid: str, entity: dict) -> None:
        number = self._number(qid)
        if number is None:
            return
        labels = entity.get("labels") or {}
        self._labels.append(
            (
                number,
                best_label(labels),
                labels.get("it"),
                int(entity.get("direct_count") or 0),
            )
        )
        if len(self._labels) >= ENRICHMENT_FLUSH_SIZE:
            self.flush()

    def record_pending(self, qid: str, kind: str, direct_count: int, payload: dict) -> None:
        number = self._number(qid)
        if number is None or not payload or not any(payload.values()):
            return
        self._pending.append(
            (
                number,
                kind,
                int(direct_count),
                json.dumps(payload, separators=(",", ":")),
            )
        )
        if len(self._pending) >= ENRICHMENT_FLUSH_SIZE:
            self.flush()

    def flush(self) -> None:
        if self._labels:
            self.db.executemany(
                """
                INSERT INTO labels(qid, en, it, statements) VALUES (?, ?, ?, ?)
                ON CONFLICT(qid) DO UPDATE SET
                    en=excluded.en, it=excluded.it, statements=excluded.statements
                """,
                self._labels,
            )
            self._labels.clear()
        if self._pending:
            self.db.executemany(
                """
                INSERT INTO pending(qid, kind, direct_count, payload)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(qid) DO UPDATE SET
                    kind=excluded.kind,
                    direct_count=excluded.direct_count,
                    payload=excluded.payload
                """,
                self._pending,
            )
            self._pending.clear()
        self.db.commit()

    def _labels_for(
        self, qids: Iterable[str]
    ) -> dict[str, tuple[str | None, str | None, int]]:
        numbers = []
        for qid in dict.fromkeys(qids):
            number = self._number(qid)
            if number is not None:
                numbers.append(number)
        result: dict[str, tuple[str | None, str | None, int]] = {}
        for offset in range(0, len(numbers), SQLITE_IN_LIMIT):
            chunk = numbers[offset : offset + SQLITE_IN_LIMIT]
            placeholders = ",".join("?" for _ in chunk)
            rows = self.db.execute(
                f"SELECT qid, en, it, statements FROM labels WHERE qid IN ({placeholders})",
                chunk,
            )
            for number, en, it, statements in rows:
                result[f"Q{number}"] = (en, it, int(statements))
        return result

    @staticmethod
    def _label(
        labels: dict[str, tuple[str | None, str | None, int]],
        qid: str | None,
        *,
        place: bool = False,
    ) -> str | None:
        row = labels.get(qid or "")
        if not row:
            return None
        en, it, _statements = row
        return (it or en) if place else (en or it)

    @staticmethod
    def _qid_values(entity: dict, properties: Iterable[str]) -> dict[str, list[str]]:
        values = {}
        for property_id in properties:
            qids = [
                value
                for value in entity["props"].get(property_id, [])
                if isinstance(value, str) and value.startswith("Q")
            ]
            if qids:
                values[property_id] = qids
        return values

    def pending_for_entity(self, qid: str, entity: dict, category: str) -> None:
        direct_count = int(entity["direct_count"])
        if category == "person":
            payload = {
                "birth": next(
                    (q for q in entity["props"].get("P19", []) if q.startswith("Q")),
                    None,
                ),
                "death": next(
                    (q for q in entity["props"].get("P20", []) if q.startswith("Q")),
                    None,
                ),
                "qid_props": self._qid_values(entity, PERSON_QID_LABEL_PROPS),
                "place_props": {
                    prop: values[0]
                    for prop, values in self._qid_values(
                        entity, PERSON_PLACE_LABEL_PROPS
                    ).items()
                },
            }
            if direct_count >= settings.rich_fields_min_statements:
                payload.update(
                    {
                        "cod": next(
                            (
                                q
                                for q in entity["props"].get("P509", [])
                                if q.startswith("Q")
                            ),
                            None,
                        ),
                        "awards": [
                            q
                            for q in entity["props"].get("P166", [])
                            if q.startswith("Q")
                        ],
                        "works": [
                            q
                            for q in entity["props"].get("P800", [])
                            if q.startswith("Q")
                        ],
                    }
                )
            self.record_pending(qid, category, direct_count, payload)
        elif category == "organization":
            self.record_pending(
                qid,
                category,
                direct_count,
                {
                    "hq": next(
                        (
                            q
                            for q in entity["props"].get("P159", [])
                            if q.startswith("Q")
                        ),
                        None,
                    ),
                    "country": next(
                        (
                            q
                            for q in entity["props"].get("P17", [])
                            if q.startswith("Q")
                        ),
                        None,
                    ),
                    "qid_props": self._qid_values(entity, ORG_QID_LABEL_PROPS),
                },
            )
        elif category == "event":
            self.record_pending(
                qid,
                category,
                direct_count,
                {
                    "location": next(
                        (
                            q
                            for q in entity["props"].get("P276", [])
                            if q.startswith("Q")
                        ),
                        None,
                    ),
                    "country": next(
                        (
                            q
                            for q in entity["props"].get("P495", [])
                            if q.startswith("Q")
                        ),
                        None,
                    ),
                },
            )

    @staticmethod
    def _multi_labels(
        labels: dict[str, tuple[str | None, str | None, int]], qids: list[str]
    ) -> list[str]:
        values = []
        seen = set()
        for qid in qids:
            label = EnrichmentStore._label(labels, qid)
            if label and label not in seen:
                seen.add(label)
                values.append(label)
            if len(values) >= 10:
                break
        return values

    def _person_update(self, qid: str, direct_count: int, payload: dict) -> dict:
        qid_props = payload.get("qid_props") or {}
        place_props = payload.get("place_props") or {}
        target_qids = [q for values in qid_props.values() for q in values]
        target_qids.extend(place_props.values())
        target_qids.extend(q for q in (payload.get("birth"), payload.get("death")) if q)
        target_qids.extend(payload.get("awards") or [])
        target_qids.extend(payload.get("works") or [])
        if payload.get("cod"):
            target_qids.append(payload["cod"])
        labels = self._labels_for(target_qids)
        update = {"_key": qid}
        for field, target in (
            ("birth_place", payload.get("birth")),
            ("death_place", payload.get("death")),
        ):
            label = self._label(labels, target, place=True)
            if label:
                update[field] = label
        if direct_count >= settings.rich_fields_min_statements:
            cause = self._label(labels, payload.get("cod"))
            if cause:
                update["cause_of_death"] = cause
            awards = payload.get("awards") or []
            if awards:
                update["awards_count"] = len(awards)
                ranked = sorted(
                    awards,
                    key=lambda item: (-labels.get(item, (None, None, 0))[2], item),
                )
                top5 = [self._label(labels, item) for item in ranked]
                top5 = [value for value in top5 if value][:5]
                if top5:
                    update["awards_top5"] = top5
            works = self._multi_labels(labels, payload.get("works") or [])
            if works:
                update["notable_works"] = works
        for prop_id, qids in qid_props.items():
            field, kind = PERSON_QID_LABEL_PROPS[prop_id]
            if kind == "single":
                label = next(
                    (
                        self._label(labels, item)
                        for item in qids
                        if self._label(labels, item)
                    ),
                    None,
                )
                if label:
                    update[field] = label
            else:
                values = self._multi_labels(labels, qids)
                if values:
                    update[field] = values
        for prop_id, target in place_props.items():
            label = self._label(labels, target, place=True)
            if label:
                field = PERSON_PLACE_LABEL_PROPS[prop_id]
                update[field] = label
                update[f"{field}_id"] = target
        return update

    def _org_update(self, qid: str, payload: dict) -> dict:
        qid_props = payload.get("qid_props") or {}
        target_qids = [q for values in qid_props.values() for q in values]
        target_qids.extend(q for q in (payload.get("hq"), payload.get("country")) if q)
        labels = self._labels_for(target_qids)
        update = {"_key": qid}
        for field, target in (
            ("headquarters", payload.get("hq")),
            ("country", payload.get("country")),
        ):
            label = self._label(labels, target)
            if label:
                update[field] = label
        for prop_id, values in qid_props.items():
            field, kind = ORG_QID_LABEL_PROPS[prop_id]
            if kind == "single":
                label = next(
                    (
                        self._label(labels, item)
                        for item in values
                        if self._label(labels, item)
                    ),
                    None,
                )
                if label:
                    update[field] = label
            else:
                resolved = self._multi_labels(labels, values)
                if resolved:
                    update[field] = resolved
        return update

    def _event_update(self, qid: str, payload: dict) -> dict:
        target_qids = [
            q for q in (payload.get("location"), payload.get("country")) if q
        ]
        labels = self._labels_for(target_qids)
        update = {"_key": qid}
        for field, target in (
            ("location", payload.get("location")),
            ("country", payload.get("country")),
        ):
            label = self._label(labels, target)
            if label:
                update[field] = label
        return update

    def apply(self, db, batch_size: int) -> dict[str, int]:
        self.flush()
        updates = {PERSONS: [], ORGANIZATIONS: [], EVENTS: []}
        applied = {"persons": 0, "organizations": 0, "events": 0}

        def flush_updates(collection: str) -> None:
            batch = updates[collection]
            if not batch:
                return
            result = db.collection(collection).import_bulk(batch, on_duplicate="update")
            errors = int(result.get("errors", 0))
            if errors:
                raise RuntimeError(
                    f"Enrichment import failed for {collection}: "
                    f"{errors} errors in {len(batch)} documents"
                )
            applied[collection] += len(batch)
            batch.clear()

        for number, kind, direct_count, raw_payload in self.db.execute(
            "SELECT qid, kind, direct_count, payload FROM pending ORDER BY qid"
        ):
            qid = f"Q{number}"
            payload = json.loads(raw_payload)
            if kind == "person":
                collection = PERSONS
                update = self._person_update(qid, direct_count, payload)
            elif kind == "organization":
                collection = ORGANIZATIONS
                update = self._org_update(qid, payload)
            else:
                collection = EVENTS
                update = self._event_update(qid, payload)
            if len(update) > 1:
                updates[collection].append(update)
                if len(updates[collection]) >= batch_size:
                    flush_updates(collection)
        for collection in updates:
            flush_updates(collection)
        return applied

    def close(self, *, remove: bool = True) -> None:
        self.flush()
        self.db.close()
        if remove:
            for candidate in (
                self.path,
                Path(str(self.path) + "-wal"),
                Path(str(self.path) + "-shm"),
            ):
                if candidate.exists():
                    candidate.unlink()
