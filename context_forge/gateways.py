"""LlmGateway implementations.

`OfflineGateway` returns structured artifacts derived only from the
sanitized transcript's structure. `NoOpGateway` is the safe default
when the user has not configured a model provider. Both keep the
worker functional without burning API budget.

`RemoteGateway` (Anthropic) and `LocalGateway` (Ollama) live in
`provider_http.py`. Both share `HttpGateway`'s retry/backoff/JSON plumbing.
"""

from __future__ import annotations

from pathlib import Path

from .domain import (
    Attempt,
    Claim,
    KnowledgeItem,
    LlmGateway,
    RuleExtraction,
    SessionArtifacts,
)
from .transcript import sanitize_transcript


class OfflineGateway:
    """Offline extractor that only inspects transcript structure.

    Never produces fact claims, so the resulting SessionArtifacts
    cannot generate knowledge or rule_candidate. Used as the default
    and as the test fixture.
    """

    name = "offline"

    def extract_session(self, transcript_path, transcript_hash,
                        session_id) -> SessionArtifacts:
        if not transcript_path:
            return SessionArtifacts(
                title=f"Session {session_id}",
                reason="no transcript_path",
                should_save=False,
            )
        sanitized = sanitize_transcript(Path(transcript_path))
        lines = [line.strip() for line in sanitized.content.splitlines() if line.strip()]
        if not lines:
            return SessionArtifacts(
                title=f"Session {session_id}",
                reason="transcript empty",
                should_save=False,
            )
        return SessionArtifacts(
            title=f"Session {session_id}",
            problem=lines[0][:500],
            attempts=[Attempt(summary="recorded problem", result="logged",
                              evidence=lines[:3])],
            outcome="offline gateway did not run a model",
            reason="offline gateway",
            should_save=True,
            knowledge=KnowledgeItem(
                id=f"k_{session_id}",
                body="Offline gateway has no model-derived knowledge. "
                     "Configure provider = remote / local for real extraction.",
                sources=[session_id],
                confidence="low",
            ),
        )

    def compile_rule(self, knowledge_id, knowledge_text) -> RuleExtraction:
        return RuleExtraction(instruction=knowledge_text.strip()[:400], confidence="low")

    def finalize_episode(self, payload):
        return {"schema_version": 1, "candidates": [], "deferred_topics": [],
                "no_memory_reason": "offline provider cannot derive long-term memory"}


class NoOpGateway:
    """Default when no provider is configured.

    Lets capture, vault and search keep working without any model call.
    """

    name = "none"

    def extract_session(self, transcript_path, transcript_hash,
                        session_id) -> SessionArtifacts:
        return SessionArtifacts(
            title=f"Session {session_id}",
            reason="model provider not configured",
            should_save=False,
        )

    def compile_rule(self, knowledge_id, knowledge_text) -> RuleExtraction:
        return RuleExtraction(instruction=knowledge_text.strip()[:400], confidence="low")

    def finalize_episode(self, payload):
        return {"schema_version": 1, "candidates": [], "deferred_topics": [],
                "no_memory_reason": "model provider not configured"}


def _fixture_artifacts() -> SessionArtifacts:
    """Default fixture used when `provider = "fake"`."""
    return SessionArtifacts(
        title="fixture session",
        problem="placeholder problem",
        outcome="fixture gateway did not run a model",
        reason="fixture gateway",
        should_save=False,
    )


class FixtureGateway:
    """Returns caller-supplied SessionArtifacts.

    Used to verify that the worker honours structured constraints.
    """

    def __init__(self, fixture: SessionArtifacts | None = None) -> None:
        self.fixture = fixture or _fixture_artifacts()
        self.name = "fixture"

    def extract_session(self, transcript_path, transcript_hash, session_id):
        return self.fixture

    def compile_rule(self, knowledge_id, knowledge_text):
        return RuleExtraction(instruction=knowledge_text.strip()[:400], confidence="low")

    def finalize_episode(self, payload):
        return {"schema_version": 1, "candidates": [], "deferred_topics": [],
                "no_memory_reason": "fixture has no finalization output"}


PROVIDER_NAMES = ('none', 'offline', 'fake', 'local', 'remote')


def egress_surface(settings=None) -> tuple[str, str]:
    """Classify where sanitized transcript text goes: `('remote'|'local'|'none', destination)`.

    This is the single derivation diagnostics use; a second copy of the
    provider vocabulary elsewhere would drift the moment a provider is added.
    """
    provider = ((settings.model_provider if settings else '') or 'none').strip().lower()
    if provider == 'remote':
        from .provider_http import resolve_remote_url

        return ('remote', resolve_remote_url(settings))
    if provider == 'local':
        return ('local', getattr(settings, 'local_api_url', '') or 'http://localhost:11434')
    return ('none', 'nothing leaves this machine')


def select_gateway(provider: str, settings=None) -> LlmGateway:
    """Map a settings-level provider name to a gateway implementation.

    Recognised names follow the §21 contract: `offline`, `fake`,
    `local`, `remote`. Anything else collapses to `NoOpGateway` so
    unconfigured deployments still let capture / vault / search run.

    `local` / `remote` require settings with credentials; missing
    credentials raise `GatewayError`.
    """
    from .provider_http import GatewayError, build_local, build_remote

    name = (provider or "").strip().lower()
    if name == "offline":
        return OfflineGateway()
    if name == "fake":
        return FixtureGateway(_fixture_artifacts())
    if name == "local":
        if settings is None:
            raise GatewayError("provider=local requires settings")
        return build_local(
            model=getattr(settings, "local_model", None) or None,
            api_url=getattr(settings, "local_api_url", None) or None,
        )
    if name == "remote":
        if settings is None:
            raise GatewayError("provider=remote requires settings")
        return build_remote(
            api_key=getattr(settings, "remote_api_key", None) or None,
            model=getattr(settings, "remote_model", None) or None,
            api_url=getattr(settings, "remote_api_url", None) or None,
        )
    return NoOpGateway()
