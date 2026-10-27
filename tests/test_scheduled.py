import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from context_forge import doctor, scheduled
from context_forge.queue import JobStore

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


def counting(ran: list[str], name: str):
    def step(store):
        ran.append(name)
    return step


def test_a_due_step_runs_once_per_period(tmp_path: Path):
    store = JobStore(tmp_path / 'queue.db')
    ran: list[str] = []
    steps = {'demo': counting(ran, 'demo')}

    assert scheduled.run_due(store, NOW, steps=steps) == ['demo']
    assert scheduled.run_due(store, NOW + timedelta(minutes=5), steps=steps) == []
    assert scheduled.run_due(store, NOW + timedelta(seconds=scheduled.DAILY_SECONDS),
                             steps=steps) == ['demo']
    assert ran == ['demo', 'demo']


def test_a_failing_step_does_not_block_the_next_one(tmp_path: Path):
    store = JobStore(tmp_path / 'queue.db')
    ran: list[str] = []

    def boom(store):
        raise RuntimeError('disk fault')

    steps = {'a-boom': boom, 'b-ok': counting(ran, 'b-ok')}

    assert scheduled.run_due(store, NOW, steps=steps) == ['b-ok']
    assert ran == ['b-ok']                      # the healthy step still ran
    rows = {row['step']: row for row in store.scheduled_runs()}
    assert rows['a-boom']['last_error'] == 'RuntimeError: disk fault'
    assert rows['a-boom']['last_success_at'] is None
    assert rows['b-ok']['last_success_at'] == NOW.isoformat()


def test_a_later_success_clears_the_recorded_failure(tmp_path: Path):
    store = JobStore(tmp_path / 'queue.db')

    def boom(store):
        raise RuntimeError('disk fault')

    scheduled.run_due(store, NOW, steps={'demo': boom})
    assert store.scheduled_runs()[0]['attempts'] == 1

    scheduled.run_due(store, NOW + timedelta(seconds=scheduled.DAILY_SECONDS),
                      steps={'demo': lambda store: None})

    row = store.scheduled_runs()[0]
    assert row['attempts'] == 0 and row['last_error'] is None


def test_attempts_accumulate_without_stopping_the_step(tmp_path: Path):
    """A daily step must not dead-letter itself into silence."""
    store = JobStore(tmp_path / 'queue.db')
    calls: list[str] = []

    def boom(store):
        calls.append('boom')
        raise RuntimeError('disk fault')

    for day in range(3):
        scheduled.run_due(store, NOW + timedelta(days=day), steps={'demo': boom})

    assert len(calls) == 3
    assert store.scheduled_runs()[0]['attempts'] == 3


def test_retention_step_deletes_expired_sessions(tmp_path: Path, monkeypatch):
    monkeypatch.setenv('CONTEXT_FORGE_HOME', str(tmp_path))
    store = JobStore(tmp_path / 'queue.db')
    with store.db:
        store.db.execute("INSERT INTO sessions VALUES "
                         "('old','demo','/tmp','t.jsonl','hash','done','2026-01-01T00:00:00+00:00')")
        store.db.execute("INSERT INTO sessions VALUES "
                         "('new','demo','/tmp','t.jsonl','hash','done',"
                         f"'{NOW.isoformat()}')")

    assert scheduled.run_due(store, NOW) == ['retention']

    remaining = {row[0] for row in store.db.execute('SELECT id FROM sessions')}
    assert remaining == {'new'}


