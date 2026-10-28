import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from context_forge.domain import SessionLifecycleEvent
from context_forge.episodes import EpisodeCollector
from context_forge.queue import EventConflict, JobStore
from context_forge.vault import Vault
from context_forge.gateways import NoOpGateway


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


def test_lifecycle_event_liveness_is_optional_and_typed():
    from context_forge.hooks.liveness import from_payload

    base = lifecycle_event().model_dump()
    assert from_payload(base).process_alive is None
    assert from_payload(base).active_tools is None
    base.update(process_alive=False, active_tools=False)
    assert from_payload(base).process_alive is False
    assert from_payload(base).active_tools is False
    base.update(process_alive="false", active_tools=0)
    assert from_payload(base).process_alive is None
    assert from_payload(base).active_tools is None


def test_session_start_registers_exact_process_identity(tmp_path, monkeypatch):
    from context_forge.hooks import claude_code, liveness

    store = JobStore(tmp_path / "queue.db")
    monkeypatch.setattr(liveness, "claude_process_identity", lambda: (1234, 45.5))
    assert claude_code.handle_payload(
        {"hook_event_name": "SessionStart", "session_id": "s1"}, store=store) is None
    identity = store.session_process_identity("s1")
    assert (identity["session_id"], identity["pid"], identity["process_created_at"]) == (
        "s1", 1234, 45.5)
    assert identity["registered_at"]


def test_session_start_without_reliable_ancestor_keeps_liveness_unknown(tmp_path, monkeypatch):
    from context_forge.hooks import claude_code, liveness

    store = JobStore(tmp_path / "queue.db")
    monkeypatch.setattr(liveness, "claude_process_identity", lambda: None)
    claude_code.handle_payload(
        {"hook_event_name": "SessionStart", "session_id": "s2"}, store=store)
    assert store.session_process_identity("s2") is None


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


