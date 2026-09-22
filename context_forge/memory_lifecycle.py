from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Iterable

from .domain import SessionArtifacts
from .memory import MemoryStore, ProtectedMemory, VersionConflict
from .memory_models import (
    AccessPolicy, Applicability, ApplicabilityScope, MemoryDraft, Provenance,
    Relation, Source,
)


@dataclass(frozen=True)
class Candidate:
    memory_id: str
    draft: MemoryDraft
    relations: tuple[Relation, ...] = ()


def _id(value: str) -> str:
    return "memory-" + sha256(value.encode("utf-8")).hexdigest()[:24]


class MemoryLifecycle:
    """Application service for automatic proposals and human corrections."""

    def __init__(self, store: MemoryStore, *, model: str | None = None,
                 prompt_version: str | None = None) -> None:
        self.store = store
        self.model = model
        self.prompt_version = prompt_version

    def candidates(self, artifacts: SessionArtifacts, *, session_id: str,
                   project: str | None = None, source_reference: str = "") -> tuple[Candidate, ...]:
        sources = self._sources(artifacts, session_id, source_reference)
        provenance = Provenance(projects=(project,) if project else (), sessions=(session_id,))
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
