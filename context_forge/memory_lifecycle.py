from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Iterable

from .domain import SessionArtifacts
from .memory import MemoryStore, ProtectedMemory, VersionConflict, digest, now
from .memory_effectiveness import MemoryEffectiveness
from .memory_merge import MemoryMerge, MergeCandidate, normalized_text, token_similarity
from .memory_models import (
    AccessPolicy, Applicability, ApplicabilityScope, MemoryDraft, MemoryRecord,
    MemorySpace, Provenance, Relation, Source,
)


CONSOLIDATION_THRESHOLD = 0.6
# One unattended pass may write at most this much, per tier. A pathological library — 300 exact
# duplicates in one afternoon — must not be canonicalized in a single silent sweep; the remainder
# is reported and picked up by the next pass.
AUTO_TIER_LIMIT = 100


def source_from_text(excerpt: str, *, identity: str = '', kind: str = 'observation',
                     reference: str = '') -> Source:
    """Build a source from a caller-supplied excerpt.

    Without an explicit identity the source is content-addressed, like
    `memory_sources`: the same words are the same source, so repeating them
    cannot pass as a second independent observation. One constructor for both
    entry points -- a CLI flag and an MCP argument must not be able to produce
    different evidence records for the same correction.
    """
    if not identity:
        identity = f"{kind}:{digest(excerpt)[:16]}"
    return Source(identity=identity, kind=kind, excerpt=excerpt, reference=reference)


def candidate_from_body(memory_id: str, body: str, *, project: str | None = None,
                        role: str = 'claim', status: str = 'active',
                        sources: tuple[Source, ...] = (),
                        provenance: Provenance | None = None) -> Candidate:
    """Build a replacement memory from a body, for supersession and overturn."""
    return Candidate(
        memory_id=memory_id,
        draft=MemoryDraft(body=body, status=status, role=role, sources=sources,
                           provenance=provenance if provenance is not None
                           else Provenance(projects=(project,) if project else ())),
        relations=(),
    )

# The four things a human may settle on for one merge candidate. `keep` changes nothing in the
# library but is still ledgered: without it the opposition rate has no denominator.
REVIEW_ACTIONS = ('keep', 'merge', 'archive', 'suppress')

# G4: "nobody used it" is not "it is wrong", so only a human may mark it stale.
DECAY_ACTIONS = ('keep', 'stale')


def adjudicate_decay(memory_id: str, action: str, *, store: MemoryStore, queue,
                     note: str = '') -> dict:
    """One human verdict on a decay candidate, ledgered the same way merge candidates are."""
    if action not in DECAY_ACTIONS:
        raise ValueError(f'unknown decay verdict; expected one of {", ".join(DECAY_ACTIONS)}')
    key = f'decay:{memory_id}'
    changed = None
    if action == 'stale':
        record = store.get(memory_id)
        if record is None:
            raise KeyError(memory_id)
        changed = MemoryLifecycle(store).mark_stale(
            memory_id, expected_version=record.version, idempotency_key=f'verdict:{key}',
            reason='human verdict: marked stale')
    queue.record_verdict(key, action, [memory_id], score=0.0, note=note)
    return {'candidate_key': key, 'action': action,
            'changed_id': changed.id if changed else None,
            'changed_version': changed.version if changed else None}


