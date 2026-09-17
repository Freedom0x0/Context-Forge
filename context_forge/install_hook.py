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
    """Build a SessionStart command that shows current state + last run.

    Claude Code runs hooks in whatever shell the user launched it from
    (often Git Bash on Windows, where `forge` isn't on PATH). Use the
    absolute path of the `forge.exe` entry-point script.

    Output combines `forge status` (queue/vault state) with the
    last_run.md file written by the previous process-now spawn, so
    the user sees "what happened last time I exited" right inside
    the agent.
    """
    scripts = Path.home() / "AppData/Local/Programs/Python/Python312/Scripts"
    forge_exe = scripts / ("forge.exe" if (scripts / "forge.exe").exists()
                             else "forge")
    if forge_exe.exists():
        bin_part = f'"{forge_exe}"'
    else:
        py = Path.home() / "AppData/Local/Programs/Python/Python312/python.exe"
        bin_part = f'"{py}" -m context_forge.cli' if py.exists() else "forge"

    last_run = Path.home() / ".context-forge" / "last_run.md"
    parts = [f"cmd /c {bin_part} status"]
    if last_run.exists():
        parts.append(f'type "{last_run}"')
    return " && ".join(parts)


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