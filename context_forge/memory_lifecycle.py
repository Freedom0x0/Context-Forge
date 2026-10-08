from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Iterable

from .domain import SessionArtifacts
from .memory import MemoryStore, ProtectedMemory, VersionConflict, now
from .memory_effectiveness import MemoryEffectiveness
from .memory_merge import MemoryMerge, MergeCandidate, normalized_text, token_similarity
from .memory_models import (
    AccessPolicy, Applicability, ApplicabilityScope, MemoryDraft, MemoryRecord,
    MemorySpace, Provenance, Relation, Source,
)


CONSOLIDATION_THRESHOLD = 0.6


@dataclass(frozen=True)
class Candidate:
    memory_id: str
    draft: MemoryDraft
    relations: tuple[Relation, ...] = ()


@dataclass(frozen=True)
class ConsolidationProposal:
    """A new higher-level memory abstracted from independently-sourced episodes."""

    memory_id: str
    source_ids: tuple[str, ...]
    draft: MemoryDraft
    relations: tuple[Relation, ...]
    reason: str


@dataclass(frozen=True)
class HygieneReport:
    """Batch review items; nothing here mutates or interrupts per record."""

    consolidation: tuple[ConsolidationProposal, ...]
    decay: tuple[MemoryRecord, ...]
    merges: tuple[MergeCandidate, ...]
    conflicts: tuple[MergeCandidate, ...]

    def as_dict(self) -> dict:
        return {
            'consolidation': [{
                'memory_id': item.memory_id,
                'source_ids': list(item.source_ids),
                'role': item.draft.role,
                'projects': list(item.draft.provenance.projects),
                'reason': item.reason,
            } for item in self.consolidation],
            'decay': [_record_view(item) for item in self.decay],
            'merges': [item.__dict__ for item in self.merges],
            'conflicts': [item.__dict__ for item in self.conflicts],
        }


def _record_view(record: MemoryRecord) -> dict:
    return {'id': record.id, 'version': record.version, 'role': record.draft.role,
            'status': record.draft.status, 'summary': record.draft.summary}


def _id(value: str) -> str:
    return "memory-" + sha256(value.encode("utf-8")).hexdigest()[:24]


def _origins(records: Iterable[MemoryRecord]) -> set[tuple[str, str]]:
    """Distinct (project, session) experiences; the unit of independence."""
    origins: set[tuple[str, str]] = set()
    for record in records:
        projects = record.draft.provenance.projects or ('',)
        sessions = record.draft.provenance.sessions or ('',)
        origins.update((project, session) for project in projects for session in sessions)
    return origins


def _group_episodes(records: list[MemoryRecord], threshold: float) -> list[list[MemoryRecord]]:
    groups: list[list[MemoryRecord]] = []
    for record in sorted(records, key=lambda item: item.id):
        text = normalized_text(record)
        for group in groups:
            if token_similarity(text, normalized_text(group[0])) >= threshold:
                group.append(record)
                break
        else:
            groups.append([record])
    return groups


def _canonical_member(members: list[MemoryRecord]) -> MemoryRecord:
    counts: dict[str, list[MemoryRecord]] = {}
    for member in members:
        counts.setdefault(normalized_text(member), []).append(member)
    best = min(counts, key=lambda key: (-len(counts[key]), key))
    return counts[best][0]


def _common(values: Iterable[Iterable[str]]) -> tuple[str, ...]:
    shared: set[str] | None = None
    for value in values:
        current = set(value)
        shared = current if shared is None else (shared & current)
    return tuple(sorted(shared or set()))


def _restrictive_access(members: list[MemoryRecord]) -> AccessPolicy | None:
    """The narrowest access among members; None when no safe scope exists.

    Widening access is a human-only action, so the abstraction may only be at
    least as restrictive as every episode it generalises.
    """
    visibilities = {member.draft.access.visibility for member in members}
    if 'selected_projects' in visibilities:
        allowed: set[str] | None = None
        for member in members:
            if member.draft.access.visibility == 'selected_projects':
                current = set(member.draft.access.allowed_projects)
                allowed = current if allowed is None else (allowed & current)
        if not allowed:
            return None
        return AccessPolicy(visibility='selected_projects', allowed_projects=tuple(sorted(allowed)))
    if 'owner' in visibilities:
        return AccessPolicy(visibility='owner')
    return AccessPolicy(visibility='shared')


