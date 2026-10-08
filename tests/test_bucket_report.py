"""Bucket report (问题 1 第二层) and the host silence alarm (8a.3)."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from context_forge import doctor
from context_forge.episodes import EpisodeCollector
from context_forge.memory import MemoryStore
from context_forge.memory_effectiveness import MemoryEffectiveness
from context_forge.memory_models import MemoryDraft, Provenance, Source
from context_forge.queue import JobStore
from context_forge.scheduled import _month_keys

def put(store: MemoryStore, memory_id: str, host: str, body: str) -> None:
    store.put(memory_id, MemoryDraft(body=body, status='active', role='claim', model='m1',
                                     prompt_version='v1',
                                     provenance=Provenance(projects=('demo',), host=host),
                                     sources=(Source(identity=f'{memory_id}:tool:1', kind='tool',
                                                     excerpt=body),)),
               expected_version=0, idempotency_key=f'put-{memory_id}', actor='tool',
               reason='fixture')


@pytest.fixture
def seeded(tmp_path: Path):
    store = MemoryStore.initialize(tmp_path / 'memory.db')
    for index in range(25):
        put(store, f'cx-{index}', 'codex', f'codex lesson number {index} about cache keys')
    put(store, 'cc-1', 'claude_code', 'claude lesson about connection pools')
    store.record_feedback('cx-1', outcome='harmful', idempotency_key='fb-1', note='wrong')
    store.aggregate_signals(_month_keys()[0])
    return store


def test_bucket_report_splits_by_host_with_absolute_counts(seeded):
    host_rows = [row for row in seeded.bucket_report(months=6, min_sample=5)
                 if row['axis'] == 'host']

    by_bucket = {row['bucket']: row for row in host_rows}
    assert by_bucket['codex']['memories'] == 25
    assert by_bucket['codex']['against_memories'] == 1
    assert by_bucket['codex']['against_events'] == 1
    assert by_bucket['codex']['against_rate'] == pytest.approx(1 / 25)
    assert by_bucket['claude_code']['memories'] == 1


def test_rate_is_withheld_below_the_sample_floor(seeded):
    rows = [row for row in seeded.bucket_report(months=6) if row['axis'] == 'host']

    by_bucket = {row['bucket']: row for row in rows}
    assert by_bucket['codex']['against_rate'] is not None       # 25 memories clears the floor
    assert by_bucket['claude_code']['against_rate'] is None      # 1 memory does not
    assert '样本不足' in by_bucket['claude_code']['note']
    assert by_bucket['claude_code']['memories'] == 1             # the absolute count stays


def test_empty_aggregate_yields_no_buckets(tmp_path: Path):
    store = MemoryStore.initialize(tmp_path / 'memory.db')

    assert store.bucket_report() == []


def test_memory_report_carries_the_buckets(seeded):
    payload = MemoryEffectiveness(seeded).report(days=90).as_dict()

    assert {row['axis'] for row in payload['buckets']} == {'host', 'model+prompt_version',
                                                           'evidence_level'}
    assert payload['signals']['aggregated_through'] == _month_keys()[0]


def episode(store: JobStore, tmp_path: Path, session_id: str, host: str, days_ago: int):
    transcript = tmp_path / f'{session_id}.jsonl'
    transcript.write_text('{"message":"one"}\n', encoding='utf-8')
    collector = EpisodeCollector(store)
    collector.collect(session_id, transcript, 'demo', host)
    episode_record = store.latest_episode(session_id)
    when = datetime.now(timezone.utc) - timedelta(days=days_ago)
    store.save_episode(episode_record.model_copy(update={'last_activity_at': when.isoformat()}))


def test_host_silence_check_passes_for_recent_activity(tmp_path: Path, monkeypatch):
    monkeypatch.setenv('CONTEXT_FORGE_HOME', str(tmp_path))
    store = JobStore(tmp_path / 'queue.db')
    episode(store, tmp_path, 's1', 'claude_code', days_ago=1)

    name, ok, detail = doctor.check_host_silence()

    assert name == 'host-silence' and ok is True and 'claude_code last collected 1d ago' in detail


def test_host_silence_check_fails_when_a_host_stops_reporting(tmp_path: Path, monkeypatch):
    monkeypatch.setenv('CONTEXT_FORGE_HOME', str(tmp_path))
    store = JobStore(tmp_path / 'queue.db')
    episode(store, tmp_path, 's1', 'claude_code', days_ago=1)
    episode(store, tmp_path, 's2', 'codex', days_ago=30)

    ok, detail = doctor.check_host_silence()[1:]

    assert ok is False and 'codex silent for 30d' in detail
    assert 'claude_code' not in detail.split(';')[0]        # the healthy host is not blamed


def test_host_silence_is_quiet_when_nothing_was_ever_collected(tmp_path: Path, monkeypatch):
    monkeypatch.setenv('CONTEXT_FORGE_HOME', str(tmp_path))
    JobStore(tmp_path / 'queue.db')

    ok, detail = doctor.check_host_silence()[1:]

    assert ok is True and 'no episodes' in detail
