from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3

import pytest
from pydantic import ValidationError

from context_forge.memory import (
    IdempotencyConflict, MemoryStore, MemoryStoreError, ProtectedMemory,
    canonical_fingerprint,
    SourceConflict, VersionConflict,
)
from context_forge.memory_models import (
    AccessPolicy, Applicability, MemoryDraft, MemorySpace, Provenance, Relation, Scope, Source,
)
from context_forge.memory_schema import SCHEMA_VERSION


@pytest.fixture
def store(tmp_path):
    return MemoryStore.initialize(tmp_path / 'memory.db')


def draft(body='SQLite 数据库事务回滚', **overrides):
    return MemoryDraft(body=body, status='active',
                       provenance=Provenance(projects=('demo',)), **overrides)


def put(store, memory_id='one', value=None, **options):
    return store.put(memory_id, value or draft(), expected_version=options.pop('expected_version', 0),
                     idempotency_key=options.pop('idempotency_key', memory_id), reason='test evidence', **options)


def test_noncreating_reads_and_explicit_schema(tmp_path):
    path = tmp_path / 'absent' / 'memory.db'
    with pytest.raises(MemoryStoreError, match='memory database'):
        MemoryStore(path)
    assert not path.parent.exists()
    store = MemoryStore.initialize(path)
    assert MemoryStore.initialize(path).get('absent') is None
    with sqlite3.connect(path) as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION
        assert connection.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    with pytest.raises(MemoryStoreError, match='read-only'):
        put(MemoryStore(path))
    put(store)


@pytest.mark.parametrize('kind', ['corrupt', 'future', 'unrelated', 'empty'])
def test_invalid_database_is_not_an_empty_store(tmp_path, kind):
    path = tmp_path / 'bad.db'
    if kind == 'corrupt':
        path.write_bytes(b'not a SQLite file')
    else:
        with sqlite3.connect(path) as connection:
            if kind == 'future':
                connection.execute('PRAGMA user_version=999')
            elif kind == 'unrelated':
                connection.execute('CREATE TABLE private_data(secret TEXT)')
    original = path.read_bytes()
    with pytest.raises(MemoryStoreError):
        MemoryStore(path)
    assert path.read_bytes() == original
    if kind != 'empty':
        with pytest.raises(MemoryStoreError):
            MemoryStore.initialize(path)
        assert path.read_bytes() == original


def test_versions_idempotency_and_actor_normalization(store):
    first = put(store, actor='user')
    second = put(store, value=draft('corrected'), expected_version=1, idempotency_key='correction', actor='human')
    assert second.version == 2
    assert put(store, actor='human') == first
    assert store.get('one') == second
    assert store.get('one', 1) == first
    assert store.history('one') == [first, second]
    assert [event['actor'] for event in store.events('one')] == ['human', 'human']
    with pytest.raises(IdempotencyConflict):
        put(store, value=draft('different'), actor='human')
    with pytest.raises(VersionConflict):
        put(store, expected_version=1, idempotency_key='stale')
    assert len(store.history('one')) == 2


def test_two_writers_have_one_winner(store):
    put(store)

    def update(number):
        try:
            other = MemoryStore(store.path, readonly=False)
            return put(other, value=draft(f'writer {number}'), expected_version=1,
                       idempotency_key=f'write-{number}').version
        except VersionConflict:
            return 'conflict'

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, [1, 2]))
    assert results.count(2) == results.count('conflict') == 1
    assert len(store.events('one')) == len(store.history('one')) == 2


def test_failure_rolls_back_every_projection(store, monkeypatch):
    original = put(store)
    source = Source(identity='tool:check', kind='tool', excerpt='check failed')

    def fail(*args):
        raise RuntimeError('injected index failure')

    monkeypatch.setattr(store, '_index', fail)
    with pytest.raises(RuntimeError, match='injected'):
        put(store, value=draft('broken replacement', sources=(source,)), expected_version=1, idempotency_key='failed')
    assert store.get('one') == original
    assert len(store.events('one')) == 1
    assert store.search('事务', project='demo') == [original]
    with sqlite3.connect(store.path) as connection:
        assert connection.execute('SELECT count(*) FROM memory_sources').fetchone()[0] == 0
        assert connection.execute('SELECT count(*) FROM memory_access_projection').fetchone()[0] == 1
        assert connection.execute('SELECT count(*) FROM memory_applicability_projection').fetchone()[0] == 1
        assert connection.execute('PRAGMA foreign_key_check').fetchall() == []


