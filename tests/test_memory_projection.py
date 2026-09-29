from pathlib import Path
from contextlib import closing

import pytest

from context_forge.memory import MemoryStore
from context_forge.memory_models import MemoryDraft
from context_forge.memory_projection import MemoryProjector, ProjectionConflict


def put(store, body="first"):
    return store.put("m1", MemoryDraft(body=body, status="active"), expected_version=0,
                     idempotency_key="create", reason="test")


def test_db_commit_survives_projection_failure_and_retry(tmp_path: Path, monkeypatch):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    record = put(store)
    projector = MemoryProjector(store, tmp_path / "vault")
    monkeypatch.setattr(projector, "_render", lambda record: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        projector.project(record.id)
    assert store.search("first", project=None)[0].id == "m1"


def test_manual_difference_conflicts_and_explicit_correction(tmp_path: Path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    put(store)
    projector = MemoryProjector(store, tmp_path / "vault")
    result = projector.project("m1")
    result.path.write_text(result.path.read_text(encoding="utf-8").replace("first", "human edit"),
                           encoding="utf-8")
    with pytest.raises(ProjectionConflict):
        projector.project("m1")
    corrected = projector.import_correction(result.path, expected_version=1)
    assert corrected.version == 2 and corrected.draft.body == "human edit"
    assert corrected.draft.user_owned is True


def test_conflict_can_explicitly_keep_database_version(tmp_path: Path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    put(store)
    projector = MemoryProjector(store, tmp_path / "vault")
    result = projector.project("m1")
    result.path.write_text(result.path.read_text(encoding="utf-8").replace("first", "discard me"),
                           encoding="utf-8")
    with pytest.raises(ProjectionConflict):
        projector.project("m1")
    import sqlite3
    with closing(sqlite3.connect(store.path)) as db:
        conflict_id = db.execute("SELECT id FROM projection_conflicts WHERE status='open'").fetchone()[0]
    resolved = projector.resolve_keep_db(conflict_id)
    assert resolved.status == "projected"
    assert "first" in resolved.path.read_text(encoding="utf-8")
