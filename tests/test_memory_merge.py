import hashlib
import sys
import types

from context_forge.memory import MemoryStore
from context_forge.memory_merge import MemoryMerge
from context_forge.memory_models import Applicability, MemoryDraft, Scope, Source


def add(store, memory_id, body, *, kind="knowledge", project="demo", applicability=(), user_owned=False):
    return store.put(
        memory_id,
        MemoryDraft(
            kind=kind, body=body, status="active", user_owned=user_owned,
            scopes=(Scope(kind="project", project=project),), applicability=applicability,
            sources=(Source(identity=f"source:{memory_id}", kind="observation", excerpt=body),),
        ),
        expected_version=0, idempotency_key=f"put:{memory_id}", reason="test",
    )


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_report_is_read_only_deterministic_and_explicit_when_empty(tmp_path):
    path = tmp_path / "memory.db"
    store = MemoryStore.initialize(path)
    add(store, "one", "unique memory")
    before = file_hash(path), path.stat().st_mtime_ns
    first = MemoryMerge(store).report().as_dict()
    second = MemoryMerge(store).report().as_dict()
    assert first == second
    assert first["empty"] is True
    assert before == (file_hash(path), path.stat().st_mtime_ns)


def test_exact_duplicate_requires_same_decision_boundary(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    add(store, "a", "Use transaction boundaries")
    add(store, "b", " use  transaction boundaries ")
    add(store, "different-project", "Use transaction boundaries", project="other")
    add(store, "different-condition", "Use transaction boundaries", applicability=(
        Applicability(condition="external API only", result="unknown"),
    ))
    report = MemoryMerge(store).report(similarity_threshold=0.5)
    assert [(item.left_id, item.right_id) for item in report.exact_duplicates] == [("a", "b")]
    assert all(not item.automatic for item in report.possible_overlaps)


def test_rules_do_not_compare_across_project_boundaries(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    add(store, "a", "always validate external input", kind="rule", project="one")
    add(store, "b", "always validate external inputs", kind="rule", project="two")
    assert MemoryMerge(store).report(similarity_threshold=0.1).possible_overlaps == ()


def test_apply_exact_is_versioned_preserves_sources_and_can_be_undone(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    add(store, "a", "same")
    add(store, "b", "same")
    merge = MemoryMerge(store)
    candidate = merge.report().exact_duplicates[0]
    archived = merge.apply_exact(candidate, idempotency_key="merge-1")
    assert merge.apply_exact(candidate, idempotency_key="merge-1").version == archived.version
    assert archived.id == "b"
    assert archived.draft.status == "archived"
    assert archived.draft.sources[0].identity == "source:b"
    relation = next(item for item in store.relations("b") if item["kind"] == "duplicate_of")
    restored = merge.undo_exact("b", expected_version=archived.version, relation_id=relation["id"],
                                idempotency_key="undo-1")
    assert restored.draft.status == "active"
    assert store.relations("b", version=archived.version) == []


def test_automatic_merge_does_not_archive_human_owned_memory(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    add(store, "a", "same")
    add(store, "b", "same", user_owned=True)
    candidate = MemoryMerge(store).report().exact_duplicates[0]
    result = MemoryMerge(store).apply_exact(candidate, idempotency_key="merge")
    assert result.draft.status == "active"


def test_mcp_report_is_read_only(tmp_path, monkeypatch):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    add(store, "a", "same")
    add(store, "b", "same")

    class FakeMCP:
        def __init__(self, name):
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
    report = server.tools["context_forge_memory_report"]()
    assert report["quality"]["automatic_candidates"] == 1
    assert store.get("b").draft.status == "active"
