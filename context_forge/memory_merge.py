from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Iterator, NamedTuple, Sequence

from .memory import MemoryStore, canonical_fingerprint, canonical_json, folded_body, tokenize
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
    return folded_body(record.draft.body)


def token_similarity(left_text: str, right_text: str) -> float:
    """Jaccard token overlap; a review signal, never proof of equivalence."""
    left_tokens = set(tokenize(left_text))
    right_tokens = set(tokenize(right_text))
    union = left_tokens | right_tokens
    return len(left_tokens & right_tokens) / len(union) if union else 0.0


def _boundary(record: MemoryRecord) -> str:
    return canonical_json({
        'kind': record.draft.kind,
        'role': record.draft.role,
        'access': record.draft.access.model_dump(mode='json'),
        'provenance': record.draft.provenance.model_dump(mode='json'),
        'scope': record.draft.scope.model_dump(mode='json'),
        'applicability': [condition.model_dump(mode='json') for condition in record.draft.applicability],
    })


def _jaccard(left: frozenset, right: frozenset) -> float:
    """The same Jaccard ratio, from tokens already split once per record."""
    union = left | right
    return len(left & right) / len(union) if union else 0.0


class _Profile(NamedTuple):
    """One record measured once. Recomputing these per pair was the merge report's cost."""

    position: int
    record: MemoryRecord
    kind: str
    text: str
    boundary: str
    tokens: frozenset
    fingerprint: str
    provenance: str


def _profiles(records: Sequence[MemoryRecord]) -> list[_Profile]:
    profiles = []
    for position, record in enumerate(records):
        text = normalized_text(record)
        profiles.append(_Profile(position, record, record.draft.kind, text,
                                 _boundary(record), frozenset(tokenize(text)),
                                 canonical_fingerprint(record.draft),
                                 canonical_json(record.draft.provenance.model_dump(mode='json'))))
    return profiles


def _duplicate_candidate(left: _Profile, right: _Profile, score: float) -> MergeCandidate:
    """One fingerprint means one assertion inside one boundary.

    Provenance is outside the fingerprint on purpose (the write gate treats a rule learned in
    two projects as one memory), but archiving the loser of such a pair would remove the other
    project from recall — so only provenance-identical pairs may be canonicalized automatically.
    """
    same_source = left.provenance == right.provenance
    return MergeCandidate(
        left.record.id, right.record.id, 'duplicate_of', score,
        'same fingerprint' + (' and same provenance' if same_source else
                              '; provenance differs, so merging would drop a source'),
        same_source)


def _group(profiles: Sequence[_Profile], key) -> dict:
    """Bucket records by a precomputed key so pairs only form inside buckets."""
    grouped: dict = {}
    for item in profiles:
        grouped.setdefault(key(item), []).append(item)
    return grouped


def _pairs_within(groups: Sequence[Sequence[_Profile]]) -> Iterator[tuple[_Profile, _Profile]]:
    for members in groups:
        yield from combinations(members, 2)


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
        profiles = _profiles(self._live_records())
        exact: list[tuple[int, int, MergeCandidate]] = []
        matched: set[tuple[int, int]] = set()
        for members in _group(profiles, lambda item: item.fingerprint).values():
            for left, right in combinations(members, 2):
                matched.add((left.position, right.position))
                exact.append((left.position, right.position, _duplicate_candidate(left, right, 1.0)))
        overlaps: list[tuple[int, int, MergeCandidate]] = []
        for kind, members in _group(profiles, lambda item: item.kind).items():
            # Rules were never compared across differing boundaries; keep that by grouping on it.
            groups = list(_group(members, lambda item: item.boundary).values()) \
                if kind == 'rule' else [members]
            for left, right in _pairs_within(groups):
                if (left.position, right.position) in matched:
                    continue
                score = _jaccard(left.tokens, right.tokens)
                if score >= similarity_threshold:
                    overlaps.append((left.position, right.position, MergeCandidate(
                        left.record.id, right.record.id, 'overlaps', round(score, 6),
                        'token similarity is a review candidate, not proof')))
        exact.sort(key=lambda item: item[:2])
        overlaps.sort(key=lambda item: item[:2])
        broad = tuple(item.record.id for item in profiles
                      if item.record.draft.scope.visibility == 'global')
        return MergeReport(len(profiles), tuple(pair[2] for pair in exact),
                           tuple(pair[2] for pair in overlaps), broad)

    def merge_candidates(self, threshold: float = 0.9) -> tuple[MergeCandidate, ...]:
        """High-similarity pairs split into merge and conflict proposals.

        Fingerprint-identical pairs sharing provenance carry `automatic=True` and
        are the only ones `apply_exact` accepts. A contradictory applicability is
        labelled `conflicts_with` and is never merged.
        """
        if not 0 <= threshold <= 1:
            raise ValueError('similarity threshold must be between zero and one')
        profiles = _profiles(self._live_records())
        proposals: list[tuple[int, int, MergeCandidate]] = []
        for members in _group(profiles, lambda item: item.kind).values():
            for left, right in combinations(members, 2):
                score = _jaccard(left.tokens, right.tokens)
                if score < threshold:
                    continue
                if _applicability_conflict(left.record, right.record):
                    proposals.append((left.position, right.position, MergeCandidate(
                        left.record.id, right.record.id, 'conflicts_with', round(score, 6),
                        'high similarity with contradictory applicability; do not merge', False)))
                elif left.fingerprint == right.fingerprint:
                    proposals.append((left.position, right.position,
                                      _duplicate_candidate(left, right, round(score, 6))))
                else:
                    proposals.append((left.position, right.position, MergeCandidate(
                        left.record.id, right.record.id, 'overlaps', round(score, 6),
                        'boundary-compatible overlap; review before merging', False)))
        proposals.sort(key=lambda item: item[:2])
        return tuple(pair[2] for pair in proposals)

    def _live_records(self) -> list[MemoryRecord]:
        return [record for record in self.store.current_records()
                if record.draft.status not in {'archived', 'disabled'} and not record.draft.suppressed]

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
        if canonical_fingerprint(canonical.draft) != canonical_fingerprint(duplicate.draft):
            raise ValueError('candidate is stale or no longer exact')
        if canonical.draft.provenance != duplicate.draft.provenance:
            raise ValueError('same fingerprint, different provenance: a human must choose the source')
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
