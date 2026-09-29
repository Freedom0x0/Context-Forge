"""Claude Code hook: print a status line above the input box.

This hook is a convenience only. Precipitation itself is done by
`forge watch-transcripts`, which reads transcripts straight from disk
and therefore keeps working no matter how often another tool (cc-switch,
an update, a manual edit) rewrites `~/.claude/settings.json`.

So this hook does no work beyond formatting one line for SessionStart.
"""

from __future__ import annotations

import json
import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path

from ..domain import SessionLifecycleEvent
from ..episodes import EpisodeCollector
from ..queue import JobStore


def handle_payload(payload: dict, *, store: JobStore | None = None) -> SessionLifecycleEvent | None:
    """Translate a supported host hook payload into the generic lifecycle protocol."""
    name = payload.get("hook_event_name")
    if name not in {"SessionEnd", "PreCompact"}:
        return None
    transcript = Path(str(payload.get("transcript_path", ""))).expanduser()
    session_id = str(payload.get("session_id", "")).strip()
    if not session_id or not transcript.is_file():
        raise ValueError("session_id and an existing transcript_path are required")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    event_id = str(payload.get("event_id") or f"claude:{hashlib.sha256(canonical.encode()).hexdigest()}")
    if store is None:
        from ..config import runtime_dir
        store = JobStore(runtime_dir() / "queue.db")
    observed_at = payload.get("observed_at")
    if observed_at is None:
        existing = store.db.execute(
            "SELECT payload_json FROM session_lifecycle_events WHERE event_id=?",
            (event_id,),
        ).fetchone()
        if existing:
            observed_at = json.loads(existing["payload_json"])["observed_at"]
        else:
            observed_at = datetime.now(timezone.utc).isoformat()
    event = SessionLifecycleEvent(
        event_id=event_id,
        type="session_end" if name == "SessionEnd" else "pre_compact",
        source="claude_code", session_id=session_id, transcript_path=str(transcript.resolve()),
        observed_at=str(observed_at),
        final_line_count=payload.get("final_line_count"),
        reason=str(payload.get("reason") or name),
    )
    store.ingest_session_event(event)
    collector = EpisodeCollector(store)
    collector.collect(session_id, transcript,
                      payload.get("project") or payload.get("cwd"), event.source)
    if event.type == "pre_compact":
        collector.checkpoint(event)
    else:
        collector.request_finalize(session_id, end_kind="explicit", event_id=event.event_id)
    return event


def _format_status_line() -> str:
    """One-line status for SessionStart to show above the input box."""
    from ..config import default_settings_path, load_settings

    settings = load_settings()
    vault_root = settings.root if settings else (
        default_settings_path().parent / "vault"
    )
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
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}
    if payload.get("hook_event_name", "SessionEnd") == "SessionStart":
        print(_format_status_line())
    else:
        try:
            handle_payload(payload)
        except (ValueError, OSError) as exc:
            print(f"[context-forge] hook rejected: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
