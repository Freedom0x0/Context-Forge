"""Logon autostart for the transcript watcher.

Drops a hidden VBS launcher into the user's Startup folder. The VBS
runs `forge watch-transcripts` with no visible window and restarts it
if it ever exits, so a crash doesn't silently stop precipitation.

No admin rights, no Task Scheduler, no service registration.

This is the piece that makes precipitation independent of Claude
Code's own configuration: the watcher reads transcripts from disk,
so rewriting `~/.claude/settings.json` cannot disable it.
"""

from __future__ import annotations

from pathlib import Path

TASK_NAME = "ContextForgeWatcher"
_LEGACY_BAT = f"{TASK_NAME}.bat"
_HEARTBEAT_STALE_SECONDS = 300


def _startup_dir() -> Path:
    return (Path.home() / "AppData/Roaming/Microsoft/Windows/Start Menu"
            / "Programs/Startup")


def _vbs_path() -> Path:
    return _startup_dir() / f"{TASK_NAME}.vbs"


def _legacy_bat_path() -> Path:
    return _startup_dir() / _LEGACY_BAT


def heartbeat_path() -> Path:
    from .config import runtime_dir
    return runtime_dir() / "watcher-heartbeat"


def _python_exe(windowless: bool = False) -> str:
    """Path to a Python interpreter, preferring the windowless one."""
    names = ("pythonw.exe", "python.exe") if windowless else ("python.exe",)
    roots = [
        Path.home() / "AppData/Local/Programs/Python/Python312",
        Path.home() / "AppData/Local/Programs/Python/Python313",
    ]
    for root in roots:
        for name in names:
            candidate = root / name
            if candidate.exists():
                return str(candidate)
    return "pythonw" if windowless else "python"


def install() -> dict[str, str]:
    """Write the hidden, self-restarting Startup launcher."""
    sd = _startup_dir()
    sd.mkdir(parents=True, exist_ok=True)

    # Older releases used a fire-and-forget .bat; replace it.
    legacy = _legacy_bat_path()
    if legacy.exists():
        legacy.unlink()

    python = _python_exe(windowless=True)
    vbs = _vbs_path()
    vbs.write_text(
        "' Context Forge watcher — hidden window, auto-restart on exit.\r\n"
        "Set sh = CreateObject(\"WScript.Shell\")\r\n"
        "Do\r\n"
        f'  sh.Run """{python}"" -m context_forge.cli watch-transcripts", 0, True\r\n'
        "  WScript.Sleep 30000\r\n"
        "Loop\r\n",
        encoding="utf-8",
    )
    return {"launcher": str(vbs)}


def uninstall() -> dict[str, str]:
    for path in (_vbs_path(), _legacy_bat_path()):
        if path.exists():
            path.unlink()
    return {"launcher": str(_vbs_path())}


def status() -> dict[str, object]:
    """Report whether the launcher exists and when the watcher last ran."""
    import json
    import os
    import time
    vbs = _vbs_path()
    heartbeat = heartbeat_path()
    result: dict[str, object] = {
        "launcher": str(vbs),
        "installed": vbs.exists(),
        "heartbeat": str(heartbeat),
        "alive": False,
        "age_seconds": None,
        "pid": None,
    }
    if heartbeat.exists():
        try:
            age = time.time() - heartbeat.stat().st_mtime
            result["age_seconds"] = round(age, 1)
            payload = json.loads(heartbeat.read_text(encoding="utf-8"))
            pid = int(payload["pid"])
            result["pid"] = pid
            if os.name == "nt":
                import ctypes
                handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
                process_alive = bool(handle)
                if handle:
                    ctypes.windll.kernel32.CloseHandle(handle)
            else:
                try:
                    os.kill(pid, 0)
                    process_alive = True
                except PermissionError:
                    process_alive = True
                except OSError:
                    process_alive = False
            result["alive"] = age < _HEARTBEAT_STALE_SECONDS and process_alive
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            pass
    return result
