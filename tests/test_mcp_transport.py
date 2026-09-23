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


def test_stdio_transport_end_to_end(tmp_path):
    pytest.importorskip(
        "mcp",
        reason="mcp package not installed; run the transport e2e with "
               "`pip install 'context-forge[mcp]'`",
    )

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    _memory(runtime)

    env = {**os.environ, "CONTEXT_FORGE_HOME": str(runtime), "PYTHONPATH": str(REPO)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "context_forge.mcp_server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=str(REPO), env=env, text=True, encoding="utf-8",
    )
    sink: "queue.Queue[str]" = queue.Queue()
    reader = threading.Thread(target=_pump, args=(proc.stdout, sink), daemon=True)
    reader.start()
    try:
        init = _call(proc, sink, 1, "initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "e2e", "version": "0"},
        })
        assert init["result"]["serverInfo"]["name"] == "context-forge"
        assert "tools" in init["result"]["capabilities"]

        proc.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        proc.stdin.flush()

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
        proc.stdin.close()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
