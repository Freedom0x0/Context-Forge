"""`forge doctor` diagnostics.

Each check returns a `(name, ok, detail)` row. The CLI prints them and
exits non-zero if any check failed. Nothing here mutates state — doctor
is read-only.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Settings, default_settings_path, load_settings
from .queue import JobStore


def _now() -> datetime:
    return datetime.now(timezone.utc)


def check_settings() -> tuple[str, bool, str]:
    path = default_settings_path()
    settings = load_settings(path)
    if settings is None:
        return ("config", True, f"no config at {path} (using local defaults)")
    try:
        settings.validate_vault()
    except (ValueError, OSError) as exc:
        return ("config", False, str(exc))
    return ("config", True, f"vault={settings.root} provider={settings.model_provider}")


def check_vault_canonical(settings: Settings | None) -> tuple[str, bool, str]:
    if settings is None:
        return ("vault", True, "no settings — skipped")
    root = settings.root
    if root.exists() and not root.is_dir():
        return ("vault", False, f"not a directory: {root}")
    canonical = root.resolve()
    vault_parent = settings.vault_path.resolve()
    if os.path.commonpath([str(canonical), str(vault_parent)]) != str(vault_parent):
        return ("vault", False, f"vault root escapes config: {canonical}")
    return ("vault", True, str(canonical))


def check_queue(stalled_hours: int = 24,
                retention_days: int | None = None) -> tuple[str, bool, str]:
    path = Path.home() / ".context-forge" / "queue.db"
    if not path.exists():
        return ("queue", True, f"queue not initialised at {path}")
    try:
        store = JobStore(path)
    except sqlite3.Error as exc:
        return ("queue", False, f"cannot open: {exc}")
    jobs = store.list_jobs()
    stalled = 0
    cutoff = (_now() - timedelta(hours=stalled_hours)).isoformat()
    for job in jobs:
        if job["status"] == "running" and (job["updated_at"] or "") < cutoff:
            stalled += 1
    parts = [f"{len(jobs)} job(s)"]
    if stalled:
        parts.append(f"{stalled} stalled > {stalled_hours}h (run forge jobs list)")
    if retention_days is not None:
        old = store.retention_preview(retention_days)
        if old:
            parts.append(
                f"{len(old)} session(s) > {retention_days}d "
                f"(run forge retention --days {retention_days} to clean)"
            )
    if stalled:
        return ("queue", False, "; ".join(parts))
    return ("queue", True, "; ".join(parts))


def check_index(vault_root: Path) -> tuple[str, bool, str]:
    if not vault_root.exists():
        return ("index", True, f"vault missing at {vault_root} — skipped")
    import hashlib
    key = hashlib.sha256(str(vault_root).encode("utf-8")).hexdigest()[:16]
    db_path = Path.home() / ".context-forge" / f"index-{key}.db"
    if not db_path.exists():
        return ("index", True, f"no index at {db_path}; run forge scan")
    try:
        rows = list(sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM documents"))
    except sqlite3.Error as exc:
        return ("index", False, f"cannot query: {exc}")
    on_disk = sum(1 for _ in vault_root.rglob("*.md") if _.name != "index.md")
    count = rows[0][0] if rows else 0
    return ("index", count == on_disk, f"fts={count} markdown={on_disk}")


def check_provider(settings: Settings | None) -> tuple[str, bool, str]:
    if settings is None:
        return ("provider", True, "no settings — assuming offline / none")
    provider = settings.model_provider
    if provider in {"offline", "none", ""}:
        return ("provider", True,
                f"provider={provider or 'none'}; capture/vault/search still work")
    return ("provider", True,
            f"provider={provider}; user-configured credentials required for live calls")


def check_watcher() -> tuple[str, bool, str]:
    """Is the transcript watcher installed, and is it alive?

    Precipitation no longer depends on Claude Code hooks, so a dead
    watcher means nothing gets saved. This is the check that catches it.
    """
    from .autostart import heartbeat_path, status

    info = status()
    if not info["installed"]:
        return ("watcher", False,
                "autostart missing — run `forge install-hook`")
    age = info["age_seconds"]
    if age is None:
        return ("watcher", False,
                "no heartbeat yet — start it or log in again")
    if not info["alive"]:
        return ("watcher", False,
                f"last beat {age:.0f}s ago (launcher is installed; "
                f"it restarts at next logon)")
    return ("watcher", True, f"alive, last beat {age:.0f}s ago")


def run_all(retention_days: int = 30) -> list[tuple[str, bool, str]]:
    settings = load_settings()
    vault_root = settings.root if settings else Path.home() / ".context-forge" / "vault"
    if settings and settings.retention_days:
        retention_days = settings.retention_days
    return [
        check_settings(),
        check_vault_canonical(settings),
        check_queue(retention_days=retention_days),
        check_index(vault_root),
        check_provider(settings),
        check_watcher(),
    ]


def render(rows: list[tuple[str, bool, str]]) -> str:
    lines = []
    failed = 0
    for name, ok, detail in rows:
        marker = "OK  " if ok else "FAIL"
        if not ok:
            failed += 1
        lines.append(f"[{marker}] {name}: {detail}")
    lines.append("")
    lines.append(f"{len(rows) - failed}/{len(rows)} checks passed")
    return "\n".join(lines), failed
