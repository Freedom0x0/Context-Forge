"""`forge doctor` diagnostics.

Each check returns a `(name, ok, detail)` row. The CLI prints them and
exits non-zero if any check failed. Nothing here mutates state — doctor
is read-only.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Settings, default_settings_path, load_settings
from .index import index_path_for, iter_vault_documents
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
    from .config import runtime_dir
    path = runtime_dir() / "queue.db"
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
    db_path = index_path_for(vault_root)
    if not db_path.exists():
        return ("index", True, f"no index at {db_path}; run forge scan")
    try:
        rows = list(sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM documents"))
    except sqlite3.Error as exc:
        return ("index", False, f"cannot query: {exc}")
    on_disk = sum(1 for _ in iter_vault_documents(vault_root))
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
    from .autostart import status

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


def check_scheduled(period_seconds: int = 86400) -> tuple[str, bool, str]:
    """Daily maintenance ledger — a failing step and a silent step both fail."""
    from .config import runtime_dir

    path = runtime_dir() / "queue.db"
    if not path.exists():
        return ("scheduled", True, "queue not initialised")
    store = JobStore(path)
    runs = store.scheduled_runs()
    if not runs:
        return ("scheduled", False,
                "no maintenance step has ever run (is `forge watch-transcripts` up?)")
    now = _now()
    problems: list[str] = []
    details: list[str] = []
    for row in runs:
        step = row["step"]
        if row["last_error"]:
            problems.append(f"{step} failing: {row['last_error']} ({row['attempts']} consecutive)")
            continue
        try:
            age = (now - datetime.fromisoformat(row["last_success_at"])).total_seconds()
        except (TypeError, ValueError):
            problems.append(f"{step} has an unreadable success stamp")
            continue
        details.append(f"{step} ok {age / 3600:.0f}h ago")
        if age > 2 * period_seconds:
            problems.append(f"{step} silent for {age / 3600:.0f}h (> 2 periods)")
    if problems:
        return ("scheduled", False, "; ".join(problems))
    return ("scheduled", True, "; ".join(details))


def check_lifecycle_hooks() -> tuple[str, bool, str]:
    from .install_hook import status
    info = status()
    missing = [name for name, present in info["events"].items() if not present]
    if missing:
        return ("lifecycle-hooks", False,
                f"missing {', '.join(missing)} at {info['path']}; run forge install-hook")
    return ("lifecycle-hooks", True, f"installed at {info['path']}")


def check_precipitation_state() -> tuple[str, bool, str]:
    from .config import runtime_dir
    queue_path = runtime_dir() / "queue.db"
    memory_path = runtime_dir() / "memory.db"
    issues = []
    if queue_path.exists():
        store = JobStore(queue_path)
        failed = store.db.execute(
            "SELECT COUNT(*) FROM finalization_receipts WHERE status='failed'").fetchone()[0]
        pending = store.db.execute(
            "SELECT COUNT(*) FROM finalization_receipts WHERE status IN ('extracting','planned')").fetchone()[0]
        if failed:
            issues.append(f"{failed} failed receipt(s)")
        if pending:
            issues.append(f"{pending} unfinished receipt(s)")
    if memory_path.exists():
        try:
            with closing(sqlite3.connect(memory_path.as_uri() + "?mode=ro", uri=True)) as db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                conflicts = db.execute(
                    "SELECT COUNT(*) FROM projection_conflicts WHERE status='open'").fetchone()[0]
            from .memory_schema import SCHEMA_VERSION
            if version != SCHEMA_VERSION:
                issues.append(f"memory schema {version}, expected {SCHEMA_VERSION}")
            if conflicts:
                issues.append(f"{conflicts} projection conflict(s)")
        except sqlite3.Error as exc:
            issues.append(f"memory diagnostics unavailable: {exc}")
    return ("precipitation", not issues, "; ".join(issues) if issues else "receipts, schema and projections healthy")


def check_memory() -> tuple[str, bool, str]:
    """Weight-cache diagnostics; never invents a verdict from non-use."""
    from .config import runtime_dir
    from .memory import MemoryStore, MemoryStoreError
    from .memory_effectiveness import MemoryEffectiveness

    path = runtime_dir() / "memory.db"
    if not path.exists():
        return ("memory", True, "no memory database yet")
    try:
        store = MemoryStore(path, readonly=True)
        report = MemoryEffectiveness(store).report()
    except MemoryStoreError as error:
        return ("memory", False, f"unreadable memory database: {error}")
    adjusted = sum(1 for record in store.current_records() if store.weight(record.id) != 1.0)
    exposure = report.exposure_vs_adoption
    return ("memory", True,
            f"{len(store.current_records())} memories, {adjusted} with non-default weight; "
            f"曝光 {exposure['exposure']} / 采用 {exposure['adoption']} "
            f"(ratio {exposure['ratio']}); 未使用待复核 {len(report.anomaly_candidates)}, "
            f"整理候选 {len(report.tidy_candidates)}")


def check_memory_sync(vault_root: Path | None) -> list[str]:
    """Warn when the vault has documents that `memory.db` does not.

    A missing database with an empty vault is not an inconsistency (there is
    nothing to import), so it stays quiet; a real gap must never be silent.
    """
    from . import runtime_sync

    state = runtime_sync.consistency(vault_root)
    warnings: list[str] = []
    if state["failure"]:
        warnings.append(f"last sync failed: {state['failure']}")
    if state["vault_documents"] == 0:
        return warnings
    if state["missing"]:
        warnings.append("memory.db missing; run forge sync")
    elif state["vault_documents"] > state["db_records"]:
        missing = state["vault_documents"] - state["db_records"]
        warnings.append(
            f"vault has {state['vault_documents']} documents, memory.db has "
            f"{state['db_records']} ({missing} not imported); run forge sync")
    return warnings


def _has_context_forge(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    servers = payload.get("mcpServers")
    return isinstance(servers, dict) and "context_forge" in servers


def _same_path(candidate: str, repo: Path) -> bool:
    try:
        return Path(candidate).resolve() == repo
    except (OSError, ValueError):
        return candidate == str(repo)


def _mcp_registration() -> tuple[str, str] | None:
    """`(file, scope)` where the host actually reads a context_forge server.

    Mirrors Claude Code's own lookup order exactly — **local → project →
    user** (enterprise is out of scope here). Reporting the wrong scope would
    name a location the host would not use. Missing/unreadable/malformed JSON
    counts as absent. The user file's location comes from the host's own
    resolver (`mcp_install.claude_config_path`), which honours
    `CLAUDE_CONFIG_DIR` and `.config.json` precedence.
    """
    from . import mcp_install

    repo = Path(__file__).resolve().parents[1]
    config = mcp_install.claude_config_path()
    payload = mcp_install._load(config)
    if isinstance(payload, dict):
        # local scope sits under `projects[<repo>]` and outranks the others.
        projects = payload.get("projects")
        if isinstance(projects, dict):
            for key, project in projects.items():
                if not isinstance(project, dict) or not _same_path(key, repo):
                    continue
                if _has_context_forge(project):
                    return f"{config} projects[{key}]", "local"

    for candidate in mcp_install.project_config_paths(repo):
        if _has_context_forge(mcp_install._load(candidate)):
            return str(candidate), "project"

    if isinstance(payload, dict) and _has_context_forge(payload):
        return str(config), "user"
    return None


def check_mcp() -> tuple[str, bool, str]:
    """Report whether the host really registers the Context Forge server.

    Importing the `mcp` package proves nothing: the server can be importable
    while the host has zero registration. This looks where the host actually
    reads, so it fails when the server is not wired up — the check that a
    silent `settings.json` write should have tripped.
    """
    import importlib.util

    from . import mcp_install

    package = importlib.util.find_spec("mcp") is not None
    located = _mcp_registration()
    if located is not None:
        where, scope = located
        detail = f"registered in {where} ({scope} scope)"
        if scope == "project":
            detail += "; project scope needs approval (enabledMcpjsonServers) to take effect"
        if not package:
            detail += "; mcp package missing (pip install 'context-forge[mcp]')"
        return ("mcp", True, detail)

    legacy = mcp_install._legacy_settings_path()
    if _has_context_forge(mcp_install._load(legacy)):
        return ("mcp", False,
                f"entry only in {legacy}, which the host does not read for "
                f"mcpServers — inert; run forge mcp-install to migrate it")

    detail = "host has no context_forge server registered (run forge mcp-install)"
    if not package:
        detail += "; mcp package also missing (pip install 'context-forge[mcp]')"
    return ("mcp", False, detail)


def run_all(retention_days: int = 30) -> list[tuple[str, bool, str]]:
    settings = load_settings()
    from .config import runtime_dir
    vault_root = settings.root if settings else runtime_dir() / "vault"
    if settings and settings.retention_days:
        retention_days = settings.retention_days
    sync_warnings = check_memory_sync(vault_root)
    return [
        check_settings(),
        check_vault_canonical(settings),
        check_queue(retention_days=retention_days),
        check_index(vault_root),
        check_provider(settings),
        check_lifecycle_hooks(),
        check_watcher(),
        check_scheduled(),
        check_memory(),
        check_precipitation_state(),
        ("memory-sync", not sync_warnings,
         "; ".join(sync_warnings) if sync_warnings else "vault and memory.db agree"),
        check_mcp(),
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
