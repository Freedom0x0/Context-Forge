"""Install or remove the Claude Code hook for Context Forge.

Writes SessionEnd, PreCompact, and SessionStart entries into the
user-level `settings.json`. Idempotent: existing entries are not
duplicated, and running with `--uninstall` removes only the entries
we own. SessionStart prints `forge status` so Claude Code can show
the user pending review counts without manual commands.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

HOOK_COMMAND = "python -m context_forge.hooks.claude_code"
HOOK_EVENTS = ("SessionEnd", "PreCompact")
SESSION_START_COMMAND = "forge status"
HOOK_OWNER_KEY = "context_forge_managed"


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
    """Add hooks + register worker daemon scheduled task."""
    path = _settings_path(target)
    payload = _load(path)
    hooks_root = payload.setdefault("hooks", {})
    added = 0
    for event in HOOK_EVENTS:
        bucket = hooks_root.setdefault(event, [])
        if any(_owned_entries([h]) for h in bucket if isinstance(h, dict)):
            continue
        bucket.append({HOOK_OWNER_KEY: True,
                       "hooks": [{"type": "command", "command": HOOK_COMMAND}]})
        added += 1
    start_bucket = hooks_root.setdefault("SessionStart", [])
    if not any(_owned_entries([h]) for h in start_bucket if isinstance(h, dict)):
        start_bucket.append({HOOK_OWNER_KEY: True,
                             "hooks": [{"type": "command",
                                        "command": SESSION_START_COMMAND}]})
        added += 1
    _save(path, payload)

    # No background daemon by default. SessionEnd hook spawns its
    # own detached processor with a progress bar, runs the worker,
    # then exits. For users who want the old logon auto-start, run
    # `forge install-daemon` separately.
    return {"added": added, "path": str(path)}


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
