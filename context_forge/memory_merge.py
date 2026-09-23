from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

from .memory import MemoryStore, canonical_json, tokenize
from .memory_models import MemoryRecord, Relation


@dataclass(frozen=True)
class MergeCandidate:
    left_id: str
    right_id: str
    kind: str
    score: float
    reason: str
    automatic: bool = False


@dataclass(frozen=True)
class MergeReport:
    total: int
    exact_duplicates: tuple[MergeCandidate, ...]
    possible_overlaps: tuple[MergeCandidate, ...]
    broad_scopes: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            'total': self.total,
            'exact_duplicates': [item.__dict__ for item in self.exact_duplicates],
            'possible_overlaps': [item.__dict__ for item in self.possible_overlaps],
            'broad_scopes': list(self.broad_scopes),
            'quality': {
                'automatic_candidates': len(self.exact_duplicates),
                'ambiguous_candidates': len(self.possible_overlaps),
                'broad_scope_observations': len(self.broad_scopes),
            },
            'empty': not self.exact_duplicates and not self.possible_overlaps and not self.broad_scopes,
        }


def normalized_text(record: MemoryRecord) -> str:
    return ' '.join(record.draft.body.casefold().split())


def token_similarity(left_text: str, right_text: str) -> float:
    """Jaccard token overlap; a review signal, never proof of equivalence."""
    left_tokens = set(tokenize(left_text))
    right_tokens = set(tokenize(right_text))
    union = left_tokens | right_tokens
    return len(left_tokens & right_tokens) / len(union) if union else 0.0


def _normalized(record: MemoryRecord) -> str:
    return normalized_text(record)


def _boundary(record: MemoryRecord) -> str:
    return canonical_json({
        'kind': record.draft.kind,
        'role': record.draft.role,
        'access': record.draft.access.model_dump(mode='json'),
        'provenance': record.draft.provenance.model_dump(mode='json'),
        'scope': record.draft.scope.model_dump(mode='json'),
        'applicability': [condition.model_dump(mode='json') for condition in record.draft.applicability],
    })


def _similarity(left: MemoryRecord, right: MemoryRecord) -> float:
    return token_similarity(_normalized(left), _normalized(right))


def _applicability_conflict(left: MemoryRecord, right: MemoryRecord) -> bool:
    """Decidable contradiction between two highly similar memories.

    Either they decide the same condition phrase oppositely, or one names as a
    counterexample what the other claims applies. Sufficient to refuse a merge:
    high text similarity is not evidence of agreement.
    """
    left_decisions = {condition.condition: condition.result for condition in left.draft.applicability}
    for condition in right.draft.applicability:
        prior = left_decisions.get(condition.condition)
        if (prior in ('true', 'false') and condition.result in ('true', 'false')
                and prior != condition.result):
            return True
    excluded = {entry.casefold() for record in (left, right) for entry in record.draft.scope.exclusions}
    claimed = {entry.casefold() for record in (left, right)
               for entry in (*record.draft.scope.technologies, *record.draft.scope.domains)}
    return bool(excluded & claimed)


