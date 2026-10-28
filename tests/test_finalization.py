import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from context_forge.domain import FinalizationOutput
from context_forge.episodes import EpisodeCollector
from context_forge.finalization import FinalizationService
from context_forge.memory import MemoryStore, ProtectedMemory, canonical_fingerprint
from context_forge.memory_models import MemoryDraft, Provenance, Source
from context_forge.queue import JobStore


class Gateway:
    def __init__(self, output):
        self.output = output
        self.calls = 0

    def finalize_episode(self, payload):
        self.calls += 1
        return self.output


class CapturingGateway(Gateway):
    def __init__(self, output):
        super().__init__(output)
        self.payload = None

    def finalize_episode(self, payload):
        self.payload = payload
        return super().finalize_episode(payload)


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
    # a source is named after its own text now, so compare the origin part
    origins = {item.identity.split("#")[0] for item in records[0].draft.sources}
    assert origins == {"episode:s0:1:1", "episode:s1:1:1"}


def test_a_second_project_widens_provenance_instead_of_replacing_it(tmp_path):
    """Measured before the fix: one rule precipitated from two projects came back with
    projects=('beta',) only — the first project had been quietly deleted."""
    queue = JobStore(tmp_path / "queue.db")
    memory = MemoryStore.initialize(tmp_path / "memory.db")
    collector = EpisodeCollector(queue)
    for session, project in (("s0", "alpha"), ("s1", "beta")):
        transcript = tmp_path / f"{session}.jsonl"
        transcript.write_text('{"message":"cache fix"}\n', encoding="utf-8")
        collector.collect(session, transcript, project, "claude_code")
        receipt = collector.request_finalize(session, end_kind="manual",
                                             event_id=f"manual-{session}")
        FinalizationService(queue, memory,
                            Gateway(made_of("SQLite 事务要回滚", session))).finalize(
            receipt.finalization_id)

    records = list(memory.current_records())
    assert len(records) == 1
    assert records[0].version == 2
    assert set(records[0].draft.provenance.projects) == {"alpha", "beta"}
    assert set(records[0].draft.provenance.sessions) == {"s0", "s1"}


def _precipitate(queue, memory, collector, tmp_path, session, body):
    """One episode through the real write path: returns (memory ids, finalization id)."""
    transcript = tmp_path / f"{session}.jsonl"
    transcript.write_text('{"message":"cache fix"}\n', encoding="utf-8")
    collector.collect(session, transcript, "demo", "claude_code")
    receipt = collector.request_finalize(session, end_kind="manual", event_id=f"manual-{session}")
    ids = FinalizationService(queue, memory, Gateway(made_of(body, session))).finalize(
        receipt.finalization_id)
    return ids, receipt.finalization_id


def _put_as_human(memory, memory_id, **updates):
    record = memory.get(memory_id)
    memory.put(memory_id, record.draft.model_copy(update=updates),
               expected_version=record.version, idempotency_key=f"human-{memory_id}",
               actor="human", reason="人说不要这条")


def test_a_vetoed_memory_absorbs_new_evidence_and_the_count_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXT_FORGE_HOME", str(tmp_path))
    """The veto holds: still archived, still invisible to recall. The count is how anyone
    learns the model keeps re-proposing an idea the human already put away."""
    queue = JobStore(tmp_path / "queue.db")
    memory = MemoryStore.initialize(tmp_path / "memory.db")
    collector = EpisodeCollector(queue)
    body = "SQLite 事务必须成对回滚"

    written, _ = _precipitate(queue, memory, collector, tmp_path, "s1", body)
    memory_id = written[0]
    _put_as_human(memory, memory_id, status="archived")

    again, finalization_id = _precipitate(queue, memory, collector, tmp_path, "s2", body)

    assert again == [memory_id]                       # the same row, not a fresh active twin
    record = memory.get(memory_id)
    assert record.draft.status == "archived"
    assert record.version == 3
    assert memory.search(body, project="demo") == []  # still withheld
    assert queue.finalization_operations(finalization_id)[0]["vetoed"] == 1
    assert queue.metrics()["veto_absorbed"] == 1

    from context_forge import status as status_mod
    snapshot = status_mod.collect(queue)
    assert snapshot["veto_absorbed"] == 1
    assert "landed on a memory you archived" in status_mod.render_text(snapshot)


