"""Install / remove a Windows logon task for the worker daemon.

Two strategies, in order of preference:
1. **Startup folder `.bat`**: drop a batch file in the user's
   Startup directory. No admin needed. Runs on every logon.
2. **schtasks /Create**: more elegant but requires "Log on as
   batch job" privilege on most setups; will fail with `拒绝访问`
   in a sandboxed environment.

We use the Startup folder approach by default since it just works
on consumer Windows installs.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


TASK_NAME = "ContextForgeWorker"


class PlatformError(RuntimeError):
    pass


def _startup_dir() -> Path:
    return (Path.home() / "AppData/Roaming/Microsoft/Windows/Start Menu"
            / "Programs/Startup")


def _bat_path() -> Path:
    return _startup_dir() / f"{TASK_NAME}.bat"


def _bat_content() -> str:
    """A self-restarting launcher that survives the user closing the cmd window."""
    return (
        "@echo off\r\n"
        ":loop\r\n"
        'echo [%date% %time%] context-forge worker started > "%TEMP%\\cf-worker.log"\r\n'
        'start /b "" "C:\\Users\\15532\\AppData\\Local\\Programs\\Python\\Python312\\python.exe" '
        '-m context_forge.cli worker run\r\n'
        "ping 127.0.0.1 -n 30 > nul\r\n"
        "goto loop\r\n"
    )


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
    """Drop a .bat launcher into the user's Startup folder."""
    sd = _startup_dir()
    sd.mkdir(parents=True, exist_ok=True)
    bat = _bat_path()
    bat.write_text(
        f'@echo off\r\n'
        f'start /b "" "{_python_exe()}" -m context_forge.cli worker run\r\n',
        encoding="utf-8",
    )
    return {"launcher": str(bat)}


def uninstall() -> dict[str, str]:
    bat = _bat_path()
    if bat.exists():
        bat.unlink()
    return {"launcher": str(bat)}


def status() -> dict[str, str]:
    bat = _bat_path()
    return {"launcher": str(bat), "installed": bat.exists()}


# Keep the schtasks path around as an alternative for users with admin
def install_schtasks() -> dict[str, str]:
    """Try the official schtasks path; requires batch-job privilege."""
    if not shutil.which("schtasks"):
        raise PlatformError("schtasks not available")
    py = _python_exe()
    cmd = f'cmd /c "{py}" -m context_forge.cli worker run'
    proc = subprocess.run(
        ["schtasks", "/Create", "/TN", TASK_NAME, "/TR", cmd,
         "/SC", "ONLOGON", "/RL", "LIMITED", "/F"],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise PlatformError(f"schtasks failed: {proc.stderr}")
    return {"task": TASK_NAME}