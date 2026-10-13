from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from math import ceil
from typing import NamedTuple, Sequence

from .memory import MemoryStore, canonical_fingerprint, canonical_json, folded_body, tokenize
from .memory_models import MemoryRecord, Provenance, Relation


# A review report nobody can read is not a report: 600 nearly-identical memories used to emit
# 179,400 overlap pairs. The cap is on what the report returns, not on what it found.
OVERLAP_REPORT_LIMIT = 100


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
    overlaps_found: int
    overlap_limit: int = OVERLAP_REPORT_LIMIT

    def as_dict(self) -> dict:
        return {
            'total': self.total,
            'exact_duplicates': [item.__dict__ for item in self.exact_duplicates],
            'possible_overlaps': [item.__dict__ for item in self.possible_overlaps],
            'broad_scopes': list(self.broad_scopes),
            'quality': {
                'automatic_candidates': len(self.exact_duplicates),
                'ambiguous_candidates': self.overlaps_found,
                'ambiguous_shown': len(self.possible_overlaps),
                'overlap_limit': self.overlap_limit,
                'truncated': self.overlaps_found > len(self.possible_overlaps),
                'broad_scope_observations': len(self.broad_scopes),
            },
            'empty': not self.exact_duplicates and not self.overlaps_found and not self.broad_scopes,
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


def _blocked_pairs(profiles: Sequence[_Profile], threshold: float) -> set[tuple[int, int]]:
    """Which pairs could still reach the threshold, by the prefix filter.

    Tokens are ordered by one global rule (rarest first, ties by token) and each record is
    indexed only under its `len - ceil(theta * len) + 1` rarest tokens. Two records sharing no
    indexed token have Jaccard strictly below the threshold, so nothing that qualifies is lost.
    The claim is checked, not trusted: the pairwise oracle in the tests walks every pair, and
    the seeded fuzz test compares the two over random corpora and thresholds.
    """
    positions = sorted(item.position for item in profiles)
    if threshold <= 0:
        return set(combinations(positions, 2))        # everything qualifies; no pruning is safe
    frequencies: dict[str, int] = {}
    for item in profiles:
        for token in item.tokens:
            frequencies[token] = frequencies.get(token, 0) + 1
    index: dict[str, list[int]] = {}
    for item in profiles:
        size = len(item.tokens)
        if not size:
            continue
        keep = max(size - ceil(threshold * size) + 1, 1)
        rarest = sorted(item.tokens, key=lambda token: (frequencies[token], token))
        for token in rarest[:keep]:
            index.setdefault(token, []).append(item.position)
    pairs: set[tuple[int, int]] = set()
    for members in index.values():
        pairs.update(combinations(sorted(members), 2))
    return pairs


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

    def report(self, *, similarity_threshold: float = 0.6,
               overlap_limit: int = OVERLAP_REPORT_LIMIT) -> MergeReport:
        if not 0 <= similarity_threshold <= 1:
            raise ValueError('similarity threshold must be between zero and one')
        if overlap_limit < 1:
            raise ValueError('overlap limit must be at least one')
        profiles = _profiles(self._live_records())
        by_position = {item.position: item for item in profiles}
        exact: list[tuple[int, int, MergeCandidate]] = []
        matched: set[tuple[int, int]] = set()
        for members in _group(profiles, lambda item: item.fingerprint).values():
            for left, right in combinations(members, 2):
                matched.add((left.position, right.position))
                exact.append((left.position, right.position, _duplicate_candidate(left, right, 1.0)))
        overlaps: list[tuple[int, int, MergeCandidate]] = []
        for first, second in sorted(_blocked_pairs(profiles, similarity_threshold)):
            left, right = by_position[first], by_position[second]
            if left.kind != right.kind or (first, second) in matched:
                continue
            # Rules were never compared across differing boundaries; that still stands.
            if left.kind == 'rule' and left.boundary != right.boundary:
                continue
            score = _jaccard(left.tokens, right.tokens)
            if score >= similarity_threshold:
                overlaps.append((first, second, MergeCandidate(
                    left.record.id, right.record.id, 'overlaps', round(score, 6),
                    'token similarity is a review candidate, not proof')))
        exact.sort(key=lambda item: item[:2])
        broad = tuple(item.record.id for item in profiles
                      if item.record.draft.scope.visibility == 'global')
        return MergeReport(len(profiles), tuple(pair[2] for pair in exact),
                           tuple(pair[2] for pair in overlaps[:overlap_limit]), broad,
                           len(overlaps), overlap_limit)

    def merge_candidates(self, threshold: float = 0.9) -> tuple[MergeCandidate, ...]:
        """High-similarity pairs split into merge and conflict proposals.

        Fingerprint-identical pairs sharing provenance carry `automatic=True` and
        are the only ones `apply_exact` accepts. A contradictory applicability is
        labelled `conflicts_with` and is never merged.
        """
        if not 0 <= threshold <= 1:
            raise ValueError('similarity threshold must be between zero and one')
        profiles = _profiles(self._live_records())
        by_position = {item.position: item for item in profiles}
        proposals: list[tuple[int, int, MergeCandidate]] = []
        for first, second in sorted(_blocked_pairs(profiles, threshold)):
            left, right = by_position[first], by_position[second]
            if left.kind != right.kind:
                continue
            score = _jaccard(left.tokens, right.tokens)
            if score < threshold:
                continue
            if _applicability_conflict(left.record, right.record):
                proposals.append((first, second, MergeCandidate(
                    left.record.id, right.record.id, 'conflicts_with', round(score, 6),
                    'high similarity with contradictory applicability; do not merge', False)))
                continue
            if left.fingerprint == right.fingerprint:
                proposals.append((first, second,
                                  _duplicate_candidate(left, right, round(score, 6))))
            else:
                proposals.append((first, second, MergeCandidate(
                    left.record.id, right.record.id, 'overlaps', round(score, 6),
                    'boundary-compatible overlap; review before merging', False)))
        return tuple(pair[2] for pair in proposals)

    def _live_records(self) -> list[MemoryRecord]:
        return [record for record in self.store.current_records()
                if record.draft.status not in {'archived', 'disabled'} and not record.draft.suppressed]

    def merge_pair(self, candidate: MergeCandidate, *, idempotency_key: str):
        """A human's override of the automatic gate: make one memory of the pair.

        `apply_exact` refuses anything that is not fingerprint-identical or whose provenance
        differs, because an unattended pass must not decide that. A human may. When they do,
        the loser's projects, sessions and sources move onto the anchor first — archiving the
        loser without that union would drop those projects from recall, which is the exact
        thing that made the pair ineligible for automatic canonicalization.
        """
        anchor_id, loser_id = sorted((candidate.left_id, candidate.right_id))
        anchor = self.store.get(anchor_id)
        loser = self.store.get(loser_id)
        if anchor is None or loser is None:
            raise KeyError((anchor_id, loser_id))
        if loser.draft.user_owned or loser.draft.suppressed or loser.draft.status == 'disabled':
            return loser
        if loser.draft.status == 'archived' and any(
            relation['kind'] == 'duplicate_of' and relation['target_id'] == anchor.id
            for relation in self.store.relations(loser.id)
        ):
            return loser        # this pair was already merged; a second pass must not mint a version
        sources = {source.identity: source for source in anchor.draft.sources}
        sources.update({source.identity: source for source in loser.draft.sources})
        unified = anchor.draft.model_copy(update={
            'sources': tuple(sources.values()),
            'provenance': Provenance(
                projects=tuple(sorted(set(anchor.draft.provenance.projects)
                                      | set(loser.draft.provenance.projects))),
                sessions=tuple(sorted(set(anchor.draft.provenance.sessions)
                                      | set(loser.draft.provenance.sessions))),
                paths=tuple(sorted(set(anchor.draft.provenance.paths)
                                   | set(loser.draft.provenance.paths))),
                host=anchor.draft.provenance.host),
        })
        # `put` replaces the version's relation set, so the anchor's own notes are carried over
        # explicitly — dropping them silently would turn one merge into two losses.
        merged_anchor = self.store.put(
            anchor_id, unified, expected_version=anchor.version,
            idempotency_key=f'{idempotency_key}:anchor', actor='human',
            reason='human merge: provenance and sources unioned onto the anchor',
            relations=tuple(self._outgoing_relations(anchor)))
        retained = self._outgoing_relations(loser)
        retained.append(Relation(target_id=merged_anchor.id,
                                 target_version=merged_anchor.version, kind='duplicate_of'))
        archived = loser.draft.model_copy(update={'status': 'archived'})
        return self.store.put(loser_id, archived, expected_version=loser.version,
                              idempotency_key=idempotency_key, actor='human',
                              reason='human merge: duplicate archived', relations=tuple(retained))

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
        if (duplicate.draft.user_owned or duplicate.draft.suppressed
                or duplicate.draft.status in {'disabled', 'conflicted'}):
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
        # A `put` replaces the version's relation set, so every note the archived row carried
        # has to be carried forward — except the duplicate edge this undo is revoking.
        result = self.store.put(memory_id, restored, expected_version=expected_version,
                                idempotency_key=idempotency_key, actor='human',
                                reason='undo duplicate canonicalization',
                                relations=tuple(self._outgoing_relations(record,
                                                                         skip_relation_id=relation_id)))
        self.store.revoke_relation(relation_id, expected_version=result.version,
                                   idempotency_key=idempotency_key + ':relation',
                                   reason='undo duplicate canonicalization', actor='human')
        return result

    def _outgoing_relations(self, record: MemoryRecord,
                            skip_relation_id: str | None = None) -> list[Relation]:
        relations = []
        for item in self.store.relations(record.id, record.version):
            if item['memory_id'] != record.id or item['version'] != record.version:
                continue
            if skip_relation_id is not None and item['id'] == skip_relation_id:
                continue
            relations.append(Relation(target_id=item['target_id'], target_version=item['target_version'],
                                      kind=item['kind'], evidence_ids=tuple(item['evidence_ids']),
                                      applicability=tuple(item['applicability'])))
        return relations
