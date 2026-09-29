import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from context_forge.episodes import EpisodeCollector
from context_forge.finalization import FinalizationService
from context_forge.memory import MemoryStore
from context_forge.memory_models import MemoryDraft, Provenance, Source
from context_forge.queue import JobStore


def test_retention_only_deletes_finalized_episode(tmp_path: Path):
    store = JobStore(tmp_path / "queue.db")
    collector = EpisodeCollector(store)
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for sid in ("done", "open"):
        path = tmp_path / f"{sid}.jsonl"
        path.write_text('{}\n', encoding="utf-8")
        episode = collector.collect(sid, path, "demo", "claude_code")
        store.save_episode(episode.model_copy(update={"last_activity_at": old.isoformat()}))
    receipt = collector.request_finalize("done", end_kind="manual", event_id="done")
    store.mark_receipt_committed(receipt.finalization_id)
    deleted = collector.apply_retention(30, now=old + timedelta(days=30))
    assert deleted == ["done"]
    assert store.latest_episode("done") is None
    assert store.latest_episode("open") is not None


def test_session_delete_tombstones_long_term_source(tmp_path: Path):
    queue = JobStore(tmp_path / "queue.db")
    memory = MemoryStore.initialize(tmp_path / "memory.db")
    source = Source(identity="episode:s1:1", kind="observation", session_id="s1",
                    excerpt="private context")
    memory.put("m", MemoryDraft(body="safe conclusion", status="active",
                                 provenance=Provenance(sessions=("s1",)), sources=(source,)),
               expected_version=0, idempotency_key="m", reason="test")
    counts = EpisodeCollector(queue).delete("s1", memory)
    assert counts["source_tombstones"] == 1
    with closing(sqlite3.connect(memory.path)) as db:
        payload = db.execute("SELECT source_json FROM memory_sources WHERE identity=?",
                             (source.identity,)).fetchone()[0]
        assert "source_deleted" in payload and "private context" not in payload
    assert memory.get("m").draft.sources[0].reference == "source_deleted"
    assert memory.get("m").draft.sources[0].excerpt == ""


def test_finalizer_rejects_second_pass_secret(tmp_path: Path):
    from context_forge.domain import FinalizationOutput
    source = Source(identity="s", kind="model", excerpt="token=abc")
    output = FinalizationOutput.model_validate({"schema_version": 1, "candidates": [{
        "candidate_id": "x", "evidence_refs": ["s"],
        "draft": MemoryDraft(body="safe", sources=(source,)).model_dump(mode="json")
    }]})
    with pytest.raises(ValueError, match="redaction"):
        FinalizationService._validate_output(output, "session")
