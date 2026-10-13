from __future__ import annotations

import pytest

from context_forge.embedding import EmbeddingError, LocalEmbedder, unpack_vector
from context_forge.embedding_eval import evaluate
from semantic_eval_cases import CASES


def test_corpus_is_bilingual_and_lexically_distant():
    assert len(CASES) >= 4
    assert any(any("数据库" in text for text in (case.query, case.positive)) for case in CASES)
    metrics = evaluate(CASES)
    assert metrics.cases == len(CASES)
    assert metrics.lexical_recall < 1.0


def test_local_model_evaluation_is_optional_and_offline():
    embedder = LocalEmbedder()
    try:
        embed = lambda text: unpack_vector(embedder.encode(text)) or ()
        metrics = evaluate(CASES, embed=embed)
    except (EmbeddingError, OSError) as exc:
        pytest.skip(f"local embedding model unavailable: {exc}")
    assert metrics.semantic_hits is not None
    assert metrics.semantic_recall is not None

