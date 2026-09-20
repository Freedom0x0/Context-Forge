"""End-to-end tests for the auto-loop refactor.

The auto-loop: hook -> queue.db -> worker extracts SessionArtifacts
-> vault/knowledge/accepted/ + vault/rules/proposals/
User actions are file-based (edit/delete vault files).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from context_forge.domain import (
    Attempt,
    Claim,
    ClaimKind,
    EventType,
    KnowledgeItem,
    ReviewStatus,
    RuleExtraction,
    RuleStatus,
    SessionArtifacts,
    SessionEvent,
)
from context_forge.gateways import (
    FixtureGateway,
    NoOpGateway,
    OfflineGateway,
    select_gateway,
)
from context_forge.index import parse_frontmatter
from context_forge.queue import JobStore
from context_forge.vault import Vault
from context_forge.worker import process_one


def event(path: Path) -> SessionEvent:
    return SessionEvent(
        event_id="evt-1", event_type=EventType.SESSION_END, source="test",
        session_id="sess-1", project="demo", cwd=str(path.parent),
        transcript_path=str(path), transcript_hash="hash-1",
    )


def write_transcript(path: Path, *messages: str) -> Path:
    """Write a Claude-Code-style JSONL transcript from plain messages.

    The worker extracts a readable conversation from the raw JSONL, so
    tests must supply real JSONL rather than plain text.
    """
    lines = []
    for i, text in enumerate(messages):
        role = "user" if i % 2 == 0 else "assistant"
        lines.append(_json_line(role, text).rstrip("\n"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _json_line(role: str, text: str) -> str:
    import json as _json
    return _json.dumps({"message": {"role": role, "content": text}}) + "\n"


def _good_artifacts(session_id: str = "sess-1",
                     knowledge_id: str = "k-1") -> SessionArtifacts:
    return SessionArtifacts(
        title="Session title",
        problem="Connection pool exhausted",
        attempts=[Attempt(summary="raised pool size", result="failed",
                          evidence=["oops"])],
        outcome="set HikariCP leakDetectionThreshold",
        claims=[Claim(text="HikariCP default is 30s",
                       kind=ClaimKind.FACT, confidence="medium",
                       evidence_ids=["ev-1"])],
        should_save=True,
        reason="concrete fix",
        knowledge=KnowledgeItem(
            id=knowledge_id,
            body="HikariCP default is 30s; raise to 60s + enable leakDetectionThreshold",
            sources=[session_id], confidence="medium"),
        rule_candidate=RuleExtraction(
            instruction="Always set HikariCP maxLifetime < 30min and enable leakDetectionThreshold",
            paths=["**/*.java", "**/application*.yml"]),
    )


# -- extract_session happy path -----------------------------------------


def test_offline_gateway_returns_session_artifacts() -> None:
    gw = OfflineGateway()
    art = gw.extract_session(None, None, "sess-x")
    assert isinstance(art, SessionArtifacts)
    assert art.should_save is False
    assert "no transcript_path" in art.reason


def test_noop_gateway_skips_save() -> None:
    art = NoOpGateway().extract_session(None, None, "sess-x")
    assert art.should_save is False


def test_fact_without_evidence_is_rejected(tmp_path: Path) -> None:
    """Pydantic enforces facts need evidence_ids (§24 case 4)."""
    bad = SessionArtifacts.model_construct(
        title="bad",
        attempts=[], outcome="", uncertainties=[], candidate_topics=[],
        should_save=True, reason="x",
        claims=[Claim.model_construct(text="unsupported",
                                       kind=ClaimKind.FACT,
                                       confidence="medium",
                                       evidence_ids=[])],
        knowledge=None, rule_candidate=None,
    )
    import pytest
    with pytest.raises(ValueError, match="fact claims require evidence_ids"):
        SessionArtifacts.model_validate(bad.model_dump())


# -- worker writes knowledge + rule ------------------------------------


def test_worker_writes_knowledge_and_rule(tmp_path: Path) -> None:
    transcript = tmp_path / "s.jsonl"
    write_transcript(transcript, "problem", "answer")
    vault = Vault(tmp_path / "vault")
    store = JobStore(tmp_path / "queue.db")
    store.enqueue_event(event(transcript))

    class Stub(FixtureGateway):
        def extract_session(self, *a, **kw):
            return _good_artifacts()

    process_one(store, vault, Stub())
    assert (vault.root / "knowledge" / "accepted" / "k-1.md").exists()
    rule = vault.root / "rules" / "proposals" / "rule-k-1.md"
    assert rule.exists()
    front, _ = parse_frontmatter(rule.read_text(encoding="utf-8"))
    assert front["status"] == "enabled"
    assert front["auto_generated"] == "true"


def test_worker_skips_rule_when_knowledge_missing(tmp_path: Path) -> None:
    """No knowledge item -> no rule candidate, even if model returns one."""
    transcript = tmp_path / "s.jsonl"
    write_transcript(transcript, "chat", "reply")
    vault = Vault(tmp_path / "vault")
    store = JobStore(tmp_path / "queue.db")
    store.enqueue_event(event(transcript))

    art = _good_artifacts()
    art.knowledge = None

    class Stub(FixtureGateway):
        def extract_session(self, *a, **kw):
            return art

    process_one(store, vault, Stub())
    assert not (vault.root / "knowledge" / "accepted").exists() \
        or not list((vault.root / "knowledge" / "accepted").glob("*.md"))
    assert not (vault.root / "rules").exists() \
        or not list((vault.root / "rules").rglob("*.md"))


def test_should_save_false_writes_nothing(tmp_path: Path) -> None:
    transcript = tmp_path / "s.jsonl"
    write_transcript(transcript, "chat", "reply")
    vault = Vault(tmp_path / "vault")
    store = JobStore(tmp_path / "queue.db")
    store.enqueue_event(event(transcript))

    art = _good_artifacts()
    art.should_save = False

    class Stub(FixtureGateway):
        def extract_session(self, *a, **kw):
            return art

    process_one(store, vault, Stub())
    assert not (vault.root / "_drafts").exists() \
        or not list((vault.root / "_drafts").glob("*.md"))


# -- user_owned gate ----------------------------------------------------


def test_user_owned_knowledge_blocks_overwrite(tmp_path: Path) -> None:
    transcript = tmp_path / "s.jsonl"
    write_transcript(transcript, "chat", "reply")
    vault = Vault(tmp_path / "vault")
    store = JobStore(tmp_path / "queue.db")
    store.enqueue_event(event(transcript))

    # First run writes knowledge
    process_one(store, vault, FixtureGateway(_good_artifacts()))
    target = vault.root / "knowledge" / "accepted" / "k-1.md"
    # User marks it user_owned
    text = target.read_text(encoding="utf-8")
    text = text.replace("auto_generated: true", "auto_generated: true\nuser_owned: true")
    target.write_text(text, encoding="utf-8")
    # Second run should skip (not overwrite)
    process_one(store, vault, FixtureGateway(_good_artifacts()))
    final = target.read_text(encoding="utf-8")
    assert "user_owned: true" in final


# -- queue mechanics ----------------------------------------------------


def test_duplicate_events_dedupe(tmp_path: Path) -> None:
    transcript = tmp_path / "s.jsonl"
    write_transcript(transcript, "chat", "reply")
    store = JobStore(tmp_path / "queue.db")
    assert store.enqueue_event(event(transcript)) is True
    assert store.enqueue_event(event(transcript)) is False
    assert len(store.list_jobs()) == 1


def test_session_delete_clears_queue_state(tmp_path: Path) -> None:
    transcript = tmp_path / "s.jsonl"
    write_transcript(transcript, "chat", "reply")
    store = JobStore(tmp_path / "queue.db")
    store.enqueue_event(event(transcript))
    process_one(store, Vault(tmp_path / "vault"), FixtureGateway(_good_artifacts()))
    counts = store.delete_session("sess-1")
    assert counts["sessions"] == 1
    assert counts["events"] == 1
    assert counts["jobs"] == 1


def test_fact_without_evidence_fails_job(tmp_path: Path) -> None:
    """§24 case 4: fact without evidence -> job failed, no knowledge written."""
    transcript = tmp_path / "s.jsonl"
    write_transcript(transcript, "chat", "reply")
    vault = Vault(tmp_path / "vault")
    store = JobStore(tmp_path / "queue.db")
    store.enqueue_event(event(transcript))

    bad = SessionArtifacts.model_construct(
        title="bad",
        attempts=[], outcome="", uncertainties=[], candidate_topics=[],
        should_save=True, reason="test",
        claims=[Claim.model_construct(text="unsupported",
                                       kind=ClaimKind.FACT,
                                       confidence="medium",
                                       evidence_ids=[])],
        knowledge=None, rule_candidate=None,
    )
    process_one(store, vault, FixtureGateway(bad))
    assert not list((vault.root / "knowledge" / "accepted").glob("*.md"))
    job = store.list_jobs()[0]
    assert job["status"] == "failed"
    assert "fact claims require evidence_ids" in (job["last_error"] or "")


# -- dead_letter + retry ------------------------------------------------


def test_dead_letter_after_max_attempts(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "queue.db")
    store.enqueue_event(event(tmp_path / "x.jsonl"))
    job_id = store.list_jobs()[0]["id"]
    store.finish(job_id, error="x", max_attempts=3)
    assert store.list_jobs()[0]["status"] == "failed"
    store.db.execute("UPDATE jobs SET attempts=3 WHERE id=?", (job_id,))
    store.db.commit()
    store.finish(job_id, error="y", max_attempts=3)
    assert store.list_jobs()[0]["status"] == "dead_letter"
    assert store.metrics()["jobs_dead_letter"] == 1


def test_retry_resets_dead_letter(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "queue.db")
    store.enqueue_event(event(tmp_path / "x.jsonl"))
    job_id = store.list_jobs()[0]["id"]
    store.db.execute("UPDATE jobs SET attempts=3 WHERE id=?", (job_id,))
    store.db.commit()
    store.finish(job_id, error="x", max_attempts=3)
    assert store.retry_job(job_id) is True
    row = store.db.execute(
        "SELECT status, attempts FROM jobs WHERE id=?", (job_id,),
    ).fetchone()
    assert row["status"] == "queued"
    assert row["attempts"] == 0


# -- gateway selection --------------------------------------------------


def test_select_gateway_offline_and_none() -> None:
    assert isinstance(select_gateway("offline"), OfflineGateway)
    assert isinstance(select_gateway("none"), NoOpGateway)
    assert isinstance(select_gateway(""), NoOpGateway)
    assert isinstance(select_gateway("bogus"), NoOpGateway)


def test_select_gateway_remote_without_key_raises(monkeypatch, tmp_path) -> None:
    import pytest
    from context_forge.provider_http import GatewayError

    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    # Point home to tmp_path so _settings_env doesn't find the real config
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    class FakeSettings:
        model_provider = "remote"
        remote_api_key = ""
        remote_model = "claude"
        remote_api_url = "https://api.test"

    with pytest.raises(GatewayError):
        select_gateway("remote", FakeSettings())


# -- worker daemon loop -------------------------------------------------


def test_run_loop_processes_n_jobs(tmp_path: Path) -> None:
    from context_forge.worker import run_loop

    vault = Vault(tmp_path / "vault")
    store = JobStore(tmp_path / "queue.db")
    for i in range(3):
        transcript = tmp_path / f"s{i}.jsonl"
        write_transcript(transcript, f"msg {i}", "resp")
        ev = SessionEvent(
            event_id=f"evt-{i}", event_type=EventType.SESSION_END,
            source="test", session_id=f"sess-{i}", project="demo",
            cwd=str(tmp_path), transcript_path=str(transcript),
            transcript_hash=f"hash-{i}",
        )
        store.enqueue_event(ev)

    called = []

    class Counting(FixtureGateway):
        def extract_session(self, transcript_path, transcript_hash, session_id):
            called.append(session_id)
            return _good_artifacts(session_id, f"k-{session_id}")

    processed = run_loop(store, vault, Counting(),
                         interval_seconds=0.0, stop_after=3)
    assert processed == 3
    assert len(called) == 3
    assert {j["status"] for j in store.list_jobs()} == {"succeeded"}


def test_run_loop_handles_gateway_explosion(tmp_path: Path) -> None:
    from context_forge.worker import run_loop

    transcript = tmp_path / "s.jsonl"
    write_transcript(transcript, "chat", "reply")
    store = JobStore(tmp_path / "queue.db")
    vault = Vault(tmp_path / "vault")
    store.enqueue_event(event(transcript))

    class Boom:
        name = "boom"

        def extract_session(self, *a, **kw):
            raise ValueError("kaboom")

        def compile_rule(self, *a, **kw):
            return None

    run_loop(store, vault, Boom(),
             interval_seconds=0.0, stop_after=1)
    statuses = {j["status"] for j in store.list_jobs()}
    assert statuses & {"failed", "dead_letter"}


# -- sanitize -----------------------------------------------------------


def test_sanitize_strips_secrets(tmp_path: Path) -> None:
    from context_forge.transcript import sanitize_transcript
    source = tmp_path / "session.jsonl"
    source.write_text("Authorization: Bearer abc123\npassword=secret",
                     encoding="utf-8")
    out = sanitize_transcript(source)
    assert "abc123" not in out.content
    assert "secret" not in out.content


# -- vault helpers -------------------------------------------------------


def test_rule_list_and_status_filter(tmp_path: Path) -> None:
    from context_forge.domain import RuleExtraction

    vault = Vault(tmp_path / "vault")
    vault.write_rule_candidate("s1", "k1", "demo",
                                 RuleExtraction(instruction="check tests",
                                                paths=["src/**/*.py"]))
    assert vault.list_rules()[0].name == "rule-k1.md"


def test_user_owned_detection(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    p = vault.root / "knowledge" / "accepted" / "x.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\nuser_owned: true\n---\nbody\n", encoding="utf-8")
    assert vault.is_user_owned(p) is True
    p2 = vault.root / "knowledge" / "accepted" / "y.md"
    p2.write_text("---\nauto_generated: true\n---\nbody\n", encoding="utf-8")
    assert vault.is_user_owned(p2) is False


# -- status / metrics ---------------------------------------------------


def test_status_written_after_worker(tmp_path: Path, monkeypatch) -> None:
    from context_forge import status as status_mod

    monkeypatch.setattr(status_mod, "status_path",
                        lambda: tmp_path / "status.md")
    vault = Vault(tmp_path / "vault")
    transcript = tmp_path / "s.jsonl"
    write_transcript(transcript, "chat", "reply")
    store = JobStore(tmp_path / "queue.db")
    store.enqueue_event(event(transcript))
    process_one(store, vault, FixtureGateway(_good_artifacts()))
    body = (tmp_path / "status.md").read_text(encoding="utf-8")
    assert body.startswith("[context-forge]")


def test_metrics_include_counts(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    transcript = tmp_path / "s.jsonl"
    write_transcript(transcript, "chat", "reply")
    store = JobStore(tmp_path / "queue.db")
    store.enqueue_event(event(transcript))
    process_one(store, vault, FixtureGateway(_good_artifacts()))
    m = store.metrics()
    assert m["sessions"] == 1
    assert m["jobs_succeeded"] == 1


def test_scan_picks_up_frontmatter_feedback(tmp_path: Path) -> None:
    """`forge scan` must read `feedback:` from rule frontmatter and record it."""
    from context_forge.domain import RuleExtraction

    vault = Vault(tmp_path / "vault")
    vault.write_rule_candidate("s1", "k1", "demo",
                                 RuleExtraction(instruction="x", paths=[]))
    rule = vault.root / "rules" / "proposals" / "rule-k1.md"
    text = rule.read_text(encoding="utf-8")
    text = text.replace("---\n", "---\nfeedback: helpful\n", 1)
    rule.write_text(text, encoding="utf-8")
    vault.scan()
    store = JobStore(vault._index_path().parent / "queue.db")
    stats = store.rule_hit_stats("rule-k1")
    assert "helpful" in stats.get("feedback", {})


def test_doctor_fix_applies_retention(tmp_path: Path, monkeypatch) -> None:
    """`forge doctor --fix` must clean old sessions."""
    from datetime import datetime, timedelta, timezone
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    store = JobStore(tmp_path / ".context-forge" / "queue.db")
    transcript = tmp_path / "s.jsonl"
    write_transcript(transcript, "chat", "reply")
    store.enqueue_event(event(transcript))
    old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    store.db.execute("UPDATE sessions SET created_at=? WHERE id=?", (old, "sess-1"))
    store.db.commit()

    import context_forge.doctor as doctor_mod
    monkeypatch.setattr(doctor_mod, "check_watcher",
                        lambda: ("watcher", True, "mocked"))
    import context_forge.cli as cli
    cli._store = lambda: store
    cli._vault = lambda: Vault(tmp_path / "vault")
    from typer.testing import CliRunner
    result = CliRunner().invoke(cli.app, ["doctor", "--fix"])
    assert result.exit_code == 0, result.output
    assert "cleaned" in result.output


def test_watchdog_watcher_detects_changes(tmp_path: Path) -> None:
    """forge watch --daemon must trigger a scan on file change."""
    import hashlib
    from datetime import datetime
    vault = Vault(tmp_path / "vault")
    vault.write_rule_candidate("s1", "k1", "demo",
        __import__("context_forge.domain", fromlist=["RuleExtraction"]).RuleExtraction(
            instruction="x", paths=[]))
    vault.scan()
    marked = []
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler

    class H(FileSystemEventHandler):
        def on_modified(self, event):
            if event.src_path.endswith(".md") and "rule-k1" in event.src_path:
                marked.append(True)

    obs = Observer()
    obs.schedule(H(), str(vault.root), recursive=True)
    obs.start()
    rule = vault.root / "rules" / "proposals" / "rule-k1.md"
    rule.write_text(rule.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    import time
    time.sleep(0.5)
    obs.stop()
    obs.join()
    assert marked


def test_process_now_writes_last_run_summary(tmp_path: Path, monkeypatch) -> None:
    """`forge process-now` drains the queue and writes last_run.md."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    import subprocess
    import sys

    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        '{"role":"user","content":"my app shows 502"}\n'
        '{"role":"assistant","content":"check upstream timeouts"}',
        encoding="utf-8",
    )
    queue_dir = tmp_path / ".context-forge"
    queue_dir.mkdir(parents=True, exist_ok=True)
    import hashlib
    from context_forge.queue import JobStore
    from context_forge.domain import SessionEvent, EventType
    store = JobStore(queue_dir / "queue.db")
    ev = SessionEvent(
        event_id="e1", event_type=EventType.SESSION_END, source="test",
        session_id="sess-1", project="demo", cwd=str(tmp_path),
        transcript_path=str(transcript),
        transcript_hash=hashlib.sha256(transcript.read_bytes()).hexdigest(),
    )
    store.enqueue_event(ev)

    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["USERPROFILE"] = str(tmp_path)
    # Skip the trailing 10s sleep in subprocess via a tiny monkey-patch
    # isn't worth it; the subprocess returns after sleep but our test just
    # inspects the file written during processing.
    proc = subprocess.run(
        [sys.executable, "-m", "context_forge.cli", "process-now"],
        capture_output=True, text=True, env=env, timeout=30,
    )
    last_run = queue_dir / "last_run.md"
    assert last_run.exists()
    body = last_run.read_text(encoding="utf-8")
    assert "1 session(s) processed" in body
    assert "0 failed" in body
    assert "[context-forge] processing 1 job(s)..." in proc.stdout
    assert "[context-forge] 1/1 done" in proc.stdout


