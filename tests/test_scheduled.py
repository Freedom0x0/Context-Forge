import sqlite3
from datetime import datetime, timedelta, timezone
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
