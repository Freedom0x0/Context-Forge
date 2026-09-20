from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from ..domain import EventType, SessionEvent
from ..queue import JobStore


def _format_status_line() -> str:
    """One-line status for SessionStart to show above the input box."""
    from context_forge.vault import Vault
    from context_forge.config import default_settings_path, load_settings

    settings = load_settings()
    if settings:
        vault_root = settings.root
    else:
        vault_root = default_settings_path().parent / "vault"
    if not vault_root.exists():
        return "[context-forge] no vault configured"
    k = len(list(vault_root.glob("knowledge/accepted/*.md")))
    r = 0
    for path in vault_root.glob("rules/**/*.md"):
        try:
            if "status: enabled" in path.read_text(encoding="utf-8"):
                r += 1
        except OSError:
            pass
    return f"[context-forge] {k} knowledge, {r} rules active"


def main() -> int:
    payload = json.load(sys.stdin)
    transcript = payload.get("transcript_path")
    transcript_hash = None
    if transcript and Path(transcript).is_file():
        transcript_hash = hashlib.sha256(Path(transcript).read_bytes()).hexdigest()
    cwd = payload.get("cwd") or str(Path.cwd())
    session_id = payload.get("session_id") or hashlib.sha256(
        f"{cwd}:{transcript or ''}".encode("utf-8")
    ).hexdigest()[:24]
    event_name = payload.get("hook_event_name", "SessionEnd")
    event = SessionEvent(
        event_id=hashlib.sha256(
            f"{session_id}:{event_name}:{transcript_hash}".encode("utf-8")
        ).hexdigest(),
        event_type=EventType.PRE_COMPACT if event_name == "PreCompact" else EventType.SESSION_END,
        source="claude_code",
        session_id=session_id,
        project=Path(cwd).name,
        cwd=cwd,
        transcript_path=transcript,
        transcript_hash=transcript_hash,
    )
    queue_path = Path.home() / ".context-forge" / "queue.db"
    JobStore(queue_path).enqueue_event(event)

    if event_name == "SessionStart":
        # Show a status line above the input box: "N knowledge, M rules".
        # Claude Code renders SessionStart hook stdout there.
        # Processing is handled by `forge watch-transcripts`, which does
        # not depend on hooks surviving.
        print(_format_status_line())
    elif event_name == "PreCompact":
        # Compaction clears the model's context. Reset the offset
        # tracker so the next SessionEnd sees the full transcript again
        # and the model gets a fresh perspective.
        JobStore(queue_path).set_lines_processed(session_id, 0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