def _merge_applicability(members: list[MemoryRecord]) -> tuple[Applicability, ...]:
    """Union of conditions; a condition decided both ways stays `unknown`.

    Once two episodes disagree the condition is latched to `unknown` for good —
    a later agreeing episode must not resurrect a decided value and erase the
    recorded counterexample.
    """
    decisions: dict[str, tuple[str, set[str]]] = {}
    conflicted: set[str] = set()
    for member in members:
        for condition in member.draft.applicability:
            result, ids = decisions.get(condition.condition, ('unknown', set()))
            ids = ids | set(condition.evidence_ids)
            if condition.condition in conflicted:
                decisions[condition.condition] = ('unknown', ids)
            elif (result in ('true', 'false') and condition.result in ('true', 'false')
                  and result != condition.result):
                conflicted.add(condition.condition)
                decisions[condition.condition] = ('unknown', ids)
            else:
                decisions[condition.condition] = (result if result != 'unknown' else condition.result, ids)
    return tuple(Applicability(condition=condition, result=result, evidence_ids=tuple(sorted(ids)))
                 for condition, (result, ids) in sorted(decisions.items()))


class MemoryLifecycle:
    """Application service for automatic proposals and human corrections."""

    def __init__(self, store: MemoryStore, *, model: str | None = None,
                 prompt_version: str | None = None, host: str = 'unknown') -> None:
        self.store = store
        self.model = model
        self.prompt_version = prompt_version
        self.host = host

    def candidates(self, artifacts: SessionArtifacts, *, session_id: str,
                   project: str | None = None, source_reference: str = "") -> tuple[Candidate, ...]:
        sources = self._sources(artifacts, session_id, source_reference)
        provenance = Provenance(projects=(project,) if project else (), sessions=(session_id,),
                                host=self.host)
        result: list[Candidate] = []
        knowledge_id = None
        if artifacts.knowledge and artifacts.should_save:
            item = artifacts.knowledge
            knowledge_id = _id(f"knowledge:{session_id}:{item.id}")
            draft = MemoryDraft(
                kind="knowledge", role=item.role, body=item.body, summary=artifacts.title,
                status="candidate", evidence_level="unverified" if sources else "unknown",
                confidence=item.confidence, model=self.model, prompt_version=self.prompt_version,
                provenance=provenance,
                scope=ApplicabilityScope(technologies=tuple(item.technologies),
                                         domains=tuple(item.domains),
                                         exclusions=tuple(item.counterexamples)),
                applicability=tuple(Applicability(condition=condition.condition, result=condition.result)
                                    for condition in item.applicability),
                sources=sources,
            )
            result.append(Candidate(knowledge_id, draft))
        if artifacts.rule_candidate and artifacts.should_save:
            rule = artifacts.rule_candidate
            draft = MemoryDraft(
                kind="rule", role=rule.role, body=rule.instruction, summary=artifacts.title,
                status="candidate", evidence_level="unverified" if sources else "unknown",
                confidence=rule.confidence, model=self.model,
                prompt_version=self.prompt_version,
                provenance=provenance,
                scope=ApplicabilityScope(technologies=tuple(rule.technologies),
                                         domains=tuple(rule.domains),
                                         exclusions=tuple(rule.counterexamples)),
                applicability=tuple(Applicability(condition=condition.condition, result=condition.result)
                                    for condition in rule.applicability),
                sources=sources,
            )
            relations = (Relation(target_id=knowledge_id, target_version=1, kind="derived_from"),) if knowledge_id else ()
            result.append(Candidate(_id(f"rule:{session_id}:{rule.instruction}"), draft, relations))
        return tuple(result)

    def publish(self, candidate: Candidate, *, actor: str = "model"):
        current = self.store.get(candidate.memory_id)
        if current and actor == "model":
            if current.draft.suppressed or current.draft.user_owned or current.draft.status == "disabled":
                raise ProtectedMemory(candidate.memory_id)
            elif current.draft.body == candidate.draft.body and current.draft.kind == candidate.draft.kind:
                return current
        draft = candidate.draft.model_copy(update={"status": "active"}) if actor == "model" else candidate.draft
        return self.store.put(candidate.memory_id, draft,
                              expected_version=current.version if current else 0,
                              idempotency_key=f"candidate:{candidate.memory_id}",
                              actor=actor, reason="automatic memory proposal",
                              relations=candidate.relations)

    def publish_session(self, artifacts: SessionArtifacts, *, session_id: str,
                        project: str | None = None, source_reference: str = ""):
        return tuple(self.publish(candidate) for candidate in self.candidates(
            artifacts, session_id=session_id, project=project, source_reference=source_reference))

    def revise(self, memory_id: str, *, body: str | None = None,
               status: str | None = None, access: AccessPolicy | None = None,
               provenance: Provenance | None = None,
               applicability_scope: ApplicabilityScope | None = None,
               reason: str, expected_version: int, idempotency_key: str):
        record = self.store.get(memory_id)
        if record is None:
            raise KeyError(memory_id)
        updates = {}
        if body is not None:
            updates["body"] = body
        if status is not None:
            updates["status"] = status
        if access is not None:
            updates["access"] = access
        if provenance is not None:
            updates["provenance"] = provenance
        if applicability_scope is not None:
            updates["scope"] = applicability_scope
        updates["last_confirmed_at"] = now()
        draft = record.draft.model_copy(update=updates)
        return self.store.put(memory_id, draft, expected_version=expected_version,
                              idempotency_key=idempotency_key, actor="human", reason=reason)

    def suppress(self, memory_id: str, *, expected_version: int, idempotency_key: str,
                 reason: str = "human suppression"):
        return self.revise(memory_id, status="disabled", expected_version=expected_version,
                           idempotency_key=idempotency_key, reason=reason)

    def restore(self, memory_id: str, *, expected_version: int, idempotency_key: str,
                reason: str = "human restoration"):
        return self.revise(memory_id, status="active", expected_version=expected_version,
                           idempotency_key=idempotency_key, reason=reason)

    def scope(self, memory_id: str, access: AccessPolicy, *, expected_version: int,
              idempotency_key: str, reason: str = "human access correction"):
        return self.revise(memory_id, access=access, expected_version=expected_version,
                           idempotency_key=idempotency_key, reason=reason)

    def supersede(self, memory_id: str, replacement: Candidate, *, expected_version: int,
                  idempotency_key: str, reason: str = "human supersession"):
        created = self.publish(replacement, actor="human")
        relation = Relation(target_id=created.id, target_version=created.version, kind="supersedes")
        current = self.store.get(memory_id)
        if current is None or current.version != expected_version:
            raise VersionConflict(memory_id)
        revised = current.draft.model_copy(update={"status": "stale"})
        self.store.put(memory_id, revised, expected_version=expected_version,
                       idempotency_key=idempotency_key, actor="human", reason=reason,
                       relations=(relation,))
        return created

    def support(self, memory_id: str, source: Source, *, reason: str, expected_version: int,
                idempotency_key: str):
        """Append supporting evidence; the level rises at most to `supported`."""
        record = self._current(memory_id, expected_version)
        level = 'supported' if record.draft.evidence_level in {'unknown', 'unverified', 'supported'} \
            else record.draft.evidence_level
        draft = record.draft.model_copy(update={
            'sources': record.draft.sources + (source,),
            'proof_count': record.draft.proof_count + 1,
            'evidence_level': level,
        })
        return self.store.put(memory_id, draft, expected_version=expected_version,
                              idempotency_key=idempotency_key, actor='model', reason=reason)

    def weaken(self, memory_id: str, source: Source, condition: str, *, reason: str,
               expected_version: int, idempotency_key: str):
        """Narrow the applicability surface with a decided-against condition."""
        record = self._current(memory_id, expected_version)
        applicability = record.draft.applicability + (
            Applicability(condition=condition, result='false', evidence_ids=(source.identity,)),)
        draft = record.draft.model_copy(update={
            'sources': record.draft.sources + (source,),
            'applicability': applicability,
        })
        return self.store.put(memory_id, draft, expected_version=expected_version,
                              idempotency_key=idempotency_key, actor='model', reason=reason)

    def extend(self, memory_id: str, *, target_id: str, kind: str, source: Source,
               reason: str, expected_version: int, idempotency_key: str):
        """Record a `refines` / `exception_to` link; the target is not rewritten."""
        if kind not in {'refines', 'exception_to'}:
            raise ValueError('extension kind must be refines or exception_to')
        record = self._current(memory_id, expected_version)
        target = self.store.get(target_id)
        if target is None:
            raise KeyError(target_id)
        relation = Relation(target_id=target_id, target_version=target.version, kind=kind,
                            evidence_ids=(source.identity,))
        draft = record.draft.model_copy(update={'sources': record.draft.sources + (source,)})
        return self.store.put(memory_id, draft, expected_version=expected_version,
                              idempotency_key=idempotency_key, actor='model', reason=reason,
                              relations=(relation,))

    def overturn(self, memory_id: str, replacement: Candidate, source: Source, *,
                 reason: str, expected_version: int, idempotency_key: str):
        """Create a contradicting memory; the refuted one is never deleted."""
        refuted = self._current(memory_id, expected_version)
        relation = Relation(target_id=memory_id, target_version=refuted.version,
                            kind='contradicts', evidence_ids=(source.identity,))
        draft = replacement.draft.model_copy(update={
            'sources': replacement.draft.sources + (source,),
        })
        return self.store.put(replacement.memory_id, draft, expected_version=0,
                              idempotency_key=idempotency_key, actor='model', reason=reason,
                              relations=replacement.relations + (relation,))

    def consolidation_candidates(self, min_sources: int = 3) -> list[ConsolidationProposal]:
        """Episodes from >= min_sources independent origins that converge.

        Read-only. Each proposal is a new higher-level memory plus `derived_from`
        links to every source instance; the sources are never rewritten.
        """
        records = [record for record in self.store.current_records()
                   if record.draft.role == 'observation'
                   and record.draft.status not in {'archived', 'disabled', 'stale'}
                   and not record.draft.suppressed]
        proposals = []
        for group in _group_episodes(records, CONSOLIDATION_THRESHOLD):
            if len(_origins(group)) < min_sources:
                continue
            proposal = self._consolidation(group)
            if proposal is not None:
                proposals.append(proposal)
        return proposals

    def consolidate(self, proposal: ConsolidationProposal, *, idempotency_key: str | None = None,
                    reason: str | None = None, actor: str = 'system'):
        """Execute a consolidation: create the abstraction and its provenance links."""
        return self.store.put(proposal.memory_id, proposal.draft, expected_version=0,
                              idempotency_key=idempotency_key or f"consolidate:{proposal.memory_id}",
                              actor=actor, reason=reason or proposal.reason,
                              relations=proposal.relations)

    def undo_consolidation(self, memory_id: str, *, expected_version: int, idempotency_key: str):
        """Archive a consolidated memory and revoke its `derived_from` links."""
        record = self._current(memory_id, expected_version)
        archived = record.draft.model_copy(update={'status': 'archived'})
        result = self.store.put(memory_id, archived, expected_version=expected_version,
                                idempotency_key=idempotency_key, actor='human',
                                reason='undo automatic consolidation')
        for relation in self.store.relations(memory_id, expected_version):
            if (relation['memory_id'] == memory_id and relation['version'] == expected_version
                    and relation['kind'] == 'derived_from'):
                self.store.revoke_relation(relation['id'], expected_version=result.version,
                                           idempotency_key=f"{idempotency_key}:{relation['id']}",
                                           reason='undo automatic consolidation', actor='human')
        return result

    def decay_candidates(self, days: int = 90) -> list[MemoryRecord]:
        """Long unused and source-unstale records, raised to tidy priority.

        A new version (e.g. appended evidence) refreshes `created_at`, so this
        is "no `applied` and no source update". A `preference` is never here.
        """
        return MemoryEffectiveness(self.store).tidy_candidates(days)

    def mark_stale(self, memory_id: str, *, expected_version: int, idempotency_key: str,
                   reason: str):
        """Decay a record with a stated basis; reversible via `restore`."""
        record = self._current(memory_id, expected_version)
        if record.draft.role == 'preference':
            raise ValueError('preference time is judged by last_confirmed_at, not non-use')
        stale = record.draft.model_copy(update={'status': 'stale'})
        return self.store.put(memory_id, stale, expected_version=expected_version,
                              idempotency_key=idempotency_key, actor='system', reason=reason)

    def hygiene_report(self, *, days: int = 90, min_sources: int = 3,
                       threshold: float = 0.9) -> HygieneReport:
        """Batch consolidation / decay / merge / conflict items in one pass."""
        candidates = MemoryMerge(self.store).merge_candidates(threshold)
        return HygieneReport(
            consolidation=tuple(self.consolidation_candidates(min_sources)),
            decay=tuple(self.decay_candidates(days)),
            merges=tuple(item for item in candidates if item.kind != 'conflicts_with'),
            conflicts=tuple(item for item in candidates if item.kind == 'conflicts_with'),
        )

    def _consolidation(self, members: list[MemoryRecord]) -> ConsolidationProposal | None:
        owners = {member.draft.space.owner for member in members}
        if len(owners) > 1:
            return None
        access = _restrictive_access(members)
        if access is None:
            return None
        canonical = _canonical_member(members)
        role = 'procedure' if all(member.draft.kind == 'rule' for member in members) else 'claim'
        sources: dict[str, Source] = {}
        for member in members:
            for source in member.draft.sources:
                sources.setdefault(source.identity, source)
        source_ids = tuple(sorted(member.id for member in members))
        draft = MemoryDraft(
            kind='rule' if role == 'procedure' else 'knowledge',
            role=role,
            body=canonical.draft.body,
            summary=canonical.draft.summary,
            status='active',
            evidence_level='supported' if sources else 'unknown',
            space=MemorySpace(
                owner=members[0].draft.space.owner,
                privacy='private' if any(member.draft.space.privacy == 'private'
                                         for member in members) else 'shared',
            ),
            access=access,
            provenance=Provenance(
                projects=tuple(sorted({p for member in members for p in member.draft.provenance.projects})),
                sessions=tuple(sorted({s for member in members for s in member.draft.provenance.sessions})),
                paths=tuple(sorted({p for member in members for p in member.draft.provenance.paths})),
            ),
            scope=ApplicabilityScope(
                technologies=_common(member.draft.scope.technologies for member in members),
                domains=_common(member.draft.scope.domains for member in members),
                constraints=_common(member.draft.scope.constraints for member in members),
                exclusions=tuple(sorted({e for member in members for e in member.draft.scope.exclusions})),
                visibility='global' if all(member.draft.scope.visibility == 'global'
                                           for member in members) else 'scoped',
            ),
            applicability=_merge_applicability(members),
            sources=tuple(sources.values()),
        )
        relations = tuple(Relation(target_id=member.id, target_version=member.version,
                                   kind='derived_from') for member in members)
        origins = len(_origins(members))
        return ConsolidationProposal(
            memory_id=_id('consolidate:' + ':'.join(source_ids)),
            source_ids=source_ids, draft=draft, relations=relations,
            reason=f'{len(members)} episodes from {origins} independent origins converge to {role}',
        )

    def _current(self, memory_id: str, expected_version: int):
        record = self.store.get(memory_id)
        if record is None:
            raise KeyError(memory_id)
        if record.version != expected_version:
            raise VersionConflict(memory_id)
        return record

    @staticmethod
    def _sources(artifacts: SessionArtifacts, session_id: str, reference: str) -> tuple[Source, ...]:
        values = [Source(identity=f"session:{session_id}", kind="observation",
                         reference=reference, excerpt=artifacts.title, session_id=session_id)]
        for index, attempt in enumerate(artifacts.attempts):
            for part, text in enumerate(attempt.evidence):
                values.append(Source(identity=f"session:{session_id}:attempt:{index}:{part}",
                                     kind="observation", reference=reference,
                                     excerpt=text, session_id=session_id))
        return tuple(values)
