"""Install the Claude Code SessionStart hook and the watcher autostart.

Two independent pieces:

1. `SessionStart` hook — prints a one-line status above the input
   box. Cosmetic; losing it changes nothing functionally.
2. Watcher autostart — a Startup-folder launcher for
   `forge watch-transcripts`. This is what actually precipitates,
   and it reads transcripts from disk so it cannot be disabled by
   another tool rewriting `~/.claude/settings.json`.

Both are idempotent; `--uninstall` removes only what this owns.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

HOOK_OWNER_KEY = "context_forge_managed"


def _absolute_forge_command() -> str:
    """Build a SessionStart command that spawns the daemon and prints status.

    SessionStart runs claude_code.py so we get both:
    - spawn the background worker (DETACHED_PROCESS)
    - print the status line that CC shows above the input box
    """
    return "python -m context_forge.hooks.claude_code"


# SessionStart records exact per-session process identity for inferred-end safety
# and prints the status line. SessionEnd / PreCompact feed the existing lifecycle path.
HOOK_COMMAND = "python -m context_forge.hooks.claude_code"
HOOK_EVENTS: tuple[str, ...] = ("SessionEnd", "PreCompact", "PreToolUse",
                               "PostToolUse", "PostToolUseFailure",
                               "UserPromptSubmit", "Stop")
SESSION_START_EVENT = "SessionStart"
SESSION_START_COMMAND = HOOK_COMMAND


def _settings_path(target: Path | None) -> Path:
    if target is not None:
        return target
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "settings.json"


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _save(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")


def _owned_entries(hooks: list[dict]) -> list[dict]:
    return [h for h in hooks if isinstance(h, dict) and h.get(HOOK_OWNER_KEY)]


def install(target: Path | None = None) -> dict[str, int]:
    """Add hooks. If owned entries already exist with outdated commands,
    update them in place.
    """
    path = _settings_path(target)
    payload = _load(path)
    hooks_root = payload.setdefault("hooks", {})
    added = 0
    updated = 0

    def _add_or_update(bucket: list, command: str) -> None:
        nonlocal added, updated
        owned = [h for h in bucket if isinstance(h, dict) and h.get(HOOK_OWNER_KEY)]
        if owned:
            current = owned[0].get("hooks", [{}])[0].get("command", "")
            if current == command:
                return
            owned[0]["hooks"] = [{"type": "command", "command": command}]
            updated += 1
            return
        bucket.append({HOOK_OWNER_KEY: True,
                       "hooks": [{"type": "command", "command": command}]})
        added += 1

    for event in HOOK_EVENTS:
        _add_or_update(hooks_root.setdefault(event, []), HOOK_COMMAND)
    _add_or_update(hooks_root.setdefault(SESSION_START_EVENT, []),
                  SESSION_START_COMMAND)
    _save(path, payload)

    return {"added": added, "updated": updated, "path": str(path)}


def uninstall(target: Path | None = None) -> dict[str, int]:
    """Remove hooks. Does not touch the optional logon daemon."""
    path = _settings_path(target)
    payload = _load(path)
    hooks_root = payload.get("hooks", {})
    removed = 0
    for event in list(hooks_root.keys()):
        bucket = hooks_root.get(event, [])
        kept = [h for h in bucket if not (isinstance(h, dict) and h.get(HOOK_OWNER_KEY))]
        removed += len(bucket) - len(kept)
        if kept:
            hooks_root[event] = kept
        else:
            hooks_root.pop(event, None)
    if not hooks_root:
        payload.pop("hooks", None)
    _save(path, payload)

    return {"removed": removed, "path": str(path)}


def status(target: Path | None = None) -> dict[str, object]:
    path = _settings_path(target)
    hooks = _load(path).get("hooks", {})
    installed = {event: bool(_owned_entries(hooks.get(event, [])))
                 for event in (*HOOK_EVENTS, SESSION_START_EVENT)}
    return {"path": str(path), "events": installed, "ok": all(installed.values())}
