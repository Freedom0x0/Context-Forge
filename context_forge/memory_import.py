from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import frontmatter

from .memory import MemoryStore, MemoryStoreError, canonical_json, digest, now
from .memory_models import DEFAULT_OWNER, MemoryDraft, Relation, Scope, Source, split_legacy_scopes


def _strings(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        values = value.split(',')
    elif isinstance(value, list):
        values = value
    else:
        values = [value]
    return tuple(text for text in (str(part).strip() for part in values) if text)


def _boolean(value: object) -> bool:
    return value is True or isinstance(value, str) and value.lower() == 'true'


_ROLES = {'observation', 'claim', 'hypothesis', 'preference', 'procedure'}


def _role(metadata: dict, kind: str) -> str:
    value = str(metadata.get('role', '')).strip().lower()
    return value if value in _ROLES else ('procedure' if kind == 'rule' else 'claim')


def _privacy(metadata: dict) -> str:
    return 'private' if str(metadata.get('privacy', '')).strip().lower() == 'private' else 'shared'


def _access(metadata: dict, split) -> object:
    visibility = str(metadata.get('access_visibility', '')).strip().lower()
    if visibility not in {'owner', 'selected_projects', 'shared'}:
        return split.access
    allowed = _strings(metadata.get('allowed_projects')) or split.provenance.projects
    if visibility == 'selected_projects' and not allowed:
        return split.access
    return split.access.model_copy(update={'visibility': visibility, 'allowed_projects': allowed})


def _applicability_scope(metadata: dict, split) -> object:
    return split.scope.model_copy(update={
        'technologies': _strings(metadata.get('technologies')) or split.scope.technologies,
        'domains': _strings(metadata.get('domains')) or split.scope.domains,
        'exclusions': _strings(metadata.get('exclusions')) or split.scope.exclusions,
    })


def _feedback(path: Path | None) -> list[dict]:
    if path is None:
        return []
    connection = None
    try:
        connection = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute('BEGIN')
        return [dict(row) for row in connection.execute(
            'SELECT id,rule_id,session_id,outcome,note,created_at FROM rule_feedback ORDER BY id')]
    except sqlite3.Error as error:
        raise MemoryStoreError(f'cannot read legacy feedback {path}: {error}') from error
    finally:
        if connection is not None:
            connection.close()


def preview_vault(root: Path, *, feedback_db: Path | None = None,
                  namespace: str | None = None, owner: str = DEFAULT_OWNER) -> dict:
    """Read a legacy snapshot without opening or creating any destination."""
    root = Path(root).resolve()
    if not root.is_dir():
        raise MemoryStoreError(f'vault directory does not exist: {root}')
    namespace = namespace or str(root)
    documents = []
    for path in sorted(root.rglob('*.md')):
        relative = path.relative_to(root).as_posix()
        if path.name == 'index.md' or relative.split('/')[0] not in {'knowledge', 'rules', 'reviews'}:
            continue
        if not path.resolve().is_relative_to(root):
            raise MemoryStoreError(f'vault symlink escapes root: {path}')
        raw = path.read_bytes()
        content_hash = hashlib.sha256(raw).hexdigest()
        try:
            parsed = frontmatter.loads(raw.decode('utf-8-sig'))
        except Exception as error:
            raise MemoryStoreError(f'cannot parse vault document {path}: {error}') from error
        metadata = json.loads(json.dumps(parsed.metadata, ensure_ascii=False, default=str))
        legacy_id = str(metadata.get('id') or path.stem)
        kind = {'knowledge': 'knowledge', 'rules': 'rule', 'reviews': 'review'}[relative.split('/')[0]]
        project = metadata.get('project')
        # `paths` and `evidence_paths` both name where the evidence lives; Gen1
        # `paths` doubled as rule applicability and vault_retag moves the
        # provenance half into `evidence_paths`. Nothing is dropped.
        declared_paths = _strings(metadata.get('paths')) + _strings(metadata.get('evidence_paths'))
        if metadata.get('scope') == 'global':
            scope = Scope(kind='global', paths=declared_paths)
        elif isinstance(project, str) and project.strip() and project.strip().lower() != 'unknown':
            scope = Scope(kind='project', project=project.strip(), paths=declared_paths)
        else:
            scope = Scope(paths=declared_paths)
        old_status = str(metadata.get('status', 'proposed')).lower()
        status = {'accepted': 'active', 'enabled': 'active', 'verified': 'active', 'approved': 'active',
                  'revised': 'active', 'disabled': 'disabled', 'archived': 'archived', 'rejected': 'archived',
                  'conflicted': 'conflicted', 'stale': 'stale', 'superseded': 'archived'}.get(old_status, 'candidate')
        source = Source(identity='vault:' + digest(canonical_json([namespace, relative, content_hash])),
                        kind='legacy', reference=relative, excerpt=raw.decode('utf-8-sig'),
                        session_id=str(metadata['session_id']) if metadata.get('session_id') else None)
        sources = [source]
        for reference in _strings(metadata.get('sources')):
            origin = ['session', source.session_id] if source.session_id else ['document', relative]
            sources.append(Source(identity='legacy-reference:' + digest(canonical_json([namespace, origin, reference])),
                                  kind='legacy', reference=reference, session_id=source.session_id))
        split = split_legacy_scopes((scope,), owner=owner, privacy=_privacy(metadata))
        session = str(metadata['session_id']) if metadata.get('session_id') else None
        provenance = split.provenance.model_copy(
            update={'sessions': (session,) if session else ()})
        draft = MemoryDraft(kind=kind, role=_role(metadata, kind),
                            body=parsed.content or '(empty legacy document)', status=status,
                            user_owned=_boolean(metadata.get('user_owned')), suppressed=status == 'disabled',
                            confidence=str(metadata['confidence']) if metadata.get('confidence') is not None else None,
                            space=split.space, access=_access(metadata, split),
                            provenance=provenance, scope=_applicability_scope(metadata, split),
                            applicability=split.applicability, sources=tuple(sources),
                            metadata={'legacy': metadata, 'legacy_id': legacy_id, 'legacy_path': relative,
                                      'legacy_hash': content_hash, 'import_namespace': namespace})
        documents.append({'path': relative, 'legacy_id': legacy_id, 'content_hash': content_hash,
                          'memory_id': 'legacy-' + digest(canonical_json([namespace, relative, legacy_id])),
                          'draft': draft})
    referenced_projects = {}
    for item in documents:
        if item['draft'].kind != 'rule':
            continue
        knowledge_id = item['draft'].metadata['legacy'].get('source_knowledge_id')
        if knowledge_id:
            projects = referenced_projects.setdefault(str(knowledge_id), set())
            projects.update(item['draft'].provenance.projects)
    for item in documents:
        draft = item['draft']
        if draft.kind == 'knowledge' and not draft.provenance.projects:
            projects = referenced_projects.get(item['legacy_id'], set())
            if projects:
                item['draft'] = draft.model_copy(update={
                    'provenance': draft.provenance.model_copy(update={'projects': tuple(sorted(projects))})})
    return {'namespace': namespace, 'documents': documents, 'feedback': _feedback(feedback_db)}


def import_vault(store: MemoryStore, root: Path, *, feedback_db: Path | None = None,
                 dry_run: bool = True, namespace: str | None = None, snapshot_order: int = 0,
                 owner: str = DEFAULT_OWNER) -> dict:
    if not isinstance(snapshot_order, int) or snapshot_order < 0:
        raise ValueError('snapshot_order must be a nonnegative integer')
    snapshot = preview_vault(root, feedback_db=feedback_db, namespace=namespace, owner=owner)
    namespace = snapshot['namespace']
    documents = snapshot['documents']
    manifest_hash = digest(canonical_json({'documents': [(item['path'], item['content_hash']) for item in documents],
                                           'feedback': snapshot['feedback']}))
    report = {'dry_run': dry_run, 'created': 0, 'updated': 0, 'unchanged': 0,
              'protected': 0, 'feedback_imported': 0, 'feedback_skipped': 0, 'diagnostics': [], 'mapping': {}}
    with store._connection(write=not dry_run) as connection:
        previous_snapshot = connection.execute('SELECT * FROM import_snapshots WHERE namespace=?', (namespace,)).fetchone()
        if previous_snapshot and (snapshot_order < previous_snapshot['snapshot_order'] or
                                  snapshot_order == previous_snapshot['snapshot_order'] and manifest_hash != previous_snapshot['manifest_hash']):
            report['protected'] = len(documents)
            report['feedback_skipped'] = len(snapshot['feedback'])
            report['diagnostics'].append('stale or changed snapshot: provide a strictly increasing snapshot_order for a new snapshot')
            return report
        legacy_mapping = {}
        written = []
        for item in documents:
            previous = connection.execute('SELECT * FROM import_items WHERE namespace=? AND path=?',
                                          (namespace, item['path'])).fetchone()
            memory_id = previous['memory_id'] if previous else item['memory_id']
            report['mapping'][item['path']] = memory_id
            legacy_mapping.setdefault(item['legacy_id'], []).append(memory_id)
            seen = connection.execute('SELECT 1 FROM import_receipts WHERE namespace=? AND path=? AND content_hash=?',
                                      (namespace, item['path'], item['content_hash'])).fetchone()
            current = store._record(connection, memory_id)
            if seen:
                report['unchanged'] += 1
                continue
            if previous and (snapshot_order <= previous['snapshot_order'] or current.version != previous['imported_version']
                             or current.draft.user_owned or current.draft.suppressed or current.draft.status == 'disabled'):
                report['protected'] += 1
                continue
            if current and previous is None:
                report['protected'] += 1
                continue
            report['updated' if previous else 'created'] += 1
            if dry_run:
                continue
            record = store._put(connection, memory_id, item['draft'], expected_version=current.version if current else 0,
                                idempotency_key='import:' + digest(canonical_json([namespace, item['path'], item['content_hash']])),
                                actor='import', reason='legacy snapshot import; evidence remains unknown')
            connection.execute('INSERT INTO import_items VALUES (?,?,?,?,?,?,?) '
                               'ON CONFLICT(namespace,path) DO UPDATE SET legacy_id=excluded.legacy_id, '
                               'content_hash=excluded.content_hash,snapshot_order=excluded.snapshot_order,imported_version=excluded.imported_version',
                               (namespace, item['path'], item['legacy_id'], memory_id, item['content_hash'], snapshot_order, record.version))
            connection.execute('INSERT INTO import_receipts VALUES (?,?,?)', (namespace, item['path'], item['content_hash']))
            written.append((item, record))
        for item, record in written:
            knowledge_id = item['draft'].metadata['legacy'].get('source_knowledge_id')
            if not knowledge_id or record.draft.kind != 'rule':
                continue
            matches = legacy_mapping.get(str(knowledge_id), [])
            if len(matches) != 1:
                report['diagnostics'].append(f"{item['path']}: missing or ambiguous source_knowledge_id {knowledge_id}")
                continue
            imported = connection.execute('SELECT memory_id,imported_version FROM import_items WHERE namespace=? AND memory_id=?',
                                          (namespace, matches[0])).fetchone()
            if imported is None:
                report['diagnostics'].append(f"{item['path']}: source knowledge was not imported")
                continue
            relation = Relation(target_id=imported['memory_id'], target_version=imported['imported_version'],
                                kind='derived_from', evidence_ids=(record.draft.sources[0].identity,))
            event = connection.execute("SELECT id,idempotency_key FROM memory_events WHERE memory_id=? AND version=? AND kind='put'",
                                       (record.id, record.version)).fetchone()
            store._add_relations(connection, record.id, record.version, record.draft, (relation,), event['idempotency_key'], event['id'])
        feedback = list(snapshot['feedback'])
        for item in documents:
            legacy = item['draft'].metadata['legacy']
            if legacy.get('feedback'):
                feedback.append({'id': 'frontmatter:' + item['path'] + ':' + item['content_hash'],
                                 'rule_id': item['legacy_id'], 'session_id': legacy.get('session_id'),
                                 'outcome': str(legacy['feedback']), 'note': 'from frontmatter scan', 'created_at': None})
        pending_keys = set()
        for entry in feedback:
            matches = legacy_mapping.get(str(entry['rule_id']), [])
            if len(matches) != 1:
                report['feedback_skipped'] += 1
                report['diagnostics'].append(f"feedback {entry['id']}: missing or ambiguous legacy id {entry['rule_id']}")
                continue
            memory_id = matches[0]
            if entry['note'] == 'from frontmatter scan':
                identity = [namespace, 'scan', entry['rule_id'], entry['session_id'], entry['outcome'], entry['note']]
            else:
                identity = [namespace, 'queue-feedback', entry['id']]
            key = 'feedback:' + digest(canonical_json(identity))
            previous = connection.execute('SELECT payload_json FROM memory_events WHERE idempotency_key=?', (key,)).fetchone()
            if previous or key in pending_keys:
                report['feedback_skipped'] += 1
                if previous and entry['note'] != 'from frontmatter scan' and json.loads(previous['payload_json']) != entry:
                    report['diagnostics'].append(f"feedback {entry['id']}: changed source row preserved without overwrite")
                continue
            pending_keys.add(key)
            report['feedback_imported'] += 1
            if dry_run:
                continue
            current = store._record(connection, memory_id)
            if current is None:
                raise MemoryStoreError(f'feedback target missing: {memory_id}')
            payload_json = canonical_json(entry)
            connection.execute('INSERT INTO memory_events(idempotency_key,request_hash,memory_id,version,kind,actor,payload_json,created_at) '
                               "VALUES (?,?,?,?,'legacy_feedback','import',?,?)",
                               (key, digest(payload_json), memory_id, current.version, payload_json, now()))
        if not dry_run:
            connection.execute('INSERT INTO import_snapshots VALUES (?,?,?) ON CONFLICT(namespace) '
                               'DO UPDATE SET snapshot_order=excluded.snapshot_order,manifest_hash=excluded.manifest_hash',
                               (namespace, snapshot_order, manifest_hash))
    return report