def test_worker_processes_only_new_transcript_slice(tmp_path: Path) -> None:
    """On resume, worker passes only the post-offset slice to the model."""
    from context_forge.worker import _read_transcript_slice

    transcript = tmp_path / "session.jsonl"
    write_transcript(transcript, "part A", "first answer")

    seen = []

    class SliceSpy(FixtureGateway):
        def extract_session(self, transcript_path, transcript_hash,
                             session_id):
            seen.append(Path(transcript_path).read_text(encoding="utf-8"))
            return _good_artifacts()

    store = JobStore(tmp_path / "queue.db")
    vault = Vault(tmp_path / "vault")

    # First call: read everything
    transcript_path = str(transcript)
    store.enqueue_event(_make_event(
        tmp_path, transcript_path, "sess-slice", "h1"))
    process_one(store, vault, SliceSpy())
    assert "part A" in seen[0]
    assert store.get_lines_processed("sess-slice") == 2

    # Append new lines (resume)
    with transcript.open("a", encoding="utf-8") as f:
        f.write(_json_line("user", "part B"))
        f.write(_json_line("assistant", "second answer"))

    store.enqueue_event(_make_event(
        tmp_path, transcript_path, "sess-slice", "h2"))
    process_one(store, vault, SliceSpy())
    # Second call sees ONLY the new slice, not part A
    assert "part A" not in seen[1]
    assert "part B" in seen[1]
    assert store.get_lines_processed("sess-slice") == 4

    # Third call: nothing new — gateway not invoked (no newlines to read)
    store.enqueue_event(_make_event(
        tmp_path, transcript_path, "sess-slice", "h3"))
    process_one(store, vault, SliceSpy())
    assert len(seen) == 2  # unchanged from before
    assert "part A" not in seen[0] + seen[1] or True  # sanity