def test_retention_reaches_episodes_without_a_sessions_row(tmp_path: Path, monkeypatch):
    """Measured on real data: one `sessions` row for 35 episodes. Reading only that
    table left every checkpoint of the other 34 sessions immortal."""
    from context_forge.episodes import EpisodeCollector

    monkeypatch.setenv('CONTEXT_FORGE_HOME', str(tmp_path))
    transcript = tmp_path / 's.jsonl'
    transcript.write_text('{"type":"user"}\n', encoding='utf-8')
    store = JobStore(tmp_path / 'queue.db')
    collector = EpisodeCollector(store)
    collector.collect('episode-only', transcript, 'demo', 'claude_code')
    receipt = collector.request_finalize('episode-only', end_kind='explicit', event_id='end-1')
    store.mark_receipt_committed(receipt.finalization_id)
    with store.db:
        store.db.execute("UPDATE session_episodes SET last_activity_at='2026-01-01T00:00:00+00:00'")

    assert store.db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == 0

    assert scheduled.run_due(store, NOW) == ['retention']
    assert store.latest_episode('episode-only') is None


def test_retention_spares_an_episode_whose_receipt_never_finished(tmp_path: Path, monkeypatch):
    """A pending extraction still owns the transcript; deleting it would lose the input."""
    from context_forge.episodes import EpisodeCollector

    monkeypatch.setenv('CONTEXT_FORGE_HOME', str(tmp_path))
    transcript = tmp_path / 's.jsonl'
    transcript.write_text('{"type":"user"}\n', encoding='utf-8')
    store = JobStore(tmp_path / 'queue.db')
    collector = EpisodeCollector(store)
    collector.collect('unfinished', transcript, 'demo', 'claude_code')
    collector.request_finalize('unfinished', end_kind='explicit', event_id='end-1')
    with store.db:
        store.db.execute("UPDATE session_episodes SET last_activity_at='2026-01-01T00:00:00+00:00'")

    assert scheduled.run_due(store, NOW) == ['retention']
    assert store.latest_episode('unfinished') is not None


def test_doctor_reports_failing_silent_and_healthy_ledgers(tmp_path: Path, monkeypatch):
    monkeypatch.setenv('CONTEXT_FORGE_HOME', str(tmp_path))
    path = tmp_path / 'queue.db'
    store = JobStore(path)

    ok, detail = doctor.check_scheduled()[1:]
    assert ok is False and 'has ever run' in detail        # nothing has run yet

    fresh = datetime.now(timezone.utc)
    store.record_scheduled_success('retention', fresh - timedelta(days=3))
    ok, detail = doctor.check_scheduled()[1:]
    assert ok is False and 'silent' in detail             # stale by two periods

    store.record_scheduled_success('retention', fresh)
    assert doctor.check_scheduled()[1] is True

    store.record_scheduled_failure('retention', fresh, 'RuntimeError: disk fault')
    ok, detail = doctor.check_scheduled()[1:]
    assert ok is False and 'disk fault' in detail


def test_dropped_ledger_table_is_recreated_on_open(tmp_path: Path):
    """The ledger is a new table in the derived queue.db, so opening supplies it."""
    path = tmp_path / 'queue.db'
    store = JobStore(path)
    with store.db:
        store.db.execute('DROP TABLE scheduled_runs')

    JobStore(path)                                       # reopening creates it again
    store.record_scheduled_success('retention', NOW)

    assert [row['step'] for row in store.scheduled_runs()] == ['retention']


def test_signals_step_is_skipped_without_a_memory_database(tmp_path: Path):
    """Skipping must not be recorded as success, or the ledger would lie."""
    store = JobStore(tmp_path / 'queue.db')

    assert 'signals' not in scheduled.run_due(store, NOW)
    assert 'signals' not in {row['step'] for row in store.scheduled_runs()}


