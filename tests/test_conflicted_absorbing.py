import json

import pytest

from context_forge.domain import KnowledgeItem, RuleExtraction, SessionArtifacts
from context_forge.episodes import EpisodeCollector
from context_forge.finalization import FinalizationService
from context_forge.memory import MemoryStore, ProtectedMemory
from context_forge.memory_lifecycle import MemoryLifecycle
from context_forge.memory_merge import MemoryMerge
from context_forge.memory_models import MemoryDraft, Provenance, Source
from context_forge.queue import JobStore


def seed(store, memory_id="m", body="use tests", status="active"):
    return store.put(
        memory_id,
        MemoryDraft(kind="knowledge", body=body, status=status,
                    provenance=Provenance(projects=("demo",)),
                    sources=(Source(identity=f"src:{memory_id}", kind="observation", excerpt=body),)),
        expected_version=0, idempotency_key=f"put:{memory_id}", reason="test")


def conflict(store, memory_id, key="fb"):
    store.record_feedback(memory_id, outcome="harmful", idempotency_key=key, note="broke prod")
    return store.get(memory_id)


def test_ac1_model_may_not_clear_a_conflict(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    conflicted = conflict(store, seed(store).id)
    assert conflicted.draft.status == "conflicted"
    draft = conflicted.draft.model_copy(update={"status": "active"})
    with pytest.raises(ProtectedMemory):
        store.put(conflicted.id, draft, expected_version=conflicted.version,
                  idempotency_key="model-clear", actor="model", reason="automatic proposal")
    after = store.get(conflicted.id)
    assert after.draft.status == "conflicted"
    assert after.version == conflicted.version


def test_ac2_model_may_enrich_a_conflict_without_clearing_it(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    conflicted = conflict(store, seed(store).id)
    updated = MemoryLifecycle(store).support(
        conflicted.id, Source(identity="src:extra", kind="observation", excerpt="more evidence"),
        reason="append evidence", expected_version=conflicted.version, idempotency_key="support")
    assert updated.draft.status == "conflicted"
    assert updated.version == conflicted.version + 1


def test_ac3_a_human_clears_the_conflict_via_restore(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    conflicted = conflict(store, seed(store).id)
    restored = MemoryLifecycle(store).restore(
        conflicted.id, expected_version=conflicted.version, idempotency_key="restore")
    assert restored.draft.status == "active"


def test_ac4_publish_skips_a_conflict_and_the_batch_continues(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    service = MemoryLifecycle(store)
    first = service.publish_session(
        SessionArtifacts(title="t", knowledge=KnowledgeItem(id="k", body="original"),
                         rule_candidate=RuleExtraction(instruction="run tests")),
        session_id="s1", project="demo")
    knowledge_id = first[0].id
    conflict(store, knowledge_id)

    second = service.publish_session(
        SessionArtifacts(title="t", knowledge=KnowledgeItem(id="k", body="reworded"),
                         rule_candidate=RuleExtraction(instruction="run the tests instead")),
        session_id="s1", project="demo")

    assert second[0].id == knowledge_id
    assert second[0].draft.status == "conflicted"     # skipped, not revived
    assert second[1].draft.status == "active"          # later candidate still published
    assert store.get(knowledge_id).draft.status == "conflicted"


def test_ac5_apply_exact_leaves_a_conflicted_duplicate_alone(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    seed(store, "a", "Use transaction boundaries")
    seed(store, "b", "Use transaction boundaries")
    candidate = MemoryMerge(store).report().exact_duplicates[0]
    conflict(store, "b")

    result = MemoryMerge(store).apply_exact(candidate, idempotency_key="merge-1")

    assert result.id == "b"
    assert result.draft.status == "conflicted"
    assert store.get("b").draft.status == "conflicted"


class _Gateway:
    def __init__(self, output):
        self.output = output

    def finalize_episode(self, payload):
        return self.output


def _output(body, session):
    source = Source(identity=f"episode:{session}:1:1", kind="observation", session_id=session,
                    excerpt=body)
    return {"schema_version": 1, "candidates": [{
        "candidate_id": "c", "evidence_refs": [source.identity],
        "draft": MemoryDraft(body=body, status="active", provenance=Provenance(sessions=(session,)),
                             sources=(source,)).model_dump(mode="json"),
    }], "deferred_topics": [], "no_memory_reason": None}


def _precipitate(queue, memory, tmp_path, session, body):
    transcript = tmp_path / f"{session}.jsonl"
    transcript.write_text('{"message":"x"}\n', encoding="utf-8")
    collector = EpisodeCollector(queue)
    collector.collect(session, transcript, "demo", "claude_code")
    receipt = collector.request_finalize(session, end_kind="manual", event_id=f"manual-{session}")
    ids = FinalizationService(queue, memory, _Gateway(_output(body, session))).finalize(
        receipt.finalization_id)
    return ids, receipt.finalization_id


def test_ac6_finalization_skips_a_conflicted_target_and_marks_it_committed(tmp_path):
    queue = JobStore(tmp_path / "queue.db")
    memory = MemoryStore.initialize(tmp_path / "memory.db")
    body = "SQLite 事务必须成对回滚"
    written, _ = _precipitate(queue, memory, tmp_path, "s1", body)
    conflict(store=memory, memory_id=written[0])
    before = memory.get(written[0])

    again, finalization_id = _precipitate(queue, memory, tmp_path, "s2", body)

    assert again == [written[0]]
    operation = queue.finalization_operations(finalization_id)[0]
    assert operation["status"] == "committed"
    assert "skipped" in json.loads(operation["result_json"])
    after = memory.get(written[0])
    assert after.draft.status == "conflicted"
    assert after.version == before.version             # the write never happened
    # A re-run does not raise and does not retry the skipped operation.
    assert FinalizationService(queue, memory, _Gateway(_output(body, "s2"))).finalize(
        finalization_id) == [written[0]]


def test_ac7_suppressed_and_disabled_still_raise_from_publish(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    service = MemoryLifecycle(store)
    artifacts = SessionArtifacts(title="t", knowledge=KnowledgeItem(id="k", body="keep"))
    record = service.publish_session(artifacts, session_id="s1", project="demo")[0]
    service.suppress(record.id, expected_version=record.version, idempotency_key="suppress")
    with pytest.raises(ProtectedMemory):
        service.publish_session(artifacts, session_id="s1", project="demo")
