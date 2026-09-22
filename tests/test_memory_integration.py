import hashlib

from context_forge.domain import KnowledgeItem, RuleExtraction, SessionArtifacts
from context_forge.gateways import FixtureGateway
from context_forge.memory import MemoryStore
from context_forge.memory_lifecycle import MemoryLifecycle
from context_forge.memory_merge import MemoryMerge
from context_forge.memory_recall import MemoryRecall
from context_forge.queue import JobStore
from context_forge.vault import Vault
from context_forge.worker import process_session


def test_worker_to_correction_recall_and_report(tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        '{"type":"user","message":{"content":"external calls need a boundary"}}\n'
        '{"type":"assistant","message":{"content":"use a transaction boundary"}}\n',
        encoding="utf-8",
    )
    artifacts = SessionArtifacts(
        title="transaction boundary",
        knowledge=KnowledgeItem(id="knowledge-1", body="Use a boundary for external integrations"),
        rule_candidate=RuleExtraction(instruction="Add a boundary around external integrations", paths=["src/**"]),
    )
    queue = JobStore(tmp_path / "queue.db")
    vault = Vault(tmp_path / "vault")
    memory = MemoryStore.initialize(tmp_path / "memory.db")
    result = process_session(
        queue, vault, FixtureGateway(artifacts), "session-1", str(transcript),
        hashlib.sha256(transcript.read_bytes()).hexdigest(), "demo", memory_store=memory,
    )
    assert result == "processed"

    records = memory.current_records()
    assert {record.draft.kind for record in records} == {"knowledge", "rule"}
    rule = next(record for record in records if record.draft.kind == "rule")
    assert any(item["kind"] == "derived_from" for item in memory.relations(rule.id))

    knowledge = next(record for record in records if record.draft.kind == "knowledge")
    corrected = MemoryLifecycle(memory).revise(
        knowledge.id, body="Use a boundary only when integration complexity justifies it",
        expected_version=knowledge.version, idempotency_key="human-correction",
        reason="avoid overengineering",
    )
    recalled = MemoryRecall(memory).search("boundary", project="demo", request_id="next-task")
    assert any(item.memory.id == corrected.id and "only when" in item.memory.draft.body for item in recalled)
    assert MemoryMerge(memory).report().total == 2


def test_worker_retry_does_not_duplicate_memory_versions(tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"type":"user","message":{"content":"remember this"}}\n', encoding="utf-8")
    artifacts = SessionArtifacts(title="lesson", knowledge=KnowledgeItem(id="k", body="stable lesson"))
    queue = JobStore(tmp_path / "queue.db")
    memory = MemoryStore.initialize(tmp_path / "memory.db")
    gateway = FixtureGateway(artifacts)
    first = process_session(queue, Vault(tmp_path / "vault"), gateway, "s", str(transcript), None, "demo", memory_store=memory)
    queue.set_lines_processed("s", 0)
    second = process_session(queue, Vault(tmp_path / "vault"), gateway, "s", str(transcript), None, "demo", memory_store=memory)
    assert first == second == "processed"
    assert len(memory.current_records()) == 1
    assert len(memory.history(memory.current_records()[0].id)) == 1