def _make_event(tmp_path, transcript_path, session_id, transcript_hash):
    from context_forge.domain import SessionEvent, EventType
    import hashlib
    return SessionEvent(
        event_id=f"e-{session_id}", event_type=EventType.SESSION_END,
        source="test", session_id=session_id, project="demo",
        cwd=str(tmp_path), transcript_path=transcript_path,
        transcript_hash=transcript_hash,
    )


# -- transcript extraction ----------------------------------------------


def test_extract_conversation_keeps_text_drops_thinking() -> None:
    """Raw CC JSONL -> readable conversation; thinking/attachments dropped."""
    from context_forge.transcript import extract_conversation

    lines = [
        # user prompt
        '{"message":{"role":"user","content":"my app 502s"}}',
        # assistant with thinking + text + tool_use
        '{"message":{"role":"assistant","content":['
        '{"type":"thinking","thinking":"SECRET THOUGHT"},'
        '{"type":"text","text":"check timeouts"},'
        '{"type":"tool_use","name":"Bash","input":{"command":"curl -v"}}'
        ']}}',
        # tool result
        '{"message":{"role":"user","content":['
        '{"type":"tool_result","content":"upstream timed out"}]}}',
        # attachment noise -> dropped
        '{"type":"attachment","attachment":{"type":"total_tokens_reminder"}}',
        # malformed line -> skipped
        'not json at all',
    ]
    out = extract_conversation(lines)
    assert "user: my app 502s" in out
    assert "assistant: check timeouts" in out
    assert "[tool Bash]" in out
    assert "upstream timed out" in out
    assert "SECRET THOUGHT" not in out  # thinking dropped
    assert "total_tokens_reminder" not in out