def test_a_second_checkpoint_stores_only_the_lines_it_adds(tmp_path: Path):
    """Copying the whole transcript per checkpoint is quadratic in episode length:
    the store grew to 300 MB across 54 checkpoints, each from line 0."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_bytes(b'{"a":1}\n{"a":2}\n')
    store = JobStore(tmp_path / "queue.db")
    collector = EpisodeCollector(store)
    collector.collect("session-1", transcript, "demo", "claude_code")

    first = collector.checkpoint(lifecycle_event(type="pre_compact", final_line_count=2))
    assert (first.start_line, first.end_line) == (0, 2)
    assert first.snapshot == '{"a":1}\n{"a":2}'

    with transcript.open("ab") as handle:
        handle.write(b'{"a":3}\n{"a":4}\n')
    collector.collect("session-1", transcript, "demo", "claude_code")

    second = collector.checkpoint(lifecycle_event(type="pre_compact", final_line_count=4))
    assert (second.start_line, second.end_line) == (2, 4)
    assert second.snapshot == '{"a":3}\n{"a":4}'
    total = sum(len(item.snapshot) for item in store.list_checkpoints("session-1"))
    assert total == len(first.snapshot) + len(second.snapshot)


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


def test_claude_hook_liveness_roundtrip_to_watcher_and_ttl(tmp_path: Path, monkeypatch):
    from context_forge import watcher
    from context_forge.hooks import liveness
    from context_forge.hooks.claude_code import handle_payload

    transcript = tmp_path / "live-session.jsonl"
    transcript.write_text('{"type":"assistant","message":{"content":[]}}\n',
                          encoding="utf-8")
    store = JobStore(tmp_path / "queue.db")
    monkeypatch.setattr(liveness, "register_current_claude_session",
                        lambda session_id, queue: queue.register_session_process(
                            session_id, 4321, 100.0))
    handle_payload({"hook_event_name": "SessionStart", "session_id": "s-live"},
                   store=store)
    handle_payload({"hook_event_name": "PreToolUse", "session_id": "s-live",
                    "tool_use_id": "tool-1"}, store=store)

    # Even though transcript looks idle, hook-backed tool evidence says active.
    monkeypatch.setattr(watcher, "_session_process_alive", lambda sid, queue: True)
    assert watcher.probe_session_liveness(transcript, "s-live", store) == (True, True)
    handle_payload({"hook_event_name": "PostToolUse", "session_id": "s-live",
                    "tool_use_id": "tool-1"}, store=store)
    assert watcher.probe_session_liveness(transcript, "s-live", store) == (True, False)
    assert watcher.probe_session_liveness(
        transcript, "s-live", store, now=store.session_liveness("s-live")["observed_at"] + 86401,
    ) == (True, None)


def test_watcher_consumes_hook_tool_evidence_conservatively(tmp_path: Path, monkeypatch):
    from context_forge import watcher
    from context_forge.hooks import liveness
    from context_forge.hooks.claude_code import handle_payload

    projects = tmp_path / "projects" / "project"
    transcript = projects / "s-watch.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text('{"type":"assistant","message":{"content":[]}}\n',
                          encoding="utf-8")
    monkeypatch.setattr(watcher, "transcript_root", lambda: tmp_path / "projects")
    monkeypatch.setattr(liveness, "register_current_claude_session",
                        lambda sid, queue: queue.register_session_process(sid, 7654, 200.0))
    monkeypatch.setattr(watcher, "_session_process_alive", lambda sid, queue: False)
    store = JobStore(tmp_path / "queue.db")
    vault = Vault(tmp_path / "vault")
    collector = EpisodeCollector(store)
    episode = collector.collect("s-watch", transcript, "project", "claude_code")
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.save_episode(episode.model_copy(update={"last_activity_at": old.isoformat()}))
    handle_payload({"hook_event_name": "SessionStart", "session_id": "s-watch"},
                   store=store)
    handle_payload({"hook_event_name": "PreToolUse", "session_id": "s-watch",
                    "tool_use_id": "pending-tool"}, store=store)

    watcher.scan_once(store, vault, NoOpGateway(), settle_seconds=0, baseline=0)
    assert store.latest_episode("s-watch").state == "open"

    handle_payload({"hook_event_name": "PostToolUse", "session_id": "s-watch",
                    "tool_use_id": "pending-tool"}, store=store)
    # Old enough for suspected close, but transcript/tool hooks provide evidence.
    current = time.time()
    store.save_episode(store.latest_episode("s-watch").model_copy(update={
        "last_activity_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
    }))
    watcher.scan_once(store, vault, NoOpGateway(), settle_seconds=0, baseline=0, now=current)
    assert store.latest_episode("s-watch").state == "inferred_pending"

    state = store.session_liveness("s-watch")
    watcher.scan_once(store, vault, NoOpGateway(), settle_seconds=0, baseline=0,
                      now=state["observed_at"] + 86401)
    assert store.latest_episode("s-watch").state == "inferred_pending"


def test_tool_liveness_unknown_without_session_start_or_tool_id(tmp_path: Path):
    from context_forge.hooks.claude_code import handle_payload
    from context_forge import watcher

    store = JobStore(tmp_path / "queue.db")
    handle_payload({"hook_event_name": "PreToolUse", "session_id": "unknown",
                    "tool_use_id": "tool-1"}, store=store)
    transcript = tmp_path / "unknown.jsonl"
    transcript.write_text('{"type":"assistant","message":{"content":[]}}\n',
                          encoding="utf-8")
    assert watcher.probe_session_liveness(transcript, "unknown", store) == (None, None)

    handle_payload({"hook_event_name": "SessionStart", "session_id": "unknown"},
                   store=store)
    handle_payload({"hook_event_name": "PreToolUse", "session_id": "unknown"},
                   store=store)
    assert watcher.probe_session_liveness(transcript, "unknown", store)[1] is None


def test_claude_hook_install_includes_liveness_events(tmp_path: Path):
    import json
    from context_forge import install_hook

    settings = tmp_path / "settings.json"
    install_hook.install(settings)
    hooks = json.loads(settings.read_text(encoding="utf-8"))["hooks"]
    for event in ("SessionStart", "UserPromptSubmit", "Stop", "PreToolUse",
                  "PostToolUse", "PostToolUseFailure"):
        assert hooks[event]


def test_inferred_end_requires_pending_and_grace_period(tmp_path: Path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"message":"one"}\n', encoding="utf-8")
    store = JobStore(tmp_path / "queue.db")
    collector = EpisodeCollector(store)
    episode = collector.collect("s1", transcript, "demo", "claude_code")
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.save_episode(episode.model_copy(update={"last_activity_at": old.isoformat()}))

    first = collector.advance_inferred(old + timedelta(minutes=30), process_alive=False,
                                       active_tools=False)
    assert first == []
    assert store.latest_episode("s1").state == "inferred_pending"

    second = collector.advance_inferred(old + timedelta(minutes=60), process_alive=False,
                                        active_tools=False)
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

    assert collector.advance_inferred(old + timedelta(minutes=30), process_alive=False,
                                     active_tools=True) == []
    assert store.latest_episode("s1").state == "open"
    assert collector.advance_inferred(old + timedelta(hours=2), new_activity=True,
                                     process_alive=False, active_tools=True) == []
    assert store.latest_episode("s1").state == "open"


def test_new_activity_shortens_the_grace_wait(tmp_path: Path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"message":"one"}\n', encoding="utf-8")
    store = JobStore(tmp_path / "queue.db")
    collector = EpisodeCollector(store)
    episode = collector.collect("s1", transcript, "demo", "claude_code")
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.save_episode(episode.model_copy(update={"last_activity_at": old.isoformat()}))

    receipts = collector.advance_inferred(old + timedelta(minutes=31), new_activity=True,
                                          process_alive=False, active_tools=False)

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

    assert collector.advance_inferred(old + timedelta(hours=1), process_alive=False,
                                      active_tools=False) == []


def test_inferred_end_falls_back_to_file_evidence_when_liveness_is_unknown(tmp_path: Path):
    """No host liveness registered (None) must not block inferred finalization."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"message":"one"}\n', encoding="utf-8")
    store = JobStore(tmp_path / "queue.db")
    collector = EpisodeCollector(store)
    episode = collector.collect("s-live", transcript, "demo", "claude_code")
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.save_episode(episode.model_copy(update={"last_activity_at": old.isoformat()}))

    # No liveness arguments at all -> probe unknown -> file evidence decides.
    assert collector.advance_inferred(old + timedelta(minutes=30)) == []
    assert store.latest_episode("s-live").state == "inferred_pending"

    receipts = collector.advance_inferred(old + timedelta(minutes=60))
    assert len(receipts) == 1 and receipts[0].end_kind == "inferred"

    # A positively-live process still suppresses the close.
    other = collector.collect("s-alive", transcript, "demo", "claude_code")
    store.save_episode(other.model_copy(update={"last_activity_at": old.isoformat()}))
    assert collector.advance_inferred(old + timedelta(hours=2), process_alive=True) == []
    assert store.latest_episode("s-alive").state == "open"


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



