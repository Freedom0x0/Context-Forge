from pathlib import Path

import pytest
from pydantic import ValidationError

from context_forge.domain import FinalizationOutput
from context_forge.episodes import EpisodeCollector
from context_forge.finalization import FinalizationService
from context_forge.memory import MemoryStore, canonical_fingerprint
from context_forge.memory_models import MemoryDraft, Provenance, Source
from context_forge.queue import JobStore


class Gateway:
    def __init__(self, output):
        self.output = output
        self.calls = 0

    def finalize_episode(self, payload):
        self.calls += 1
        return self.output


def setup_receipt(tmp_path: Path):
    transcript = tmp_path / "s.jsonl"
    transcript.write_text('{"message":"cache fix"}\n', encoding="utf-8")
    queue = JobStore(tmp_path / "queue.db")
    collector = EpisodeCollector(queue)
    collector.collect("s1", transcript, "demo", "claude_code")
    receipt = collector.request_finalize("s1", end_kind="manual", event_id="manual-1")
    return queue, receipt


def output():
    source = Source(identity="episode:s1:1:1", kind="observation", session_id="s1",
                    excerpt="cache fix")
    return {"schema_version": 1, "candidates": [{
        "candidate_id": "cache", "evidence_refs": [source.identity],
        "draft": MemoryDraft(body="Invalidate permission caches together", status="active",
                             provenance=Provenance(sessions=("s1",)), sources=(source,)).model_dump(mode="json"),
    }], "deferred_topics": [], "no_memory_reason": None}


def test_output_requires_reason_and_enforces_limit():
    with pytest.raises(ValidationError):
        FinalizationOutput()
    item = output()["candidates"][0]
    with pytest.raises(ValidationError):
        FinalizationOutput.model_validate({"schema_version": 1, "candidates": [item] * 6})


def test_finalization_freezes_model_output_and_recovers_without_second_call(tmp_path: Path, monkeypatch):
    queue, receipt = setup_receipt(tmp_path)
    memory = MemoryStore.initialize(tmp_path / "memory.db")
    gateway = Gateway(output())
    service = FinalizationService(queue, memory, gateway)
    original = queue.complete_finalization_operation
    failed = False

    def fail_once(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("crash after memory commit")
        return original(*args, **kwargs)

    monkeypatch.setattr(queue, "complete_finalization_operation", fail_once)
    with pytest.raises(RuntimeError):
        service.finalize(receipt.finalization_id)
    assert gateway.calls == 1
    monkeypatch.setattr(queue, "complete_finalization_operation", original)
    ids = service.finalize(receipt.finalization_id)
    assert gateway.calls == 1
    assert len(ids) == 1
    record = memory.get(ids[0])
    assert memory.find_exact(canonical_fingerprint(record.draft)).id == record.id
    assert queue.receipt(receipt.finalization_id).status == "committed"


def test_unknown_evidence_and_relations_are_rejected(tmp_path: Path):
    queue, receipt = setup_receipt(tmp_path)
    bad = output()
    bad["candidates"][0]["evidence_refs"] = ["missing"]
    with pytest.raises(ValueError, match="unknown evidence"):
        FinalizationService(queue, MemoryStore.initialize(tmp_path / "memory.db"), Gateway(bad)).finalize(
            receipt.finalization_id)


def test_worker_consumes_pending_receipt_without_inventing_offline_memory(tmp_path: Path):
    from context_forge.gateways import NoOpGateway
    from context_forge.worker import process_pending_finalizations

    queue, receipt = setup_receipt(tmp_path)
    memory = MemoryStore.initialize(tmp_path / "memory.db")
    assert process_pending_finalizations(queue, NoOpGateway(), memory) == 1
    assert queue.receipt(receipt.finalization_id).status == "committed"
    assert memory.current_records() == []


def test_two_workers_only_one_claims_model_extraction(tmp_path: Path):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import time

    queue, receipt = setup_receipt(tmp_path)
    MemoryStore.initialize(tmp_path / "memory.db")

    class SlowGateway(Gateway):
        def __init__(self):
            super().__init__(output())
            self.lock = threading.Lock()

        def finalize_episode(self, payload):
            with self.lock:
                self.calls += 1
            time.sleep(.1)
            return self.output

    gateway = SlowGateway()

    def run():
        return FinalizationService(JobStore(tmp_path / "queue.db"),
                                   MemoryStore(tmp_path / "memory.db", readonly=False),
                                   gateway).finalize(receipt.finalization_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert gateway.calls == 1
    assert sum(bool(result) for result in results) == 1


def test_projection_failure_keeps_committed_receipt_for_retry(tmp_path: Path):
    from context_forge.worker import process_pending_finalizations

    queue, receipt = setup_receipt(tmp_path)
    memory = MemoryStore.initialize(tmp_path / "memory.db")

    class BrokenProjector:
        def project(self, memory_id):
            raise OSError("disk full")

    assert process_pending_finalizations(queue, Gateway(output()), memory, BrokenProjector()) == 1
    saved = queue.receipt(receipt.finalization_id)
    assert saved.status == "committed"
    assert "disk full" in saved.error
    assert receipt.finalization_id in [item.finalization_id for item in queue.pending_receipts()]


def made_of(body, session):
    source = Source(identity=f"episode:{session}:1:1", kind="observation",
                    session_id=session, excerpt=body)
    return {"schema_version": 1, "candidates": [{
        "candidate_id": "cache", "evidence_refs": [source.identity],
        "draft": MemoryDraft(body=body, status="active",
                             provenance=Provenance(sessions=(session,)), sources=(source,))
        .model_dump(mode="json"),
    }], "deferred_topics": [], "no_memory_reason": None}


def test_a_case_variant_lands_on_the_same_memory(tmp_path):
    """One assertion written twice, differing only in case, must not fork a second record."""
    queue = JobStore(tmp_path / "queue.db")
    memory = MemoryStore.initialize(tmp_path / "memory.db")
    collector = EpisodeCollector(queue)
    for index, body in enumerate(("SQLite 事务要回滚", "sqlite 事务要回滚")):
        session = f"s{index}"
        transcript = tmp_path / f"{session}.jsonl"
        transcript.write_text('{"message":"cache fix"}\n', encoding="utf-8")
        collector.collect(session, transcript, "demo", "claude_code")
        receipt = collector.request_finalize(session, end_kind="manual",
                                             event_id=f"manual-{index}")
        FinalizationService(queue, memory, Gateway(made_of(body, session))).finalize(
            receipt.finalization_id)

    records = list(memory.current_records())
    assert len(records) == 1
    assert records[0].version == 2
    assert {item.identity for item in records[0].draft.sources} == {"episode:s0:1:1",
                                                                   "episode:s1:1:1"}
