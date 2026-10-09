"""F4: a high-risk rule waits for a human in a terminal, and the wait is server-side state.

None of these guarantees can be carried by text in a response, which is the whole reason the
ledger exists. Each guard was mutated and the result recorded in the task notes.
"""

import io
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from context_forge import doctor
from context_forge.episodes import EpisodeCollector
from context_forge.finalization import FinalizationService
from context_forge.memory import CONFIRMATION_TTL_SECONDS, MemoryStore, now
from context_forge.memory_models import MemoryDraft, Provenance, Source
from context_forge.queue import JobStore

HIGH_RISK = '生产环境回滚所有迁移前必须先备份'


class Gateway:
    def __init__(self, body, session):
        self.body, self.session = body, session

    def finalize_episode(self, payload):
        source = Source(identity=f'episode:{self.session}:1:1', kind='observation',
                        session_id=self.session, excerpt=self.body)
        return {'schema_version': 1, 'candidates': [{
            'candidate_id': 'rule', 'evidence_refs': [source.identity],
            'draft': MemoryDraft(body=self.body, kind='rule', status='active',
                                  provenance=Provenance(sessions=(self.session,)),
                                  sources=(source,)).model_dump(mode='json')}],
            'deferred_topics': [], 'no_memory_reason': None}


def precipitate(tmp_path: Path, session: str, body: str = HIGH_RISK):
    queue = JobStore(tmp_path / 'queue.db')
    memory = MemoryStore.initialize(tmp_path / 'memory.db')
    collector = EpisodeCollector(queue)
    transcript = tmp_path / f'{session}.jsonl'
    transcript.write_text('{"message":"x"}\n', encoding='utf-8')
    collector.collect(session, transcript, 'demo', 'claude_code')
    receipt = collector.request_finalize(session, end_kind='manual', event_id=f'e-{session}')
    ids = FinalizationService(queue, memory, Gateway(body, session)).finalize(receipt.finalization_id)
    return memory, ids[0]


def store_with_rule(tmp_path: Path, name: str = 'memory.db', status: str = 'candidate'):
    store = MemoryStore.initialize(tmp_path / name)
    store.put('r', MemoryDraft(body=HIGH_RISK, kind='rule', status=status,
                               provenance=Provenance(projects=('demo',)),
                               sources=(Source(identity='s1', kind='observation', excerpt='x'),)),
              expected_version=0, idempotency_key='put-r', actor='model', reason='t')
    return store


def test_a_high_risk_rule_parks_as_candidate_and_announces_itself(tmp_path):
    memory, memory_id = precipitate(tmp_path, 's1')

    record = memory.get(memory_id)
    assert (record.draft.status, record.draft.kind, record.draft.risk) == ('candidate', 'rule', 'high')
    assert memory.search(HIGH_RISK, project='demo') == []        # recall refuses candidates
    pending = memory.pending_confirmations()
    assert [(item['memory_id'], item['risk'], item['kind']) for item in pending] \
        == [(memory_id, 'high', 'rule')]
    assert pending[0]['body'] == HIGH_RISK                       # the human reads the real text


def test_an_ordinary_rule_is_unaffected_by_the_gate(tmp_path):
    memory, memory_id = precipitate(tmp_path, 's2', body='prefer the smallest working change')

    assert memory.get(memory_id).draft.status == 'active'
    assert memory.pending_confirmations() == []


def test_an_approval_cannot_be_piped_and_a_forged_token_does_not_exist(tmp_path):
    store = store_with_rule(tmp_path)
    token = store.issue_confirmation('r', 1, reason='high-risk rule awaiting approval')

    with pytest.raises(PermissionError, match='terminal'):
        store.approve_confirmation(token, from_tty=False)
    with pytest.raises(KeyError):
        store.approve_confirmation('0' * 36, from_tty=True)

    assert store.get('r').draft.status == 'candidate'            # refusal changed nothing


def test_tokens_are_not_recomputable_from_the_content_they_unlock(tmp_path):
    store = store_with_rule(tmp_path)

    first = store.issue_confirmation('r', 1, reason='a')
    second = store.issue_confirmation('r', 1, reason='a')

    assert first != second                                       # no handle derived from content


def test_approving_publishes_once_and_answering_twice_is_refused(tmp_path):
    store = store_with_rule(tmp_path)
    token = store.issue_confirmation('r', 1, reason='high-risk rule awaiting approval')

    record = store.approve_confirmation(token, from_tty=True)

    assert (record.id, record.draft.status, record.version) == ('r', 'active', 2)
    assert store.search(HIGH_RISK, project='demo') != []
    assert store.pending_confirmations() == []
    with pytest.raises(PermissionError, match='already answered'):
        store.approve_confirmation(token, from_tty=True)
    trail = store.confirmation_audit_trail()
    assert [(row['approved_from_tty'], row['memory_id'], row['version']) for row in trail] \
        == [(1, 'r', 1)]


def test_a_new_version_invalidates_the_outstanding_approval(tmp_path):
    store = store_with_rule(tmp_path)
    token = store.issue_confirmation('r', 1, reason='high-risk rule awaiting approval')
    current = store.get('r')
    store.put('r', current.draft.model_copy(update={'body': HIGH_RISK + ' v2'}),
              expected_version=current.version, idempotency_key='edit', actor='human',
              reason='reworded')

    assert store.pending_confirmations() == []                   # not about the memory as it stands
    with pytest.raises(PermissionError, match='no longer applies'):
        store.approve_confirmation(token, from_tty=True)