def test_sources_deduplicate_and_collisions_fail(store):
    source = Source(identity='tool:run:1', kind='tool', excerpt='two checks passed', session_id='session-a')
    first = put(store, value=draft(sources=(source, source)))
    put(store, 'other', draft(sources=(source,)))
    assert first.draft.sources == (source,)
    changed = Source(identity=source.identity, kind='tool', excerpt='different result')
    with pytest.raises(SourceConflict):
        put(store, 'collision', draft(sources=(changed,)))
    assert store.get('collision') is None
    with sqlite3.connect(store.path) as connection:
        assert connection.execute('SELECT count(*) FROM memory_sources').fetchone()[0] == 1
        assert connection.execute('SELECT count(*) FROM memory_version_sources').fetchone()[0] == 2


def test_conditions_relations_history_and_revoke(store):
    put(store, 'target')
    source = Source(identity='tool:1', kind='tool', excerpt='Linux passed; Windows unchecked')
    conditions = (Applicability(condition='Linux', result='true', evidence_ids=(source.identity,)),
                  Applicability(condition='Windows', result='unknown'),
                  Applicability(condition='Python 2', result='false'))
    relation = Relation(target_id='target', target_version=1, kind='refines',
                        evidence_ids=(source.identity,), applicability=conditions)
    record = put(store, value=draft(sources=(source,), applicability=conditions), relations=(relation,))
    assert store.get('one').draft.applicability == conditions
    linked = store.relations('one')[0]
    assert [entry['result'] for entry in linked['applicability']] == ['true', 'unknown', 'false']
    assert linked['evidence_ids'] == [source.identity]
    assert store.relations('target')[0] == linked
    put(store, value=draft('new understanding'), expected_version=1, idempotency_key='new')
    assert store.relations('one') == []
    assert store.relations('one', record.version) == [linked]
    with pytest.raises(VersionConflict):
        store.revoke_relation(linked['id'], expected_version=1, idempotency_key='revoke', reason='wrong')
    store.revoke_relation(linked['id'], expected_version=2, idempotency_key='revoke', reason='wrong')
    store.revoke_relation(linked['id'], expected_version=2, idempotency_key='revoke', reason='wrong')
    assert store.relations('one', 1) == []
    assert store.relations('one', 1, include_revoked=True)[0]['revoked_event'] is not None


def test_relation_invalid_endpoint_rolls_back(store):
    with pytest.raises(MemoryStoreError, match='FOREIGN KEY'):
        put(store, relations=(Relation(target_id='missing', target_version=1, kind='supports'),))
    assert store.get('one') is None
    assert store.events('one') == []
    assert store.search('SQLite', project='demo') == []


def test_relation_missing_source_rolls_back(store):
    put(store, 'target')
    with pytest.raises(ValueError, match='relation evidence'):
        put(store, relations=(Relation(target_id='target', target_version=1, kind='supports', evidence_ids=('missing',)),))
    assert store.get('one') is None


@pytest.mark.parametrize('fields', [{'user_owned': True}, {'suppressed': True}, {'status': 'disabled'}])
def test_protected_memory_requires_human(store, fields):
    value = draft().model_copy(update=fields)
    put(store, value=value, actor='human')
    with pytest.raises(ProtectedMemory):
        put(store, value=draft('background'), expected_version=1, idempotency_key='background')
    corrected = put(store, value=draft('human restore'), expected_version=1, idempotency_key='human', actor='user')
    assert corrected.version == 2


def test_model_only_verification_and_invalid_conditions_rejected():
    with pytest.raises(ValidationError):
        draft(evidence_level='verified', sources=(Source(identity='model:1', kind='model', excerpt='I am sure'),))
    with pytest.raises(ValidationError):
        draft(applicability=(Applicability(condition='Linux', result='true', evidence_ids=('missing',)),))
    with pytest.raises(ValidationError):
        Scope(kind='global', project='demo')
    with pytest.raises(ValidationError):
        Scope(kind='project', project='unknown')


def test_search_chinese_ascii_safety_and_limits(store):
    restricted = AccessPolicy(visibility='selected_projects', allowed_projects=('other',))
    for number in range(12):
        put(store, f'other-{number}', MemoryDraft(body='SQLite 数据库事务回滚', status='active', access=restricted))
    record = put(store, 'visible')
    put(store, 'disabled', draft().model_copy(update={'status': 'disabled'}))
    assert store.search('数据库', project='demo', limit=1) == [record]
    assert store.search('SQLITE', project='demo') == [record]
    assert store.search('库', project='demo') == [record]
    assert store.search('SQLite', project=None) == [record]
    assert len(store.search('数据库', project='other', limit=None)) == 13
    assert store.search('"* - : ()', project='demo') == []
    # 'or' stays an ordinary word (quoted in the match string), and a term no document
    # contains is shed rather than dooming the query — but the hit has to say so.
    assert store.search('SQLite OR missing', project='demo') == [record]
    breakdown = store.search_scored('SQLite OR missing', project='demo')[0][1]
    assert '匹配不上的词' in breakdown.explanation
    assert store.search('SQLite', project='demo', byte_budget=1) == []


