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


# Only SessionStart is wired: it prints the status line CC shows
# above the input box. Precipitation is the watcher's job, so
# SessionEnd / PreCompact hooks would be dead weight.
HOOK_COMMAND = "python -m context_forge.hooks.claude_code"
HOOK_EVENTS: tuple[str, ...] = ()
SESSION_START_COMMAND = _absolute_forge_command()


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
    _add_or_update(hooks_root.setdefault("SessionStart", []),
                  SESSION_START_COMMAND)
    _save(path, payload)

    # The reliable half: a logon launcher for the transcript watcher.
    # The hooks above are a latency optimisation only — the watcher
    # reads transcripts straight from disk and cannot be disabled by
    # another tool rewriting ~/.claude/settings.json.
    try:
        from . import autostart
        result = autostart.install()
        print(f"[install-hook] watcher autostart at {result['launcher']}")
    except Exception as exc:
        print(f"[install-hook] warning: autostart not installed ({exc})")

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

    try:
        from . import autostart
        autostart.uninstall()
    except Exception as exc:
        print(f"[install-hook] warning: autostart not removed ({exc})")

    return {"removed": removed, "path": str(path)}