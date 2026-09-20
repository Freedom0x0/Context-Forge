from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

MAX_BLOCK_CHARS = 400


@dataclass(frozen=True)
class SanitizedTranscript:
    source_path: Path
    source_hash: str
    content: str


_SECRET_PATTERNS = [
    (re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s\"']+"), r"\1[REDACTED]") ,
    (re.compile(r"(?i)(api[_-]?key|token|password|secret)\s*[:=]\s*[^\s,\"']+"), r"\1=[REDACTED]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "[REDACTED:private_key]"),
    (re.compile(r"(?i)(postgres|mysql|mongodb)(://)[^\s\"']+"), r"\1\2[REDACTED]"),
]


def sanitize_text(text: str) -> str:
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def sanitize_transcript(path: Path) -> SanitizedTranscript:
    import hashlib

    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    text = raw.decode("utf-8", errors="replace")
    return SanitizedTranscript(path, digest, sanitize_text(text))


def extract_conversation(lines: list[str]) -> str:
    """Turn raw Claude Code transcript JSONL into a readable conversation.

    CC's transcript is one JSON object per line containing tool results,
    thinking blocks, attachments and metadata. Feeding that raw to a
    model makes it improvise (we saw `{"summary": ...}` instead of the
    requested schema). This extracts just the human-readable parts:

      user: <prompt text>
      assistant: <reply text>
      assistant [tool Bash]: <first N chars of input>
      [tool result]: <first N chars>

    Thinking blocks, token reminders and attachments are dropped.
    """
    out: list[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        msg = rec.get("message")
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or rec.get("type") or "?"
        content = msg.get("content")
        if isinstance(content, str):
            text = content.strip()
            if text:
                out.append(f"{role}: {_clip(text)}")
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                text = (block.get("text") or "").strip()
                if text:
                    out.append(f"{role}: {_clip(text)}")
            elif kind == "tool_use":
                name = block.get("name") or "tool"
                args = block.get("input")
                out.append(f"{role} [tool {name}]: {_clip(_stringify(args))}")
            elif kind == "tool_result":
                body = block.get("content")
                out.append(f"[tool result]: {_clip(_stringify(body))}")
            # 'thinking' / 'redacted_thinking' -> dropped
    return "\n".join(out)


def _clip(text: str) -> str:
    text = " ".join(text.split())
    if len(text) <= MAX_BLOCK_CHARS:
        return text
    return text[:MAX_BLOCK_CHARS] + "..."


def _stringify(value: object) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)