def test_hard_filter_before_truncation_and_stable_order(store):
    put(store, 'a-hidden', MemoryDraft(body='transaction', status='active',
                                       access=AccessPolicy(visibility='selected_projects',
                                                           allowed_projects=('hidden',))))
    for memory_id in ['z', 'b']:
        put(store, memory_id, MemoryDraft(body='transaction', status='active'))
    assert [record.id for record in store.search('transaction', project='demo', limit=1)] == ['b']
    assert [record.id for record in store.search('transaction', project='demo', limit=None)] == ['b', 'z']
    assert [record.id for record in store.search('transaction', project='hidden', limit=None)] == ['a-hidden', 'b', 'z']


def test_rebuild_missing_index_and_rollback(store, monkeypatch):
    put(store)
    expected = store.search('事务', project='demo')
    with sqlite3.connect(store.path) as connection:
        connection.execute('DROP TABLE memory_fts')
    with pytest.raises(MemoryStoreError, match='memory_fts'):
        store.search('事务', project='demo')
    assert store.rebuild_fts() == 1
    assert store.search('事务', project='demo') == expected

    def fail(*args):
        raise RuntimeError('rebuild interrupted')

    monkeypatch.setattr(store, '_index', fail)
    with pytest.raises(RuntimeError):
        store.rebuild_fts()
    assert store.search('事务', project='demo') == expected


def test_live_wal_backup_restore_and_export(store, tmp_path):
    with sqlite3.connect(store.path) as keeper:
        keeper.execute('PRAGMA wal_autocheckpoint=0')
        keeper.execute('BEGIN')
        keeper.execute('SELECT count(*) FROM memories').fetchone()
        put(store, 'target')
        source = Source(identity='tool:backup', kind='tool', excerpt='evidence retained')
        put(store, '../unsafe/id', draft(sources=(source,), applicability=(Applicability(condition='production'),)),
            relations=(Relation(target_id='target', target_version=1, kind='supports', evidence_ids=(source.identity,)),))
        put(store, 'target', draft('updated SQLite'), expected_version=1, idempotency_key='target-update')
        assert Path(str(store.path) + '-wal').exists()
        backup = store.backup(tmp_path / 'snapshot.db')
        restored = MemoryStore.restore(backup, tmp_path / 'restore' / 'memory.db')
        assert restored.history('target') == store.history('target')
        assert restored.events('../unsafe/id') == store.events('../unsafe/id')
        assert restored.relations('../unsafe/id') == store.relations('../unsafe/id')
        assert restored.get('../unsafe/id') == store.get('../unsafe/id')
        assert restored.search('SQLite', project='demo') == store.search('SQLite', project='demo')
        with pytest.raises(FileExistsError):
            MemoryStore.restore(backup, store.path)
    output = tmp_path / 'export'
    paths = restored.export_markdown(output)
    first = {path.name: path.read_bytes() for path in paths}
    assert all(path.parent == output for path in paths)
    assert {path.name: path.read_bytes() for path in restored.export_markdown(output)} == first
    failure = tmp_path / 'not-a-directory'
    failure.write_text('blocked')
    before = restored.history('target')
    with pytest.raises(OSError):
        restored.export_markdown(failure)
    assert restored.history('target') == before


def test_schema_constraint_rejects_bad_current_pointer(store):
    put(store)
    with sqlite3.connect(store.path) as connection:
        connection.execute('PRAGMA foreign_keys=ON')
        with pytest.raises(sqlite3.IntegrityError):
            with connection:
                connection.execute("UPDATE memories SET current_version=99 WHERE id='one'")
    assert store.get('one').version == 1


def test_export_does_not_follow_existing_symlink(store, tmp_path):
    from context_forge.memory import digest
    put(store)
    outside = tmp_path / 'outside.md'
    outside.write_text('keep this')
    output = tmp_path / 'export'
    output.mkdir()
    try:
        (output / (digest('one') + '.md')).symlink_to(outside)
    except OSError as error:
        pytest.skip(f'OS does not permit symlink creation: {error}')
    with pytest.raises(MemoryStoreError, match='symlink'):
        store.export_markdown(output)
    assert outside.read_text() == 'keep this'


