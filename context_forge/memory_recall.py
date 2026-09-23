from __future__ import annotations

from dataclasses import dataclass

from .memory import (
    DEFAULT_OWNER, MemoryStore, MemoryStoreError, RetrievalContext, ScoreBreakdown,
    boundary_view, boundary_visible, relevance,
)
from .memory_models import MemoryRecord


@dataclass(frozen=True)
class RecallResult:
    memory: MemoryRecord
    relations: tuple[dict, ...]
    explanation: str
    score: ScoreBreakdown | None = None
    expanded: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        draft = self.memory.draft
        payload = {
            'id': self.memory.id,
            'version': self.memory.version,
            'kind': draft.kind,
            'role': draft.role,
            'summary': draft.summary,
            'body': draft.body,
            'status': draft.status,
            'evidence_level': draft.evidence_level,
            'applicability': [value.model_dump(mode='json') for value in draft.applicability],
            'sources': [value.model_dump(mode='json') for value in draft.sources],
            'provenance': draft.provenance.model_dump(mode='json'),
            'scope': draft.scope.model_dump(mode='json'),
            'relations': list(self.relations),
            'expanded': list(self.expanded),
            'explanation': self.explanation,
        }
        if self.score is not None:
            payload['score'] = {
                'total': self.score.total, 'match': self.score.match,
                'applicability': self.score.applicability,
                'provenance_bonus': self.score.provenance_bonus,
                'evidence': self.score.evidence, 'freshness': self.score.freshness,
                'feedback': self.score.feedback, 'text': self.score.text,
                'conflict_penalty': self.score.conflict_penalty,
                'admitted_by': self.score.admitted_by,
            }
        return payload


class MemoryRecall:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def search(self, query: str, *, project: str | None, changed_path: str | None = None,
               limit: int | None = 8, byte_budget: int = 65536,
               request_id: str | None = None, session_id: str | None = None,
               stage: str = 'retrieved', technologies: frozenset[str] = frozenset(),
               domains: frozenset[str] = frozenset(), legacy_filter: bool = False
               ) -> tuple[RecallResult, ...]:
        """Scoped recall units with a four-part explanation.

        `legacy_filter=True` is the rollback hatch: it reverts candidate-pool
        admission, five-dimension scoring and relevance top-K. It does NOT
        revert the boundary trio, `project` no longer being a default filter,
        provenance retention, or the tri-state exclusion semantics.
        """
        ctx = RetrievalContext(query=query, project=project, changed_path=changed_path,
                               technologies=frozenset(technologies), domains=frozenset(domains))
        scored = self.store.search_scored(
            query, project=project, changed_path=changed_path, limit=limit,
            byte_budget=byte_budget, technologies=ctx.technologies, domains=ctx.domains,
            legacy_filter=legacy_filter)
        expanded = self._expanded([record.id for record, _ in scored], ctx)
        results = tuple(self._result(record, score, expanded) for record, score in scored)
        if request_id:
            for result in results:
                try:
                    self.store.record_retrieval(
                        result.memory.id, result.memory.version, request_id=request_id, stage=stage,
                        session_id=session_id,
                        idempotency_key=f'{request_id}:{stage}:{result.memory.id}:{result.memory.version}',
                        explanation={'query': query, 'project': project, 'changed_path': changed_path},
                    )
                except MemoryStoreError:
                    pass
        return results

    def get(self, memory_id: str, *, version: int | None = None) -> RecallResult | None:
        record = self.store.get(memory_id, version)
        return self._result(record) if record else None

    def mark(self, result: RecallResult, *, request_id: str, stage: str,
             session_id: str | None = None, explanation: dict | None = None) -> None:
        self.store.record_retrieval(
            result.memory.id, result.memory.version, request_id=request_id, stage=stage,
            session_id=session_id,
            idempotency_key=f'{request_id}:{stage}:{result.memory.id}:{result.memory.version}',
            explanation=explanation,
        )

    def _expanded(self, seed_ids: list[str], ctx: RetrievalContext) -> tuple[str, ...]:
        """One-hop association context, boundary-checked like any recalled detail."""
        neighbours = self.store.expand_relations(seed_ids)
        visible = []
        for memory_id in neighbours:
            record = self.store.get(memory_id)
            if record and boundary_visible(boundary_view(record.draft), owner=DEFAULT_OWNER, ctx=ctx):
                visible.append(memory_id)
        return tuple(visible)

    def _result(self, record: MemoryRecord, score: ScoreBreakdown | None = None,
                expanded: tuple[str, ...] = ()) -> RecallResult:
        relations = tuple(self.store.relations(record.id, record.version))
        if score is None:
            score = relevance(record, RetrievalContext())
        return RecallResult(record, relations, score.explanation, score, expanded)