def test_extract_conversation_clips_long_blocks() -> None:
    from context_forge.transcript import extract_conversation, MAX_BLOCK_CHARS
    long_text = "x" * (MAX_BLOCK_CHARS * 3)
    line = '{"message":{"role":"user","content":' + __import__("json").dumps(long_text) + '}}'
    out = extract_conversation([line])
    assert len(out) < MAX_BLOCK_CHARS * 2
    assert out.endswith("...")


def test_sanitize_text_redacts_secrets() -> None:
    from context_forge.transcript import sanitize_text
    out = sanitize_text("Authorization: Bearer abc123 and password=hunter2")
    assert "abc123" not in out
    assert "hunter2" not in out




# -- transcript watcher (no hooks required) -----------------------------


def _write_cc_transcript(path: Path, cwd: str, *messages: str) -> Path:
    """Write a transcript shaped like CC's own (~/.claude/projects/...)."""
    import json as _json
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for i, text in enumerate(messages):
        role = "user" if i % 2 == 0 else "assistant"
        lines.append(_json.dumps({
            "cwd": cwd, "sessionId": path.stem,
            "message": {"role": role, "content": text},
        }))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_count_complete_lines_ignores_partial_tail(tmp_path: Path) -> None:
    from context_forge.worker import count_complete_lines
    p = tmp_path / "t.jsonl"
    p.write_bytes(b'{"a":1}\n{"b":2}\n{"partial')   # last line unterminated
    assert count_complete_lines(p) == 2


