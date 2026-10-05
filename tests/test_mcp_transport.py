"""Real stdio-transport e2e for the MCP server.

The server runs as a **child process**; the test speaks newline-delimited
JSON-RPC 2.0 over its stdio (this is MCP's stdio framing — not a
`Content-Length` header). It asserts the returned body is real data written
to a temp `memory.db`, not merely that some JSON came back.
"""

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BODY = "zebrafish calibration drift seen during the transport e2e"


def _memory(runtime: Path):
    from context_forge.memory import MemoryStore
    from context_forge.memory_models import MemoryDraft, Provenance, Source

    store = MemoryStore.initialize(runtime / "memory.db")
    store.put(
        "e2e-memory",
        MemoryDraft(
            body=BODY,
            summary="e2e fixture",
            role="procedure",
            status="active",
            provenance=Provenance(projects=("e2e",)),
            sources=(Source(identity="e2e:tool:1", kind="tool", excerpt=BODY),),
        ),
        expected_version=0,
        idempotency_key="e2e:put",
        actor="tool",
        reason="e2e transport fixture",
    )


def _pump(stream, sink: "queue.Queue[str]") -> None:
    for line in stream:
        sink.put(line)


def _call(proc, sink, request_id, method, params, timeout=30.0):
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id,
                                 "method": method, "params": params}) + "\n")
    proc.stdin.flush()
    while True:
        line = sink.get(timeout=timeout)
        message = json.loads(line)
        if message.get("id") == request_id:
            return message


def _start_server(runtime: Path):
    """Spawn the MCP server over stdio and complete the initialize handshake."""
    env = {**os.environ, "CONTEXT_FORGE_HOME": str(runtime), "PYTHONPATH": str(REPO)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "context_forge.mcp_server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=str(REPO), env=env, text=True, encoding="utf-8",
    )
    sink: "queue.Queue[str]" = queue.Queue()
    threading.Thread(target=_pump, args=(proc.stdout, sink), daemon=True).start()
    init = _call(proc, sink, 1, "initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "e2e", "version": "0"},
    })
    proc.stdin.write(json.dumps(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
    proc.stdin.flush()
    return proc, sink, init


def _stop_server(proc, sink):  # noqa: ARG001 - sink kept for symmetry with start
    proc.stdin.close()
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def _tool(proc, sink, request_id, name, arguments):
    call = _call(proc, sink, request_id, "tools/call",
                 {"name": name, "arguments": arguments})
    assert "error" not in call, call
    payload = "".join(part.get("text", "") for part in call["result"]["content"])
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return payload


def test_stdio_transport_end_to_end(tmp_path):
    pytest.importorskip(
        "mcp",
        reason="mcp package not installed; run the transport e2e with "
               "`pip install 'context-forge[mcp]'`",
    )

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    _memory(runtime)

    proc, sink, init = _start_server(runtime)
    try:
        assert init["result"]["serverInfo"]["name"] == "context-forge"
        assert "tools" in init["result"]["capabilities"]

        listing = _call(proc, sink, 2, "tools/list", {})
        names = {tool["name"] for tool in listing["result"]["tools"]}
        assert {"context_forge_memory_search", "context_forge_memory_get"} <= names

        call = _call(proc, sink, 3, "tools/call", {
            "name": "context_forge_memory_search",
            "arguments": {"query": "zebrafish", "project": "e2e"},
        })
        assert "error" not in call, call
        text = "".join(part.get("text", "") for part in call["result"]["content"])
        assert BODY in text
    finally:
        _stop_server(proc, sink)


def test_memory_mark_records_host_votes_over_stdio(tmp_path):
    """The write-back tool must land in retrieval_events, and refuse exposure stages."""
    pytest.importorskip(
        "mcp",
        reason="mcp package not installed; run the transport e2e with "
               "`pip install 'context-forge[mcp]'`",
    )
    import sqlite3

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    _memory(runtime)

    proc, sink, _init = _start_server(runtime)
    try:
        listed = _call(proc, sink, 2, "tools/list", {})
        assert "context_forge_memory_mark" in {t["name"] for t in listed["result"]["tools"]}

        marked = _tool(proc, sink, 3, "context_forge_memory_mark", {
            "memory_id": "e2e-memory", "request_id": "req-1", "stage": "applied",
            "session_id": "sess-1",
        })
        assert marked["memory_id"] == "e2e-memory" and marked["stage"] == "applied"

        again = _tool(proc, sink, 4, "context_forge_memory_mark", {
            "memory_id": "e2e-memory", "request_id": "req-1", "stage": "applied",
            "session_id": "sess-1",
        })
        assert again["weight"] == marked["weight"]          # retry echoes, it does not stack

        refused = _call(proc, sink, 5, "tools/call", {
            "name": "context_forge_memory_mark",
            "arguments": {"memory_id": "e2e-memory", "request_id": "req-1",
                          "stage": "presented"},
        })
        # FastMCP surfaces tool exceptions as isError + text, not a JSON-RPC error.
        assert refused["result"]["isError"] is True
        assert "referenced, applied, validated" in refused["result"]["content"][0]["text"]

        with sqlite3.connect(runtime / "memory.db") as connection:
            rows = connection.execute(
                "SELECT stage, request_id FROM retrieval_events").fetchall()
    finally:
        _stop_server(proc, sink)

    assert rows == [("applied", "req-1")]                   # exactly one recorded mark