def test_an_expired_request_cannot_be_answered(tmp_path):
    store = store_with_rule(tmp_path)
    token = store.issue_confirmation('r', 1, reason='high-risk rule awaiting approval')
    with sqlite3.connect(store.path) as connection:
        connection.execute('UPDATE pending_confirmations SET expires_at=? WHERE token=?',
                           ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), token))

    assert store.pending_confirmations() == []
    with pytest.raises(PermissionError, match='expired'):
        store.approve_confirmation(token, from_tty=True)


def test_the_migration_gains_the_ledger_without_touching_records(tmp_path):
    path = tmp_path / 'memory.db'
    store = MemoryStore.initialize(path)
    store.put('one', MemoryDraft(body='plain', status='active',
                                 provenance=Provenance(projects=('demo',))),
              expected_version=0, idempotency_key='put-one', reason='t')
    with sqlite3.connect(path) as connection:
        connection.execute('PRAGMA user_version=8')
        connection.execute('DROP TABLE pending_confirmations')

    report = MemoryStore.migrate(path, dry_run=True)
    assert (report['from'], report['to'], report['dry_run']) == (8, 9, True)
    with sqlite3.connect(path) as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0] == 8   # dry run wrote nothing

    MemoryStore.migrate(path, dry_run=False)
    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute(
            'PRAGMA table_info(pending_confirmations)')}
        assert connection.execute('SELECT COUNT(*) FROM memories').fetchone()[0] == 1
    assert {'token', 'approved_at', 'approved_from_tty', 'used_at'} <= columns


def _cli_store_and_token(tmp_path, monkeypatch):
    import context_forge.cli as cli

    monkeypatch.setenv('CONTEXT_FORGE_HOME', str(tmp_path))
    store = store_with_rule(tmp_path, 'memory.db')
    token = store.issue_confirmation('r', 1, reason='high-risk rule awaiting approval')
    return cli, store, token


def test_the_cli_lists_a_waiting_rule_then_publishes_it_after_a_challenge(tmp_path, monkeypatch,
                                                                          capsys):
    from typer.testing import CliRunner

    cli, store, token = _cli_store_and_token(tmp_path, monkeypatch)

    listing = CliRunner().invoke(cli.app, ['approve'])
    assert token[:8] in listing.output and 'awaiting approval' in listing.output

    preview = CliRunner().invoke(cli.app, ['approve', '--token', token])
    assert 'dry run' in preview.output and HIGH_RISK in preview.output
    assert store.get('r').draft.status == 'candidate'

    challenge = token.replace('-', '')[-6:]
    answers = io.StringIO(challenge + '\n')
    answers.isatty = lambda: True
    monkeypatch.setattr('sys.stdin', answers)
    monkeypatch.setattr(cli, '_ask', lambda prompt: answers.readline())

    # Called directly: CliRunner swaps sys.stdin for its own pipe, which would trip the very
    # tty guard this test is about.
    cli.approve(token=token, apply=True)

    out = capsys.readouterr().out
    assert 'dry run' in preview.output and HIGH_RISK in preview.output      # the preview wrote nothing
    assert 'status=active' in out and store.get('r').draft.status == 'active'


def test_a_wrong_challenge_publishs_nothing(tmp_path, monkeypatch, capsys):
    cli, store, token = _cli_store_and_token(tmp_path, monkeypatch)
    answers = io.StringIO('zzzzzz\n')
    answers.isatty = lambda: True
    monkeypatch.setattr('sys.stdin', answers)
    monkeypatch.setattr(cli, '_ask', lambda prompt: answers.readline())

    import typer

    with pytest.raises(typer.Exit) as stopped:
        cli.approve(token=token, apply=True)

    assert stopped.value.exit_code == 2
    assert 'challenge did not match' in capsys.readouterr().out
    assert store.get('r').draft.status == 'candidate'


def test_a_piped_approval_is_refused_before_anything_is_answered(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    cli, store, token = _cli_store_and_token(tmp_path, monkeypatch)

    piped = CliRunner().invoke(cli.app, ['approve', '--token', token, '--apply'], input='x\n')

    assert piped.exit_code == 2 and 'needs a terminal' in piped.output
    assert store.get('r').draft.status == 'candidate'


def test_doctor_reports_a_stalled_approval_and_ignores_a_fresh_one(tmp_path, monkeypatch):
    monkeypatch.setenv('CONTEXT_FORGE_HOME', str(tmp_path))
    store = store_with_rule(tmp_path, 'memory.db')
    token = store.issue_confirmation('r', 1, reason='high-risk rule awaiting approval')

    name, ok, detail = doctor.check_confirmations()

    assert (name, ok) == ('confirmations', True) and '1 awaiting' in detail

    with sqlite3.connect(tmp_path / 'memory.db') as connection:
        connection.execute('UPDATE pending_confirmations SET expires_at=? WHERE token=?',
                           ('2000-01-01T00:00:00+00:00', token))

    name, ok, detail = doctor.check_confirmations()

    assert ok is False and 'expired unanswered' in detail