def test_export_rejects_symlink_directory(store, tmp_path):
    put(store)
    outside = tmp_path / 'outside'
    outside.mkdir()
    output = tmp_path / 'export-link'
    try:
        output.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f'OS does not permit symlink creation: {error}')
    with pytest.raises(MemoryStoreError, match='directory is a symlink'):
        store.export_markdown(output)
    assert list(outside.iterdir()) == []


def test_model_cannot_revoke_relation_from_disabled_memory(store):
    put(store, 'target')
    source = Source(identity='tool:disabled', kind='tool', excerpt='observed')
    disabled = draft(sources=(source,)).model_copy(update={'status': 'disabled'})
    put(
        store,
        'disabled-source',
        disabled,
        relations=(
            Relation(
                target_id='target',
                target_version=1,
                kind='supports',
                evidence_ids=(source.identity,),
            ),
        ),
    )
    relation_id = store.relations('disabled-source')[0]['id']
    with pytest.raises(ProtectedMemory):
        store.revoke_relation(
            relation_id,
            expected_version=1,
            idempotency_key='model-revoke-disabled',
            reason='background cleanup',
            actor='model',
        )
    assert store.relations('disabled-source')[0]['id'] == relation_id


def test_discard_is_not_recalled(store):
    put(store, value=draft(value='discard'))
    assert store.search('SQLite', project='demo') == []


def test_put_preserves_created_at_and_fingerprint_column_order(store):
    record = put(store, 'column-order')

    loaded = store.get(record.id)
    assert loaded is not None
    assert loaded.created_at == record.created_at
    assert loaded.created_at[:4].isdigit()
    assert store.find_exact(canonical_fingerprint(record.draft)).id == record.id


def test_fingerprint_folds_case_and_spacing_but_still_separates_boundaries():
    """Case and whitespace are wording; kind, privacy and source projects are not."""
    folded = canonical_fingerprint(draft(body='SQLite 事务 要回滚'))

    assert canonical_fingerprint(draft(body='   sqlite   事务\n要回滚  ')) == folded
    assert canonical_fingerprint(draft(body='SQLite 事务 要回滚', kind='rule')) != folded
    assert canonical_fingerprint(draft(body='SQLite 事务 要回滚',
                                       space=MemorySpace(owner='default', privacy='private'))) != folded
    # Provenance is deliberately outside the fingerprint: the same assertion learned twice
    # in two projects is one memory, and the write gate must not fork it.
    assert canonical_fingerprint(MemoryDraft(
        body='SQLite 事务 要回滚', status='active',
        provenance=Provenance(projects=('other',)))) == folded


def test_find_exact_reaches_a_memory_written_with_different_case(store):
    """The hole this closes: 'SQLite …' and 'sqlite …' used to land as two active memories."""
    put(store, 'upper', draft(body='SQLite 事务 要回滚'))

    hit = store.find_exact(canonical_fingerprint(draft(body='sqlite 事务 要回滚')))

    assert hit is not None and hit.id == 'upper'


def test_migrate_recomputes_fingerprints_from_the_snapshot(tmp_path):
    """The fingerprint is a derived cache: authority stays in snapshot_json, so a definition
    change is one more backfill, not a data migration."""
    path = tmp_path / 'memory.db'
    store = MemoryStore.initialize(path)
    record = put(store, 'mixed-case', draft(body='SQLite 事务 要回滚'))
    expected = canonical_fingerprint(record.draft)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE memory_versions SET canonical_fingerprint=''")
        connection.execute('PRAGMA user_version=7')
    assert expected != ''

    report = MemoryStore.migrate(path, dry_run=True)
    assert (report['from'], report['to'], report['dry_run']) == (7, SCHEMA_VERSION, True)
    with sqlite3.connect(path) as connection:
        assert connection.execute('SELECT canonical_fingerprint FROM memory_versions').fetchone()[0] == ''

    MemoryStore.migrate(path, dry_run=False)
    with sqlite3.connect(path) as connection:
        stored = connection.execute('SELECT canonical_fingerprint FROM memory_versions').fetchone()[0]
        assert connection.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION
    assert stored == expected


@pytest.mark.parametrize('version', [6, 7])
def test_migrate_accepts_every_schema_older_than_the_current_one(tmp_path, version):
    """A hard-coded list of accepted versions rots on the next bump: v6 could not reach v7."""
    path = tmp_path / 'memory.db'
    put(MemoryStore.initialize(path), 'one', draft())
    with sqlite3.connect(path) as connection:
        connection.execute(f'PRAGMA user_version={version}')

    with pytest.raises(MemoryStoreError, match='initialize/migrate'):
        MemoryStore(path)                       # ordinary open must not migrate

    report = MemoryStore.migrate(path, dry_run=False)
    assert (report['from'], report['to']) == (version, SCHEMA_VERSION)
