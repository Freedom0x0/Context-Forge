import inspect
import json
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from context_forge.memory import MemoryStore, MemoryStoreError
from context_forge.memory_models import (
    DEFAULT_OWNER, AccessPolicy, ApplicabilityScope, MemoryDraft, MemorySpace,
    Provenance, Scope, derive_level,
)
from context_forge.memory_schema import SCHEMA_VERSION


def draft(**overrides):
    return MemoryDraft(body='SQLite 事务回滚', status='active', **overrides)


def put(store, memory_id, value, **options):
    return store.put(memory_id, value, expected_version=options.pop('expected_version', 0),
                     idempotency_key=options.pop('idempotency_key', memory_id),
                     reason='test', **options)


# P1.t1 -- legacy Scope(kind) maps deterministically, and every record gets a MemorySpace
def test_split_legacy_scopes_maps_every_kind_and_space():
    from context_forge.memory_models import split_legacy_scopes as split

    global_split = split((Scope(kind='global', paths=('src/**',)),))
    assert global_split.access.visibility == 'shared'
    assert global_split.scope.visibility == 'global'
    assert global_split.provenance.paths == ('src/**',)
    assert global_split.applicability == ()

    project_split = split((Scope(kind='project', project='demo', paths=('src/**/*.py',)),))
    assert project_split.provenance.projects == ('demo',)
    assert project_split.provenance.paths == ('src/**/*.py',)
    assert project_split.access.visibility == 'shared'
    assert project_split.scope.visibility == 'scoped'
    assert project_split.applicability == ()

    unknown_split = split((Scope(),))
    assert unknown_split.provenance.projects == ()
    assert unknown_split.scope.visibility == 'scoped'
    assert [c.result for c in unknown_split.applicability] == ['unknown']

    for split_value in (global_split, project_split, unknown_split):
        assert split_value.space.owner == DEFAULT_OWNER
        assert split_value.space.privacy == 'shared'

    private = split((Scope(),), owner='agent:other', privacy='private')
    assert (private.space.owner, private.space.privacy) == ('agent:other', 'private')
    merged = split((Scope(kind='project', project='b'), Scope(kind='project', project='a'),
                    Scope(kind='global', paths=('z/**', 'a/**'))))
    assert merged.provenance.projects == ('a', 'b')
    assert merged.provenance.paths == ('a/**', 'z/**')
    assert merged.scope.visibility == 'global'


# P1.t2 -- derive_level covers every role with no missing branch
def test_derive_level_covers_every_role():
    from typing import get_args

    roles = get_args(MemoryDraft.model_fields['role'].annotation)
    assert set(roles) == {'observation', 'claim', 'hypothesis', 'preference', 'procedure'}
    empty = ApplicabilityScope()
    scoped = ApplicabilityScope(technologies=('antd',))
    for role in roles:
        plain = draft(role=role, scope=empty,
                      provenance=Provenance(projects=('demo',)) if role == 'observation' else Provenance())
        tagged = draft(role=role, scope=scoped,
                       provenance=Provenance(projects=('demo',)) if role == 'observation' else Provenance())
        if role == 'observation':
            assert derive_level(plain) == derive_level(tagged) == 'instance'
        elif role == 'preference':
            assert derive_level(plain) == derive_level(tagged) == 'principle'
        else:
            assert derive_level(plain) == 'principle'
            assert derive_level(tagged) == 'pattern'
            assert derive_level(draft(role=role, scope=ApplicabilityScope(constraints=('offline',)))) == 'pattern'
    assert derive_level(draft(role='claim', scope=ApplicabilityScope(technologies=('a',)))) == \
        derive_level(draft(role='hypothesis', scope=ApplicabilityScope(technologies=('a',))))


def legacy_database(path: Path, records: tuple[tuple[str, dict], ...] = ()) -> Path:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            'CREATE TABLE memories(id TEXT PRIMARY KEY, kind TEXT NOT NULL, current_version INTEGER NOT NULL, '
            'status TEXT NOT NULL, user_owned INTEGER NOT NULL, suppressed INTEGER NOT NULL);\n'
            'CREATE TABLE memory_versions(memory_id TEXT NOT NULL, version INTEGER NOT NULL, '
            'snapshot_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(memory_id,version));\n'
            'CREATE TABLE memory_scopes(memory_id TEXT NOT NULL, version INTEGER NOT NULL, ordinal INTEGER NOT NULL, '
            'kind TEXT NOT NULL, project TEXT, paths_json TEXT NOT NULL, '
            'PRIMARY KEY(memory_id,version,ordinal));')
        for memory_id, snapshot in records:
            connection.execute('INSERT INTO memories VALUES (?,?,?,?,?,?)', (memory_id, 'knowledge', 1, 'active', 0, 0))
            connection.execute('INSERT INTO memory_versions VALUES (?,?,?,?)',
                               (memory_id, 1, json.dumps(snapshot), '2026-01-01'))
            connection.execute('INSERT INTO memory_scopes VALUES (?,?,?,?,?,?)',
                               (memory_id, 1, 0, snapshot['scopes'][0]['kind'],
                                snapshot['scopes'][0].get('project'), '[]'))
        connection.execute('PRAGMA user_version=1')
    return path


