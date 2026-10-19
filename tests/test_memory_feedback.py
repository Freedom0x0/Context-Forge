from pathlib import Path

from typer.testing import CliRunner

from context_forge import cli
from context_forge.mcp_server import record_memory_feedback
from context_forge.memory import MemoryStore
from context_forge.memory_models import MemoryDraft, Provenance


def _seed(tmp_path: Path, monkeypatch) -> MemoryStore:
    monkeypatch.setenv("CONTEXT_FORGE_HOME", str(tmp_path / "home"))
    store = MemoryStore.initialize(tmp_path / "home" / "memory.db")
    store.put("m1", MemoryDraft(body="a durable lesson about transactions", status="active",
                                provenance=Provenance(projects=("demo",))),
              expected_version=0, idempotency_key="seed", reason="test")
    return store


def test_cli_feedback_is_idempotent_with_a_request_id(tmp_path, monkeypatch):
    store = _seed(tmp_path, monkeypatch)
    runner = CliRunner()
    for _ in range(2):
        result = runner.invoke(cli.app, ["memory", "feedback", "m1",
                                         "--outcome", "harmful", "--request-id", "r1"])
        assert result.exit_code == 0, result.output
    assert store.get("m1").draft.status == "conflicted"
    assert store.weight("m1") < 1.0
    assert len(store.feedback("m1")) == 1


def test_cli_suppress_and_restore_are_reversible(tmp_path, monkeypatch):
    store = _seed(tmp_path, monkeypatch)
    runner = CliRunner()

    suppressed = runner.invoke(cli.app, ["memory", "suppress", "m1"])
    assert suppressed.exit_code == 0, suppressed.output
    assert store.get("m1").draft.status == "disabled"

    again = runner.invoke(cli.app, ["memory", "suppress", "m1"])
    assert again.exit_code == 0, again.output
    assert "already" in again.output

    restored = runner.invoke(cli.app, ["memory", "restore", "m1"])
    assert restored.exit_code == 0, restored.output
    assert store.get("m1").draft.status == "active"


def test_mcp_feedback_parks_harmful_for_review(tmp_path, monkeypatch):
    store = _seed(tmp_path, monkeypatch)

    payload = record_memory_feedback("m1", "harmful", note="misapplied", request_id="r2")

    assert payload["status"] == "conflicted"
    assert payload["weight"] < 1.0
    assert store.get("m1").draft.status == "conflicted"


def test_feedback_rejects_an_unknown_outcome(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    result = CliRunner().invoke(cli.app, ["memory", "feedback", "m1", "--outcome", "meh"])
    assert result.exit_code != 0