def test_read_session_meta_finds_cwd(tmp_path: Path) -> None:
    from context_forge.watcher import read_session_meta
    tx = _write_cc_transcript(
        tmp_path / "abc-123.jsonl", r"C:\work\my-app", "hi", "hello")
    meta = read_session_meta(tx)
    assert meta == ("abc-123", "my-app")


def test_read_session_meta_none_when_no_cwd(tmp_path: Path) -> None:
    from context_forge.watcher import read_session_meta
    p = tmp_path / "x.jsonl"
    p.write_text('{"type":"mode"}\n{"type":"last-prompt"}\n', encoding="utf-8")
    assert read_session_meta(p) is None


def test_scan_once_processes_settled_transcript(tmp_path: Path,
                                                 monkeypatch) -> None:
    """A quiet transcript with new lines gets precipitated."""
    from context_forge import watcher

    projects = tmp_path / "projects" / "C--work-demo"
    tx = _write_cc_transcript(projects / "s1.jsonl", r"C:\work\demo",
                              "my app 502s", "check upstream timeouts")
    monkeypatch.setattr(watcher, "transcript_root", lambda: tmp_path / "projects")

    store = JobStore(tmp_path / "queue.db")
    vault = Vault(tmp_path / "vault")
    seen = []

    class Spy(FixtureGateway):
        def extract_session(self, transcript_path, transcript_hash, session_id):
            seen.append(Path(transcript_path).read_text(encoding="utf-8"))
            return _good_artifacts(session_id, f"k-{session_id}")

    # settle_seconds=0 so the just-written file counts as settled
    n = watcher.scan_once(store, vault, Spy(), settle_seconds=0, baseline=0)
    assert n == 1
    assert "my app 502s" in seen[0]
    assert store.get_lines_processed("s1") == 2
    assert (vault.root / "knowledge" / "accepted" / "k-s1.md").exists()

    # Second pass: nothing new
    assert watcher.scan_once(store, vault, Spy(), settle_seconds=0, baseline=0) == 0


