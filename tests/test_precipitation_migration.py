from pathlib import Path

from context_forge.memory import MemoryStore
from context_forge.memory_models import MemoryDraft
from context_forge.precipitation_migration import PrecipitationMigration


def add(store, memory_id, body):
    return store.put(memory_id, MemoryDraft(body=body, status="active"), expected_version=0,
                     idempotency_key=memory_id, reason="test")


def test_dry_run_does_not_change_database_and_apply_is_idempotent(tmp_path: Path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    add(store, "keep", "same exact assertion")
    add(store, "duplicate", "same exact assertion")
    add(store, "similar", "same exact assertion in one case")
    before = store.path.read_bytes()
    migration = PrecipitationMigration(store)
    report = migration.plan()
    assert store.path.read_bytes() == before
    assert report.exact_groups == (("duplicate", "keep"),)
    applied = migration.apply(batch_id="batch-1")
    repeated = migration.apply(batch_id="batch-1")
    assert applied.archived == repeated.archived == ("keep",)
    assert store.get("keep").draft.status == "archived"
    assert store.get("similar").draft.status == "active"
    assert store.get("duplicate").id == "duplicate"


def test_batch_rollback_restores_effective_state_without_rewriting_id(tmp_path: Path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    add(store, "a", "same")
    add(store, "b", "same")
    migration = PrecipitationMigration(store)
    applied = migration.apply(batch_id="batch")
    archived = applied.archived[0]
    archived_version = store.get(archived).version
    assert migration.rollback("batch") == (archived,)
    restored = store.get(archived)
    assert restored.id == archived and restored.draft.status == "active"
    assert restored.version == archived_version + 1
