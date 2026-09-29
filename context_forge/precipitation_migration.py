from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass

from .memory import MemoryStore, canonical_fingerprint, now
from .memory_models import MemoryDraft, Relation


@dataclass(frozen=True)
class MigrationReport:
    batch_id: str | None
    exact_groups: tuple[tuple[str, ...], ...]
    archived: tuple[str, ...]
    skipped_protected: tuple[str, ...]


class PrecipitationMigration:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def plan(self) -> MigrationReport:
        groups: dict[str, list[str]] = {}
        for record in self.store.current_records():
            groups.setdefault(canonical_fingerprint(record.draft), []).append(record.id)
        exact = tuple(tuple(sorted(ids)) for ids in groups.values() if len(ids) > 1)
        return MigrationReport(None, tuple(sorted(exact)), (), ())

    def apply(self, *, batch_id: str | None = None) -> MigrationReport:
        preview = self.plan()
        batch_id = batch_id or str(uuid.uuid4())
        operations = []
        archived, skipped = [], []
        for group in preview.exact_groups:
            survivor = self.store.get(group[0])
            for memory_id in group[1:]:
                record = self.store.get(memory_id)
                if record.draft.user_owned or record.draft.suppressed or record.draft.status == 'disabled':
                    skipped.append(memory_id)
                    continue
                operations.append({"memory_id": memory_id, "before": record.draft.model_dump(mode="json"),
                                   "before_version": record.version, "survivor": survivor.id,
                                   "survivor_version": survivor.version})
        plan_json = json.dumps(operations, sort_keys=True, separators=(",", ":"))
        with closing(sqlite3.connect(self.store.path)) as db:
            prior = db.execute('SELECT plan_json,status FROM precipitation_migrations WHERE batch_id=?',
                               (batch_id,)).fetchone()
            if prior:
                frozen = json.loads(prior[0])
                return MigrationReport(batch_id, preview.exact_groups,
                                       tuple(item["memory_id"] for item in frozen), tuple(skipped))
            db.execute('INSERT INTO precipitation_migrations VALUES (?,?,?, ?,NULL)',
                       (batch_id, 'applying', plan_json, now()))
            db.commit()
        for ordinal, operation in enumerate(operations):
            record = self.store.get(operation["memory_id"])
            if record.draft.status != 'archived':
                draft = record.draft.model_copy(update={"status": "archived"})
                self.store.put(record.id, draft, expected_version=record.version,
                               idempotency_key=f"migration:{batch_id}:{ordinal}", actor='system',
                               reason='exact duplicate precipitation cleanup',
                               relations=(Relation(target_id=operation["survivor"],
                                                   target_version=operation["survivor_version"],
                                                   kind='duplicate_of'),))
            archived.append(record.id)
        with closing(sqlite3.connect(self.store.path)) as db:
            db.execute("UPDATE precipitation_migrations SET status='applied' WHERE batch_id=?", (batch_id,))
            db.commit()
        return MigrationReport(batch_id, preview.exact_groups, tuple(archived), tuple(skipped))

    def rollback(self, batch_id: str) -> tuple[str, ...]:
        with closing(sqlite3.connect(self.store.path)) as db:
            row = db.execute('SELECT status,plan_json FROM precipitation_migrations WHERE batch_id=?',
                             (batch_id,)).fetchone()
        if row is None:
            raise KeyError(batch_id)
        if row[0] == 'rolled_back':
            return tuple(item['memory_id'] for item in json.loads(row[1]))
        restored = []
        for ordinal, operation in enumerate(json.loads(row[1])):
            current = self.store.get(operation['memory_id'])
            self.store.put(current.id, MemoryDraft.model_validate(operation['before']),
                           expected_version=current.version,
                           idempotency_key=f"migration-rollback:{batch_id}:{ordinal}",
                           actor='system', reason='rollback precipitation migration')
            restored.append(current.id)
        with closing(sqlite3.connect(self.store.path)) as db:
            db.execute("UPDATE precipitation_migrations SET status='rolled_back',rolled_back_at=? WHERE batch_id=?",
                       (now(), batch_id))
            db.commit()
        return tuple(restored)
