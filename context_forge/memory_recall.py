from __future__ import annotations

from dataclasses import dataclass

from .memory import MemoryStore, MemoryStoreError
from .memory_models import MemoryRecord


@dataclass(frozen=True)
class RecallResult:
    memory: MemoryRecord
    relations: tuple[dict, ...]
    explanation: str

    def as_dict(self) -> dict:
        draft = self.memory.draft
        return {
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
            'relations': list(self.relations),
            'explanation': self.explanation,
        }


class MemoryRecall:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def search(self, query: str, *, project: str | None, changed_path: str | None = None,
               limit: int | None = 8, byte_budget: int = 65536,
               request_id: str | None = None, session_id: str | None = None,
               stage: str = 'retrieved') -> tuple[RecallResult, ...]:
        records = self.store.search(query, project=project, changed_path=changed_path,
                                    limit=limit, byte_budget=byte_budget)
        results = tuple(self._result(record) for record in records)
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

    def _result(self, record: MemoryRecord) -> RecallResult:
        relations = tuple(self.store.relations(record.id, record.version))
        limits = []
        unknown = [condition.condition for condition in record.draft.applicability if condition.result == 'unknown']
        false = [condition.condition for condition in record.draft.applicability if condition.result == 'false']
        if unknown:
            limits.append('unresolved conditions: ' + '; '.join(unknown))
        if false:
            limits.append('known exceptions: ' + '; '.join(false))
        if any(relation['kind'] in {'conflicts_with', 'contradicts', 'exception_to'} for relation in relations):
            limits.append('related conflict or exception requires evaluation')
        explanation = '; '.join(limits) or 'matched current scoped memory'
        return RecallResult(record, relations, explanation)
