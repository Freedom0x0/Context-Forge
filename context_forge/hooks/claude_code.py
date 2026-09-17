from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from ..domain import EventType, SessionEvent
from ..queue import JobStore


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
    queued = JobStore(queue_path).enqueue_event(event)
    # KISS: hook only enqueues. No spawn, no toast — CC's hook output
    # is hidden from the user terminal, and detached-process output
    # never reaches the user's shell reliably on Windows. The user
    # runs `forge worker run --once` after exiting CC; the result is
    # visible next time they open CC via SessionStart cat last_run.md.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
