import json
from pathlib import Path

import pytest

from context_forge.domain import SessionLifecycleEvent
from context_forge.episodes import EpisodeCollector
from context_forge.queue import EventConflict, JobStore


def lifecycle_event(**updates):
    values = {
        "event_id": "event-1",
        "type": "session_end",
        "source": "test",
        "session_id": "session-1",
        "transcript_path": "session.jsonl",
        "observed_at": "2026-09-24T00:00:00+00:00",
        "final_line_count": 2,
        "reason": "explicit",
    }
    values.update(updates)
    return SessionLifecycleEvent(**values)


def test_lifecycle_event_is_request_idempotent(tmp_path: Path):
    store = JobStore(tmp_path / "queue.db")
    event = lifecycle_event()

    assert store.ingest_session_event(event) == "created"
    assert store.ingest_session_event(event) == "existing"

    with pytest.raises(EventConflict):
        store.ingest_session_event(lifecycle_event(reason="changed"))


def test_collector_appends_only_complete_new_lines(tmp_path: Path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"message":"one"}\n{"message":"partial"}', encoding="utf-8")
    store = JobStore(tmp_path / "queue.db")
    collector = EpisodeCollector(store)

    first = collector.collect("session-1", transcript, "demo")
    assert first.collected_through_line == 1
    assert first.state == "open"
    assert collector.collect("session-1", transcript, "demo") == first

    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(' tail\n')
    second = collector.collect("session-1", transcript, "demo")
    assert second.collected_through_line == 2
    assert second.transcript_digest != first.transcript_digest


def test_precompact_creates_checkpoint_without_long_term_write(tmp_path: Path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps({"type": "user", "message": {"content": "investigate cache"}}) + "\n",
        encoding="utf-8",
    )
    store = JobStore(tmp_path / "queue.db")
    collector = EpisodeCollector(store)
    collector.collect("session-1", transcript, "demo")

    event = lifecycle_event(type="pre_compact", reason="context_compaction", final_line_count=1)
    assert store.ingest_session_event(event) == "created"
    checkpoint = collector.checkpoint(event)
    assert checkpoint.session_id == "session-1"
    assert checkpoint.end_line == 1
    assert store.list_checkpoints("session-1") == [checkpoint]


def test_manual_finalize_is_idempotent_and_marks_episode(tmp_path: Path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"message":"one"}\n', encoding="utf-8")
    store = JobStore(tmp_path / "queue.db")
    collector = EpisodeCollector(store)
    collector.collect("session-1", transcript, "demo")

    first = collector.request_finalize("session-1", end_kind="manual", event_id="manual-1")
    second = collector.request_finalize("session-1", end_kind="manual", event_id="manual-1")
    assert first == second
    assert first.status == "extracting"
    assert store.episode("session-1").state == "finalizing"


def test_collect_after_finalized_episode_opens_new_segment(tmp_path: Path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"message":"one"}\n', encoding="utf-8")
    store = JobStore(tmp_path / "queue.db")
    collector = EpisodeCollector(store)
    collector.collect("session-1", transcript, "demo")
    receipt = collector.request_finalize("session-1", end_kind="explicit", event_id="end-1")
    store.mark_receipt_committed(receipt.finalization_id)

    with transcript.open("a", encoding="utf-8") as handle:
        handle.write('{"message":"two"}\n')
    resumed = collector.collect("session-1", transcript, "demo")
    assert resumed.segment == 2
    assert resumed.state == "open"
