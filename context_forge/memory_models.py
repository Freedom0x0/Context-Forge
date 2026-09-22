from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


Truth = Literal['true', 'false', 'unknown']
Actor = Literal['human', 'user', 'model', 'tool', 'import', 'system']


class MemoryModel(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)


class Scope(MemoryModel):
    kind: Literal['global', 'project', 'unknown'] = 'unknown'
    project: str | None = None
    paths: tuple[str, ...] = ()

    @model_validator(mode='after')
    def valid_project(self) -> Scope:
        if self.kind == 'project' and (not self.project or self.project == 'unknown'):
            raise ValueError('project scope requires a known project')
        if self.kind != 'project' and self.project is not None:
            raise ValueError('only project scope may carry a project')
        if any(not path for path in self.paths):
            raise ValueError('path patterns must not be empty')
        return self


class Applicability(MemoryModel):
    condition: str = Field(min_length=1)
    result: Truth = 'unknown'
    evidence_ids: tuple[str, ...] = ()


class Source(MemoryModel):
    identity: str = Field(min_length=1)
    kind: Literal['observation', 'tool', 'model', 'human', 'legacy']
    reference: str = ''
    excerpt: str = ''
    session_id: str | None = None
    observed_at: str | None = None


class MemoryDraft(MemoryModel):
    kind: Literal['knowledge', 'rule', 'review'] = 'knowledge'
    role: Literal['observation', 'claim', 'hypothesis', 'preference', 'procedure'] = 'claim'
    body: str = Field(min_length=1)
    summary: str = ''
    status: Literal['candidate', 'active', 'disabled', 'archived', 'conflicted', 'stale'] = 'candidate'
    user_owned: bool = False
    suppressed: bool = False
    evidence_level: Literal['unknown', 'unverified', 'supported', 'verified', 'contradicted'] = 'unknown'
    value: str | None = None
    confidence: str | None = None
    risk: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    scopes: tuple[Scope, ...] = (Scope(),)
    applicability: tuple[Applicability, ...] = ()
    sources: tuple[Source, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator('sources')
    @classmethod
    def unique_sources(cls, sources: tuple[Source, ...]) -> tuple[Source, ...]:
        unique = {}
        for source in sources:
            if source.identity in unique and unique[source.identity] != source:
                raise ValueError('source identity has conflicting evidence')
            unique[source.identity] = source
        return tuple(unique.values())

    @model_validator(mode='after')
    def valid_evidence(self) -> MemoryDraft:
        identities = {source.identity for source in self.sources}
        if self.evidence_level in {'supported', 'verified', 'contradicted'} and not identities:
            raise ValueError('evidence level requires sources')
        if self.evidence_level == 'verified' and not any(source.kind == 'tool' and source.excerpt.strip() for source in self.sources):
            raise ValueError('verified evidence requires a retained tool observation')
        for condition in self.applicability:
            if not set(condition.evidence_ids) <= identities:
                raise ValueError('applicability evidence must reference this version sources')
        if not self.scopes:
            raise ValueError('at least one explicit scope is required')
        return self


class Relation(MemoryModel):
    target_id: str = Field(min_length=1)
    target_version: int = Field(ge=1)
    kind: Literal['derived_from', 'duplicate_of', 'overlaps', 'conflicts_with',
                  'supersedes', 'supports', 'contradicts', 'refines', 'exception_to']
    evidence_ids: tuple[str, ...] = ()
    applicability: tuple[Applicability, ...] = ()


class MemoryRecord(MemoryModel):
    id: str
    version: int
    draft: MemoryDraft
    created_at: str
