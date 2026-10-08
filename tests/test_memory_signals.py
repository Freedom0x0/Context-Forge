"""Aggregates, derived dimension columns and the aggregate-before-prune invariant (F2)."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from context_forge.memory import MemoryStore
from context_forge.memory_models import MemoryDraft, Provenance, Source

BODY = 'cache keys must include the tenant id'


@pytest.fixture
def store(tmp_path: Path):
    memory = MemoryStore.initialize(tmp_path / 'memory.db')
    memory.put('m1', MemoryDraft(body=BODY, status='active', role='claim',
                                 model='test-model', prompt_version='session-artifacts-v1',
                                 provenance=Provenance(projects=('demo',), sessions=('s1',),
                                                       host='codex'),
                                 sources=(Source(identity='m1:tool:1', kind='tool', excerpt=BODY),)),
               expected_version=0, idempotency_key='put-1', actor='tool', reason='fixture')
    return memory


def columns(path: Path) -> list[str]:
    with sqlite3.connect(path) as connection:
        return [row[1] for row in connection.execute('PRAGMA table_info(memories)')]


def memory_row(path: Path, memory_id: str = 'm1') -> dict:
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute('SELECT * FROM memories WHERE id=?', (memory_id,)).fetchone()
        return dict(row)


def insert_event(store: MemoryStore, stage: str, created_at: str, key: str) -> None:
    with store._connection(write=True) as connection:
        connection.execute(
            'INSERT INTO retrieval_events(idempotency_key,request_id,stage,memory_id,version,'
            'session_id,explanation_json,created_at) VALUES (?,?,?,?,?,?,?,?)',
            (key, key, stage, 'm1', 1, 's1', '{}', created_at))


def this_month() -> str:
    return datetime.now(timezone.utc).isoformat()[:7]


def test_write_path_caches_the_bucket_dimensions(tmp_path: Path, store: MemoryStore):
    row = memory_row(tmp_path / 'memory.db')

    assert row['host'] == 'codex'
    assert row['model'] == 'test-model'
    assert row['prompt_version'] == 'session-artifacts-v1'


def test_aggregation_overwrites_rather_than_accumulates(store: MemoryStore):
    month = this_month()
    insert_event(store, 'applied', f'{month}-03T10:00:00+00:00', 'e1')
    insert_event(store, 'validated', f'{month}-03T11:00:00+00:00', 'e2')

    store.aggregate_signals(month)
    first = store.signal_totals()
    store.aggregate_signals(month)

    assert first == {'stage:applied': 1, 'stage:validated': 1}
    assert store.signal_totals() == first          # a re-run is a re-state, not a double count


def test_feedback_lands_in_the_vote_signal(store: MemoryStore):
    store.record_feedback('m1', outcome='harmful', idempotency_key='fb-1', note='bad advice')

    store.aggregate_signals(this_month())

    assert store.signal_totals()['vote:harmful'] == 1


def test_nothing_is_pruned_before_an_aggregate_covers_it(tmp_path: Path, store: MemoryStore):
    insert_event(store, 'applied', '2026-01-05T10:00:00+00:00', 'old')

    assert store.prune_retrieval_events() == 0
    assert memory_row(tmp_path / 'memory.db')          # store still usable
    with store._connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM retrieval_events "
                                  "WHERE created_at LIKE '2026-01%'").fetchone()[0] == 1


def test_pruning_removes_months_the_aggregate_already_covers(tmp_path: Path, store: MemoryStore):
    insert_event(store, 'applied', '2026-01-05T10:00:00+00:00', 'old')
    insert_event(store, 'applied', f'{this_month()}-05T10:00:00+00:00', 'fresh')
    assert store.aggregate_signals('2026-01')['rows'] == 1

    deleted = store.prune_retrieval_events()

    assert deleted == 1
    assert store.aggregated_through() == '2026-01'
    with store._connection() as connection:
        remaining = connection.execute(
            'SELECT created_at FROM retrieval_events').fetchall()
    assert [row['created_at'][:7] for row in remaining] == [this_month()]


def build_v5_database(tmp_path: Path) -> Path:
    path = tmp_path / 'memory.db'
    store = MemoryStore.initialize(path)
    store.put('m1', MemoryDraft(body=BODY, status='active', role='claim', model='test-model',
                                prompt_version='session-artifacts-v1',
                                provenance=Provenance(projects=('demo',), host='codex'),
                                sources=(Source(identity='m1:tool:1', kind='tool', excerpt=BODY),)),
               expected_version=0, idempotency_key='put-1', actor='tool', reason='fixture')
    with sqlite3.connect(path) as connection:
        connection.execute('DROP TABLE signal_counts')
        for name in ('prompt_version', 'model', 'host'):
            connection.execute(f'ALTER TABLE memories DROP COLUMN {name}')
        connection.execute('PRAGMA user_version=5')
    return path


def test_v5_database_gains_dimensions_and_the_aggregate(tmp_path: Path):
    path = build_v5_database(tmp_path / 'memory.db')

    report = MemoryStore.migrate(path, dry_run=False)

    assert report['to'] == 6
    assert {'host', 'model', 'prompt_version'} <= set(columns(path))
    row = memory_row(path)
    assert (row['host'], row['model'], row['prompt_version']) == ('codex', 'test-model',
                                                                 'session-artifacts-v1')
    with sqlite3.connect(path) as connection:
        assert connection.execute('SELECT COUNT(*) FROM signal_counts').fetchone()[0] == 0


def test_migrate_dry_run_leaves_a_v5_database_untouched(tmp_path: Path):
    path = build_v5_database(tmp_path / 'memory.db')
    before = path.read_bytes()

    report = MemoryStore.migrate(path)

    assert report['dry_run'] is True and report['from'] == 5
    assert path.read_bytes() == before
