import hashlib
import sqlite3

import pytest

from context_forge.memory import MemoryStore, MemoryStoreError
from context_forge.memory_import import preview_vault
from context_forge.memory_models import MemoryDraft


def write_rule(root, *, body='事务 SQLite guidance', status='enabled', user_owned=False, feedback='helpful'):
    path = root / 'rules' / 'proposals' / 'rule.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'---\nid: rule-one\nproject: demo\nstatus: {status}\nuser_owned: {str(user_owned).lower()}\n'
                    f'paths:\n  - "src/**/*.py"\nfeedback: {feedback}\n---\n\n{body}\n', encoding='utf-8')
    return path


def feedback_database(path):
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE rule_feedback(id INTEGER PRIMARY KEY,rule_id TEXT,session_id TEXT,outcome TEXT,note TEXT,created_at TEXT)')
        connection.executemany('INSERT INTO rule_feedback VALUES (?,?,?,?,?,?)', [
            (1, 'rule-one', 's1', 'helpful', 'explicit feedback', '2026-01-01'),
            (2, 'rule-one', 's2', 'helpful', 'explicit feedback', '2026-01-02'),
            (3, 'rule-one', None, 'helpful', 'from frontmatter scan', '2026-01-03'),
            (4, 'rule-one', None, 'helpful', 'from frontmatter scan', '2026-01-04'),
        ])
    return path


def test_preview_and_dry_run_leave_source_and_destination_unchanged(tmp_path):
    root = tmp_path / 'vault'
    source = write_rule(root, status='disabled', user_owned=True)
    feedback = feedback_database(tmp_path / 'queue.db')
    original_source = (source.read_bytes(), source.stat().st_mtime_ns)
    original_queue = (feedback.read_bytes(), feedback.stat().st_mtime_ns)
    assert len(preview_vault(root, feedback_db=feedback)['documents']) == 1
    assert not (tmp_path / 'memory.db').exists()
    store = MemoryStore.initialize(tmp_path / 'memory.db')
    original_database = store.path.read_bytes()
    report = MemoryStore(store.path).import_vault(root, feedback_db=feedback)
    assert report['created'] == 1
    assert report['feedback_imported'] == 3
    assert store.path.read_bytes() == original_database
    assert store.get(report['mapping']['rules/proposals/rule.md']) is None
    result = store.import_vault(root, feedback_db=feedback, dry_run=False)
    memory_id = result['mapping']['rules/proposals/rule.md']
    record = store.get(memory_id)
    assert record.draft.user_owned and record.draft.suppressed
    assert record.draft.status == 'disabled'
    assert record.draft.evidence_level == 'unknown'
    assert record.draft.provenance.paths == ('src/**/*.py',)
    assert record.draft.metadata['legacy_hash'] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert len(store.events(memory_id)) == 4
    assert (source.read_bytes(), source.stat().st_mtime_ns) == original_source
    assert (feedback.read_bytes(), feedback.stat().st_mtime_ns) == original_queue
    again = store.import_vault(root, feedback_db=feedback, dry_run=False)
    assert again['unchanged'] == 1
    assert again['feedback_imported'] == 0
    assert len(store.events(memory_id)) == 4


def test_new_snapshot_updates_only_untouched_records_and_older_is_rejected(tmp_path):
    root = tmp_path / 'vault'
    path = write_rule(root, feedback='')
    original = path.read_bytes()
    store = MemoryStore.initialize(tmp_path / 'memory.db')
    report = store.import_vault(root, dry_run=False, snapshot_order=1)
    memory_id = report['mapping']['rules/proposals/rule.md']
    write_rule(root, body='new source', feedback='')
    assert store.import_vault(root, dry_run=False, snapshot_order=1)['protected'] == 1
    assert store.import_vault(root, dry_run=False, snapshot_order=2)['updated'] == 1
    assert store.get(memory_id).version == 2
    path.write_bytes(original)
    assert store.import_vault(root, dry_run=False, snapshot_order=1)['protected'] == 1
    assert store.get(memory_id).draft.body == 'new source'
    assert store.import_vault(root, dry_run=False, snapshot_order=3)['unchanged'] == 1
    assert store.get(memory_id).version == 2
    corrected = store.get(memory_id).draft.model_copy(update={'body': 'human correction'})
    store.put(memory_id, corrected, expected_version=2, idempotency_key='human', actor='human', reason='correction')
    write_rule(root, body='subsequent source edit', feedback='')
    assert store.import_vault(root, dry_run=False, snapshot_order=4)['protected'] == 1
    assert store.get(memory_id).draft.body == 'human correction'


def test_unknown_scope_is_not_global_and_duplicate_ids_do_not_merge(tmp_path):
    root = tmp_path / 'vault'
    path = write_rule(root)
    unknown = root / 'knowledge' / 'accepted' / 'knowledge.md'
    unknown.parent.mkdir(parents=True)
    unknown.write_text('---\nid: rule-one\nstatus: accepted\n---\n\nSQLite 事务', encoding='utf-8')
    store = MemoryStore.initialize(tmp_path / 'memory.db')
    report = store.import_vault(root, dry_run=False)
    assert report['created'] == 2
    assert report['feedback_skipped'] == 1
    assert len(set(report['mapping'].values())) == 2
    assert len(store.search('SQLite', project='demo', changed_path='src/main.py')) == 2
    unresolved = store.get(report['mapping']['knowledge/accepted/knowledge.md']).draft
    assert unresolved.provenance.projects == ()
    assert unresolved.scope.visibility == 'scoped'
    assert [condition.result for condition in unresolved.applicability] == ['unknown']


