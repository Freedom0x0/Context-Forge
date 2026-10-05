import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

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

    first = collector.collect("session-1", transcript, "demo", "claude_code")
    assert first.collected_through_line == 1
    assert first.state == "open"
    assert collector.collect("session-1", transcript, "demo", "claude_code") == first

    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(' tail\n')
    second = collector.collect("session-1", transcript, "demo", "claude_code")
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
    collector.collect("session-1", transcript, "demo", "claude_code")

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
    collector.collect("session-1", transcript, "demo", "claude_code")

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
    collector.collect("session-1", transcript, "demo", "claude_code")
    receipt = collector.request_finalize("session-1", end_kind="explicit", event_id="end-1")
    store.mark_receipt_committed(receipt.finalization_id)

    with transcript.open("a", encoding="utf-8") as handle:
        handle.write('{"message":"two"}\n')
    resumed = collector.collect("session-1", transcript, "demo", "claude_code")
    assert resumed.segment == 2
    assert resumed.state == "open"


def test_watcher_quiet_period_collects_but_never_publishes(tmp_path: Path, monkeypatch):
    from context_forge import watcher
    from context_forge.vault import Vault

    root = tmp_path / "projects" / "demo"
    root.mkdir(parents=True)
    transcript = root / "session-1.jsonl"
    transcript.write_text(
        json.dumps({"cwd": str(tmp_path / "demo"), "type": "user",
                    "message": {"content": "work in progress"}}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(watcher, "transcript_root", lambda: tmp_path / "projects")

    class ForbiddenGateway:
        def extract_session(self, *args, **kwargs):
            raise AssertionError("quiet-period collection called the model")

    store = JobStore(tmp_path / "queue.db")
    collected = watcher.scan_once(
        store, Vault(tmp_path / "vault"), ForbiddenGateway(),
        settle_seconds=0, baseline=0,
    )
    assert collected == 1
    assert store.latest_episode("session-1").collected_through_line == 1
    assert list((tmp_path / "vault").rglob("*.md")) == []


def test_hook_translates_session_end_and_precompact(tmp_path: Path):
    from context_forge.hooks.claude_code import handle_payload

    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"message":"one"}\n', encoding="utf-8")
    store = JobStore(tmp_path / "queue.db")

    pre = handle_payload({"hook_event_name": "PreCompact", "session_id": "s1",
                          "transcript_path": str(transcript)}, store=store)
    assert pre.type == "pre_compact"
    assert len(store.list_checkpoints("s1")) == 1

    end = handle_payload({"hook_event_name": "SessionEnd", "session_id": "s1",
                          "transcript_path": str(transcript)}, store=store)
    assert end.type == "session_end"
    assert store.latest_episode("s1").state == "finalizing"


def test_repeated_hook_session_end_reuses_first_observed_at(tmp_path: Path):
    from context_forge.hooks.claude_code import handle_payload

    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"message":"one"}\n', encoding="utf-8")
    store = JobStore(tmp_path / "queue.db")
    payload = {"hook_event_name": "SessionEnd", "session_id": "s1",
               "transcript_path": str(transcript)}

    first = handle_payload(payload, store=store)
    second = handle_payload(payload, store=store)

    assert second == first
    assert len(store.pending_receipts()) == 1


def test_inferred_end_requires_pending_and_grace_period(tmp_path: Path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"message":"one"}\n', encoding="utf-8")
    store = JobStore(tmp_path / "queue.db")
    collector = EpisodeCollector(store)
    episode = collector.collect("s1", transcript, "demo", "claude_code")
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.save_episode(episode.model_copy(update={"last_activity_at": old.isoformat()}))

    first = collector.advance_inferred(old + timedelta(minutes=30))
    assert first == []
    assert store.latest_episode("s1").state == "inferred_pending"

    second = collector.advance_inferred(old + timedelta(minutes=60))
    assert len(second) == 1
    assert second[0].end_kind == "inferred"
    # Inferred closes are counted separately so the guess stays visible (8c).
    assert store.metrics()["episodes_inferred"] == 1


