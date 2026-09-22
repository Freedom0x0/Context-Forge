import pytest
import sys
import types

from context_forge.memory import IdempotencyConflict, MemoryStore, MemoryStoreError
from context_forge.memory_models import Applicability, MemoryDraft, Scope, Source
from context_forge.memory_recall import MemoryRecall


def add(store, memory_id, body, *, project="demo", applicability=()):
    return store.put(
        memory_id,
        MemoryDraft(
            body=body,
            status="active",
            scopes=(Scope(kind="project", project=project),),
            applicability=applicability,
            sources=(Source(identity=f"source:{memory_id}", kind="observation", excerpt=body),),
        ),
        expected_version=0,
        idempotency_key=f"put:{memory_id}",
        reason="test",
    )


def test_recall_filters_project_and_returns_conditions(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    add(store, "one", "transaction boundary", applicability=(
        Applicability(condition="only for external integrations", result="unknown"),
    ))
    add(store, "two", "transaction boundary", project="other")
    results = MemoryRecall(store).search("transaction", project="demo")
    assert [result.memory.id for result in results] == ["one"]
    payload = results[0].as_dict()
    assert payload["applicability"][0]["result"] == "unknown"
    assert "unresolved conditions" in payload["explanation"]


def test_retrieval_stages_are_idempotent_and_do_not_change_memory(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    record = add(store, "one", "stable memory")
    recall = MemoryRecall(store)
    result = recall.search("stable", project="demo", request_id="request-1", stage="tool_returned")[0]
    recall.mark(result, request_id="request-1", stage="presented")
    recall.mark(result, request_id="request-1", stage="presented")
    assert [event["stage"] for event in store.retrieval_events("request-1")] == ["tool_returned", "presented"]
    assert store.get("one").version == record.version
    assert store.get("one").draft.evidence_level == "unknown"


def test_retrieval_idempotency_conflict_is_explicit(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    add(store, "one", "first memory")
    add(store, "two", "second memory")
    store.record_retrieval("one", 1, request_id="r", stage="retrieved", idempotency_key="same")
    with pytest.raises(IdempotencyConflict):
        store.record_retrieval("two", 1, request_id="r", stage="retrieved", idempotency_key="same")


def test_suppressed_and_stale_versions_are_not_recalled(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    record = add(store, "one", "hidden memory")
    disabled = record.draft.model_copy(update={"status": "disabled", "suppressed": True})
    store.put("one", disabled, expected_version=1, idempotency_key="disable", actor="human", reason="hide")
    assert MemoryRecall(store).search("hidden", project="demo") == ()
    assert MemoryRecall(store).get("one", version=1).memory.version == 1


def test_tracking_failure_does_not_discard_computed_results(tmp_path, monkeypatch):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    add(store, "one", "available result")
    monkeypatch.setattr(store, "record_retrieval", lambda *args, **kwargs: (_ for _ in ()).throw(MemoryStoreError("busy")))
    results = MemoryRecall(store).search("available", project="demo", request_id="request")
    assert results[0].memory.id == "one"


def test_mcp_registers_legacy_and_memory_tools(tmp_path, monkeypatch):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    add(store, "one", "mcp memory")

    class FakeMCP:
        def __init__(self, name):
            self.name = name
            self.tools = {}

        def tool(self):
            def register(function):
                self.tools[function.__name__] = function
                return function
            return register

    fastmcp = types.ModuleType("mcp.server.fastmcp")
    fastmcp.FastMCP = FakeMCP
    monkeypatch.setitem(sys.modules, "mcp", types.ModuleType("mcp"))
    monkeypatch.setitem(sys.modules, "mcp.server", types.ModuleType("mcp.server"))
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fastmcp)
    import context_forge.mcp_server as mcp_server
    monkeypatch.setattr(mcp_server, "_memory", lambda: store)

    server = mcp_server.create_server()
    assert "context_forge_search" in server.tools
    payload = server.tools["context_forge_memory_search"]("mcp", "demo")
    assert payload[0]["id"] == "one"
    assert "applicability" in payload[0]
