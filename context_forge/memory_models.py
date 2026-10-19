from __future__ import annotations

from typing import Any, Literal, NamedTuple, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


Truth = Literal['true', 'false', 'unknown']
Actor = Literal['human', 'user', 'model', 'tool', 'import', 'system']
Role = Literal['observation', 'claim', 'hypothesis', 'preference', 'procedure']
Level = Literal['principle', 'pattern', 'instance']

DEFAULT_OWNER = 'local'


class MemoryModel(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)


class Scope(MemoryModel):
    """Legacy boundary shape (pre-P1 snapshots and Gen1 vault metadata)."""

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


class MemorySpace(MemoryModel):
    """所属主体/记忆空间 — 不承担项目授权。"""

    owner: str = Field(default=DEFAULT_OWNER, min_length=1)
    privacy: Literal['shared', 'private'] = 'shared'


class AccessPolicy(MemoryModel):
    """可见性 — 只剩主体边界。

    项目级显式授权（原 `selected_projects` + `allowed_projects`）已按产品决策
    移除：跨项目召回是产品核心，来源 project 只作弱排序，任何记忆都可按 id 读取。
    """

    visibility: Literal['owner', 'shared'] = 'shared'


class Provenance(MemoryModel):
    """来源上下文 — 只作溯源与排序加分，不决定可见性。"""

    projects: tuple[str, ...] = ()
    sessions: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    # Which agent produced the memory. 'unknown' rather than a host name: imported and
    # hand-written records have no agent, and blaming Claude Code for them would poison
    # the very bucket the report exists to read.
    host: str = 'unknown'


class ApplicabilityScope(MemoryModel):
    """适用条件 — 参与排序与采用判断。"""

    technologies: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()
    visibility: Literal['global', 'scoped'] = 'scoped'


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
    role: Role = 'claim'
    body: str = Field(min_length=1)
    summary: str = ''
    status: Literal['candidate', 'active', 'disabled', 'archived', 'conflicted', 'stale'] = 'candidate'
    user_owned: bool = False
    suppressed: bool = False
    evidence_level: Literal['unknown', 'unverified', 'supported', 'verified', 'contradicted'] = 'unknown'
    value: str | None = None
    confidence: str | None = None
    risk: Literal['normal', 'high'] = 'normal'
    model: str | None = None
    prompt_version: str | None = None
    space: MemorySpace = MemorySpace()
    access: AccessPolicy = AccessPolicy()
    provenance: Provenance = Provenance()
    scope: ApplicabilityScope = ApplicabilityScope()
    applicability: tuple[Applicability, ...] = ()
    sources: tuple[Source, ...] = ()
    proof_count: int = 0
    last_confirmed_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator('risk', mode='before')
    @classmethod
    def known_risk_only(cls, risk: Any) -> Any:
        # Risk is decided by `risk.classify_risk` on the write path, not by the
        # producer, so an unexpected self-report must degrade instead of failing.
        return risk if risk in ('normal', 'high') else 'normal'

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
        if self.role == 'observation' and not (self.provenance.projects or self.provenance.sessions):
            raise ValueError('episodic memory requires provenance projects or sessions')
        return self


class MemoryApplication(MemoryModel):
    """One attributable real-world application of a memory version.

    Version-pinned so attribution cannot drift. `attribution='confirmed'` is
    the only value that lets an application change weight, evidence or status.
    """

    application_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    memory_id: str = Field(min_length=1)
    memory_version: int = Field(ge=1)
    action: str = Field(min_length=1)
    expected_outcome: str = Field(min_length=1)
    actual_outcome: str | None = None
    validation_source_ids: tuple[str, ...] = ()
    attribution: Literal['confirmed', 'plausible', 'unknown'] = 'unknown'


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


class BoundarySplit(NamedTuple):
    space: MemorySpace
    access: AccessPolicy
    provenance: Provenance
    scope: ApplicabilityScope
    applicability: tuple[Applicability, ...]


def derive_level(draft: MemoryDraft) -> Level:
    """三层抽象由 role + ApplicabilityScope 推导，不落库。"""
    if draft.role == 'observation':
        return 'instance'
    if draft.role == 'preference':
        return 'principle'
    scope = draft.scope
    return 'pattern' if (scope.technologies or scope.domains or scope.constraints) else 'principle'


def split_legacy_scopes(scopes: Sequence[Scope], *, owner: str = DEFAULT_OWNER,
                        privacy: Literal['shared', 'private'] = 'shared') -> BoundarySplit:
    """Deterministic Scope → 边界三分 mapping; provenance is never dropped."""
    projects = tuple(sorted({scope.project for scope in scopes if scope.kind == 'project' and scope.project}))
    paths = tuple(sorted({path for scope in scopes for path in scope.paths}))
    global_visible = any(scope.kind == 'global' for scope in scopes)
    unresolved = Applicability(condition='未声明适用范围', result='unknown') if all(
        scope.kind == 'unknown' for scope in scopes) else None
    return BoundarySplit(
        space=MemorySpace(owner=owner, privacy=privacy),
        access=AccessPolicy(),
        provenance=Provenance(projects=projects, paths=paths),
        scope=ApplicabilityScope(visibility='global' if global_visible else 'scoped'),
        applicability=(unresolved,) if unresolved else (),
    )
