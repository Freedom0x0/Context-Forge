"""Top-level CLI.

After the auto-loop refactor, only 6 commands remain for daily use:

  forge init <vault-path>           first-time setup
  forge install-hook [--uninstall] wire Claude Code hooks
  forge worker run [--once]         manual / debug trigger (daemon is the default)
  forge status [--json]             one-line state peek
  forge doctor                      health check
  forge retention --apply [--days N]  manual cleanup

Everything else is either:
- a diagnostic / debug tool (`session-info`, `jobs`, `validate`)
- an escape hatch for power users (`provider-check`, `import-transcript`)
- removed because user actions live in Obsidian / frontmatter instead

See `docs/context-forge-usage.md` for the full daily flow.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import typer

from .config import load_settings
from .gateways import select_gateway
from .queue import JobStore
from .vault import Vault
from .worker import process_one, run_loop

app = typer.Typer(no_args_is_help=True)
worker_app = typer.Typer(no_args_is_help=True)
app.add_typer(worker_app, name="worker")


def _store() -> JobStore:
    return JobStore(Path.home() / ".context-forge" / "queue.db")


def _vault() -> Vault:
    settings = load_settings()
    if settings:
        return Vault(settings.root)
    return Vault(Path.home() / ".context-forge" / "vault")


def _index_path(vault_root: Path) -> Path:
    import hashlib
    key = hashlib.sha256(str(vault_root).encode("utf-8")).hexdigest()[:16]
    return Path.home() / ".context-forge" / f"index-{key}.db"


# -- core 6 --------------------------------------------------------------


@app.command()
def init(vault_path: Path) -> None:
    """Write a default config pointing at `<vault-path>` and create dirs.

    Overwrites an existing config so re-running with a different path
    is fine. Existing vault files are not touched.
    """
    from .config import Settings, default_settings_path
    import tomli_w

    cfg_path = default_settings_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    settings = Settings(vault_path=vault_path.resolve())
    cfg_path.write_bytes(tomli_w.dumps(settings.model_dump(mode="json")))
    Vault(vault_path / "context-forge")  # create dirs
    typer.echo(f"wrote {cfg_path} (vault = {settings.root})")


@app.command("install-hook")
def install_hook(target: Path = typer.Option(None, "--target"),
                  uninstall: bool = typer.Option(False, "--uninstall")) -> None:
    """Install or remove the Claude Code SessionEnd/PreCompact/SessionStart hooks."""
    from . import install_hook as installer

    result = installer.uninstall(target) if uninstall else installer.install(target)
    if uninstall:
        typer.echo(f"removed {result['removed']} hook entries at {result['path']}")
    else:
        typer.echo(f"added {result['added']} hook entries at {result['path']}")


@worker_app.command("run")
def worker_run(once: bool = typer.Option(False, "--once"),
               interval: float = typer.Option(0.0, "--interval", min=0.0)) -> None:
    """Process queued review jobs (long-running daemon).

    For the session-following UX, prefer `forge process-now` which
    shows a progress bar and exits when done.
    """
    settings = load_settings()
    provider = settings.model_provider if settings else "none"
    gateway = select_gateway(provider, settings)
    max_attempts = settings.max_attempts if settings else 3
    if once:
        processed = process_one(_store(), _vault(), gateway, max_attempts)
        typer.echo("processed" if processed else "empty")
        return
    if interval <= 0:
        interval = settings.poll_interval_seconds if settings else 2.0
    run_loop(_store(), _vault(), gateway,
             interval_seconds=interval, max_attempts=max_attempts)


@app.command("process-now")
def process_now() -> None:
    """Process all queued jobs with a visible progress window.

    Designed to be spawned by the SessionEnd hook. The window stays
    open ~10s so the user can watch the precipitation happen as they
    exit Claude Code. A `last_run.md` is also written so the next
    SessionStart can show what happened if the window was missed.
    """
    import sys
    import time

    settings = load_settings()
    provider = settings.model_provider if settings else "none"
    gateway = select_gateway(provider, settings)
    max_attempts = settings.max_attempts if settings else 3
    store = _store()
    vault = _vault()
    pending = sum(1 for j in store.list_jobs() if j["status"] == "queued")
    print(f"[context-forge] processing {pending} job(s)...", flush=True)
    if pending == 0:
        # No work; close quickly so the user doesn't wonder why a window opened.
        time.sleep(2)
        return
    done = 0
    failures = 0
    while True:
        try:
            settings2 = load_settings()
            p2 = settings2.model_provider if settings2 else "none"
            gw = select_gateway(p2, settings2)
            processed = process_one(store, vault, gw, max_attempts)
        except Exception as exc:
            print(f"[error] {type(exc).__name__}: {exc}", flush=True)
            failures += 1
            processed = False
        if processed:
            done += 1
            print(f"[context-forge] {done}/{pending} done", flush=True)
        else:
            break
    jobs = store.list_jobs()
    still = sum(1 for j in jobs if j["status"] in ("queued", "running"))
    failed = sum(1 for j in jobs if j["status"] in ("failed", "dead_letter"))
    knowledge_count = len(list(vault.root.glob("knowledge/accepted/*.md")))
    rule_count = len(list(vault.root.glob("rules/proposals/*.md")))
    print(
        f"[context-forge] finished: {done} ok, {failed} failed, "
        f"{still} pending",
        flush=True,
    )
    print(
        f"[context-forge] vault: {knowledge_count} knowledge, "
        f"{rule_count} rules",
        flush=True,
    )

    from pathlib import Path
    home = Path.home() / ".context-forge"
    home.mkdir(parents=True, exist_ok=True)
    (home / "last_run.md").write_text(
        f"# Context Forge last run\n\n"
        f"- {done} session(s) processed\n"
        f"- {failed} failed, {still} pending\n"
        f"- {knowledge_count} knowledge file(s) in vault\n"
        f"- {rule_count} rule(s) in vault\n",
        encoding="utf-8",
    )

    print("(this closes in 3s; full log in last_run.md)", flush=True)
    time.sleep(3)


@app.command()
def status(as_json: bool = typer.Option(False, "--json")) -> None:
    """One-line summary of pending reviews + dead-letter jobs."""
    from . import status as status_mod

    snapshot = status_mod.collect(_store())
    status_mod.write_status_file(status_mod.status_path(), snapshot)
    if as_json:
        typer.echo(status_mod.render_json(snapshot))
    else:
        typer.echo(status_mod.render_text(snapshot))


@app.command()
def doctor(fix: bool = typer.Option(False, "--fix")) -> None:
    """Run read-only diagnostics for vault, queue, index and provider.

    With `--fix`, also apply retention for sessions older than
    `retention_days` from config (default 30).
    """
    from . import doctor as diag

    rows = diag.run_all()
    output, failed = diag.render(rows)
    typer.echo(output)
    if fix:
        settings = load_settings()
        days = settings.retention_days if settings else 30
        store = _store()
        targets = store.retention_preview(days)
        if not targets:
            typer.echo(f"no sessions older than {days}d to clean")
        else:
            store.retention_apply(days)
            typer.echo(f"cleaned {len(targets)} session(s) older than {days}d")
    if failed:
        raise typer.Exit(code=1)


@app.command()
def retention(days: int = typer.Option(30, "--days", min=0),
               apply: bool = typer.Option(False, "--apply"),
               dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    """List or delete sessions older than `--days` (default 30).

    Without `--apply`, only lists. `--dry-run` is the default for safety.
    """
    store = _store()
    if apply:
        targets = store.retention_apply(days, dry_run=dry_run)
        verb = "would delete" if dry_run else "deleted"
    else:
        targets = [r["session_id"] for r in store.retention_preview(days)]
        verb = "would delete"
    typer.echo(f"{verb} {len(targets)} session(s)")
    for sid in targets:
        typer.echo(f"  {sid}")


@app.command("install-daemon")
def install_daemon(uninstall: bool = typer.Option(False, "--uninstall")) -> None:
    """Opt-in: install a logon-time worker daemon (alternative to
    session-following model).

    Default flow uses `forge process-now` on SessionEnd (transient,
    progress bar, exits when done). Use this only if you want the
    worker running continuously in the background at all times.
    """
    from .worker_service import install as _install, uninstall as _uninstall

    try:
        if uninstall:
            result = _uninstall()
            typer.echo(f"removed launcher at {result['launcher']}")
        else:
            result = _install()
            typer.echo(f"installed launcher at {result['launcher']}")
    except Exception as exc:
        typer.echo(f"failed: {exc}")
        raise typer.Exit(code=1)


@app.command("scan")
def scan_cmd() -> None:
    """Rebuild the FTS index and scan for rule frontmatter feedback."""
    result = _vault().scan()
    typer.echo(f"indexed {result['files']} documents; index.md generated")


@app.command("watch")
def watch_cmd(daemon: bool = typer.Option(False, "--daemon"),
               interval: float = typer.Option(2.0, "--interval", min=0.5)) -> None:
    """Watch the vault for file changes and auto-rebuild the index.

    `--daemon` uses OS-native file notifications (watchdog). Falls
    back to mtime polling without it.
    """
    import time
    vault = _vault()
    last_mtime = 0.0

    if daemon:
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler
        except ImportError:
            typer.echo("watchdog not installed; try `pip install watchdog`")
            raise typer.Exit(code=1)

        class Handler(FileSystemEventHandler):
            def on_modified(self, event):
                if event.src_path.endswith(".md"):
                    vault.scan()
                    typer.echo(f"re-indexed ({event.src_path})")

        observer = Observer()
        observer.schedule(Handler(), str(vault.root), recursive=True)
        observer.start()
        try:
            while True:
                time.sleep(10)
        except KeyboardInterrupt:
            observer.stop()
        observer.join()
        return

    while True:
        mtime = max((p.stat().st_mtime for p in vault.root.rglob("*.md")
                     if p.name != "index.md"), default=0.0)
        if mtime > last_mtime:
            vault.scan()
            last_mtime = mtime
        time.sleep(interval)


# -- escape hatches ------------------------------------------------------


@app.command("provider-check")
def provider_check() -> None:
    """Ping the configured model provider and report reachability."""
    from .provider_http import GatewayError

    settings = load_settings()
    provider = settings.model_provider if settings else "none"
    if provider not in {"remote", "local"}:
        typer.echo(f"provider={provider}; no live check needed")
        return
    try:
        gateway = select_gateway(provider, settings)
    except GatewayError as exc:
        typer.echo(f"provider={provider} FAILED to construct: {exc}")
        raise typer.Exit(code=1)
    try:
        gateway._post(  # type: ignore[attr-defined]
            url=gateway.api_url,  # type: ignore[attr-defined]
            headers={"content-type": "application/json"},
            body={"model": gateway.model,  # type: ignore[attr-defined]
                  "max_tokens": 8,
                  "messages": [{"role": "user", "content": "ping"}]},
        )
    except GatewayError as exc:
        typer.echo(f"provider={provider} UNREACHABLE: {exc}")
        raise typer.Exit(code=1)
    typer.echo(f"provider={provider} OK ({gateway.name})")


@app.command("import-transcript")
def import_transcript(path: Path,
                       project: str = typer.Option("", "--project"),
                       session_id: str | None = typer.Option(None, "--session")) -> None:
    """Backfill an old transcript into the queue without a live hook."""
    import hashlib

    if not path.exists():
        raise typer.BadParameter(f"transcript not found: {path}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    from .domain import EventType, SessionEvent

    cwd = str(path.parent)
    settings = load_settings()
    proj = project or (settings.root.name if settings else path.parent.name)
    sid = session_id or f"import-{digest[:12]}"
    event = SessionEvent(
        event_id=f"evt-import-{digest[:12]}",
        event_type=EventType.SESSION_END,
        source="import",
        session_id=sid,
        project=proj,
        cwd=cwd,
        transcript_path=str(path),
        transcript_hash=digest,
    )
    queued = _store().enqueue_event(event)
    typer.echo("queued" if queued else "duplicate")


@app.command("session-info")
def session_info_cmd(session_id: str) -> None:
    """Show the full state of a single session for debugging."""
    import json

    info = _store().session_info(session_id)
    if not info:
        raise typer.BadParameter(f"session not found: {session_id}")
    typer.echo(json.dumps(info, indent=2, ensure_ascii=False))


@app.command("jobs")
def jobs(limit: int = typer.Option(50, "--limit", min=1),
          status_filter: str = typer.Option("", "--status")) -> None:
    """List jobs filtered by status (debug only)."""
    rows = _store().list_jobs()
    for row in rows[:limit]:
        if status_filter and row["status"] != status_filter:
            continue
        typer.echo(f"{row['id']}\t{row['status']}\tattempts={row['attempts']}\t{row['last_error'] or ''}")


@app.command("jobs-retry")
def jobs_retry(job_id: str) -> None:
    """Reset a failed or dead-letter job back to queued."""
    if _store().retry_job(job_id):
        typer.echo(f"requeued {job_id}")
    else:
        typer.echo(f"job not found or not in failed/dead_letter: {job_id}")
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