def legacy_snapshot(**overrides):
    snapshot = {'kind': 'knowledge', 'role': 'claim', 'body': 'legacy', 'summary': '', 'status': 'active',
                'user_owned': False, 'suppressed': False, 'evidence_level': 'unknown', 'value': None,
                'confidence': None, 'risk': None, 'model': None, 'prompt_version': None,
                'scopes': [{'kind': 'project', 'project': 'keep-me', 'paths': ['a/**']}],
                'applicability': [], 'sources': [], 'metadata': {}}
    snapshot.update(overrides)
    return snapshot


# P1.t3 -- only initialize/migrate may change the schema
def test_implicit_migration_is_refused(tmp_path):
    path = legacy_database(tmp_path / 'memory.db', (('m', legacy_snapshot()),))
    before = path.read_bytes()
    with pytest.raises(MemoryStoreError, match='initialize/migrate'):
        MemoryStore(path)
    assert path.read_bytes() == before
    report = MemoryStore.migrate(path)
    assert (report['dry_run'], report['from'], report['to']) == (True, 1, SCHEMA_VERSION)
    assert path.read_bytes() == before
    assert MemoryStore.initialize(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE name='memory_scopes'").fetchone() is None


def test_migration_backup_is_restorable(tmp_path):
    path = legacy_database(tmp_path / 'memory.db', (('m', legacy_snapshot()),))
    report = MemoryStore.migrate(path, dry_run=False)
    restored = MemoryStore.restore(Path(report['backup']), tmp_path / 'restored.db')
    assert restored.get('m').draft.body == 'legacy'
    assert restored.get('m').draft.provenance.projects == ('keep-me',)


# a plain scalar silently breaks the whole frontmatter block
def test_yaml_scalar_quotes_every_dangerous_scalar():
    from context_forge.index import yaml_scalar

    for value in ('', '**/*', '*alias', '&anchor', '!tag', ':colon', '-dash', '?question',
                  ',comma', '#hash', '%dir', '@at', '`tick', '|pipe', '>fold',
                  'key: value', 'a #b', 'ends with colon:', 'multi\nline'):
        rendered = yaml_scalar(value)
        assert rendered != value, value
        assert rendered.startswith('"') and rendered.endswith('"'), (value, rendered)
    for value in ('plain', 'a:b', '中文值', 'src/**/*.ts,**/*.tsx', 'true', '42'):
        assert yaml_scalar(value) == value, value
    assert yaml_scalar('[a, b]') == '[a, b]' and yaml_scalar('{a: b}') == '{a: b}'


# P1.t4 -- tech_detect reads both manifest families
def test_tech_detect_reads_pyproject_and_package_json(tmp_path):
    from context_forge.tech_detect import detect

    (tmp_path / 'pyproject.toml').write_text(
        '[project]\nname = "demo"\nversion = "1.0.0"\ndescription = "a demo project for tests"\n'
        'dependencies = [\n  "pydantic>=2.0,<3.0",\n  "httpx>=0.27",\n]\n'
        '[project.optional-dependencies]\nmcp = ["mcp>=1.0,<2.0"]\n', encoding='utf-8')
    (tmp_path / 'package.json').write_text(
        json.dumps({'name': 'demo', 'dependencies': {'antd': '^5.0.0', 'react': '^18.0.0'},
                    'devDependencies': {'vite': '^5.0.0'}}), encoding='utf-8')
    profile = detect(tmp_path)
    assert {'pydantic', 'httpx', 'mcp', 'antd', 'react', 'vite'} <= set(profile.technologies)
    assert 'demo' not in profile.technologies
    assert 'a-demo-project-for-tests' not in profile.technologies
    assert profile.raw_evidence['antd'] == ('antd',)


# P1.t5 -- the vault migration writes nothing in dry-run
def test_retag_dry_run_writes_nothing(tmp_path):
    from context_forge.vault_retag import retag

    root = tmp_path / 'vault'
    rule = root / 'rules' / 'proposals'
    rule.mkdir(parents=True)
    target = rule / 'rule-one.md'
    original = ('---\ntype: rule\nid: rule-one\nproject: demo\nstatus: enabled\n'
                'sources: E1,tool-result:typecheck-output\n'
                'paths: **/*\n---\n\nalways validate external input before trusting it\n')
    target.write_text(original, encoding='utf-8')
    report = retag(root, dry_run=True)
    assert report['documents'] == 1
    assert report['changed'] == 1
    assert report['paths_collapsed'] == 1
    assert target.read_text(encoding='utf-8') == original
    assert not (root / '.retag-backup').exists()
    applied = retag(root, dry_run=False)
    assert applied['applied'] == 1
    assert (root / '.retag-backup' / 'rules' / 'proposals' / 'rule-one.md').read_text(encoding='utf-8') == original
    assert 'paths' not in target.read_text(encoding='utf-8')
    assert 'confidence: ' in target.read_text(encoding='utf-8')


def test_retag_leaves_user_owned_documents_alone(tmp_path):
    from context_forge.vault_retag import retag

    root = tmp_path / 'vault'
    rules = root / 'rules' / 'proposals'
    rules.mkdir(parents=True)
    target = rules / 'hand-written.md'
    original = '---\ntype: rule\nid: hand-written\nuser_owned: true\nstatus: enabled\n---\n\nhand written rule\n'
    target.write_text(original, encoding='utf-8')
    report = retag(root, dry_run=False)
    assert report['skipped_user_owned'] == 1
    assert report['changed'] == 0
    assert target.read_text(encoding='utf-8') == original


# P1.t6 -- the default filter path never matches on `project`
def test_search_never_filters_on_provenance_project(tmp_path):
    store = MemoryStore.initialize(tmp_path / 'memory.db')
    foreign = put(store, 'foreign', draft(provenance=Provenance(projects=('other',), sessions=('s1',))))
    local = put(store, 'local', draft(provenance=Provenance(projects=('demo',), sessions=('s2',))))
    hits = store.search('事务', project='demo')
    assert {record.id for record in hits} == {'local', 'foreign'}
    assert store.search('事务', project=None) == hits
    source = inspect.getsource(MemoryStore._boundary_visible)
    assert '.provenance' not in source
    assert 'allowed_projects' in source
    assert foreign.draft.provenance.projects == ('other',)


# P1.t7 -- provenance survives for every role and every migration
def test_provenance_is_preserved_for_every_role(tmp_path):
    from typing import get_args

    store = MemoryStore.initialize(tmp_path / 'memory.db')
    for role in get_args(MemoryDraft.model_fields['role'].annotation):
        record = put(store, f'memory-{role}', draft(
            role=role, provenance=Provenance(projects=('demo', 'legacy'), sessions=('s1',), paths=('src/**',))))
        assert record.draft.provenance.projects == ('demo', 'legacy')
        assert store.get(f'memory-{role}').draft.provenance.sessions == ('s1',)
        assert store.get(f'memory-{role}').draft.provenance.paths == ('src/**',)

    v1 = legacy_database(tmp_path / 'v1.db', (('m', legacy_snapshot()),))
    report = MemoryStore.migrate(v1, dry_run=False)
    assert report['mapping']['m@1']['projects'] == ['keep-me']
    store = MemoryStore.initialize(v1)
    migrated = store.get('m').draft
    assert migrated.provenance.projects == ('keep-me',)
    assert migrated.provenance.paths == ('a/**',)
    assert migrated.space.owner == DEFAULT_OWNER


# P1.t8 -- AccessPolicy and MemorySpace are different fields with different enums
def test_access_policy_and_privacy_are_distinct_hard_filters(tmp_path):
    from typing import get_args

    assert set(get_args(AccessPolicy.model_fields['visibility'].annotation)) == {
        'owner', 'selected_projects', 'shared'}
    assert set(get_args(MemorySpace.model_fields['privacy'].annotation)) == {'shared', 'private'}
    with pytest.raises(ValidationError):
        AccessPolicy(visibility='private')
    with pytest.raises(ValidationError):
        MemorySpace(privacy='selected_projects')
    with pytest.raises(ValidationError):
        AccessPolicy(visibility='selected_projects')

    store = MemoryStore.initialize(tmp_path / 'memory.db')
    put(store, 'mine-private', draft(space=MemorySpace(owner='user:local', privacy='private')))
    put(store, 'theirs-private', draft(space=MemorySpace(owner='agent:other', privacy='private')))
    put(store, 'theirs-shared', draft(space=MemorySpace(owner='agent:other', privacy='shared')))
    put(store, 'locked', draft(space=MemorySpace(owner='user:local'),
                               access=AccessPolicy(visibility='selected_projects',
                                                   allowed_projects=('demo',))))
    put(store, 'free', draft(space=MemorySpace(owner='user:local')))
    assert {record.id for record in store.search('事务', project='demo', owner='user:local')} == {
        'mine-private', 'locked', 'free'}
    assert {record.id for record in store.search('事务', project='other', owner='user:local')} == {
        'mine-private', 'free'}
    assert {record.id for record in store.search('事务', project='demo', owner='agent:other')} == {
        'theirs-private', 'theirs-shared'}

    # privacy is its own elimination, not a synonym for owner: the owner check
    # already drops every cross-owner record, so only a source pin can catch
    # the privacy rule being deleted
    source = inspect.getsource(MemoryStore._boundary_visible)
    assert 'space.privacy' in source and 'allowed_projects' in source


def test_exclusions_eliminate_before_results_are_returned(tmp_path):
    store = MemoryStore.initialize(tmp_path / 'memory.db')
    put(store, 'excluded', draft(scope=ApplicabilityScope(exclusions=('**/*.md',))))
    put(store, 'kept', draft())
    assert {record.id for record in store.search('事务', project='demo', changed_path='doc/readme.md')} == {'kept'}
    assert len(store.search('事务', project='demo', changed_path='src/main.py')) == 2
    assert len(store.search('事务', project='demo')) == 2


def test_projections_are_rebuildable_from_snapshots(tmp_path):
    store = MemoryStore.initialize(tmp_path / 'memory.db')
    put(store, 'one', draft(access=AccessPolicy(visibility='selected_projects', allowed_projects=('a',)),
                            scope=ApplicabilityScope(technologies=('antd',), domains=('search',),
                                                     exclusions=('legacy',)),
                            space=MemorySpace(owner='user:local', privacy='private')))
    with sqlite3.connect(store.path) as connection:
        connection.execute('DROP TABLE memory_access_projection')
        connection.execute('DROP TABLE memory_applicability_projection')
    assert store.rebuild_fts() == 1
    with sqlite3.connect(store.path) as connection:
        access = connection.execute('SELECT * FROM memory_access_projection').fetchone()
        applicability = connection.execute('SELECT * FROM memory_applicability_projection').fetchone()
    assert access[2:] == ('user:local', 'private', 'selected_projects', '["a"]')
    assert applicability[2:] == ('["antd"]', '["search"]', '["legacy"]')


# P1.0a -- the ranking key actually layers on real-shaped vault data
def test_rule_ranking_layers_on_confidence_and_path_hit(tmp_path):
    import os

    from context_forge.rules import RuleMatcher

    rules = tmp_path / 'rules' / 'proposals'
    rules.mkdir(parents=True)

    def write(name, confidence, paths, mtime):
        front = ['---', 'type: rule', f'id: {name}', 'project: demo', 'status: enabled']
        if confidence:
            front.append(f'confidence: {confidence}')
        if paths:
            front.append(f'paths: {paths}')
        path = rules / f'{name}.md'
        path.write_text('\n'.join(front) + '\n---\n\nvalidate external input\n', encoding='utf-8')
        os.utime(path, (mtime, mtime))

    write('hit-low', 'low', 'src/**/*.py', 300)
    write('hit-high', 'high', 'src/**/*.py', 200)
    write('hit-medium', 'medium', 'src/**/*.py', 100)
    write('hit-unstated', None, 'src/**/*.py', 50)
    write('plain-high', 'high', '', 400)

    matcher = RuleMatcher(tmp_path)
    ranked = [match.rule_id for match in matcher.match('demo', 'src/main.py', limit=0)]
    assert ranked == ['hit-high', 'hit-medium', 'hit-unstated', 'hit-low', 'plain-high']
    assert [match.rule_id for match in matcher.match('demo', 'src/main.py', limit=2)] == [
        'hit-high', 'hit-medium']
    assert len(matcher.match('demo', 'src/main.py', limit=0, legacy_filter=True)) == 5