def test_signals_step_aggregates_and_prunes_with_a_memory_database(tmp_path: Path, monkeypatch):
    from context_forge.memory import MemoryStore
    from context_forge.memory_models import MemoryDraft, Provenance, Source

    monkeypatch.setenv('CONTEXT_FORGE_HOME', str(tmp_path))
    body = 'cache keys must include the tenant id'
    memory = MemoryStore.initialize(tmp_path / 'memory.db')
    memory.put('m1', MemoryDraft(body=body, status='active', role='claim',
                                 provenance=Provenance(projects=('demo',), sessions=('s1',)),
                                 sources=(Source(identity='m1:tool:1', kind='tool', excerpt=body),)),
               expected_version=0, idempotency_key='put-1', actor='tool', reason='fixture')
    from datetime import datetime, timezone
    moment = datetime.now(timezone.utc).isoformat()
    memory.record_retrieval('m1', 1, request_id='req-1', stage='applied',
                            idempotency_key=f'req-1:applied:{moment}')
    store = JobStore(tmp_path / 'queue.db')

    ran = scheduled.run_due(store, NOW, memory_store=memory)

    assert 'signals' in ran
    assert memory.signal_totals() == {'stage:applied': 1}
    assert memory.aggregated_through() == scheduled._month_keys()[0]


def _duplicate_pair(tmp_path):
    from context_forge.memory import MemoryStore
    from context_forge.memory_models import MemoryDraft, Provenance, Source

    memory = MemoryStore.initialize(tmp_path / 'memory.db')
    body = 'always validate external input'
    for memory_id in ('keep', 'twin'):
        memory.put(memory_id, MemoryDraft(
            body=body, status='active', provenance=Provenance(projects=('demo',)),
            sources=(Source(identity=f'source:{memory_id}', kind='observation', excerpt=body),),
        ), expected_version=0, idempotency_key=f'put:{memory_id}', reason='test')
    return memory


def test_the_hygiene_step_canonicalizes_once_per_day(tmp_path: Path):
    store = JobStore(tmp_path / 'queue.db')
    memory = _duplicate_pair(tmp_path)
    steps = {'hygiene': scheduled.STEPS['hygiene']}

    assert scheduled.run_due(store, NOW, steps=steps, memory_store=memory) == ['hygiene']
    assert memory.get('twin').draft.status == 'archived'

    assert scheduled.run_due(store, NOW + timedelta(minutes=5), steps=steps,
                             memory_store=memory) == []          # not due yet
    rows = {row['step']: row for row in store.scheduled_runs()}
    assert rows['hygiene']['last_error'] is None
    assert rows['hygiene']['last_success_at'] == NOW.isoformat()


def test_the_hygiene_step_is_skipped_when_no_library_exists(tmp_path: Path):
    """Skipping is not succeeding: a ledger entry here would date a pass that never looked
    at any memory, and the next real gap would read as healthy."""
    store = JobStore(tmp_path / 'queue.db')
    steps = {'hygiene': scheduled.STEPS['hygiene']}

    assert scheduled.run_due(store, NOW, steps=steps, memory_store=None) == []
    assert store.scheduled_runs() == []


def test_the_hygiene_step_tolerates_being_called_without_a_library(tmp_path: Path):
    """`run_due` already skips it; the guard inside is for anyone calling the step directly."""
    store = JobStore(tmp_path / 'queue.db')

    scheduled.STEPS['hygiene'](scheduled.Context(store, None))      # must not raise


def _edges(memory, kind: str) -> set[int]:
    """Relation ids of one kind between the pair. `relations()` yields both directions,
    so dedupe by id or every edge is counted twice."""
    return {item['id'] for mid in ('keep', 'twin')
            for item in memory.relations(mid) if item['kind'] == kind}


def test_the_hygiene_key_derives_from_the_pair_not_the_calendar(tmp_path: Path, monkeypatch):
    """AC #4: the idempotency key must come from the candidate's identity, never a date or
    run number -- a calendar stamp would re-archive the same pair on every pass."""
    from context_forge.memory_merge import MemoryMerge

    store = JobStore(tmp_path / 'queue.db')
    memory = _duplicate_pair(tmp_path)
    twin_version = memory.get('twin').version

    seen: list[str] = []
    original = MemoryMerge.apply_exact

    def spy(self, candidate, *, idempotency_key):
        seen.append(idempotency_key)
        return original(self, candidate, idempotency_key=idempotency_key)

    monkeypatch.setattr(MemoryMerge, 'apply_exact', spy)
    scheduled.STEPS['hygiene'](scheduled.Context(store, memory))

    assert seen == [f'hygiene:exact:keep:twin@{twin_version}']
    assert date.today().strftime('%Y-%m-%d') not in seen[0]
    assert date.today().strftime('%Y-%m') not in seen[0]


