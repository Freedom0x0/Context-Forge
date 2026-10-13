from array import array
import hashlib
import math
from pathlib import Path
import re
import sqlite3

import pytest

from context_forge.embedding import DIMENSIONS, EmbeddingError, LocalEmbedder, model_identity, pack_vector, unpack_vector
from context_forge.memory import MemoryStore
from context_forge.memory_models import MemoryDraft, Provenance


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


# --- hybrid recall: the semantic channel wired into the real recall path ----------------

class HashingEmbedder:
    """Content-dependent fake: cosine tracks shared words.

    A shared anchor component keeps every pair above zero cosine — a real embedding is
    never exactly orthogonal, and a zero semantic score would make a "semantic-only" hit
    indistinguishable from "not found".
    """

    dimensions = DIMENSIONS

    def __init__(self, identity=None):
        self.identity = identity or model_identity()

    def encode(self, text):
        vector = [0.0] * self.dimensions
        vector[0] = 1.0
        for token in text.lower().split():
            digest = hashlib.sha1(token.encode('utf-8')).digest()
            vector[1 + int.from_bytes(digest[:4], 'big') % (self.dimensions - 1)] += 1.0
        norm = math.sqrt(sum(value * value for value in vector))
        return pack_vector([value / norm for value in vector])


def _recall(store, body, memory_id, *, project='demo'):
    store.put(memory_id, MemoryDraft(
        body=body, status='active',
        provenance=Provenance(projects=(project,), sessions=(f's-{memory_id}',))),
        expected_version=0, idempotency_key=f'put:{memory_id}', reason='test')


def _match_scores(explanation):
    lexical = re.search(r'词法=([0-9.]+)', explanation)
    semantic = re.search(r'语义=([0-9.]+)', explanation)
    return (float(lexical.group(1)) if lexical else 0.0,
            float(semantic.group(1)) if semantic else 0.0)


def test_semantic_only_hit_enters_results_labelled_with_its_cosine(tmp_path):
    store = MemoryStore.initialize(tmp_path / 'memory.db')
    store.embedder = HashingEmbedder()
    # No shared token with the query, so lexical score is 0: only the semantic channel reaches it.
    _recall(store, 'quarterly revenue forecast', 'sem')

    found = {record.id: breakdown for record, breakdown in
             store.search_scored('knitting pattern', project='demo')}

    assert 'sem' in found
    assert found['sem'].text > 0.0
    assert '通道=semantic' in found['sem'].explanation
    assert '语义=' in found['sem'].explanation


def test_embedding_from_another_model_identity_is_skipped(tmp_path):
    store = MemoryStore.initialize(tmp_path / 'memory.db')
    store.embedder = HashingEmbedder('sentence-transformers:drifted-model:384')
    _recall(store, 'quarterly revenue forecast', 'drifted')
    # A different configured model: the stored vector is not comparable, so it must be ignored.
    store.embedder = HashingEmbedder(model_identity())

    found = {record.id for record, _ in store.search_scored('knitting pattern', project='demo')}

    assert 'drifted' not in found


def test_text_dimension_is_the_max_of_lexical_and_semantic(tmp_path):
    store = MemoryStore.initialize(tmp_path / 'memory.db')
    store.embedder = HashingEmbedder()
    # doc-a is the strongest lexical match (lex > sem); doc-b is a weak lexical hit whose
    # long body drags its normalised lexical score to 0, so only the cosine keeps it up
    # (sem > lex). One record on each side pins both directions of max().
    _recall(store, 'proxy timeout policy alpha', 'doc-a')
    _recall(store, 'proxy timeout policy beta gamma delta epsilon zeta eta theta', 'doc-b')

    results = store.search_scored('proxy timeout policy', project='demo')

    assert len(results) == 2
    sides = set()
    for record, breakdown in results:
        lexical, semantic = _match_scores(breakdown.explanation)
        assert breakdown.text == pytest.approx(max(lexical, semantic), abs=0.01)
        if lexical > semantic + 0.01:
            sides.add('lexical')
        if semantic > lexical + 0.01:
            sides.add('semantic')
    assert sides == {'lexical', 'semantic'}    # both directions actually exercised


def test_doctor_reports_embedding_write_failures(tmp_path, monkeypatch):
    monkeypatch.setenv('CONTEXT_FORGE_HOME', str(tmp_path))
    from context_forge import doctor

    store = MemoryStore.initialize(tmp_path / 'memory.db')
    assert doctor.check_embedding_failures()[1:] == (True, 'no embedding write failures')

    store.embedder = FakeEmbedder(fail=True)
    store.put('m', MemoryDraft(body='hello'), expected_version=0,
              idempotency_key='put', reason='test')

    name, ok, detail = doctor.check_embedding_failures()
    assert name == 'embedding-failures' and ok is False
    assert 'offline' in detail and 'x1' in detail
    assert MemoryStore(tmp_path / 'memory.db', readonly=True).embedding_failures()


def test_v7_migration_backup_restore_returns_records(tmp_path):
    path = tmp_path / 'memory.db'
    store = MemoryStore.initialize(path)
    store.put('m', MemoryDraft(body='SQLite 事务回滚', status='active',
                               provenance=Provenance(projects=('demo',))),
              expected_version=0, idempotency_key='put', reason='test')
    with sqlite3.connect(path) as connection:
        connection.execute('PRAGMA user_version=7')

    report = MemoryStore.migrate(path, dry_run=False)
    restored = MemoryStore.restore(Path(report['backup']), tmp_path / 'restored' / 'memory.db')

    assert restored.get('m').draft.body == 'SQLite 事务回滚'
    assert restored.get('m').draft.provenance.projects == ('demo',)
