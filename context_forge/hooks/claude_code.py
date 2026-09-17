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
    """Launch a detached processor; output inherits the user's terminal.

    We DO NOT use DETACHED_PROCESS — the spawn keeps the file
    descriptors of CC's hook process, which inherited them from the
    user's shell. So the spawn's print() output appears in the
    terminal where Claude Code was running, alongside the
    "Resume this session with: ..." prompt.

    We DO NOT use CREATE_NEW_CONSOLE either — that would pop a
    separate flash window.
    """
    import tempfile

    python = Path.home() / "AppData/Local/Programs/Python/Python312/python.exe"
    if not python.exists():
        return
    cwd = Path(__file__).resolve().parent.parent.parent  # project root

    # Open a log file so progress survives even if the terminal
    # closes before the processor finishes.
    log_path = Path(tempfile.gettempdir()) / "cf-process-now.log"
    log_handle = open(log_path, "w", encoding="utf-8")

    # stdout -> inherited terminal (user sees progress)
    # stderr -> log file (we don't want to muddle stderr with the
    #                user's shell; the visible output is enough)
    # stdin  -> closed
    subprocess.Popen(
        [str(python), "-m", "context_forge.cli", "process-now"],
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=None,        # inherit parent's terminal
        stderr=log_handle,
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
    # Write to stderr — Claude Code shows SessionEnd stderr alongside
    # the "See ya! / Resume with: claude --resume ..." exit message.
    # stdout of detached spawns isn't reliably visible across platforms.
    if event_name == "SessionEnd":
        sys.stderr.write(
            f"[context-forge] queued {session_id[:8]}; "
            f"processing in background (will write {Path.home() / '.context-forge' / 'last_run.md'})\n"
        )
        sys.stderr.flush()
        _spawn_processor()
    else:
        sys.stderr.write(
            f"[context-forge] {event_name} queued={queued} for {session_id[:8]}\n"
        )
        sys.stderr.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