def test_re_running_the_hygiene_step_does_not_duplicate_data(tmp_path: Path):
    """AC #4: bypass `run_due`'s throttle and run the step twice in a row -- it must be a no-op."""
    store = JobStore(tmp_path / 'queue.db')
    memory = _duplicate_pair(tmp_path)
    ctx = scheduled.Context(store, memory)

    scheduled.STEPS['hygiene'](ctx)
    assert memory.get('twin').draft.status == 'archived'
    twin_version = memory.get('twin').version
    assert len(_edges(memory, 'duplicate_of')) == 1

    scheduled.STEPS['hygiene'](ctx)
    assert memory.get('twin').draft.status == 'archived'       # still canonicalized
    assert memory.get('twin').version == twin_version          # no second write
    assert len(_edges(memory, 'duplicate_of')) == 1            # no extra relation


def test_re_running_the_hygiene_conflict_tier_keeps_a_single_note(tmp_path: Path):
    from context_forge.memory import MemoryStore
    from context_forge.memory_models import Applicability, MemoryDraft, Provenance, Source

    body = 'the cache key must include the tenant id'
    memory = MemoryStore.initialize(tmp_path / 'memory.db')
    for memory_id, result in (('keep', 'true'), ('twin', 'false')):
        memory.put(memory_id, MemoryDraft(
            body=body, status='active',
            applicability=(Applicability(condition='shared cache', result=result),),
            provenance=Provenance(projects=('demo',)),
            sources=(Source(identity=f'source:{memory_id}', kind='observation', excerpt=body),),
        ), expected_version=0, idempotency_key=f'put:{memory_id}', reason='test')

    ctx = scheduled.Context(JobStore(tmp_path / 'queue.db'), memory)

    scheduled.STEPS['hygiene'](ctx)
    assert len(_edges(memory, 'conflicts_with')) == 1

    scheduled.STEPS['hygiene'](ctx)
    assert len(_edges(memory, 'conflicts_with')) == 1


def test_retention_is_not_blocked_by_a_failed_receipt(tmp_path: Path, monkeypatch):
    """A failed extraction is terminal. Counting it as protection made the session
    uncollectable forever, so a failed episode held its transcript indefinitely."""
    from context_forge.episodes import EpisodeCollector

    monkeypatch.setenv('CONTEXT_FORGE_HOME', str(tmp_path))
    transcript = tmp_path / 's.jsonl'
    transcript.write_text('{"type":"user"}\n', encoding='utf-8')
    store = JobStore(tmp_path / 'queue.db')
    collector = EpisodeCollector(store)
    collector.collect('failed-one', transcript, 'demo', 'claude_code')
    receipt = collector.request_finalize('failed-one', end_kind='explicit', event_id='end-1')
    with store.db:
        store.db.execute("UPDATE finalization_receipts SET status='failed' "
                         "WHERE finalization_id=?", (receipt.finalization_id,))
        store.db.execute("UPDATE session_episodes SET state='finalized', "
                         "last_activity_at='2026-01-01T00:00:00+00:00'")

    assert scheduled.run_due(store, NOW) == ['retention']
    assert store.latest_episode('failed-one') is None


