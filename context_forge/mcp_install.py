"""Register the Context Forge MCP server with the host (Claude Code).

The host config (`~/.claude/settings.json`) is user-global: this backs it up
first, then only *adds* the `context_forge` entry under `mcpServers`. Existing
entries are never rewritten. `--uninstall` removes only that one key, so the
config returns to its prior shape.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path

MCP_KEY = "context_forge"


def _settings_path(target: Path | None) -> Path:
    if target is not None:
        return Path(target)
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "settings.json"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def entry() -> dict[str, object]:
    """The server block; `cwd` is the repo so `memory.db` resolves correctly."""
    return {
        "command": "python",
        "args": ["-m", "context_forge.mcp_server"],
        "cwd": str(_repo_root()),
    }


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save(path: Path, payload: dict) -> None:
    # Preserve the file's existing newline and trailing-newline presence:
    # text-mode write_text would rewrite every CRLF line as LF and add a
    # trailing newline, silently churning the user's whole global config
    # (and making --uninstall non-byte-exact) for a one-key addition.
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    existing = path.read_bytes() if path.exists() else b""
    newline = "\r\n" if b"\r\n" in existing else "\n"
    if not existing or existing.endswith((b"\n", b"\r\n")):
        text += newline
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.replace("\n", newline).encode("utf-8"))


def _backup(path: Path) -> str | None:
    if not path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    target = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, target)
    return str(target)


def install(target: Path | None = None) -> dict:
    path = _settings_path(target)
    payload = _load(path)
    backup = _backup(path)
    payload.setdefault("mcpServers", {})[MCP_KEY] = entry()
    _save(path, payload)
    return {"path": str(path), "backup": backup}


def uninstall(target: Path | None = None) -> dict:
    path = _settings_path(target)
    if not path.exists():
        return {"path": str(path), "backup": None, "removed": 0}
    payload = _load(path)
    backup = _backup(path)
    servers = payload.get("mcpServers", {})
    removed = 1 if MCP_KEY in servers else 0
    servers.pop(MCP_KEY, None)
    if not servers:
        payload.pop("mcpServers", None)
    _save(path, payload)
    return {"path": str(path), "backup": backup, "removed": removed}
