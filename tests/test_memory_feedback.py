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


def _user_owned(tmp_path: Path, monkeypatch) -> MemoryStore:
    monkeypatch.setenv("CONTEXT_FORGE_HOME", str(tmp_path / "home"))
    store = MemoryStore.initialize(tmp_path / "home" / "memory.db")
    store.put("m1", MemoryDraft(body="a lesson the user wrote", status="active",
                                user_owned=True,
                                provenance=Provenance(projects=("demo",))),
              expected_version=0, idempotency_key="seed", reason="test")
    return store


def test_repeated_model_verdicts_cannot_drive_the_weight_without_bound(tmp_path, monkeypatch):
    """Measured: 20 model verdicts drove one memory's weight to -19.0, which is -20 on
    the feedback axis against a 1.0 match axis."""
    from context_forge.memory import WEIGHT_FLOOR

    store = _seed(tmp_path, monkeypatch)
    for index in range(20):
        store.record_feedback("m1", outcome="harmful",
                              idempotency_key=f"f{index}", actor="model")

    assert store.weight("m1") >= WEIGHT_FLOOR
    assert store.weight("m1") < 1.0


def test_a_conflicted_memory_is_not_reweighted_by_a_repeated_verdict(tmp_path, monkeypatch):
    """The first harmful verdict is what parks the record; repeating it must not keep
    pushing the weight down."""
    store = _seed(tmp_path, monkeypatch)
    store.record_feedback("m1", outcome="harmful", idempotency_key="f1", actor="model")
    after_first = store.weight("m1")

    store.record_feedback("m1", outcome="harmful", idempotency_key="f2", actor="model")

    assert store.get("m1").draft.status == "conflicted"
    assert store.weight("m1") == after_first


def test_a_model_verdict_cannot_demote_a_human_owned_memory(tmp_path, monkeypatch):
    """A model may park a human's memory for review, but the weight is the human's."""
    store = _user_owned(tmp_path, monkeypatch)
    store.record_feedback("m1", outcome="harmful", idempotency_key="m1", actor="model")

    assert store.get("m1").draft.status == "active"
    assert store.weight("m1") == 1.0


def test_a_human_verdict_still_moves_a_human_owned_memory(tmp_path, monkeypatch):
    store = _user_owned(tmp_path, monkeypatch)
    store.record_feedback("m1", outcome="irrelevant", idempotency_key="h1", actor="human")

    assert store.weight("m1") < 1.0


def test_the_verdict_is_attributed_to_who_gave_it(tmp_path, monkeypatch):
    """The status change was written as `actor='system'`, which erased the fact that
    a model -- not a person -- had flagged the memory."""
    store = _seed(tmp_path, monkeypatch)
    store.record_feedback("m1", outcome="harmful", idempotency_key="f1", actor="model")

    puts = [event for event in store.feedback("m1") if event["kind"] == "feedback"]
    assert puts and puts[0]["actor"] == "model"


def test_the_mcp_feedback_tool_refuses_a_call_without_a_request_id(tmp_path, monkeypatch):
    """Without one, every call minted a fresh key, so a retry counted twice."""
    import pytest

    _seed(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="request_id"):
        record_memory_feedback("m1", "harmful")


def test_the_vote_buckets_separate_humans_from_models(tmp_path, monkeypatch):
    """Pooling them made every opposed bucket look unanimous."""
    store = _seed(tmp_path, monkeypatch)
    store.record_feedback("m1", outcome="harmful", idempotency_key="f1", actor="model")
    store.record_feedback("m1", outcome="irrelevant", idempotency_key="f2", actor="human")
    from datetime import datetime, timezone
    store.aggregate_signals(datetime.now(timezone.utc).strftime("%Y-%m"))

    totals = store.signal_totals()
    assert totals.get("vote:harmful:model") == 1
    assert totals.get("vote:irrelevant:human") == 1
    assert "vote:harmful" not in totals


def test_suppress_and_restore_are_recorded_as_a_verdict(tmp_path, monkeypatch):
    """A suppression is a strong objection and a restoration is its withdrawal. Both
    used to be status changes only, so neither reached the weight or the votes."""
    from context_forge.memory_lifecycle import MemoryLifecycle

    store = _seed(tmp_path, monkeypatch)
    lifecycle = MemoryLifecycle(store)

    lifecycle.suppress("m1", expected_version=1, idempotency_key="s1")
    assert store.get("m1").draft.status == "disabled"
    assert store.weight("m1") < 1.0
    assert [event["outcome"] for event in store.feedback("m1")] == ["revoked"]

    lifecycle.restore("m1", expected_version=2, idempotency_key="r1")
    assert store.get("m1").draft.status == "active"
    assert store.weight("m1") == 1.0
    assert [event["outcome"] for event in store.feedback("m1")] == ["revoked", "restored"]