def test_import_failure_rolls_back_documents_receipts_and_feedback(tmp_path, monkeypatch):
    root = tmp_path / 'vault'
    write_rule(root)
    feedback = feedback_database(tmp_path / 'queue.db')
    store = MemoryStore.initialize(tmp_path / 'memory.db')

    def fail(*args):
        raise RuntimeError('disk fault')

    monkeypatch.setattr(store, '_index', fail)
    with pytest.raises(RuntimeError):
        store.import_vault(root, feedback_db=feedback, dry_run=False)
    with sqlite3.connect(store.path) as connection:
        for table in ['memories', 'memory_versions', 'memory_sources', 'memory_events', 'import_items', 'import_receipts', 'import_snapshots']:
            assert connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0


def test_missing_feedback_database_not_created(tmp_path):
    root = tmp_path / 'vault'
    write_rule(root)
    missing = tmp_path / 'missing.db'
    with pytest.raises(MemoryStoreError, match='legacy feedback'):
        preview_vault(root, feedback_db=missing)
    assert not missing.exists()


def test_invalid_yaml_no_partial_import(tmp_path):
    root = tmp_path / 'vault'
    write_rule(root)
    (root / 'rules' / 'bad.md').write_text('---\nid: [unclosed\n---\nbody', encoding='utf-8')
    store = MemoryStore.initialize(tmp_path / 'memory.db')
    with pytest.raises(MemoryStoreError, match='cannot parse'):
        store.import_vault(root, dry_run=False)
    with sqlite3.connect(store.path) as connection:
        assert connection.execute('SELECT count(*) FROM memories').fetchone()[0] == 0


def test_disabled_snapshot_never_reenabled_by_import(tmp_path):
    root = tmp_path / 'vault'
    write_rule(root, status='disabled', feedback='')
    store = MemoryStore.initialize(tmp_path / 'memory.db')
    imported = store.import_vault(root, dry_run=False)
    write_rule(root, status='enabled', feedback='')
    assert store.import_vault(root, dry_run=False, snapshot_order=1)['protected'] == 1
    assert store.get(next(iter(imported['mapping'].values()))).draft.status == 'disabled'


def test_import_vault_output_retains_project_and_versioned_derivation(tmp_path):
    from context_forge.domain import KnowledgeItem, RuleExtraction, SessionArtifacts
    from context_forge.vault import Vault
    root = tmp_path / 'vault'
    vault = Vault(root)
    knowledge = KnowledgeItem(id='lesson', body='SQLite 事务', sources=['E1'])
    vault.write_knowledge_auto('session-a', knowledge, SessionArtifacts(title='lesson', knowledge=knowledge))
    vault.write_rule_candidate('session-a', 'lesson', 'demo', RuleExtraction(instruction='SQLite 事务', paths=['src/**']))
    store = MemoryStore.initialize(tmp_path / 'memory.db')
    report = store.import_vault(root, dry_run=False)
    knowledge_id = report['mapping']['knowledge/accepted/lesson.md']
    rule_id = report['mapping']['rules/proposals/rule-lesson.md']
    assert store.get(knowledge_id).draft.provenance.projects == ('demo',)
    assert knowledge_id in [record.id for record in store.search('SQLite', project='demo')]
    assert knowledge_id in [record.id for record in store.search('SQLite', project='other')]
    relation = store.relations(rule_id)[0]
    assert (relation['target_id'], relation['target_version'], relation['kind']) == (knowledge_id, 1, 'derived_from')
    assert store.import_vault(root, dry_run=False)['unchanged'] == 2
    assert len(store.relations(rule_id)) == 1


def test_session_local_evidence_and_multi_project_knowledge(tmp_path):
    root = tmp_path / 'vault'
    knowledge = root / 'knowledge' / 'accepted'
    rules = root / 'rules'
    knowledge.mkdir(parents=True)
    rules.mkdir()
    for label in ['a', 'b']:
        (knowledge / f'{label}.md').write_text(f'---\nid: {label}\nsession_id: {label}\nsources: E1\nstatus: accepted\n---\nSQLite', encoding='utf-8')
        (rules / f'{label}.md').write_text(f'---\nid: rule-{label}\nsource_knowledge_id: a\nproject: project-{label}\nstatus: enabled\n---\nSQLite', encoding='utf-8')
    store = MemoryStore.initialize(tmp_path / 'memory.db')
    report = store.import_vault(root, dry_run=False)
    first = store.get(report['mapping']['knowledge/accepted/a.md'])
    second = store.get(report['mapping']['knowledge/accepted/b.md'])
    assert first.draft.sources[1].identity != second.draft.sources[1].identity
    assert set(first.draft.provenance.projects) == {'project-a', 'project-b'}
    assert second.draft.provenance.projects == ()
    assert second.draft.scope.visibility == 'scoped'
    assert [condition.result for condition in second.draft.applicability] == ['unknown']
    assert len(store.search('SQLite', project='project-c')) == 4