def test_an_ordinary_update_is_not_counted_as_a_veto(tmp_path):
    queue = JobStore(tmp_path / "queue.db")
    memory = MemoryStore.initialize(tmp_path / "memory.db")
    collector = EpisodeCollector(queue)
    body = "SQLite 事务必须成对回滚"

    written, finalization_id = _precipitate(queue, memory, collector, tmp_path, "s1", body)
    again, second = _precipitate(queue, memory, collector, tmp_path, "s2", body)

    assert again == written
    assert memory.get(written[0]).draft.status == "active"
    assert memory.search(body, project="demo") != []
    assert queue.finalization_operations(second)[0]["vetoed"] == 0
    assert queue.metrics()["veto_absorbed"] == 0


def test_a_suppressed_row_refuses_the_write_instead_of_absorbing_it(tmp_path):
    """The two veto kinds differ, and only one is silent: `archived` absorbs new evidence,
    while a suppressed, disabled or human-owned row refuses it at `put` (ProtectedMemory)."""
    queue = JobStore(tmp_path / "queue.db")
    memory = MemoryStore.initialize(tmp_path / "memory.db")
    collector = EpisodeCollector(queue)
    body = "SQLite 事务必须成对回滚"

    written, _ = _precipitate(queue, memory, collector, tmp_path, "s1", body)
    _put_as_human(memory, written[0], suppressed=True)

    # The refusal is per candidate now: the batch survives, the refusal is recorded.
    _, finalization_id = _precipitate(queue, memory, collector, tmp_path, "s2", body)

    assert memory.get(written[0]).draft.suppressed is True
    assert memory.search(body, project="demo") == []
    assert queue.metrics()["veto_absorbed"] == 0        # refused, not absorbed
    failed = [row for row in queue.finalization_operations(finalization_id)
              if row["status"] == "failed"]
    assert failed and "ProtectedMemory" in failed[0]["error"]


def test_an_older_queue_database_gains_the_column_without_breaking_writes(tmp_path):
    """`vetoed` arrives through ALTER, so the insert must name its columns; a positional
    VALUES list would have silently mismatched the moment the column appeared."""
    import sqlite3

    path = tmp_path / "queue.db"
    JobStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE finalization_operations DROP COLUMN vetoed")

    queue = JobStore(path)
    with queue.db:
        columns = [row[1] for row in queue.db.execute(
            "PRAGMA table_info(finalization_operations)").fetchall()]
    assert "vetoed" in columns

    memory = MemoryStore.initialize(tmp_path / "memory.db")
    written, finalization_id = _precipitate(queue, memory, EpisodeCollector(queue), tmp_path,
                                           "s1", "SQLite 事务必须成对回滚")
    assert len(written) == 1
    assert queue.finalization_operations(finalization_id)[0]["vetoed"] == 0


def test_the_status_line_announces_an_absorbed_veto_and_stays_quiet_otherwise():
    from context_forge import status as status_mod

    snapshot = {"reviews_pending": 0, "jobs_dead_letter": 0, "sessions": 3,
                "rules_enabled": 0, "memory_unsynced": 0, "veto_absorbed": 2}

    assert "2 write(s) landed on a memory you archived" in status_mod.render_text(snapshot)
    assert "landed on a memory" not in status_mod.render_text({**snapshot, "veto_absorbed": 0})


def test_the_prompt_carries_checkpoint_metadata_not_their_text(tmp_path: Path):
    """`transcript` is already the whole episode. Repeating each checkpoint's text put
    m near-copies of it in the prompt -- the same quadratic copy the store had."""
    from context_forge.domain import SessionLifecycleEvent

    transcript = tmp_path / "s.jsonl"
    transcript.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "one"}}) + "\n",
        encoding="utf-8")
    queue = JobStore(tmp_path / "queue.db")
    collector = EpisodeCollector(queue)
    collector.collect("s1", transcript, "demo", "claude_code")
    event = SessionLifecycleEvent(
        event_id="pre-1", type="pre_compact", source="test", session_id="s1",
        transcript_path=str(transcript), observed_at="2026-09-24T00:00:00+00:00",
        final_line_count=1, reason="context_compaction")
    queue.ingest_session_event(event)
    collector.checkpoint(event)
    receipt = collector.request_finalize("s1", end_kind="manual", event_id="manual-1")
    gateway = CapturingGateway(output())

    FinalizationService(queue, MemoryStore.initialize(tmp_path / "memory.db"),
                        gateway).finalize(receipt.finalization_id)

    checkpoint = gateway.payload["checkpoints"][0]
    assert "snapshot" not in checkpoint
    assert checkpoint["end_line"] == 1
    assert gateway.payload["transcript"] == "user: one"