def test_session_end_without_a_transcript_is_a_noop(tmp_path: Path):
    """`claude mcp list` and one-shot prompts fire SessionEnd with no transcript file.
    Raising there prints a hook failure into every such command."""
    from context_forge.hooks.claude_code import handle_payload

    store = JobStore(tmp_path / "queue.db")
    missing = tmp_path / "absent.jsonl"

    assert handle_payload({"hook_event_name": "SessionEnd", "session_id": "s1",
                           "transcript_path": str(missing)}, store=store) is None
    assert store.latest_episode("s1") is None
    assert store.db.execute("SELECT COUNT(*) FROM session_lifecycle_events").fetchone()[0] == 0


def test_the_hook_skips_a_sibling_tools_own_session(tmp_path: Path):
    """The watcher skips these sessions; the hook path has to agree, or a tool's
    prompts still reach the library through the other door."""
    from context_forge.hooks.claude_code import handle_payload

    transcript = tmp_path / "x.jsonl"
    transcript.write_text('{"type":"user"}\n', encoding="utf-8")
    store = JobStore(tmp_path / "queue.db")

    result = handle_payload({"hook_event_name": "SessionEnd", "session_id": "tool-1",
                              "cwd": r"C:\Users\me\.claude-mem\observer-sessions",
                              "transcript_path": str(transcript)}, store=store)

    assert result is None
    assert store.latest_episode("tool-1") is None


def test_a_cwd_and_a_directory_name_land_on_the_same_project(tmp_path: Path):
    """The hook hands over `cwd`; the transcript scan hands over the directory
    name. One session filed under two projects cannot be recalled by either."""
    from context_forge.hooks.claude_code import handle_payload
    from context_forge import watcher

    transcript = tmp_path / "projects" / "C--work-demo" / "s1.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps({"cwd": r"C:\work\demo",
                                      "message": {"role": "user", "content": "x"}}) + "\n",
                          encoding="utf-8")
    store = JobStore(tmp_path / "queue.db")

    handle_payload({"hook_event_name": "SessionEnd", "session_id": "s1",
                    "cwd": r"C:\work\demo", "transcript_path": str(transcript)},
                   store=store)
    assert store.latest_episode("s1").project == "demo"

    watcher.transcript_root = lambda: tmp_path / "projects"
    watcher.scan_once(store, Vault(tmp_path / "vault"), NoOpGateway(),
                      settle_seconds=0, baseline=0)
    assert store.latest_episode("s1").project == "demo"
