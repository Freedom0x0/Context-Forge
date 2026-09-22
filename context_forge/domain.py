from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Protocol

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


class RuleExtraction(BaseModel):
    instruction: str = Field(min_length=1)
    paths: list[str] = Field(default_factory=list)
    confidence: str = "medium"

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
