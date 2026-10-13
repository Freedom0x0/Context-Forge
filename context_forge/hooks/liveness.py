"""Host liveness evidence for inferred episode finalization.

Hosts may report both values in lifecycle payloads.  Adapters must preserve
``None`` when evidence is unavailable; callers must never turn an unknown
state into ``False`` merely to make an inferred close proceed.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import time
from typing import Any


@dataclass(frozen=True)
class LivenessEvidence:
    process_alive: bool | None = None
    active_tools: bool | None = None


def from_payload(payload: dict[str, Any]) -> LivenessEvidence:
    """Extract validated liveness fields from a host event payload."""
    process_alive = payload.get("process_alive")
    active_tools = payload.get("active_tools")
    if not isinstance(process_alive, bool):
        process_alive = None
    if not isinstance(active_tools, bool):
        active_tools = None
    return LivenessEvidence(process_alive, active_tools)


def unknown() -> LivenessEvidence:
    """Return conservative evidence for hosts without a probe."""
    return LivenessEvidence()


def claude_process_identity() -> tuple[int, float] | None:
    """Find the nearest Claude Code process that launched this hook process.

    The current process is the hook's Python interpreter. Only an actual
    ancestor whose executable identifies Claude Code is accepted; a sibling
    Claude process or a command line containing the session ID is insufficient.
    """
    try:
        import psutil
        current = psutil.Process(os.getpid())
        for ancestor in current.parents():
            try:
                name = ancestor.name().lower()
                executable = os.path.basename(ancestor.exe()).lower()
                command = [item.lower() for item in ancestor.cmdline()]
                tokens = [name, executable, *command]
                if any(
                    os.path.basename(token).removesuffix('.exe') in {'claude', 'claude-code'}
                    or '@anthropic-ai/claude-code' in token
                    for token in tokens
                ):
                    return ancestor.pid, ancestor.create_time()
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
                continue
    except (ImportError, OSError):
        return None
    return None


def register_current_claude_session(session_id: str, store) -> bool:
    """Persist a session-to-process identity when the hook has exact ancestry evidence."""
    identity = claude_process_identity()
    if identity is None:
        return False
    store.register_session_process(session_id, *identity)
    return True


def process_alive_for_session(session_id: str, store, *, now: float | None = None,
                              ttl_seconds: float = 86400.0) -> bool | None:
    """Check the registered PID and creation time, detecting PID reuse safely."""
    identity = store.session_process_identity(session_id)
    if identity is None:
        return None
    try:
        from datetime import datetime
        registered = datetime.fromisoformat(identity['registered_at']).timestamp()
        current = time.time() if now is None else now
        if current - registered > ttl_seconds:
            return None
    except (ValueError, TypeError, OSError):
        return None
    try:
        import psutil
    except ImportError:
        return None
    try:
        process = psutil.Process(identity['pid'])
        if abs(process.create_time() - identity['process_created_at']) > 0.01:
            return False
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False
    except (psutil.AccessDenied, psutil.ZombieProcess, OSError):
        return None
