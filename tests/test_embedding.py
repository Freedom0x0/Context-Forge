from array import array

import pytest

from context_forge.embedding import DIMENSIONS, EmbeddingError, LocalEmbedder, model_identity, pack_vector, unpack_vector
from context_forge.memory import MemoryStore
from context_forge.memory_models import MemoryDraft


def test_vector_pack_round_trip_and_dimension_guard():
    values = [0.25] * DIMENSIONS
    blob = pack_vector(values)
    assert len(blob) == DIMENSIONS * 4
    assert unpack_vector(blob) == pytest.approx(values)
    assert unpack_vector(blob[:-4]) is None
    with pytest.raises(ValueError):
        pack_vector(values[:-1])


def test_model_identity_is_explicit():
    assert model_identity() == "sentence-transformers:all-MiniLM-L6-v2:384"


def test_missing_optional_provider_is_reported(monkeypatch):
    import builtins
    original = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "sentence_transformers":
            raise ImportError("blocked")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(EmbeddingError, match="not installed"):
        LocalEmbedder().encode("hello")


class FakeEmbedder:
    identity = model_identity()
    dimensions = DIMENSIONS
    def __init__(self, fail=False):
        self.fail = fail
    def encode(self, text):
        if self.fail:
            raise RuntimeError("offline")
        return pack_vector([1.0] + [0.0] * (DIMENSIONS - 1))


def test_memory_write_keeps_going_when_embedding_fails(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    store.embedder = FakeEmbedder(fail=True)
    store.put("m", MemoryDraft(body="hello"), expected_version=0,
              idempotency_key="put", reason="test")
    with store._connection() as connection:
        row = connection.execute("SELECT embedding FROM memory_versions WHERE memory_id='m'").fetchone()
    assert row[0] is None


def test_memory_write_stores_local_embedding(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    store.embedder = FakeEmbedder()
    store.put("m", MemoryDraft(body="hello"), expected_version=0,
              idempotency_key="put", reason="test")
    with store._connection() as connection:
        row = connection.execute("SELECT embedding_model,embedding FROM memory_versions WHERE memory_id='m'").fetchone()
    assert row[0] == model_identity()
    assert unpack_vector(row[1])[0] == pytest.approx(1.0)


def test_reindex_embeddings_updates_cache_without_new_versions(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    store.put("m", MemoryDraft(body="existing memory"), expected_version=0,
              idempotency_key="put", reason="test")
    store.embedder = FakeEmbedder()

    result = store.reindex_embeddings(store.embedder)

    assert result == {"total": 1, "updated": 1, "failed": 0, "stale": 0}
    assert store.get("m").version == 1
    with store._connection() as connection:
        row = connection.execute(
            "SELECT embedding_model,embedding FROM memory_versions WHERE memory_id='m'"
        ).fetchone()
    assert row[0] == model_identity()
    assert unpack_vector(row[1])[0] == pytest.approx(1.0)


def test_reindex_embeddings_counts_provider_failure(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    store.put("m", MemoryDraft(body="existing memory"), expected_version=0,
              idempotency_key="put", reason="test")
    store.embedder = FakeEmbedder(fail=True)

    assert store.reindex_embeddings(store.embedder) == {
        "total": 1, "updated": 0, "failed": 1, "stale": 0}
    assert store.get("m").version == 1