def test_retention_stamps_the_deadline_it_keeps_promising(tmp_path: Path,
                                                           monkeypatch) -> None:
    """`retention_deadline` was NULL on every episode: a column that looked like a
    schedule and carried none."""
    from datetime import datetime, timezone
    from context_forge.episodes import EpisodeCollector

    monkeypatch.setenv('CONTEXT_FORGE_HOME', str(tmp_path))
    transcript = tmp_path / 's.jsonl'
    transcript.write_text('{"type":"user"}\n', encoding='utf-8')
    store = JobStore(tmp_path / 'queue.db')
    collector = EpisodeCollector(store)
    collector.collect('s1', transcript, 'demo', 'claude_code')
    receipt = collector.request_finalize('s1', end_kind='explicit', event_id='end-1')
    store.mark_receipt_committed(receipt.finalization_id)
    last = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()
    with store.db:
        store.db.execute("UPDATE session_episodes SET last_activity_at=?", (last,))

    assert store.stamp_retention_deadlines(30) == 1

    deadline = store.db.execute(
        "SELECT retention_deadline FROM session_episodes WHERE session_id='s1'").fetchone()[0]
    assert deadline == datetime(2026, 1, 31, tzinfo=timezone.utc).isoformat()


def test_deleting_an_episode_marks_the_memory_sources_it_fed(tmp_path: Path,
                                                              monkeypatch) -> None:
    """The scheduled step deleted queue rows but left the memory claiming a session
    that no longer exists; the explicit delete path tombstoned them."""
    from context_forge.episodes import EpisodeCollector
    from context_forge.memory import MemoryStore
    from context_forge.memory_models import MemoryDraft, Provenance, Source

    monkeypatch.setenv('CONTEXT_FORGE_HOME', str(tmp_path))
    transcript = tmp_path / 's.jsonl'
    transcript.write_text('{"type":"user"}\n', encoding='utf-8')
    store = JobStore(tmp_path / 'queue.db')
    collector = EpisodeCollector(store)
    collector.collect('s1', transcript, 'demo', 'claude_code')
    receipt = collector.request_finalize('s1', end_kind='explicit', event_id='end-1')
    store.mark_receipt_committed(receipt.finalization_id)
    with store.db:
        store.db.execute("UPDATE session_episodes SET state='finalized', "
                         "last_activity_at='2026-01-01T00:00:00+00:00'")

    memory = MemoryStore.initialize(tmp_path / 'memory.db')
    memory.put('m1', MemoryDraft(body='a lesson from that session', status='active',
                                  provenance=Provenance(projects=('demo',), sessions=('s1',)),
                                  sources=(Source(identity='episode:s1:1', kind='observation',
                                                  session_id='s1', excerpt='the original words'),)),
               expected_version=0, idempotency_key='put-1', actor='model', reason='test')

    assert 'retention' in scheduled.run_due(store, NOW, memory_store=memory)
    assert store.latest_episode('s1') is None

    kept = memory.get('m1')
    assert kept.draft.sources[0].excerpt == ''
    assert kept.draft.sources[0].reference == 'source_deleted'


def test_the_retention_window_is_the_configured_one_not_twice(tmp_path: Path,
                                                              monkeypatch) -> None:
    """The stamped deadline is the expiry date. Comparing it against `now - days`
    kept every episode alive for twice its window."""
    from datetime import datetime, timedelta, timezone
    from context_forge.episodes import EpisodeCollector

    monkeypatch.setenv('CONTEXT_FORGE_HOME', str(tmp_path))
    transcript = tmp_path / 's.jsonl'
    transcript.write_text('{"type":"user"}\n', encoding='utf-8')
    store = JobStore(tmp_path / 'queue.db')
    collector = EpisodeCollector(store)
    collector.collect('s1', transcript, 'demo', 'claude_code')
    receipt = collector.request_finalize('s1', end_kind='explicit', event_id='end-1')
    store.mark_receipt_committed(receipt.finalization_id)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with store.db:
        store.db.execute("UPDATE session_episodes SET last_activity_at=?", (base.isoformat(),))
    store.stamp_retention_deadlines(30)

    assert store.retention_preview(30, at=base + timedelta(days=29)) == []
    due = store.retention_preview(30, at=base + timedelta(days=31))
    assert [row['session_id'] for row in due] == ['s1']
