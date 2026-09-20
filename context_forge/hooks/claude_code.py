"""Claude Code hook: print a status line above the input box.

This hook is a convenience only. Precipitation itself is done by
`forge watch-transcripts`, which reads transcripts straight from disk
and therefore keeps working no matter how often another tool (cc-switch,
an update, a manual edit) rewrites `~/.claude/settings.json`.

So this hook does no work beyond formatting one line for SessionStart.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())