"""End-to-end: the chain across process boundaries.

Skipped by default. It starts real subprocesses — the hook, the MCP server — and
takes about a minute, so it is the one test that checks the seams the unit suite
cannot see: a hook that exits non-zero, an MCP handshake that never completes, a
memory that is written but not readable back through the tool a host would call.

    pytest -m e2e

Four stages, deliberately minimal: hook → finalize → recall → MCP call. The
wider walk (PreCompact checkpoints, the six correction verbs, retention) lives in
the unit suite; this one is for the boundaries.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from context_forge.memory import MemoryStore
from context_forge.memory_models import MemoryDraft, Provenance, Source
from context_forge.memory_recall import MemoryRecall
from context_forge.queue import JobStore

pytestmark = pytest.mark.e2e

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture()
def home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("CONTEXT_FORGE_HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    from context_forge.config import runtime_dir

    # The whole point is that this never touches the real library. If the env var
    # did not take, stop here rather than write into the user's home.
    assert runtime_dir().resolve() == tmp_path.resolve(), "e2e refused: not isolated"
    return tmp_path


def _env() -> dict[str, str]:
    return {**os.environ, "PYTHONPATH": str(REPO)}


def _write_transcript(path: Path, session_id: str, turns: list[tuple[str, str]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(
        json.dumps({"cwd": r"C:\work\demo", "sessionId": session_id, "type": role,
                    "message": {"role": role, "content": text}}, ensure_ascii=False) + "\n"
        for role, text in turns), encoding="utf-8")
    return path


def _read_line(stream, timeout: float = 20.0) -> str:
    """A blocking readline that cannot hang the suite."""
    box: list[str] = []
    reader = threading.Thread(target=lambda: box.append(stream.readline()), daemon=True)
    reader.start()
    reader.join(timeout)
    if reader.is_alive():
        raise TimeoutError("MCP server did not answer")
    return box[0] if box else ""


def test_the_chain_from_hook_to_mcp(home: Path) -> None:
    session_id = "e2e-session-0001"
    transcript = _write_transcript(home / "projects" / "C--work-demo" / f"{session_id}.jsonl",
                                   session_id, [
                                       ("user", "缓存失效为什么成对执行？"),
                                       ("assistant", "因为单边失效会读到半旧的权限。"),
                                   ])
    body = "缓存失效必须成对执行，否则会读到半旧的权限"

    # --- 1. the hook, in its own process ------------------------------------
    payload = {"hook_event_name": "SessionEnd", "session_id": session_id,
               "cwd": r"C:\work\demo", "transcript_path": str(transcript),
               "final_line_count": 2, "reason": "explicit"}
    hook = subprocess.run([sys.executable, "-m", "context_forge.hooks.claude_code"],
                          input=json.dumps(payload), capture_output=True, text=True,
                          env=_env(), cwd=REPO, timeout=120)
    assert hook.returncode == 0, hook.stderr
    store = JobStore(home / "queue.db")
    assert store.latest_episode(session_id) is not None

    # --- 2. finalize through the real service, fake model --------------------
    from context_forge.worker import process_pending_finalizations

    source = Source(identity=f"episode:{session_id}:1", kind="observation",
                    session_id=session_id, excerpt="单边失效会读到半旧的权限")
    candidate = {"schema_version": 1, "candidates": [{
        "candidate_id": "cache-pairwise",
        "evidence_refs": [source.identity],
        "draft": MemoryDraft(body=body, status="active", role="claim",
                             provenance=Provenance(projects=("demo",),
                                                   sessions=(session_id,)),
                             sources=(source,)).model_dump(mode="json"),
        "relations": [],
    }], "deferred_topics": [], "no_memory_reason": None}

    class FakeGateway:
        name = "e2e-gateway"

        def __init__(self):
            self.payload = None

        def finalize_episode(self, payload):
            self.payload = payload
            return candidate

    gateway = FakeGateway()
    memory = MemoryStore.initialize(home / "memory.db")
    assert process_pending_finalizations(store, gateway, memory) == 1
    records = memory.current_records()
    assert len(records) == 1
    # The model sees the conversation, not the raw log, and not the checkpoints.
    assert gateway.payload["transcript"].startswith("user: ")
    assert all("snapshot" not in item for item in gateway.payload["checkpoints"])

    # --- 3. recall ----------------------------------------------------------
    hit = MemoryRecall(memory).search("缓存失效", project="demo", limit=5,
                                      request_id="e2e-recall-1")
    assert len(hit.results) == 1
    assert hit.results[0].score.channel == "bm25"
    assert hit.results[0].explanation

    # --- 4. the MCP surface a host actually calls ---------------------------
    process = subprocess.Popen(
        [sys.executable, "-m", "context_forge.mcp_server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=_env(), cwd=REPO, encoding="utf-8", errors="replace", bufsize=1)
    try:
        process.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "e2e", "version": "0"}}}) + "\n")
        process.stdin.flush()
        assert "result" in _read_line(process.stdout)

        process.stdin.write(json.dumps({
            "jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        process.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n")
        process.stdin.flush()
        tools = [item["name"] for item in
                 json.loads(_read_line(process.stdout))["result"]["tools"]]
        assert {"context_forge_memory_search", "context_forge_memory_feedback"} <= set(tools)

        process.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "context_forge_memory_search",
                       "arguments": {"query": "缓存失效", "project": "demo"}}}) + "\n")
        process.stdin.flush()
        assert body in _read_line(process.stdout)
    finally:
        process.stdin.close()
        process.kill()
        process.wait(timeout=20)