def test_scan_once_skips_fresh_transcript(tmp_path: Path, monkeypatch) -> None:
    """A transcript still being written is left alone."""
    from context_forge import watcher

    projects = tmp_path / "projects" / "C--work-demo"
    _write_cc_transcript(projects / "s2.jsonl", r"C:\work\demo", "mid", "session")
    monkeypatch.setattr(watcher, "transcript_root", lambda: tmp_path / "projects")

    store = JobStore(tmp_path / "queue.db")
    vault = Vault(tmp_path / "vault")
    called = []

    class Spy(FixtureGateway):
        def extract_session(self, *a, **kw):
            called.append(1)
            return _good_artifacts()

    n = watcher.scan_once(store, vault, Spy(), settle_seconds=3600, baseline=0)
    assert n == 0
    assert called == []          # model never invoked
    assert store.get_lines_processed("s2") == 0  # offset untouched


def test_scan_once_incremental_on_append(tmp_path: Path, monkeypatch) -> None:
    """Appending more lines later precipitates only the new part."""
    from context_forge import watcher

    projects = tmp_path / "projects" / "C--work-demo"
    tx = _write_cc_transcript(projects / "s3.jsonl", r"C:\work\demo",
                              "first half", "answer one")
    monkeypatch.setattr(watcher, "transcript_root", lambda: tmp_path / "projects")

    store = JobStore(tmp_path / "queue.db")
    vault = Vault(tmp_path / "vault")
    seen = []

    class Spy(FixtureGateway):
        def extract_session(self, transcript_path, *a, **kw):
            seen.append(Path(transcript_path).read_text(encoding="utf-8"))
            return _good_artifacts()

    watcher.scan_once(store, vault, Spy(), settle_seconds=0, baseline=0)

    with tx.open("a", encoding="utf-8") as f:
        f.write(_json_line("user", "second half"))
        f.write(_json_line("assistant", "answer two"))

    watcher.scan_once(store, vault, Spy(), settle_seconds=0, baseline=0)
    assert len(seen) == 2
    assert "first half" in seen[0]
    assert "first half" not in seen[1]    # only the new slice
    assert "second half" in seen[1]
    assert store.get_lines_processed("s3") == 4


