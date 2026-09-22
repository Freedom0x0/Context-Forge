from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .memory_models import Actor, MemoryDraft, MemoryRecord, Relation, Scope
from .memory_schema import SCHEMA, SCHEMA_VERSION
from .rules import glob_match


class MemoryStoreError(RuntimeError):
    pass


class VersionConflict(MemoryStoreError):
    pass


class IdempotencyConflict(MemoryStoreError):
    pass


class SourceConflict(MemoryStoreError):
    pass


class ProtectedMemory(MemoryStoreError):
    pass


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def tokenize(text: str) -> list[str]:
    tokens = []
    for match in re.finditer(r'[a-zA-Z0-9_]+|[\u3400-\u9fff\U00020000-\U0002fa1f]+', text):
        word = match.group().lower()
        if word.isascii():
            tokens.append(word)
        elif len(word) > 1:
            tokens.extend(word[index:index + 2] for index in range(len(word) - 1))
    return tokens


def _actor(actor: Actor) -> str:
    if actor not in {'human', 'user', 'model', 'tool', 'import', 'system'}:
        raise ValueError('unknown actor')
    return 'human' if actor == 'user' else actor


class MemoryStore:
    """An existing database handle; connections live for one operation only."""

    def __init__(self, path: Path, *, readonly: bool = True):
        self.path = Path(path).resolve()
        self.readonly = readonly
        with self._connection():
            pass

    @classmethod
    def initialize(cls, path: Path) -> MemoryStore:
        path = Path(path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=5)
        try:
            connection.execute('PRAGMA foreign_keys=ON')
            version = connection.execute('PRAGMA user_version').fetchone()[0]
            if version == 0:
                if connection.execute("SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchone():
                    raise MemoryStoreError('refusing to initialize an unrelated database')
                connection.executescript('BEGIN IMMEDIATE;\n' + SCHEMA + f'\nPRAGMA user_version={SCHEMA_VERSION};\nCOMMIT;')
            elif version != SCHEMA_VERSION:
                raise MemoryStoreError(f'unsupported memory schema {version}; expected {SCHEMA_VERSION}')
            connection.execute('PRAGMA journal_mode=WAL')
        except sqlite3.Error as error:
            connection.rollback()
            raise MemoryStoreError(f'cannot initialize memory database {path}: {error}') from error
        finally:
            connection.close()
        return cls(path, readonly=False)

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        if write and self.readonly:
            raise MemoryStoreError('store was opened read-only')
        connection = None
        try:
            mode = 'rw' if write else 'ro'
            connection = sqlite3.connect(self.path.as_uri() + f'?mode={mode}', uri=True, timeout=5)
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('PRAGMA busy_timeout=5000')
            version = connection.execute('PRAGMA user_version').fetchone()[0]
            if version != SCHEMA_VERSION:
                raise MemoryStoreError(f'unsupported memory schema {version}; explicitly initialize/migrate first')
            connection.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            connection.execute('SELECT id,current_version,status,user_owned,suppressed FROM memories LIMIT 0')
            yield connection
            if write:
                connection.commit()
        except sqlite3.Error as error:
            raise MemoryStoreError(f'memory database {self.path}: {error}') from error
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _record(connection: sqlite3.Connection, memory_id: str, version: int | None = None) -> MemoryRecord | None:
        row = connection.execute(
            'SELECT v.* FROM memory_versions v JOIN memories m ON m.id=v.memory_id '
            'WHERE m.id=? AND v.version=COALESCE(?,m.current_version)', (memory_id, version),
        ).fetchone()
        if row is None:
            return None
        return MemoryRecord(id=memory_id, version=row['version'],
                            draft=MemoryDraft.model_validate_json(row['snapshot_json']),
                            created_at=row['created_at'])

    def get(self, memory_id: str, version: int | None = None) -> MemoryRecord | None:
        with self._connection() as connection:
            return self._record(connection, memory_id, version)

    def history(self, memory_id: str) -> list[MemoryRecord]:
        with self._connection() as connection:
            versions = connection.execute('SELECT version FROM memory_versions WHERE memory_id=? ORDER BY version', (memory_id,))
            return [self._record(connection, memory_id, row['version']) for row in versions]

    def events(self, memory_id: str) -> list[dict]:
        with self._connection() as connection:
            return [dict(row) for row in connection.execute('SELECT * FROM memory_events WHERE memory_id=? ORDER BY id', (memory_id,))]

    def record_retrieval(self, memory_id: str, version: int, *, request_id: str,
                         stage: str, idempotency_key: str, session_id: str | None = None,
                         explanation: dict | None = None) -> None:
        if stage not in {'retrieved', 'tool_returned', 'presented', 'referenced', 'applied', 'validated'}:
            raise ValueError('invalid retrieval stage')
        if not request_id or not idempotency_key:
            raise ValueError('request and idempotency key required')
        payload = canonical_json(explanation or {})
        with self._connection(write=True) as connection:
            prior = connection.execute('SELECT memory_id,version,request_id,stage,session_id,explanation_json '
                                       'FROM retrieval_events WHERE idempotency_key=?', (idempotency_key,)).fetchone()
            values = (memory_id, version, request_id, stage, session_id, payload)
            if prior:
                if tuple(prior) != values:
                    raise IdempotencyConflict('retrieval idempotency key was used for a different event')
                return
            if self._record(connection, memory_id, version) is None:
                raise KeyError((memory_id, version))
            connection.execute('INSERT INTO retrieval_events(idempotency_key,request_id,stage,memory_id,version,session_id,explanation_json,created_at) '
                               'VALUES (?,?,?,?,?,?,?,?)',
                               (idempotency_key, request_id, stage, memory_id, version, session_id, payload, now()))

    def retrieval_events(self, request_id: str | None = None) -> list[dict]:
        with self._connection() as connection:
            if request_id is None:
                rows = connection.execute('SELECT * FROM retrieval_events ORDER BY id')
            else:
                rows = connection.execute('SELECT * FROM retrieval_events WHERE request_id=? ORDER BY id', (request_id,))
            return [dict(row) for row in rows]

    def put(self, memory_id: str, draft: MemoryDraft, *, expected_version: int,
            idempotency_key: str, actor: Actor = 'model', reason: str,
            relations: tuple[Relation, ...] = ()) -> MemoryRecord:
        with self._connection(write=True) as connection:
            return self._put(connection, memory_id, draft, expected_version=expected_version,
                             idempotency_key=idempotency_key, actor=actor, reason=reason, relations=relations)

    def _put(self, connection: sqlite3.Connection, memory_id: str, draft: MemoryDraft, *,
             expected_version: int, idempotency_key: str, actor: Actor, reason: str,
             relations: tuple[Relation, ...] = ()) -> MemoryRecord:
        if not memory_id or not idempotency_key or not reason or expected_version < 0:
            raise ValueError('id, idempotency key, reason and nonnegative expected_version required')
        draft = MemoryDraft.model_validate(draft.model_dump())
        relations = tuple(Relation.model_validate(relation.model_dump()) for relation in relations)
        actor = _actor(actor)
        payload = {'draft': draft.model_dump(mode='json'), 'relations': [relation.model_dump(mode='json') for relation in relations],
                   'expected_version': expected_version, 'reason': reason}
        request_hash = digest(canonical_json({'memory_id': memory_id, 'actor': actor, **payload}))
        prior = connection.execute('SELECT * FROM memory_events WHERE idempotency_key=?', (idempotency_key,)).fetchone()
        if prior:
            if prior['kind'] != 'put' or prior['request_hash'] != request_hash:
                raise IdempotencyConflict('idempotency key was used for a different request')
            return self._record(connection, prior['memory_id'], prior['version'])
        previous = self._record(connection, memory_id)
        if (previous.version if previous else 0) != expected_version:
            raise VersionConflict(f'{memory_id}: expected version {expected_version}, found {previous.version if previous else 0}')
        if previous and previous.draft.kind != draft.kind:
            raise ValueError('memory kind cannot change')
        if previous and actor != 'human' and (previous.draft.suppressed or previous.draft.user_owned or previous.draft.status == 'disabled'):
            raise ProtectedMemory('human-owned or suppressed memory requires a human correction')
        version = expected_version + 1
        timestamp = now()
        for source in draft.sources:
            source_json = canonical_json(source.model_dump(mode='json'))
            stored = connection.execute('SELECT source_json FROM memory_sources WHERE identity=?', (source.identity,)).fetchone()
            if stored and stored['source_json'] != source_json:
                raise SourceConflict(f'source identity {source.identity!r} already has different content')
            connection.execute('INSERT OR IGNORE INTO memory_sources VALUES (?,?,?,?)',
                               (source.identity, digest(source.excerpt), source_json, timestamp))
        if previous is None:
            connection.execute('INSERT INTO memories VALUES (?,?,?,?,?,?)',
                               (memory_id, draft.kind, version, draft.status, draft.user_owned, draft.suppressed))
        else:
            changed = connection.execute('UPDATE memories SET current_version=?,status=?,user_owned=?,suppressed=? '
                                         'WHERE id=? AND current_version=?',
                                         (version, draft.status, draft.user_owned, draft.suppressed, memory_id, expected_version))
            if changed.rowcount != 1:
                raise VersionConflict(memory_id)
        connection.execute('INSERT INTO memory_versions VALUES (?,?,?,?)',
                           (memory_id, version, canonical_json(draft.model_dump(mode='json')), timestamp))
        for source_id in sorted({source.identity for source in draft.sources}):
            connection.execute('INSERT INTO memory_version_sources VALUES (?,?,?)', (memory_id, version, source_id))
        for ordinal, scope in enumerate(draft.scopes):
            connection.execute('INSERT INTO memory_scopes VALUES (?,?,?,?,?,?)',
                               (memory_id, version, ordinal, scope.kind, scope.project, canonical_json(scope.paths)))
        event = connection.execute('INSERT INTO memory_events(idempotency_key,request_hash,memory_id,version,kind,actor,payload_json,created_at) '
                                   "VALUES (?,?,?,?,'put',?,?,?)",
                                   (idempotency_key, request_hash, memory_id, version, actor, canonical_json(payload), timestamp))
        self._add_relations(connection, memory_id, version, draft, relations, idempotency_key, event.lastrowid)
        self._index(connection, memory_id, draft)
        return self._record(connection, memory_id, version)

    @staticmethod
    def _add_relations(connection: sqlite3.Connection, memory_id: str, version: int,
                       draft: MemoryDraft, relations: tuple[Relation, ...],
                       idempotency_key: str, event_id: int) -> None:
        source_ids = {source.identity for source in draft.sources}
        for ordinal, relation in enumerate(relations):
            evidence_ids = set(relation.evidence_ids)
            for condition in relation.applicability:
                evidence_ids.update(condition.evidence_ids)
            if not evidence_ids <= source_ids:
                raise ValueError('relation evidence must reference this version sources')
            relation_id = digest(canonical_json([idempotency_key, ordinal]))
            connection.execute('INSERT INTO memory_relations VALUES (?,?,?,?,?,?,?,?,NULL)',
                               (relation_id, memory_id, version, relation.target_id, relation.target_version, relation.kind,
                                canonical_json([condition.model_dump(mode='json') for condition in relation.applicability]), event_id))
            for source_id in sorted(evidence_ids):
                connection.execute('INSERT INTO memory_relation_sources VALUES (?,?)', (relation_id, source_id))

    @staticmethod
    def _index(connection: sqlite3.Connection, memory_id: str, draft: MemoryDraft) -> None:
        connection.execute('DELETE FROM memory_fts WHERE memory_id=?', (memory_id,))
        connection.execute('INSERT INTO memory_fts VALUES (?,?)',
                           (memory_id, ' '.join(tokenize(draft.summary + '\n' + draft.body))))

    def relations(self, memory_id: str, version: int | None = None, *, include_revoked: bool = False) -> list[dict]:
        with self._connection() as connection:
            record = self._record(connection, memory_id, version)
            if record is None:
                return []
            rows = connection.execute('SELECT * FROM memory_relations WHERE '
                                      '((memory_id=? AND version=?) OR (target_id=? AND target_version=?)) '
                                      'AND (? OR revoked_event IS NULL) ORDER BY id',
                                      (memory_id, record.version, memory_id, record.version, include_revoked))
            result = []
            for row in rows:
                relation = dict(row)
                relation['evidence_ids'] = [source['source_id'] for source in connection.execute(
                    'SELECT source_id FROM memory_relation_sources WHERE relation_id=? ORDER BY source_id', (row['id'],))]
                relation['applicability'] = json.loads(relation.pop('applicability_json'))
                result.append(relation)
            return result

    def revoke_relation(self, relation_id: str, *, expected_version: int,
                        idempotency_key: str, reason: str, actor: Actor = 'human') -> None:
        actor = _actor(actor)
        if not idempotency_key or not reason:
            raise ValueError('idempotency key and reason required')
        payload = {'relation_id': relation_id, 'expected_version': expected_version, 'reason': reason}
        request_hash = digest(canonical_json({**payload, 'actor': actor}))
        with self._connection(write=True) as connection:
            prior = connection.execute('SELECT kind,request_hash FROM memory_events WHERE idempotency_key=?', (idempotency_key,)).fetchone()
            if prior:
                if prior['kind'] != 'revoke_relation' or prior['request_hash'] != request_hash:
                    raise IdempotencyConflict(idempotency_key)
                return
            relation = connection.execute('SELECT * FROM memory_relations WHERE id=?', (relation_id,)).fetchone()
            if relation is None:
                raise KeyError(relation_id)
            current = self._record(connection, relation['memory_id'])
            if current.version != expected_version:
                raise VersionConflict(relation['memory_id'])
            if actor != 'human' and (
                current.draft.user_owned
                or current.draft.suppressed
                or current.draft.status == 'disabled'
            ):
                raise ProtectedMemory(relation['memory_id'])
            if relation['revoked_event'] is not None:
                raise VersionConflict('relation already revoked')
            event = connection.execute('INSERT INTO memory_events(idempotency_key,request_hash,memory_id,version,kind,actor,payload_json,created_at) '
                                       "VALUES (?,?,?,?,'revoke_relation',?,?,?)",
                                       (idempotency_key, request_hash, current.id, current.version, actor, canonical_json(payload), now()))
            connection.execute('UPDATE memory_relations SET revoked_event=? WHERE id=?', (event.lastrowid, relation_id))

    @staticmethod
    def _visible(scopes: tuple[Scope, ...], project: str | None, changed_path: str | None) -> bool:
        for scope in scopes:
            if scope.kind == 'unknown' or (scope.kind == 'project' and scope.project != project):
                continue
            if scope.paths and (changed_path is None or not any(glob_match(changed_path.replace('\\', '/'), pattern) for pattern in scope.paths)):
                continue
            return True
        return False

    def search(self, query: str, *, project: str | None, changed_path: str | None = None,
               limit: int | None = 8, byte_budget: int = 65536) -> list[MemoryRecord]:
        if limit is not None and limit < 1 or byte_budget < 1:
            raise ValueError('positive limit and byte budget required')
        tokens = list(dict.fromkeys(tokenize(query)))[:128]
        single_han = re.fullmatch(r'[\u3400-\u9fff\U00020000-\U0002fa1f]', query.strip()) is not None
        with self._connection() as connection:
            if single_han:
                rows = connection.execute('SELECT m.id FROM memories m JOIN memory_versions v '
                                          'ON v.memory_id=m.id AND v.version=m.current_version '
                                          "WHERE m.status IN ('active','conflicted') AND m.suppressed=0 "
                                          "AND instr(json_extract(v.snapshot_json,'$.body') || json_extract(v.snapshot_json,'$.summary'),?)>0 ORDER BY m.id", (query.strip(),))
            elif tokens:
                match = ' AND '.join('"' + token.replace('"', '""') + '"' for token in tokens)
                rows = connection.execute('SELECT m.id FROM memory_fts JOIN memories m ON m.id=memory_fts.memory_id '
                                          "WHERE memory_fts MATCH ? AND m.status IN ('active','conflicted') AND m.suppressed=0 "
                                          'ORDER BY bm25(memory_fts),m.id', (match,))
            else:
                return []
            result = []
            used_bytes = 0
            effective_limit = min(limit or 100, 100) if single_han else limit
            for row in rows:
                record = self._record(connection, row['id'])
                if record.draft.value == 'discard' or not self._visible(record.draft.scopes, project, changed_path):
                    continue
                size = len(record.model_dump_json().encode('utf-8'))
                if used_bytes + size > byte_budget:
                    continue
                result.append(record)
                used_bytes += size
                if effective_limit is not None and len(result) >= effective_limit:
                    break
            return result

    def rebuild_fts(self) -> int:
        with self._connection(write=True) as connection:
            connection.execute('DROP TABLE IF EXISTS memory_fts')
            connection.execute('CREATE VIRTUAL TABLE memory_fts USING fts5(memory_id UNINDEXED, tokens)')
            rows = list(connection.execute('SELECT id FROM memories ORDER BY id'))
            for row in rows:
                self._index(connection, row['id'], self._record(connection, row['id']).draft)
            return len(rows)

    def backup(self, target: Path) -> Path:
        target = Path(target).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb'):
            pass
        try:
            with self._connection() as source:
                destination = sqlite3.connect(target)
                try:
                    source.backup(destination)
                    if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise MemoryStoreError('backup integrity check failed')
                finally:
                    destination.close()
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return target

    @classmethod
    def restore(cls, backup: Path, target: Path) -> MemoryStore:
        source = cls(backup)
        source.backup(target)
        return cls.initialize(target)

    def export_markdown(self, target: Path) -> list[Path]:
        target = Path(target)
        if target.is_symlink():
            raise MemoryStoreError(f'export directory is a symlink: {target}')
        target = target.resolve()
        with self._connection() as connection:
            records = [self._record(connection, row['id']) for row in connection.execute('SELECT id FROM memories ORDER BY id')]
        target.mkdir(parents=True, exist_ok=True)
        exported = []
        for record in records:
            metadata = record.model_dump(mode='json')
            body = metadata['draft'].pop('body')
            content = '---\n' + json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2) + '\n---\n\n' + body.rstrip() + '\n'
            path = target / (digest(record.id) + '.md')
            if path.is_symlink():
                raise MemoryStoreError(f'export destination is a symlink: {path}')
            descriptor, temporary = tempfile.mkstemp(prefix='.memory-export-', dir=target)
            try:
                with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            finally:
                Path(temporary).unlink(missing_ok=True)
            exported.append(path)
        return exported

    def import_vault(self, root: Path, *, feedback_db: Path | None = None,
                     dry_run: bool = True, namespace: str | None = None,
                     snapshot_order: int = 0) -> dict:
        from .memory_import import import_vault
        return import_vault(self, root, feedback_db=feedback_db, dry_run=dry_run,
                            namespace=namespace, snapshot_order=snapshot_order)
