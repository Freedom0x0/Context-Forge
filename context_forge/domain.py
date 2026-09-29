from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class EventType(StrEnum):
    SESSION_END = "session_end"
    PRE_COMPACT = "pre_compact"


class ReviewStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class KnowledgeStatus(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REVISED = "revised"
    CONFLICTED = "conflicted"
    ARCHIVED = "archived"


class RuleStatus(StrEnum):
    PROPOSED = "proposed"
    ENABLED = "enabled"
    VERIFIED = "verified"
    STALE = "stale"
    DISABLED = "disabled"
    ARCHIVED = "archived"


class ClaimKind(StrEnum):
    FACT = "fact"
    INFERENCE = "inference"
    OPEN_QUESTION = "open_question"


MemoryRole = Literal["observation", "claim", "hypothesis", "preference", "procedure"]


class SessionEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    event_id: str = Field(min_length=1)
    event_type: EventType
    source: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    project: str = Field(min_length=1)
    cwd: str = Field(min_length=1)
    transcript_path: str | None = None
    transcript_hash: str | None = None
    occurred_at: datetime = Field(default_factory=utc_now)


class Evidence(BaseModel):
    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    source_line: int | None = Field(default=None, ge=1)


class Claim(BaseModel):
    text: str = Field(min_length=1)
    kind: ClaimKind
    confidence: str = "medium"
    evidence_ids: list[str] = Field(default_factory=list)


class Attempt(BaseModel):
    summary: str = Field(min_length=1)
    result: str = Field(min_length=1)
    evidence: list[str] = Field(default_factory=list)


class Condition(BaseModel):
    """Tri-state applicability: a rule is a hypothesis, not a creed."""

    condition: str = Field(min_length=1)
    result: Literal["true", "false", "unknown"] = "unknown"


class KnowledgeItem(BaseModel):
    """Reusable lesson distilled from one or more reviews.

    Knowledge auto-publishes to `knowledge/accepted/` — the user
    participates by editing or archiving files, not by gating writes.
    `user_owned=True` in the frontmatter prevents the worker from
    overwriting a hand-written entry.
    """

    id: str = Field(min_length=1)
    body: str = Field(min_length=1)
    sources: list[str] = Field(default_factory=list)
    confidence: str = "medium"
    topics: list[str] = Field(default_factory=list)
    role: MemoryRole = "claim"
    technologies: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    applicability: list[Condition] = Field(default_factory=list)
    counterexamples: list[str] = Field(default_factory=list)


class RuleExtraction(BaseModel):
    instruction: str = Field(min_length=1)
    paths: list[str] = Field(default_factory=list)
    confidence: str = "medium"
    role: MemoryRole = "procedure"
    technologies: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    applicability: list[Condition] = Field(default_factory=list)
    counterexamples: list[str] = Field(default_factory=list)

    @field_validator("confidence", mode="before")
    @classmethod
    def blank_confidence_is_medium(cls, value):
        return value or "medium"


class SessionArtifacts(BaseModel):
    """One model response per session end.

    The worker auto-writes up to two vault files per session:
    an `accepted/` knowledge item (when should_save=true AND knowledge
    is non-empty), and an `enabled` rule (when rule_candidate is
    non-empty). User participation is file-based, never command-based.
    """

    title: str = Field(min_length=1)
    problem: str = ""
    attempts: list[Attempt] = Field(default_factory=list)
    outcome: str = ""
    claims: list[Claim] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    candidate_topics: list[str] = Field(default_factory=list)
    should_save: bool = True
    reason: str = ""
    knowledge: KnowledgeItem | None = None
    rule_candidate: RuleExtraction | None = None

    @field_validator("claims")
    @classmethod
    def facts_need_evidence(cls, claims: list[Claim]) -> list[Claim]:
        for claim in claims:
            if claim.kind is ClaimKind.FACT and not claim.evidence_ids:
                raise ValueError("fact claims require evidence_ids")
        return claims


class SessionLifecycleEvent(BaseModel):
    event_id: str = Field(min_length=1)
    type: Literal["session_end", "pre_compact"]
    source: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    transcript_path: str = Field(min_length=1)
    observed_at: str = Field(min_length=1)
    final_line_count: int | None = Field(default=None, ge=0)
    reason: str = Field(min_length=1)


class EpisodeRecord(BaseModel):
    session_id: str
    segment: int = 1
    project: str | None = None
    host: str = Field(min_length=1)
    transcript_path: str
    collected_through_line: int = 0
    transcript_digest: str
    state: Literal["open", "inferred_pending", "finalizing", "finalized", "reopened"] = "open"
    started_at: str
    last_activity_at: str
    end_kind: Literal["explicit", "manual", "inferred"] | None = None
    retention_deadline: str | None = None


class EpisodeCheckpoint(BaseModel):
    checkpoint_id: str
    session_id: str
    segment: int = 1
    start_line: int
    end_line: int
    input_digest: str
    snapshot: str
    created_at: str


class FinalizationReceipt(BaseModel):
    finalization_id: str
    session_id: str
    segment: int
    episode_digest: str
    end_kind: Literal["explicit", "manual", "inferred"]
    extraction_json: str | None = None
    write_plan_json: str | None = None
    status: Literal["extracting", "planned", "committed", "projected", "failed"]
    error: str | None = None
    created_at: str
    updated_at: str
    extraction_owner: str | None = None
    extraction_lease_until: str | None = None


class DeferredTopic(BaseModel):
    topic: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class FinalMemoryCandidate(BaseModel):
    candidate_id: str = Field(min_length=1)
    draft: "MemoryDraft"
    evidence_refs: tuple[str, ...] = ()
    relations: tuple["Relation", ...] = ()


class FinalizationOutput(BaseModel):
    schema_version: Literal[1] = 1
    candidates: tuple[FinalMemoryCandidate, ...] = ()
    deferred_topics: tuple[DeferredTopic, ...] = ()
    no_memory_reason: str | None = None

    @field_validator("candidates")
    @classmethod
    def bounded_candidates(cls, values):
        if len(values) > 5:
            raise ValueError("finalization produces at most 5 candidates")
        return values

    @field_validator("no_memory_reason")
    @classmethod
    def nonblank_reason(cls, value):
        if value is not None and not value.strip():
            raise ValueError("no_memory_reason cannot be blank")
        return value

    @field_validator("deferred_topics")
    @classmethod
    def unique_deferred(cls, values):
        if len({item.topic for item in values}) != len(values):
            raise ValueError("deferred topics must be unique")
        return values

    def model_post_init(self, __context) -> None:
        if not self.candidates and not self.no_memory_reason:
            raise ValueError("zero candidates require no_memory_reason")


from .memory_models import MemoryDraft, Relation
FinalMemoryCandidate.model_rebuild()


class EventEnvelope(BaseModel):
    event: SessionEvent
    received_at: datetime = Field(default_factory=utc_now)
    raw: dict[str, Any] = Field(default_factory=dict)


class GatewayError(Exception):
    """Raised when a gateway refuses to produce structured output.

    Used for §24 case 4: fact claims without evidence must fail the job
    rather than silently landing in accepted knowledge.
    """


class LlmGateway(Protocol):
    """Boundary that turns sanitized evidence into structured artifacts.

    A gateway must be cheap to call but never the source of truth for
    product state. It must never receive raw transcripts and must never
    return free-form text in place of a validated model.
    """

    name: str

    def extract_session(self, transcript_path: str | None,
                         transcript_hash: str | None,
                         session_id: str) -> SessionArtifacts: ...

    def compile_rule(self, knowledge_id: str, knowledge_text: str) -> RuleExtraction: ...
