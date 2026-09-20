"""Logon autostart for the transcript watcher.

Drops a small launcher into the user's Startup folder so
`forge watch-transcripts` runs in the background at every logon.
No admin rights, no Task Scheduler, no service registration.

This is the piece that makes precipitation independent of Claude
Code's own configuration: the watcher reads transcripts from disk,
so rewriting `~/.claude/settings.json` cannot disable it.
"""

from __future__ import annotations

from pathlib import Path

TASK_NAME = "ContextForgeWatcher"


class PlatformError(RuntimeError):
    pass


def _startup_dir() -> Path:
    return (Path.home() / "AppData/Roaming/Microsoft/Windows/Start Menu"
            / "Programs/Startup")


def _bat_path() -> Path:
    return _startup_dir() / f"{TASK_NAME}.bat"


def _python_exe() -> str:
    candidates = [
        Path.home() / "AppData/Local/Programs/Python/Python312/python.exe",
        Path.home() / "AppData/Local/Microsoft/WindowsApps/python3.exe",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return "python"


def install() -> dict[str, str]:
    """Write the Startup launcher."""
    sd = _startup_dir()
    sd.mkdir(parents=True, exist_ok=True)
    bat = _bat_path()
    bat.write_text(
        "@echo off\r\n"
        f'start /b "" "{_python_exe()}" -m context_forge.cli '
        "watch-transcripts\r\n",
        encoding="utf-8",
    )
    return {"launcher": str(bat)}


def uninstall() -> dict[str, str]:
    bat = _bat_path()
    if bat.exists():
        bat.unlink()
    return {"launcher": str(bat)}


def status() -> dict[str, object]:
    bat = _bat_path()
    return {"launcher": str(bat), "installed": bat.exists()}