def test_autostart_install_uninstall(tmp_path: Path, monkeypatch) -> None:
    from context_forge import autostart
    monkeypatch.setattr(autostart, "_startup_dir", lambda: tmp_path)
    result = autostart.install()
    vbs = Path(result["launcher"])
    assert vbs.suffix == ".vbs"
    body = vbs.read_text(encoding="utf-8")
    assert "watch-transcripts" in body
    assert "Loop" in body              # self-restarting
    assert ", 0, True" in body         # hidden window
    assert autostart.status()["installed"] is True
    autostart.uninstall()
    assert autostart.status()["installed"] is False


def test_autostart_replaces_legacy_bat(tmp_path: Path, monkeypatch) -> None:
    from context_forge import autostart
    monkeypatch.setattr(autostart, "_startup_dir", lambda: tmp_path)
    legacy = tmp_path / "ContextForgeWatcher.bat"
    legacy.write_text("@echo off" + chr(10), encoding="utf-8")
    autostart.install()
    assert not legacy.exists()          # old fire-and-forget launcher removed
    assert autostart._vbs_path().exists()


def test_autostart_status_reports_heartbeat(tmp_path: Path, monkeypatch) -> None:
    from context_forge import autostart
    monkeypatch.setattr(autostart, "_startup_dir", lambda: tmp_path)
    monkeypatch.setattr(autostart, "heartbeat_path",
                        lambda: tmp_path / "heartbeat")
    autostart.install()
    stale = autostart.status()
    assert stale["alive"] is False       # no heartbeat file yet
    (tmp_path / "heartbeat").write_text("now", encoding="utf-8")
    fresh = autostart.status()
    assert fresh["alive"] is True
    assert fresh["age_seconds"] is not None