def test_committing_a_receipt_drops_the_checkpoint_text_and_keeps_its_coverage(tmp_path: Path):
    """The body is a backup of the host log; once the extraction is frozen it only
    duplicates the transcript. Line ranges stay so coverage remains auditable."""
    from context_forge.domain import SessionLifecycleEvent

    transcript = tmp_path / "s.jsonl"
    transcript.write_text('{"message":"one"}\n', encoding="utf-8")
    queue = JobStore(tmp_path / "queue.db")
    collector = EpisodeCollector(queue)
    collector.collect("s1", transcript, "demo", "claude_code")
    event = SessionLifecycleEvent(
        event_id="pre-1", type="pre_compact", source="test", session_id="s1",
        transcript_path=str(transcript), observed_at="2026-09-24T00:00:00+00:00",
        final_line_count=1, reason="context_compaction")
    queue.ingest_session_event(event)
    collector.checkpoint(event)
    receipt = collector.request_finalize("s1", end_kind="manual", event_id="manual-1")

    FinalizationService(queue, MemoryStore.initialize(tmp_path / "memory.db"),
                        Gateway(output())).finalize(receipt.finalization_id)

    [checkpoint] = queue.list_checkpoints("s1")
    assert checkpoint.snapshot == ""
    assert (checkpoint.start_line, checkpoint.end_line) == (0, 1)
    assert checkpoint.input_digest


def test_the_finalizer_receives_the_conversation_not_the_raw_log(tmp_path: Path):
    """Raw transcript lines make the model improvise (see transcript.py)."""
    transcript = tmp_path / "s.jsonl"
    transcript.write_text(
        json.dumps({"type": "user", "message": {"role": "user",
                                                "content": "investigate the cache"}}) + "\n",
        encoding="utf-8")
    queue = JobStore(tmp_path / "queue.db")
    collector = EpisodeCollector(queue)
    collector.collect("s1", transcript, "demo", "claude_code")
    receipt = collector.request_finalize("s1", end_kind="manual", event_id="manual-1")
    gateway = CapturingGateway(output())

    FinalizationService(queue, MemoryStore.initialize(tmp_path / "memory.db"),
                        gateway).finalize(receipt.finalization_id)

    assert gateway.payload["transcript"] == "user: investigate the cache"


def test_a_huge_episode_is_truncated_to_the_finalizer_budget(tmp_path: Path):
    """A 21 MB session was 2.16M tokens against a 1.05M limit: the call was rejected
    outright and the episode was left dead."""
    from context_forge import finalization as fin

    transcript = tmp_path / "s.jsonl"
    transcript.write_text(
        "".join(json.dumps({"type": "user", "message": {"role": "user",
                                                         "content": "x" * 400}}) + "\n"
                    for _ in range(20000)), encoding="utf-8")
    queue = JobStore(tmp_path / "queue.db")
    collector = EpisodeCollector(queue)
    collector.collect("s1", transcript, "demo", "claude_code")
    receipt = collector.request_finalize("s1", end_kind="manual", event_id="manual-1")
    gateway = CapturingGateway(output())

    FinalizationService(queue, MemoryStore.initialize(tmp_path / "memory.db"),
                        gateway).finalize(receipt.finalization_id)

    payload = gateway.payload["transcript"]
    assert fin.estimate_tokens(payload) <= fin.FINALIZER_TOKEN_BUDGET
    assert payload.startswith(fin.TRUNCATION_NOTICE)


def test_a_failed_finalization_releases_the_episode(tmp_path: Path):
    """`active_episodes()` offers only `open` and `inferred_pending`, so an episode
    left in `finalizing` is never offered again and never cleaned up."""
    from context_forge.worker import process_pending_finalizations

    class FailingGateway:
        def finalize_episode(self, payload):
            raise RuntimeError("provider refused")

    transcript = tmp_path / "s.jsonl"
    transcript.write_text('{"message":"one"}\n', encoding="utf-8")
    queue = JobStore(tmp_path / "queue.db")
    collector = EpisodeCollector(queue)
    collector.collect("s1", transcript, "demo", "claude_code")
    receipt = collector.request_finalize("s1", end_kind="manual", event_id="manual-1")
    memory = MemoryStore.initialize(tmp_path / "memory.db")

    process_pending_finalizations(queue, FailingGateway(), memory)

    assert queue.receipt(receipt.finalization_id).status == "failed"
    assert queue.latest_episode("s1").state == "open"


