import json

import pytest

from context_forge.hooks.generic import handle_event, handle_json
from context_forge.queue import JobStore


def test_generic_host_event_uses_shared_pipeline(tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"message":"hello"}\n', encoding="utf-8")
    store = JobStore(tmp_path / "queue.db")
    event = handle_event({
        "event_id": "codex-end-1", "type": "session_end", "source": "codex",
        "session_id": "s1", "transcript_path": str(transcript),
        "observed_at": "2026-01-01T00:00:00+00:00", "reason": "exit",
        "project": "demo", "process_alive": False, "active_tools": False,
    }, store=store)
    assert event.source == "codex"
    assert event.process_alive is False
    assert event.active_tools is False
    assert store.latest_episode("s1").host == "codex"
    assert store.pending_receipts()[0].end_kind == "explicit"


def test_generic_host_does_not_invent_liveness_evidence(tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"message":"hello"}\n', encoding="utf-8")
    event = handle_event({
        "event_id": "other-end-1", "type": "session_end", "source": "other",
        "session_id": "s1", "transcript_path": str(transcript),
        "observed_at": "2026-01-01T00:00:00+00:00", "reason": "exit",
    }, store=JobStore(tmp_path / "queue.db"))
    assert event.process_alive is None
    assert event.active_tools is None


def test_generic_host_preserves_explicit_liveness_values(tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"message":"hello"}\n', encoding="utf-8")
    event = handle_event({
        "event_id": "reported-end-1", "type": "session_end", "source": "other",
        "session_id": "s1", "transcript_path": str(transcript),
        "observed_at": "2026-01-01T00:00:00+00:00", "reason": "exit",
        "process_alive": False, "active_tools": False,
    }, store=JobStore(tmp_path / "queue.db"))
    assert event.process_alive is False
    assert event.active_tools is False


def test_generic_host_rejects_invalid_json_or_missing_transcript(tmp_path):
    store = JobStore(tmp_path / "queue.db")
    with pytest.raises(ValueError, match="one JSON object"):
        handle_json("[]", store=store)
    payload = {"event_id": "x", "type": "session_end", "source": "other",
               "session_id": "s", "transcript_path": str(tmp_path / "missing"),
               "observed_at": "2026-01-01T00:00:00+00:00", "reason": "exit"}
    with pytest.raises(ValueError, match="existing file"):
        handle_event(payload, store=store)