def test_inferred_close_is_suppressed_while_a_tool_is_outstanding(tmp_path: Path):
    """A session that stopped on an unanswered tool_use is not finished."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(json.dumps({
        "type": "assistant", "message": {"content": [
            {"type": "text", "text": "running it"},
            {"type": "tool_use", "name": "Bash", "input": {"command": "sleep 1"}}]}}) + "\n",
        encoding="utf-8")
    store = JobStore(tmp_path / "queue.db")
    collector = EpisodeCollector(store)
    episode = collector.collect("s1", transcript, "demo", "claude_code")
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.save_episode(episode.model_copy(update={"last_activity_at": old.isoformat()}))

    assert collector.advance_inferred(old + timedelta(minutes=30)) == []
    assert store.latest_episode("s1").state == "open"
    assert collector.advance_inferred(old + timedelta(hours=2), new_activity=True) == []
    assert store.latest_episode("s1").state == "open"


def test_new_activity_shortens_the_grace_wait(tmp_path: Path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"message":"one"}\n', encoding="utf-8")
    store = JobStore(tmp_path / "queue.db")
    collector = EpisodeCollector(store)
    episode = collector.collect("s1", transcript, "demo", "claude_code")
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.save_episode(episode.model_copy(update={"last_activity_at": old.isoformat()}))

    receipts = collector.advance_inferred(old + timedelta(minutes=31), new_activity=True)

    assert len(receipts) == 1 and receipts[0].end_kind == "inferred"


def test_missing_transcript_is_skipped_instead_of_finalizing(tmp_path: Path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"message":"one"}\n', encoding="utf-8")
    store = JobStore(tmp_path / "queue.db")
    collector = EpisodeCollector(store)
    episode = collector.collect("s1", transcript, "demo", "claude_code")
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.save_episode(episode.model_copy(update={"last_activity_at": old.isoformat()}))
    transcript.unlink()

    assert collector.advance_inferred(old + timedelta(hours=1)) == []
    assert store.latest_episode("s1").state == "open"


def test_append_cancels_inferred_pending(tmp_path: Path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"message":"one"}\n', encoding="utf-8")
    store = JobStore(tmp_path / "queue.db")
    collector = EpisodeCollector(store)
    episode = collector.collect("s1", transcript, "demo", "claude_code")
    store.save_episode(episode.model_copy(update={"state": "inferred_pending"}))
    transcript.write_text('{"message":"one"}\n{"message":"two"}\n', encoding="utf-8")

    assert collector.collect("s1", transcript, "demo", "claude_code").state == "open"


def test_event_ingest_and_manual_finalize_cli(tmp_path: Path, monkeypatch):
    from context_forge import cli

    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"message":"one"}\n', encoding="utf-8")
    monkeypatch.setenv("CONTEXT_FORGE_HOME", str(tmp_path / "home"))
    store = JobStore(tmp_path / "home" / "queue.db")
    monkeypatch.setattr(cli, "_store", lambda: store)
    payload = {"event_id": "evt-1", "type": "pre_compact", "source": "test",
               "session_id": "s1", "transcript_path": str(transcript),
               "observed_at": "2026-01-01T00:00:00+00:00", "final_line_count": 1,
               "reason": "test"}
    runner = CliRunner()
    result = runner.invoke(cli.app, ["event", "ingest"], input=json.dumps(payload))
    assert result.exit_code == 0, result.output
    result = runner.invoke(cli.app, ["session", "finalize", "s1"])
    assert result.exit_code == 0, result.output
    assert store.latest_episode("s1").state == "finalizing"


def test_hook_self_test_releases_temporary_database():
    from context_forge import cli

    result = CliRunner().invoke(cli.app, ["install-hook", "--self-test"])
    assert result.exit_code == 0, result.output
    assert "passed" in result.output


def episode_host_columns(store: JobStore) -> list[str]:
    return [row[1] for row in store.db.execute("PRAGMA table_info(session_episodes)")]


def test_collect_records_the_host_that_produced_the_episode(tmp_path: Path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"message":"one"}\n', encoding="utf-8")
    store = JobStore(tmp_path / "queue.db")

    episode = EpisodeCollector(store).collect("session-1", transcript, "demo", "codex")

    assert episode.host == "codex"
    assert store.latest_episode("session-1").host == "codex"
    # save_episode inserts positionally, so host must stay the last column.
    assert episode_host_columns(store)[-1] == "host"


def test_host_counts_bucket_episodes_by_host(tmp_path: Path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"message":"one"}\n', encoding="utf-8")
    store = JobStore(tmp_path / "queue.db")
    collector = EpisodeCollector(store)

    collector.collect("session-1", transcript, "demo", "claude_code")
    collector.collect("session-2", transcript, "demo", "claude_code")
    collector.collect("session-3", transcript, "demo", "codex")

    assert store.host_counts() == {"claude_code": 2, "codex": 1}


def test_legacy_queue_database_gains_host_as_the_last_column(tmp_path: Path):
    path = tmp_path / "queue.db"
    store = JobStore(path)
    store.db.executescript(
        "DROP TABLE session_episodes;"
        "CREATE TABLE session_episodes ("
        "  session_id TEXT NOT NULL, segment INTEGER NOT NULL DEFAULT 1,"
        "  project TEXT, transcript_path TEXT NOT NULL,"
        "  collected_through_line INTEGER NOT NULL DEFAULT 0,"
        "  transcript_digest TEXT NOT NULL, state TEXT NOT NULL,"
        "  started_at TEXT NOT NULL, last_activity_at TEXT NOT NULL,"
        "  end_kind TEXT, retention_deadline TEXT,"
        "  PRIMARY KEY(session_id, segment));"
    )
    store.db.execute(
        "INSERT INTO session_episodes VALUES "
        "('legacy-1',1,'demo','/tmp/legacy.jsonl',3,'digest','open',"
        "'2026-09-01T00:00:00+00:00','2026-09-01T00:00:00+00:00',NULL,NULL)")
    store.db.commit()

    reopened = JobStore(path)

    assert episode_host_columns(reopened)[-1] == "host"
    assert reopened.latest_episode("legacy-1").host == "claude_code"

    transcript = tmp_path / "new.jsonl"
    transcript.write_text('{"message":"one"}\n', encoding="utf-8")
    EpisodeCollector(reopened).collect("new-1", transcript, "demo", "codex")

    assert reopened.host_counts() == {"claude_code": 1, "codex": 1}


def test_status_reports_episode_host_counts(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CONTEXT_FORGE_HOME", str(tmp_path))
    from context_forge import status as status_mod

    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"message":"one"}\n', encoding="utf-8")
    store = JobStore(tmp_path / "queue.db")
    EpisodeCollector(store).collect("session-1", transcript, "demo", "codex")

    snapshot = status_mod.collect(store)

    assert snapshot["episodes_by_host"] == "codex=1"