def test_the_model_cannot_reach_the_restoration_outcome(tmp_path, monkeypatch):
    """`restored` is a human's acquittal; the feedback API that a host can call
    must not offer it."""
    from context_forge.memory import FEEDBACK_OUTCOMES, HUMAN_FEEDBACK_OUTCOMES

    assert "restored" not in FEEDBACK_OUTCOMES
    assert "restored" in HUMAN_FEEDBACK_OUTCOMES


def test_the_correction_verbs_have_a_cli_exit(tmp_path, monkeypatch):
    """Five verbs existed with no way to reach them, so half of the PRD's
    "人工纠正旁路" was unwired."""
    store = _seed(tmp_path, monkeypatch)
    runner = CliRunner()

    scoped = runner.invoke(cli.app, ["memory", "scope", "m1", "--visibility", "owner"])
    assert scoped.exit_code == 0, scoped.output
    assert store.get("m1").draft.access.visibility == "owner"

    supported = runner.invoke(cli.app, ["memory", "support", "m1",
                                        "--excerpt", "it happened again in prod"])
    assert supported.exit_code == 0, supported.output
    assert store.get("m1").draft.proof_count == 1

    weakened = runner.invoke(cli.app, ["memory", "weaken", "m1",
                                       "--condition", "shared cache",
                                       "--excerpt", "it failed there"])
    assert weakened.exit_code == 0, weakened.output
    assert any(item.condition == "shared cache" for item in store.get("m1").draft.applicability)

    superseded = runner.invoke(cli.app, ["memory", "supersede", "m1",
                                         "--body", "a better statement"])
    assert superseded.exit_code == 0, superseded.output
    assert store.get("m1").draft.status == "stale"

    overturned = runner.invoke(cli.app, ["memory", "overturn", "m1",
                                         "--body", "this is wrong",
                                         "--excerpt", "the log says otherwise"])
    assert overturned.exit_code == 0, overturned.output
    assert any(relation['kind'] == 'contradicts' for relation in store.relations("m1"))


def test_extend_links_a_memory_to_the_one_it_refines(tmp_path, monkeypatch):
    store = _seed(tmp_path, monkeypatch)
    store.put("m2", MemoryDraft(body="another lesson", status="active",
                                provenance=Provenance(projects=("demo",))),
              expected_version=0, idempotency_key="seed-2", reason="test")
    runner = CliRunner()

    extended = runner.invoke(cli.app, ["memory", "extend", "m1", "--target", "m2",
                                        "--kind", "refines",
                                        "--excerpt", "the same rule, narrowed"])
    assert extended.exit_code == 0, extended.output
    kinds = {relation['kind'] for relation in store.relations("m1")}
    assert "refines" in kinds


def test_a_concurrent_correction_is_reported_not_crashed(tmp_path, monkeypatch):
    """A stale expected version used to surface as a traceback, which buried the one
    thing the operator needed to know."""
    store = _seed(tmp_path, monkeypatch)
    store.put("m1", MemoryDraft(body="a newer version of the lesson", status="active",
                                provenance=Provenance(projects=("demo",))),
              expected_version=1, idempotency_key="bump", reason="test")
    runner = CliRunner()

    result = runner.invoke(cli.app, ["memory", "suppress", "m1", "--version", "1"])

    assert result.exit_code != 0
    assert "expected version" in result.output


def test_a_model_cannot_acquit_a_memory(tmp_path, monkeypatch):
    """The acquittal is a human's. The guard belongs in the store, not in the
    wrappers that happen to be human-operated."""
    import pytest

    store = _seed(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match='human'):
        store.record_feedback("m1", outcome="restored", idempotency_key="r1", actor="model")


def test_repeating_one_excerpt_is_not_a_second_independent_observation(tmp_path, monkeypatch):
    """`proof_count` counts independent observations; the same words again are not
    evidence, and the evidence strength must not follow them up."""
    from context_forge.memory_lifecycle import MemoryLifecycle, source_from_text

    store = _seed(tmp_path, monkeypatch)
    lifecycle = MemoryLifecycle(store)
    excerpt = "it happened again in production"

    first = lifecycle.support("m1", source_from_text(excerpt), reason="first observation",
                              expected_version=1, idempotency_key="sup-1")
    assert first.draft.proof_count == 1

    second = lifecycle.support("m1", source_from_text(excerpt), reason="the same words again",
                               expected_version=first.version, idempotency_key="sup-2")
    assert second.draft.proof_count == 1
    assert len(second.draft.sources) == 1

    other = lifecycle.support("m1", source_from_text("a different observation"),
                              reason="a different observation",
                              expected_version=second.version, idempotency_key="sup-3")
    assert other.draft.proof_count == 2
    assert len(other.draft.sources) == 2
