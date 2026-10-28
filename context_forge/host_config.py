"""Read and write a host's JSON config without losing what is already in it.

Both installers write the same global file (`~/.claude/settings.json`) and used to
carry their own copy of this logic. The copies disagreed: one swallowed a parse
error and then wrote its own object over the whole file, and it rewrote CRLF line
endings that the other one carefully preserved.

A file that is present but unreadable is the one case where writing must stop. It
is someone's whole host configuration — `env`, `permissions`, other tools' hooks —
and a half-written file is not an empty one.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path


class ConfigUnreadable(RuntimeError):
    """The file exists but is not a JSON object. Refuse to write over it."""


def load_config(path: Path) -> dict:
    """Parse a host config. A missing file is empty; a corrupt one raises."""
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigUnreadable(f"cannot read {path}: {exc}") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigUnreadable(
            f"{path} is not valid JSON ({exc.msg} at line {exc.lineno}); "
            "refusing to overwrite a configuration that could not be read") from exc
    if not isinstance(payload, dict):
        raise ConfigUnreadable(f"{path} is not a JSON object; refusing to overwrite it")
    return payload


def save_config(path: Path, payload: dict, *, backup: bool = True) -> str | None:
    """Write atomically, preserving the file's newline style and trailing newline.

    Text-mode `write_text` would rewrite every CRLF line as LF and add a trailing
    newline, silently churning the user's whole global config.
    """
    existing = path.read_bytes() if path.exists() else b""
    saved_to = _backup(path) if backup and existing else None
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    newline = "\r\n" if b"\r\n" in existing else "\n"
    if not existing or existing.endswith((b"\n", b"\r\n")):
        text += newline
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(path.parent),
                                         prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(text.replace("\n", newline).encode("utf-8"))
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return saved_to


def _backup(path: Path) -> str:
    stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    target = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, target)
    return str(target)
