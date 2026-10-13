from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterator, Sequence

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
    # Set when the text is a high-risk rule nobody has approved: the payload carries a handle
    # instead of the instruction, so "not approved" is enforced here rather than requested.
    confirmation: dict | None = None

    def as_dict(self) -> dict:
        if self.confirmation is not None:
            return self.confirmation
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


@dataclass(frozen=True)
class MemoryFamily:
    representative: tuple[MemoryRecord, ScoreBreakdown]
    members: tuple[tuple[MemoryRecord, ScoreBreakdown], ...]


@dataclass(frozen=True)
class RecallResponse(Sequence[RecallResult]):
    results: tuple[RecallResult, ...]
    candidate_truncated: bool = False
    families: tuple[tuple[str, ...], ...] = ()

    def __iter__(self) -> Iterator[RecallResult]:
        return iter(self.results)

    def __len__(self) -> int:
        return len(self.results)

    def __getitem__(self, index):
        return self.results[index]

    def __eq__(self, other):
        if isinstance(other, RecallResponse):
            return (self.results, self.candidate_truncated, self.families) == (
                other.results, other.candidate_truncated, other.families)
        return self.results == other


def _tokens(text: str) -> set[str]:
    from .memory import tokenize
    return set(tokenize(text))


def group_for_presentation(scored, threshold: float = 0.90) -> tuple[MemoryFamily, ...]:
    """Anchor-based (non-transitive) near-duplicate presentation grouping."""
    families: list[list] = []
    for item in scored:
        record = item[0]
        placed = False
        for family in families:
            anchor = family[0][0]
            if boundary_view(record.draft) != boundary_view(anchor.draft):
                continue
            left, right = _tokens(record.draft.body), _tokens(anchor.draft.body)
            similarity = len(left & right) / max(1, len(left | right))
            if similarity >= threshold:
                family.append(item)
                placed = True
                break
        if not placed:
            families.append([item])
    return tuple(MemoryFamily(group[0], tuple(group)) for group in families)


class MemoryRecall:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def search(self, query: str, *, project: str | None, changed_path: str | None = None,
               limit: int | None = 8, byte_budget: int = 65536,
               request_id: str | None = None, session_id: str | None = None,
               stage: str = 'retrieved', technologies: frozenset[str] = frozenset(),
               domains: frozenset[str] = frozenset(), legacy_filter: bool = False
               ) -> RecallResponse:
        """Scoped recall units with a four-part explanation.

        `legacy_filter=True` is the rollback hatch: it reverts candidate-pool
        admission, five-dimension scoring and relevance top-K. It does NOT
        revert the boundary trio, `project` no longer being a default filter,
        provenance retention, or the tri-state exclusion semantics.
        """
        ctx = RetrievalContext(query=query, project=project, changed_path=changed_path,
                               technologies=frozenset(technologies), domains=frozenset(domains))
        pool = self.store.search_candidates(
            query, project=project, changed_path=changed_path, candidate_limit=100,
            technologies=ctx.technologies, domains=ctx.domains,
            legacy_filter=legacy_filter)
        families = group_for_presentation(pool.items)
        selected = [item for family in families for item in family.members]
        if limit is not None:
            selected = selected[:limit]
        scored = self.store._fit(selected, byte_budget)
        expanded = self._expanded([record.id for record, _ in scored], ctx)
        results = tuple(self._result(record, score, expanded, request_id=request_id,
                                     session_id=session_id) for record, score in scored)
        if request_id:
            for result in results:
                try:
                    self.store.record_retrieval(
                        result.memory.id, result.memory.version, request_id=request_id, stage=stage,
                        session_id=session_id,
                        idempotency_key=f'{request_id}:{stage}:{result.memory.id}:{result.memory.version}',
                        explanation={'query': query, 'project': project, 'changed_path': changed_path,
                                     'channel': result.score.channel},
                    )
                except MemoryStoreError:
                    pass
        return RecallResponse(results, pool.truncated,
                              tuple(tuple(item[0].id for item in family.members)
                                    for family in families))

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
                expanded: tuple[str, ...] = (), *, request_id: str | None = None,
                session_id: str | None = None) -> RecallResult:
        relations = tuple(self.store.relations(record.id, record.version))
        if score is None:
            score = relevance(record, RetrievalContext())
        return RecallResult(record, relations, score.explanation, score, expanded,
                            self._withheld(record, request_id=request_id, session_id=session_id))

    def _withheld(self, record: MemoryRecord, *, request_id: str | None = None,
                  session_id: str | None = None) -> dict | None:
        """What a host gets instead of a rule text no human has read yet.

        Deliberately no `applicability`, `sources` or explanation here: those fields restate the
        instruction in pieces, and half a destructive command is still a lead to follow. `body`
        is present but null so a host reads "withheld on purpose", not "missing by accident".
        """
        if not self.store.needs_confirmation(record):
            return None
        draft = record.draft
        opened = self.store.open_confirmation(
            record.id, record.version, reason='high-risk rule text withheld until approved',
            request_id=request_id, session_id=session_id)
        return {
            'id': record.id, 'version': record.version, 'kind': draft.kind, 'role': draft.role,
            'status': draft.status, 'evidence_level': draft.evidence_level,
            'summary': draft.summary, 'body': None,
            'confirmation_required': True, 'token': opened['token'],
            'expires_at': opened['expires_at'],
            'note': ('高危规则，正文已扣留：这条规则还没有人在终端里批准过。'
                     '请让人运行 `forge approve` 查看并批准这个 handle，之后再调用 '
                     'context_forge_memory_confirm(token) 取回正文。'
                     '不要自行批准、不要改写库、不要按摘要猜测正文。'),
        }