class MemoryMerge:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def report(self, *, similarity_threshold: float = 0.6) -> MergeReport:
        if not 0 <= similarity_threshold <= 1:
            raise ValueError('similarity threshold must be between zero and one')
        records = [record for record in self.store.current_records()
                   if record.draft.status not in {'archived', 'disabled'} and not record.draft.suppressed]
        exact = []
        overlaps = []
        for left, right in combinations(records, 2):
            if left.draft.kind != right.draft.kind:
                continue
            same_text = _normalized(left) == _normalized(right)
            same_boundary = _boundary(left) == _boundary(right)
            if same_text and same_boundary:
                exact.append(MergeCandidate(left.id, right.id, 'duplicate_of', 1.0,
                                            'normalized content and decision boundary are identical', True))
                continue
            if left.draft.kind == 'rule' and not same_boundary:
                continue
            score = _similarity(left, right)
            if score >= similarity_threshold:
                overlaps.append(MergeCandidate(left.id, right.id, 'overlaps', round(score, 6),
                                               'token similarity is a review candidate, not proof'))
        broad = tuple(record.id for record in records if record.draft.scope.visibility == 'global')
        return MergeReport(len(records), tuple(exact), tuple(overlaps), broad)

    def merge_candidates(self, threshold: float = 0.9) -> tuple[MergeCandidate, ...]:
        """High-similarity pairs split into merge and conflict proposals.

        Boundary-identical pairs carry `automatic=True` and are the only ones
        `apply_exact` accepts. A contradictory applicability is labelled
        `conflicts_with` and is never merged.
        """
        if not 0 <= threshold <= 1:
            raise ValueError('similarity threshold must be between zero and one')
        records = [record for record in self.store.current_records()
                   if record.draft.status not in {'archived', 'disabled'} and not record.draft.suppressed]
        proposals = []
        for left, right in combinations(records, 2):
            if left.draft.kind != right.draft.kind:
                continue
            score = _similarity(left, right)
            if score < threshold:
                continue
            if _applicability_conflict(left, right):
                proposals.append(MergeCandidate(
                    left.id, right.id, 'conflicts_with', round(score, 6),
                    'high similarity with contradictory applicability; do not merge', False))
            elif _normalized(left) == _normalized(right) and _boundary(left) == _boundary(right):
                proposals.append(MergeCandidate(
                    left.id, right.id, 'duplicate_of', round(score, 6),
                    'normalized content and decision boundary are identical', True))
            else:
                proposals.append(MergeCandidate(
                    left.id, right.id, 'overlaps', round(score, 6),
                    'boundary-compatible overlap; review before merging', False))
        return tuple(proposals)

    def apply_conflict(self, candidate: MergeCandidate, *, idempotency_key: str):
        """Annotate a contradictory pair with a `conflicts_with` relation.

        Neither side is rewritten or hidden; the relation is revocable.
        """
        if candidate.kind != 'conflicts_with':
            raise ValueError('only conflicts_with candidates may be applied here')
        left = self.store.get(candidate.left_id)
        right = self.store.get(candidate.right_id)
        if left is None or right is None:
            raise KeyError((candidate.left_id, candidate.right_id))
        relation = Relation(target_id=right.id, target_version=right.version, kind='conflicts_with')
        return self.store.put(
            left.id, left.draft, expected_version=left.version,
            idempotency_key=idempotency_key, actor='system',
            reason='high similarity with contradictory applicability', relations=(relation,))

    def apply_exact(self, candidate: MergeCandidate, *, idempotency_key: str):
        if not candidate.automatic or candidate.kind != 'duplicate_of':
            raise ValueError('only exact duplicate candidates may be applied automatically')
        canonical_id, duplicate_id = sorted((candidate.left_id, candidate.right_id))
        canonical = self.store.get(canonical_id)
        duplicate = self.store.get(duplicate_id)
        if canonical is None or duplicate is None:
            raise KeyError((canonical_id, duplicate_id))
        if _normalized(canonical) != _normalized(duplicate) or _boundary(canonical) != _boundary(duplicate):
            raise ValueError('candidate is stale or no longer exact')
        if duplicate.draft.status == 'archived' and any(
            relation['kind'] == 'duplicate_of' and relation['target_id'] == canonical.id
            for relation in self.store.relations(duplicate.id)
        ):
            return duplicate
        if duplicate.draft.user_owned or duplicate.draft.suppressed or duplicate.draft.status == 'disabled':
            return duplicate
        retained = self._outgoing_relations(duplicate)
        retained.append(Relation(target_id=canonical.id, target_version=canonical.version, kind='duplicate_of'))
        archived = duplicate.draft.model_copy(update={'status': 'archived'})
        return self.store.put(duplicate.id, archived, expected_version=duplicate.version,
                              idempotency_key=idempotency_key, actor='system',
                              reason='exact duplicate canonicalization', relations=tuple(retained))

    def undo_exact(self, memory_id: str, *, expected_version: int, relation_id: str,
                   idempotency_key: str):
        record = self.store.get(memory_id)
        if record is None:
            raise KeyError(memory_id)
        restored = record.draft.model_copy(update={'status': 'active'})
        result = self.store.put(memory_id, restored, expected_version=expected_version,
                                idempotency_key=idempotency_key, actor='human',
                                reason='undo duplicate canonicalization')
        self.store.revoke_relation(relation_id, expected_version=result.version,
                                   idempotency_key=idempotency_key + ':relation',
                                   reason='undo duplicate canonicalization', actor='human')
        return result

    def _outgoing_relations(self, record: MemoryRecord) -> list[Relation]:
        relations = []
        for item in self.store.relations(record.id, record.version):
            if item['memory_id'] != record.id or item['version'] != record.version:
                continue
            relations.append(Relation(target_id=item['target_id'], target_version=item['target_version'],
                                      kind=item['kind'], evidence_ids=tuple(item['evidence_ids']),
                                      applicability=tuple(item['applicability'])))
        return relations