def adjudicate(candidate, action: str, *, store: MemoryStore, queue,
               target_id: str | None = None, note: str = '') -> dict:
    """Apply one human verdict to one merge candidate, and ledger it.

    Only a human belongs here. The automatic tier refuses anything that is not
    fingerprint-identical with the same provenance, and this is how that refusal gets
    overridden on purpose rather than by silence. `archive` leaves the veto absorbing
    evidence; `suppress` disables the row, after which model writes to it are refused outright.
    """
    if action not in REVIEW_ACTIONS:
        raise ValueError(f'unknown verdict; expected one of {", ".join(REVIEW_ACTIONS)}')
    key = f'{candidate.left_id}:{candidate.right_id}'
    lifecycle = MemoryLifecycle(store)
    changed = None
    if action == 'merge':
        changed = MemoryMerge(store).merge_pair(candidate, idempotency_key=f'verdict:{key}')
    elif action in ('archive', 'suppress'):
        if target_id not in (candidate.left_id, candidate.right_id):
            raise ValueError('target must be one of the two memories in the candidate')
        record = store.get(target_id)
        if record is None:
            raise KeyError(target_id)
        reason = ('human verdict: archived as duplicate' if action == 'archive'
                  else 'human verdict: suppressed as duplicate')
        if action == 'archive':
            changed = lifecycle.revise(target_id, status='archived', expected_version=record.version,
                                       idempotency_key=f'verdict:{key}:{target_id}', reason=reason)
        else:
            changed = lifecycle.suppress(target_id, expected_version=record.version,
                                         idempotency_key=f'verdict:{key}:{target_id}', reason=reason)
    queue.record_verdict(key, action, [candidate.left_id, candidate.right_id],
                         score=candidate.score, note=note)
    return {'candidate_key': key, 'action': action, 'target_id': target_id,
            'changed_id': changed.id if changed else None,
            'changed_version': changed.version if changed else None}


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
    least as restrictive as every episode it generalises. Only `owner` narrows
    anything now that project-level access is gone.
    """
    visibilities = {member.draft.access.visibility for member in members}
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
            current = decisions.get(condition.condition)
            ids = (current[1] if current else set()) | set(condition.evidence_ids)
            if condition.condition in conflicted:
                decisions[condition.condition] = ('unknown', ids)
            elif condition.result == 'unknown':
                # `unknown` is the pessimistic state: it is absorbing too, so a later
                # `true` cannot resurrect a condition whose evidence was missing.
                conflicted.add(condition.condition)
                decisions[condition.condition] = ('unknown', ids)
            elif current is None:
                decisions[condition.condition] = (condition.result, ids)
            elif current[0] != condition.result:
                conflicted.add(condition.condition)
                decisions[condition.condition] = ('unknown', ids)
            else:
                decisions[condition.condition] = (current[0], ids)
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
            if current.draft.status == "conflicted":
                # Skip, never raise: this runs in the worker's `publish_session` before
                # `set_lines_processed`, and the batch is a generator — an exception would loop
                # the same slice and abort every later candidate. The conflict stays until a human.
                return current
            if current.draft.body == candidate.draft.body and current.draft.kind == candidate.draft.kind:
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
               reason: str, expected_version: int, idempotency_key: str,
               actor: str = 'human'):
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
                              idempotency_key=idempotency_key, actor=actor, reason=reason)

    def suppress(self, memory_id: str, *, expected_version: int, idempotency_key: str,
                 reason: str = "human suppression"):
        """Stop recalling a memory, and record the opposition.

        The verdict is the point: a suppression is a strong objection, so it has to
        reach the weight and the vote buckets. It used to be only a status change,
        which left the opposition invisible to every report.
        """
        revised = self.revise(memory_id, status="disabled", expected_version=expected_version,
                              idempotency_key=idempotency_key, reason=reason)
        self.store.record_feedback(memory_id, outcome="revoked", note=reason,
                                   idempotency_key=idempotency_key + ":vote", actor="human")
        return revised

    def restore(self, memory_id: str, *, expected_version: int, idempotency_key: str,
                reason: str = "human restoration"):
        """Return a suppressed / conflicted memory to `active`, and acquit it.

        The acquittal cancels one revocation. Without it a suppressed-then-restored
        memory kept the suppression's negative weight forever.
        """
        revised = self.revise(memory_id, status="active", expected_version=expected_version,
                              idempotency_key=idempotency_key, reason=reason)
        self.store.record_feedback(memory_id, outcome="restored", note=reason,
                                   idempotency_key=idempotency_key + ":vote", actor="human")
        return revised

    def scope(self, memory_id: str, access: AccessPolicy, *, expected_version: int,
              idempotency_key: str, reason: str = "human access correction",
              actor: str = 'human'):
        return self.revise(memory_id, access=access, expected_version=expected_version,
                           idempotency_key=idempotency_key, reason=reason, actor=actor)

    def supersede(self, memory_id: str, replacement: Candidate, *, expected_version: int,
                  idempotency_key: str, reason: str = "human supersession",
                  actor: str = 'human'):
        created = self.publish(replacement, actor=actor)
        relation = Relation(target_id=created.id, target_version=created.version, kind="supersedes")
        current = self.store.get(memory_id)
        if current is None or current.version != expected_version:
            raise VersionConflict(memory_id)
        revised = current.draft.model_copy(update={"status": "stale"})
        self.store.put(memory_id, revised, expected_version=expected_version,
                       idempotency_key=idempotency_key, actor=actor, reason=reason,
                       relations=(relation,))
        return created

    def support(self, memory_id: str, source: Source, *, reason: str, expected_version: int,
                idempotency_key: str, actor: str = 'human'):
        """Append supporting evidence; the level rises at most to `supported`.

        `proof_count` counts *independent* observations, so a source the record
        already carries is not evidence again -- neither the same identity nor
        the same words under a new one. Repeating one excerpt must not raise
        the evidence strength.
        """
        record = self._current(memory_id, expected_version)
        known_identities = {item.identity for item in record.draft.sources}
        known_excerpts = {item.excerpt for item in record.draft.sources}
        independent = (source.identity not in known_identities
                       and source.excerpt not in known_excerpts)
        if not independent:
            return record
        level = 'supported' if record.draft.evidence_level in {'unknown', 'unverified', 'supported'} \
            else record.draft.evidence_level
        draft = record.draft.model_copy(update={
            'sources': record.draft.sources + (source,),
            'proof_count': record.draft.proof_count + 1,
            'evidence_level': level,
        })
        return self.store.put(memory_id, draft, expected_version=expected_version,
                              idempotency_key=idempotency_key, actor=actor, reason=reason)

    def weaken(self, memory_id: str, source: Source, condition: str, *, reason: str,
               expected_version: int, idempotency_key: str, actor: str = 'human'):
        """Narrow the applicability surface with a decided-against condition."""
        record = self._current(memory_id, expected_version)
        applicability = record.draft.applicability + (
            Applicability(condition=condition, result='false', evidence_ids=(source.identity,)),)
        draft = record.draft.model_copy(update={
            'sources': record.draft.sources + (source,),
            'applicability': applicability,
        })
        return self.store.put(memory_id, draft, expected_version=expected_version,
                              idempotency_key=idempotency_key, actor=actor, reason=reason)

    def extend(self, memory_id: str, *, target_id: str, kind: str, source: Source,
               reason: str, expected_version: int, idempotency_key: str, actor: str = 'human'):
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
                              idempotency_key=idempotency_key, actor=actor, reason=reason,
                              relations=(relation,))

    def overturn(self, memory_id: str, replacement: Candidate, source: Source, *,
                 reason: str, expected_version: int, idempotency_key: str,
                 actor: str = 'human'):
        """Create a contradicting memory; the refuted one is never deleted."""
        refuted = self._current(memory_id, expected_version)
        relation = Relation(target_id=memory_id, target_version=refuted.version,
                            kind='contradicts', evidence_ids=(source.identity,))
        draft = replacement.draft.model_copy(update={
            'sources': replacement.draft.sources + (source,),
        })
        return self.store.put(replacement.memory_id, draft, expected_version=0,
                              idempotency_key=idempotency_key, actor=actor, reason=reason,
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

    def apply_auto_tier(self, *, similarity_threshold: float = 0.6,
                        merge_threshold: float = 0.9, limit: int = AUTO_TIER_LIMIT) -> dict:
        """Run the two tiers that may work unattended: G1 exact duplicates, G2 conflict notes.

        Everything else stays a report. Three rules hold this together:
        - only `automatic` candidates are applied, and a candidate whose anchor went inactive
          mid-pass is skipped, because a three-way duplicate group emits (a,b), (a,c) and (b,c)
          and (b,c) would otherwise canonicalize onto an already-archived row;
        - conflict notes are skipped when the pair already carries the relation, since relation
          identity derives from the idempotency key and a fresh evidence version would otherwise
          add a second note on every pass;
        - the pass is capped at `limit` per action, and whatever it did not reach is reported.
        """
        merge = MemoryMerge(self.store)
        live = {'active', 'conflicted'}

        def is_live(record) -> bool:
            return record is not None and record.draft.status in live and not record.draft.suppressed

        exact_applied, exact_skipped, exact_deferred = 0, 0, 0
        for candidate in merge.report(similarity_threshold=similarity_threshold).exact_duplicates:
            if not candidate.automatic:
                exact_skipped += 1                     # provenance differs: a human decides
                continue
            if exact_applied + exact_skipped >= limit:
                exact_deferred += 1
                continue
            before = self.store.get(candidate.right_id)
            if not (is_live(self.store.get(candidate.left_id)) and is_live(before)):
                exact_skipped += 1                     # anchored on a row that left the library
                continue
            result = merge.apply_exact(
                candidate, idempotency_key=f'hygiene:exact:{candidate.left_id}:{candidate.right_id}'
                                           f'@{before.version}')
            if before is not None and result.draft.status == 'archived' \
                    and before.draft.status != 'archived':
                exact_applied += 1
            else:
                exact_skipped += 1                     # human-owned, disabled or already done

        conflicts = [item for item in merge.merge_candidates(threshold=merge_threshold)
                     if item.kind == 'conflicts_with']
        conflict_applied, conflict_skipped, conflict_deferred = 0, 0, 0
        for candidate in conflicts:
            if conflict_applied + conflict_skipped >= limit:
                conflict_deferred += 1
                continue
            noted = any(item['kind'] == 'conflicts_with' and item['target_id'] == candidate.right_id
                        for item in self.store.relations(candidate.left_id))
            if noted:
                conflict_skipped += 1
                continue
            left = self.store.get(candidate.left_id)
            if left is None:
                continue
            merge.apply_conflict(candidate, idempotency_key=f'hygiene:conflict:{candidate.left_id}'
                                                            f'@{left.version}'
                                                            f':{candidate.right_id}')
            conflict_applied += 1

        return {'exact_applied': exact_applied, 'exact_skipped': exact_skipped,
                'exact_deferred': exact_deferred, 'conflicts_annotated': conflict_applied,
                'conflicts_skipped': conflict_skipped, 'conflicts_deferred': conflict_deferred,
                'limit': limit}

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