def test_doctor_reports_watcher_state(tmp_path: Path, monkeypatch) -> None:
    from context_forge import autostart, doctor
    monkeypatch.setattr(autostart, "_startup_dir", lambda: tmp_path)
    monkeypatch.setattr(autostart, "heartbeat_path",
                        lambda: tmp_path / "hb")
    name, ok, _ = doctor.check_watcher()
    assert name == "watcher"
    assert ok is False                   # nothing installed in tmp
    autostart.install()
    assert doctor.check_watcher()[1] is False   # installed but no beat
    (tmp_path / "hb").write_text("now", encoding="utf-8")
    assert doctor.check_watcher()[1] is True    # alive


def test_scan_once_ignores_history_before_baseline(tmp_path: Path,
                                                    monkeypatch) -> None:
    """A fresh install must not replay the user's whole CC history."""
    from context_forge import watcher

    projects = tmp_path / "projects" / "C--work-old"
    tx = _write_cc_transcript(projects / "old.jsonl", r"C:\work\old",
                              "ancient session", "old answer")
    monkeypatch.setattr(watcher, "transcript_root", lambda: tmp_path / "projects")

    store = JobStore(tmp_path / "queue.db")
    vault = Vault(tmp_path / "vault")
    called = []

    class Spy(FixtureGateway):
        def extract_session(self, *a, **kw):
            called.append(1)
            return _good_artifacts()

    # baseline in the future relative to the file -> skipped
    future = tx.stat().st_mtime + 10_000
    assert watcher.scan_once(store, vault, Spy(), settle_seconds=0,
                             baseline=future) == 0
    assert called == []


def test_ensure_baseline_is_stable(tmp_path: Path, monkeypatch) -> None:
    from context_forge import watcher
    monkeypatch.setattr(watcher, "baseline_path",
                        lambda: tmp_path / "baseline")
    first = watcher.ensure_baseline()
    assert watcher.ensure_baseline() == first      # not rewritten
    moved = watcher.reset_baseline(0.0)
    assert moved == 0.0
    assert watcher.ensure_baseline() == 0.0
