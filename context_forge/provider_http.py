"""HTTP-backed LlmGateway implementations.

`RemoteGateway` talks to the Anthropic Messages API; `LocalGateway`
talks to an Ollama-compatible `/api/chat` endpoint. Both share
`HttpGateway` which owns HTTP plumbing, retry/backoff and JSON parsing.

The provider is pluggable: when the user sets `model_provider = "remote"`
or `"local"` and supplies credentials, the worker actually calls the
model. Without credentials the gateway is skipped and the system
keeps working -- capture / vault / search remain usable.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .domain import (
    LlmGateway,
    RuleExtraction,
    SessionArtifacts,
)


_PROMPT = (
    "You are reviewing a sanitized Claude Code session transcript. "
    "Reply with strict JSON only -- no prose, no markdown fences -- "
    "that matches this schema:\n"
    "{{\n"
    '"title": string, '
    '"problem": string, '
    '"attempts": [{{"summary": string, "result": string, '
    '"evidence": [string]}}], '
    '"outcome": string, '
    '"claims": [{{"text": string, '
    '"kind": "fact|inference|open_question", '
    '"confidence": "low|medium|high", '
    '"evidence_ids": [string]}}], '
    '"uncertainties": [string], '
    '"candidate_topics": [string], '
    '"should_save": boolean, '
    '"reason": string, '
    '"knowledge": {{"id": string, "body": string, '
    '"sources": [string], '
    '"confidence": "low|medium|high", '
    '"topics": [string]}} | null, '
    '"rule_candidate": {{"instruction": string, "paths": [string], '
    '"confidence": "low|medium|high"}} | null'
    "}}\n"
    "Facts MUST cite at least one evidence id. Open questions and "
    "inferences can omit evidence. Set should_save=false if the session "
    "lacks an actionable lesson. If should_save=true and you derive a "
    "reusable lesson, populate `knowledge`; if that lesson would "
    "generalise into a project rule, populate `rule_candidate` too. "
    "Skip both fields if no reusable knowledge emerged.\n\n"
    "Transcript (sanitized):\n{transcript}\n"
)


@dataclass
class _CallResult:
    text: str
    attempts: int


class GatewayError(RuntimeError):
    """Raised by HTTP gateways when the model is unreachable or misbehaves."""


class RateLimited(GatewayError):
    """The provider is throttling us; retrying later will help.

    The watcher treats this differently from a hard failure: the
    session is left un-advanced and simply picked up on a later pass,
    so a busy proxy never loses a session.
    """


def _retry_after(response) -> float | None:
    """Parse a Retry-After header (seconds form) if the server sent one."""
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


class HttpGateway:
    """HTTP + retry + JSON plumbing shared by RemoteGateway and LocalGateway."""

    name = "http"

    # A shared local proxy needs long gaps, not quick retries.
    rate_limit_delay = 20.0

    def __init__(self, max_attempts: int = 3, base_delay: float = 1.0,
                 timeout: float = 30.0) -> None:
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.timeout = timeout
        self._client = None  # lazy: tests inject a MockTransport

    def _client_instance(self):
        if self._client is None:
            import httpx
            self._client = httpx.Client(timeout=self.timeout)
        return self._client

    def _post(self, url: str, headers: dict[str, str],
              body: dict[str, Any]) -> _CallResult:
        import httpx

        client = self._client_instance()
        last_exc: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = client.post(url, headers=headers, json=body)
            except httpx.HTTPError as exc:
                last_exc = exc
                self._sleep_backoff(attempt)
                continue
            if response.status_code == 429:
                # The local proxy is shared with Claude Code itself, so
                # we compete for quota. Honour Retry-After when present
                # and back off far longer than for a transient 5xx.
                delay = _retry_after(response) or self.rate_limit_delay * attempt
                last_exc = RateLimited(
                    f"429 rate limited; sleeping {delay:.0f}s"
                )
                time.sleep(min(delay, 300.0))
                continue
            if response.status_code in {500, 502, 503, 504}:
                last_exc = GatewayError(
                    f"{response.status_code} {response.reason_phrase}"
                )
                self._sleep_backoff(attempt)
                continue
            if response.status_code >= 400:
                raise GatewayError(
                    f"{response.status_code} {response.text[:200]}"
                )
            return _CallResult(text=response.text, attempts=attempt)
        if isinstance(last_exc, RateLimited):
            raise last_exc
        raise GatewayError(f"max attempts exhausted: {last_exc}")

    def _sleep_backoff(self, attempt: int) -> None:
        time.sleep(min(self.base_delay * (2 ** (attempt - 1)), 8.0))

    @staticmethod
    def _parse_json_object(text: str) -> dict[str, Any]:
        decoder = json.JSONDecoder()
        idx = text.find("{")
        while idx >= 0:
            try:
                obj, _end = decoder.raw_decode(text[idx:])
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                pass
            idx = text.find("{", idx + 1)
        raise GatewayError(f"model returned no JSON object: {text[:120]}")

    @staticmethod
    def _transcript_excerpt(transcript_path: str | None,
                             transcript_hash: str | None) -> str:
        if not transcript_path:
            return f"(no transcript; hash={transcript_hash or 'unknown'})"
        from pathlib import Path
        from .transcript import sanitize_transcript
        sanitized = sanitize_transcript(Path(transcript_path))
        body = sanitized.content.strip()
        # Keep both ends: a resumed session's newest turns carry the
        # actual work, but the opening frames the problem. A plain
        # head-truncation fed the model only the oldest material.
        limit = 12000
        if len(body) > limit:
            head = limit // 3
            tail = limit - head
            body = (body[:head] + "\n... (middle omitted) ...\n"
                    + body[-tail:])
        return body or "(empty)"

    def extract_session(self, transcript_path, transcript_hash, session_id):
        excerpt = self._transcript_excerpt(transcript_path, transcript_hash)
        prompt = _PROMPT.format(transcript=excerpt)
        reply = self._call_chat(prompt)
        data = self._parse_json_object(reply)
        try:
            return SessionArtifacts.model_validate(data)
        except Exception as exc:
            # The model returned JSON, but of the wrong shape (we have
            # seen `{"summary": ...}` from lighter models). Re-ask once
            # with the validation error attached — cheaper and more
            # reliable than failing the job.
            repair = (
                "Your previous reply did not match the required schema.\n"
                f"Validation error: {exc}\n"
                f"Your reply was: {json.dumps(data, ensure_ascii=False)[:600]}\n\n"
                "Reply again with ONLY a JSON object using EXACTLY the "
                "schema keys shown. Do not add or rename keys.\n\n"
                f"{prompt}"
            )
            reply2 = self._call_chat(repair)
            data2 = self._parse_json_object(reply2)
            return SessionArtifacts.model_validate(data2)

    def compile_rule(self, knowledge_id, knowledge_text):
        prompt = (
            "Distill the following knowledge into a single-sentence "
            "rule instruction, a comma-separated list of glob "
            "patterns and a confidence. Reply with strict JSON only: "
            '{"instruction": string, "paths": [string], '
            '"confidence": "low|medium|high"}\n\n'
            f"Knowledge id: {knowledge_id}\n{knowledge_text}"
        )
        reply = self._call_chat(prompt)
        data = self._parse_json_object(reply)
        return RuleExtraction.model_validate(data)

    # subclasses override
    def _call_chat(self, prompt: str) -> str:
        raise NotImplementedError


class RemoteGateway(HttpGateway):
    """Anthropic Messages API.

    Reads env vars in this order so users rarely need to set anything:
    - key: `ANTHROPIC_API_KEY` then `ANTHROPIC_AUTH_TOKEN`
    - url: `ANTHROPIC_API_URL` then `ANTHROPIC_BASE_URL`
    - model: `ANTHROPIC_MODEL` then `ANTHROPIC_DEFAULT_{SONNET,OPUS,HAIKU}_MODEL`
    """

    name = "remote"

    def __init__(self, api_url: str, api_key: str, model: str,
                 max_attempts: int = 3) -> None:
        super().__init__(max_attempts=max_attempts)
        if not api_key:
            raise GatewayError("remote_api_key is empty")
        self.api_url = api_url.rstrip("/") + "/v1/messages"
        self.api_key = api_key
        self.model = model

    def _call_chat(self, prompt: str) -> str:
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body = {
            "model": self.model,
            # Reasoning-capable models spend most of the budget on a
            # thinking block before emitting text. At 2048 the reply
            # was cut off mid-thought and contained no text block at
            # all (measured: stop_reason=max_tokens, thinking only).
            "max_tokens": 8192,
            "messages": [{"role": "user", "content": prompt}],
        }
        result = self._post(self.api_url, headers, body)
        try:
            payload = json.loads(result.text)
        except json.JSONDecodeError as exc:
            raise GatewayError(f"anthropic returned non-JSON: {exc}") from exc
        blocks = payload.get("content") or []
        texts = [b.get("text", "") for b in blocks
                 if b.get("type") == "text" and (b.get("text") or "").strip()]
        if not texts:
            # No usable text block: usually the reply was cut off inside
            # a thinking block (bump max_tokens), or the proxy returned
            # an error-shaped 200. Surface the shape so it's diagnosable
            # instead of a bare "no JSON object".
            kinds = [b.get("type") for b in blocks]
            sizes = [len(b.get("text") or b.get("thinking") or "")
                     for b in blocks]
            raise GatewayError(
                f"empty response (stop_reason={payload.get('stop_reason')!r}, "
                f"blocks={kinds}, sizes={sizes}, "
                f"usage={payload.get('usage')})"
            )
        return "\n".join(texts)


class LocalGateway(HttpGateway):
    """Ollama-compatible /api/chat endpoint."""

    name = "local"

    def __init__(self, api_url: str, model: str,
                 max_attempts: int = 3) -> None:
        super().__init__(max_attempts=max_attempts)
        self.api_url = api_url.rstrip("/") + "/api/chat"
        self.model = model

    def _call_chat(self, prompt: str) -> str:
        headers = {"content-type": "application/json"}
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "format": "json",
        }
        result = self._post(self.api_url, headers, body)
        try:
            payload = json.loads(result.text)
        except json.JSONDecodeError as exc:
            raise GatewayError(f"local model returned non-JSON: {exc}") from exc
        message = payload.get("message") or {}
        return message.get("content", "")


def _settings_env(name: str) -> str:
    """Read an env var from ~/.claude/settings.json as fallback.

    Claude Code stores per-session env vars in its own settings file.
    The worker daemon runs as a separate scheduled task and does NOT
    inherit these, so it must read them directly.
    """
    try:
        path = Path.home() / ".claude" / "settings.json"
        if path.exists():
            import json as _json
            payload = _json.loads(path.read_text(encoding="utf-8"))
            return payload.get("env", {}).get(name, "") or ""
    except Exception:
        pass
    return ""


def build_remote(api_key: str | None = None, model: str | None = None,
                 api_url: str | None = None) -> LlmGateway:
    """Factory honouring environment overrides for the remote gateway.

    Priority: explicit arg > os.environ > ~/.claude/settings.json env block
    """
    key = (
        api_key
        or os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        or _settings_env("ANTHROPIC_AUTH_TOKEN")
        or _settings_env("ANTHROPIC_API_KEY")
        or ""
    )
    url = (
        api_url
        or os.environ.get("ANTHROPIC_API_URL")
        or os.environ.get("ANTHROPIC_BASE_URL")
        or _settings_env("ANTHROPIC_BASE_URL")
        or _settings_env("ANTHROPIC_API_URL")
        or "https://api.anthropic.com"
    )
    chosen = (
        model
        or os.environ.get("ANTHROPIC_MODEL")
        or os.environ.get("ANTHROPIC_DEFAULT_SONNET_MODEL")
        or os.environ.get("ANTHROPIC_DEFAULT_OPUS_MODEL")
        or os.environ.get("ANTHROPIC_DEFAULT_HAIKU_MODEL")
        or _settings_env("ANTHROPIC_DEFAULT_SONNET_MODEL")
        or _settings_env("ANTHROPIC_DEFAULT_OPUS_MODEL")
        or _settings_env("ANTHROPIC_DEFAULT_HAIKU_MODEL")
        or "claude-3-5-sonnet-20241022"
    )
    if not key:
        raise GatewayError(
            "ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN not set"
        )
    return RemoteGateway(api_url=url, api_key=key, model=chosen)


def build_local(model: str | None = None,
                api_url: str | None = None) -> LlmGateway:
    url = api_url or os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    chosen = model or os.environ.get("OLLAMA_MODEL", "llama3")
    return LocalGateway(api_url=url, model=chosen)