def test_a_protocol_conforming_gateway_still_names_itself(tmp_path: Path):
    """`LlmGateway` declares `name`, not `model`. The finalizer took only `model`,
    so a conforming gateway's memories were recorded with an empty model."""
    from context_forge.worker import process_pending_finalizations

    class NamedOnlyGateway:
        name = "my-gateway"

        def finalize_episode(self, payload):
            return output()

    transcript = tmp_path / "s.jsonl"
    transcript.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "one"}}) + "\n",
        encoding="utf-8")
    queue = JobStore(tmp_path / "queue.db")
    collector = EpisodeCollector(queue)
    collector.collect("s1", transcript, "demo", "claude_code")
    receipt = collector.request_finalize("s1", end_kind="manual", event_id="manual-1")
    memory = MemoryStore.initialize(tmp_path / "memory.db")

    process_pending_finalizations(queue, NamedOnlyGateway(), memory)

    written = memory.current_records()
    assert len(written) == 1
    assert written[0].draft.model == "my-gateway"


def test_the_budget_is_counted_in_tokens_not_characters(tmp_path: Path):
    """A character budget cannot stand in for a token budget: 2,000,000 characters is
    500K tokens in English and 2M in Chinese, and this library's text is Chinese."""
    from context_forge import finalization as fin

    chinese = "缓存失效要成对执行，否则会读到半旧的权限。" * 40_000
    assert len(chinese) > fin.FINALIZER_TOKEN_BUDGET          # way over in characters
    assert fin.estimate_tokens(chinese) > fin.FINALIZER_TOKEN_BUDGET

    fitted = fin._fit_budget(chinese)

    assert fitted.startswith(fin.TRUNCATION_NOTICE)
    assert fin.estimate_tokens(fitted) <= fin.FINALIZER_TOKEN_BUDGET


def test_a_short_chinese_episode_is_not_truncated(tmp_path: Path):
    from context_forge import finalization as fin

    chinese = "缓存失效要成对执行，否则会读到半旧的权限。"
    assert fin._fit_budget(chinese) == chinese


def test_two_candidates_may_quote_one_source_differently(tmp_path: Path):
    """The model names a source by file and then quotes different passages in two
    candidates. Naming the source after the passage is what stops the second write
    from colliding with the first."""
    queue = JobStore(tmp_path / "queue.db")
    memory = MemoryStore.initialize(tmp_path / "memory.db")
    collector = EpisodeCollector(queue)
    transcript = tmp_path / "s.jsonl"
    transcript.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "one"}}) + "\n",
        encoding="utf-8")
    collector.collect("s1", transcript, "demo", "claude_code")
    receipt = collector.request_finalize("s1", end_kind="manual", event_id="manual-1")

    def source(excerpt):
        return Source(identity="s1/transcript", kind="observation", session_id="s1",
                      excerpt=excerpt)

    first, second = source("the clocks differ by 47 seconds"), source("gateways must use NTP")
    output = {"schema_version": 1, "candidates": [
        {"candidate_id": "a", "evidence_refs": [first.identity],
         "draft": MemoryDraft(body="clock skew causes 401s", status="active",
                              provenance=Provenance(sessions=("s1",)),
                              sources=(first,)).model_dump(mode="json")},
        {"candidate_id": "b", "evidence_refs": [second.identity],
         "draft": MemoryDraft(body="gateways must force NTP", status="active",
                              provenance=Provenance(sessions=("s1",)),
                              sources=(second,)).model_dump(mode="json")},
    ], "deferred_topics": [], "no_memory_reason": None}

    FinalizationService(queue, memory, Gateway(output)).finalize(receipt.finalization_id)

    assert len(memory.current_records()) == 2
    assert queue.receipt(receipt.finalization_id).status == "committed"
    assert [row for row in queue.finalization_operations(receipt.finalization_id)
            if row["status"] == "failed"] == []


def test_a_failed_attempt_keeps_the_receipt_retryable_until_the_cap(tmp_path: Path):
    """Bounding the wait without a retry would trade "blocks the watcher" for
    "loses the memory"."""
    queue, receipt = setup_receipt(tmp_path)

    assert queue.fail_finalization(receipt.finalization_id, "timeout", max_attempts=3) == "planned"
    assert receipt.finalization_id in [item.finalization_id for item in queue.pending_receipts()]
    assert queue.fail_finalization(receipt.finalization_id, "timeout", max_attempts=3) == "planned"
    assert queue.fail_finalization(receipt.finalization_id, "timeout", max_attempts=3) == "failed"
    assert queue.receipt(receipt.finalization_id).attempts == 3
    assert receipt.finalization_id not in [item.finalization_id for item in queue.pending_receipts()]
