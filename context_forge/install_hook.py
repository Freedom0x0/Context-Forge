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

HOOK_OWNER_KEY = "context_forge_managed"


def _absolute_forge_command() -> str:
    """Build a SessionStart command that spawns the daemon and prints status.

    SessionStart runs claude_code.py so we get both:
    - spawn the background worker (DETACHED_PROCESS)
    - print the status line that CC shows above the input box
    """
    return "python -m context_forge.hooks.claude_code"


HOOK_COMMAND = "python -m context_forge.hooks.claude_code"
HOOK_EVENTS = ("SessionEnd", "PreCompact")
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

    # Mirror the hooks into cc-switch's provider templates so a
    # provider switch doesn't wipe them (cc-switch rewrites
    # ~/.claude/settings.json from `providers.settings_config`).
    try:
        from . import cc_switch
        if cc_switch.is_available():
            result = cc_switch.sync_hooks(payload["hooks"])
            print(f"[install-hook] cc-switch: {result['updated']}/"
                  f"{result['providers']} provider(s) updated")
    except Exception as exc:
        print(f"[install-hook] warning: cc-switch sync failed ({exc})")

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
        from . import cc_switch
        if cc_switch.is_available():
            cc_switch.remove_hooks()
    except Exception as exc:
        print(f"[install-hook] warning: cc-switch cleanup failed ({exc})")

    return {"removed": removed, "path": str(path)}