"""Scan-failure counting (8a.3's failure half — problem 1 第二层)."""

import time
from pathlib import Path
from unittest import mock

from context_forge import doctor, watcher
from context_forge.memory import MemoryStore
from context_forge.memory_effectiveness import MemoryEffectiveness
from context_forge.queue import JobStore


def _scan(tmp_path: Path, monkeypatch, transcript: Path) -> None:
    """Run one watcher pass over a projects dir containing `transcript`."""
    monkeypatch.setenv('CONTEXT_FORGE_HOME', str(tmp_path))
    projects = transcript.parent.parent
    monkeypatch.setattr(watcher, 'transcript_root', lambda: projects)
    store = JobStore(tmp_path / 'queue.db')
    watcher.scan_once(store, None, None, settle_seconds=0.0, baseline=0.0,
                      now=time.time() + 10)


def test_no_meta_transcript_is_counted_and_not_precipitated(tmp_path: Path, monkeypatch):
    projects = tmp_path / 'projects' / 'slug'
    projects.mkdir(parents=True)
    transcript = projects / 's1.jsonl'
    # A complete line, but no `cwd` anywhere → read_session_meta returns None.
    transcript.write_text('{"message":"hi"}\n', encoding='utf-8')

    _scan(tmp_path, monkeypatch, transcript)

    store = JobStore(tmp_path / 'queue.db')
    assert store.scan_failures() == {'claude_code': {'no-meta': 1}}
    assert store.list_episodes() == []          # still skipped, only traced


def test_unreadable_transcript_is_counted_and_skipped(tmp_path: Path, monkeypatch):
    projects = tmp_path / 'projects'
    slug = projects / 'slug'
    slug.mkdir(parents=True)
    transcript = slug / 's1.jsonl'
    transcript.write_text('{"cwd":"/x","message":"hi"}\n', encoding='utf-8')

    real_stat = Path.stat

    def broken_stat(self, *args, **kwargs):
        if self.name == 's1.jsonl':
            raise OSError('transcript vanished mid-scan')
        return real_stat(self, *args, **kwargs)

    monkeypatch.setenv('CONTEXT_FORGE_HOME', str(tmp_path))
    monkeypatch.setattr(watcher, 'transcript_root', lambda: projects)
    with mock.patch.object(Path, 'stat', broken_stat):
        store = JobStore(tmp_path / 'queue.db')
        watcher.scan_once(store, None, None, settle_seconds=0.0, baseline=0.0,
                          now=time.time() + 10)

    store = JobStore(tmp_path / 'queue.db')
    assert store.scan_failures() == {'claude_code': {'unreadable': 1}}
    assert store.list_episodes() == []


def test_empty_transcript_is_counted_as_its_own_category(tmp_path: Path, monkeypatch):
    projects = tmp_path / 'projects' / 'slug'
    projects.mkdir(parents=True)
    transcript = projects / 's1.jsonl'
    transcript.write_text('no newline yet', encoding='utf-8')   # zero complete lines

    _scan(tmp_path, monkeypatch, transcript)

    store = JobStore(tmp_path / 'queue.db')
    assert store.scan_failures() == {'claude_code': {'empty': 1}}


def test_counts_bucket_by_host_and_accumulate_across_passes(tmp_path: Path, monkeypatch):
    projects = tmp_path / 'projects' / 'slug'
    projects.mkdir(parents=True)
    transcript = projects / 's1.jsonl'
    transcript.write_text('{"message":"hi"}\n', encoding='utf-8')

    _scan(tmp_path, monkeypatch, transcript)
    _scan(tmp_path, monkeypatch, transcript)     # second pass accumulates

    store = JobStore(tmp_path / 'queue.db')
    store.record_scan_failure('codex', 'empty')  # a second host, its own bucket
    assert store.scan_failures() == {
        'claude_code': {'no-meta': 2},
        'codex': {'empty': 1},
    }


def test_report_layer_reads_failures_beside_silence(tmp_path: Path, monkeypatch):
    projects = tmp_path / 'projects' / 'slug'
    projects.mkdir(parents=True)
    transcript = projects / 's1.jsonl'
    transcript.write_text('{"message":"hi"}\n', encoding='utf-8')
    _scan(tmp_path, monkeypatch, transcript)

    queue = JobStore(tmp_path / 'queue.db')
    memory = MemoryStore.initialize(tmp_path / 'memory.db')
    payload = MemoryEffectiveness(memory, queue).report(days=90).as_dict()

    health = payload['host_health']['claude_code']
    assert health['scan_failures'] == {'no-meta': 1}
    assert 'last_activity_at' in health           # silence sits beside the count


def test_doctor_names_host_and_category_and_can_fail(tmp_path: Path, monkeypatch):
    monkeypatch.setenv('CONTEXT_FORGE_HOME', str(tmp_path))
    store = JobStore(tmp_path / 'queue.db')
    store.record_scan_failure('claude_code', 'no-meta')
    store.record_scan_failure('claude_code', 'empty')

    name, ok, detail = doctor.check_host_silence()

    assert name == 'host-silence' and ok is False          # a gate that can fail
    assert 'claude_code scan failures' in detail
    assert 'no-meta=1' in detail and 'empty=1' in detail


def test_doctor_stays_green_when_no_failures(tmp_path: Path, monkeypatch):
    monkeypatch.setenv('CONTEXT_FORGE_HOME', str(tmp_path))
    JobStore(tmp_path / 'queue.db')

    name, ok, detail = doctor.check_host_silence()

    assert name == 'host-silence' and ok is True
