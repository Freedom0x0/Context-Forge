"""`forge status` — one-line summary of queue + vault state.

Designed to be cheap (single read-only query, no model calls) so it
can run on every SessionStart without burning API budget. Output
format is plain text by default; `--json` emits a machine-readable
blob suitable for hooks or scripts.
"""

from __future__ import annotations

from pathlib import Path

from .config import default_settings_path, load_settings
from .queue import JobStore


def collect(store: JobStore) -> dict[str, int | str]:
    from .config import load_settings

    m = store.metrics()
    settings = load_settings()
    from .config import runtime_dir
    vault_root = settings.root if settings else runtime_dir() / "vault"
    rules_enabled = 0
    if vault_root.exists():
        for path in vault_root.glob("rules/**/*.md"):
            if path.name == "index.md":
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if "status: enabled" in text:
                rules_enabled += 1
    from . import runtime_sync

    sync = runtime_sync.consistency(vault_root)
    from .gateways import egress_surface
    egress_kind, egress_destination = egress_surface(settings)
    return {
        "sessions": m["sessions"],
        "episodes_last_by_host": ", ".join(
            f"{host}={str(info['last_activity_at'])[:10]}({info['episodes']})"
            for host, info in store.host_activity().items()),
        "episodes_inferred": m["episodes_inferred"],
        "veto_absorbed": m["veto_absorbed"],
        "egress": egress_destination if egress_kind != 'none' else "none",
        "episodes_by_host": ", ".join(f"{host}={count}" for host, count in store.host_counts().items()),
        "reviews": m["reviews"],
        "reviews_pending": m["reviews_draft"],
        "rules_enabled": rules_enabled,
        "jobs_dead_letter": m["jobs_dead_letter"],
        "jobs_succeeded": m["jobs_succeeded"],
        "memory_vault_documents": sync["vault_documents"],
        "memory_db_records": sync["db_records"],
        "memory_unsynced": max(0, sync["vault_documents"] - sync["db_records"]),
        "last_updated": _now_iso(),
    }


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def render_text(status: dict[str, int | str]) -> str:
    pending = status["reviews_pending"]
    dead = status["jobs_dead_letter"]
    parts: list[str] = []
    if pending:
        parts.append(f"{pending} review(s) pending")
    if dead:
        parts.append(f"{dead} job(s) in dead-letter")
    if status.get("memory_unsynced"):
        parts.append(f"{status['memory_unsynced']} vault document(s) not in memory.db (run forge sync)")
    if status.get("veto_absorbed"):
        parts.append(f"{status['veto_absorbed']} write(s) landed on a memory you archived "
                     "(it stays out of recall; disabled or suppressed ones refuse outright)")
    if not parts:
        parts.append("queue clean")
    return f"[context-forge] {'; '.join(parts)} ({status['sessions']} sessions, {status['rules_enabled']} rules)"


def render_json(status: dict[str, int | str]) -> str:
    import json
    return json.dumps(status, ensure_ascii=False)


def write_status_file(path: Path, status: dict[str, int | str]) -> None:
    """Persist the latest status so hooks / other tools can read it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_text(status) + "\n", encoding="utf-8", newline="\n")


def status_path() -> Path:
    return default_settings_path().parent / "status.md"
