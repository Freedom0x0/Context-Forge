"""Model-agnostic retrieval evaluation helpers.

The helpers are deliberately independent of ``MemoryStore`` so they can be
used before a provider is enabled and in CI without downloading a model.
"""
from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass


TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)


@dataclass(frozen=True)
class RetrievalMetric:
    cases: int
    lexical_hits: int
    semantic_hits: int | None

    @property
    def lexical_recall(self) -> float:
        return self.lexical_hits / self.cases if self.cases else 0.0

    @property
    def semantic_recall(self) -> float | None:
        if self.semantic_hits is None:
            return None
        return self.semantic_hits / self.cases if self.cases else 0.0


def lexical_similarity(left: str, right: str) -> float:
    a, b = set(TOKEN_RE.findall(left.lower())), set(TOKEN_RE.findall(right.lower()))
    return len(a & b) / math.sqrt(len(a) * len(b)) if a and b else 0.0


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    norm = math.sqrt(sum(a * a for a in left) * sum(b * b for b in right))
    return dot / norm if norm else 0.0


def evaluate(cases, *, embed: Callable[[str], Sequence[float]] | None = None) -> RetrievalMetric:
    lexical_hits = semantic_hits = 0
    for case in cases:
        candidates = (case.positive,) + tuple(case.distractors)
        if max(range(len(candidates)), key=lambda i: lexical_similarity(case.query, candidates[i])) == 0:
            lexical_hits += 1
        if embed is not None:
            query_vector = embed(case.query)
            vectors = [embed(candidate) for candidate in candidates]
            if max(range(len(vectors)), key=lambda i: cosine(query_vector, vectors[i])) == 0:
                semantic_hits += 1
    return RetrievalMetric(len(cases), lexical_hits, semantic_hits if embed is not None else None)

