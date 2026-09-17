from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from ..domain import EventType, SessionEvent
from ..queue import JobStore


def _spawn_processor() -> None:
    """Launch a detached process that shows a progress bar while
    processing queued jobs, then exits.

    Detached so this hook (which has a 1.5s timeout) returns instantly.
    A small console window flashes with "Context Forge: processing..."
    then auto-closes when the worker finishes.
    """
    import threading
    try:
        import ctypes  # type: ignore
    except ImportError:
        ctypes = None

    python = Path.home() / "AppData/Local/Programs/Python/Python312/python.exe"
    if not python.exists():
        return
    cwd = Path(__file__).resolve().parent.parent.parent  # project root
    flags = 0x00000008  # DETACHED_PROCESS
    if ctypes is not None:
        try:
            # CREATE_NEW_CONSOLE so the user sees a small progress window
            flags |= 0x00000010
        except Exception:
            pass
    subprocess.Popen(
        [str(python), "-m", "context_forge.cli", "process-now"],
        cwd=str(cwd),
        creationflags=flags,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


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
    print("queued" if queued else "duplicate")
    # Only spawn the processor on SessionEnd; PreCompact fires mid-session
    # and we don't want to interrupt the user.
    if event_name == "SessionEnd":
        _spawn_processor()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
