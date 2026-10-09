"""Top-level CLI.

After the auto-loop refactor, only 6 commands remain for daily use:

  forge init <vault-path>           first-time setup
  forge install-hook [--uninstall] wire Claude Code hooks
  forge worker run [--once]         manual / debug trigger (daemon is the default)
  forge status [--json]             one-line state peek
  forge doctor                      health check
  forge retention --apply [--days N]  manual cleanup

Everything else is either:
- a diagnostic / debug tool (`session-info`, `jobs`, `rule-match`, `search`)
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
from .index import iter_vault_documents
from .queue import JobStore
from .vault import Vault
from .worker import process_one, run_loop

app = typer.Typer(no_args_is_help=True)
worker_app = typer.Typer(no_args_is_help=True)
event_app = typer.Typer(no_args_is_help=True)
session_app = typer.Typer(no_args_is_help=True)
app.add_typer(event_app, name="event")
app.add_typer(session_app, name="session")
app.add_typer(worker_app, name="worker")


def _store() -> JobStore:
    from .config import runtime_dir
    return JobStore(runtime_dir() / "queue.db")


def _memory():
    from .config import runtime_dir
    from .memory import MemoryStore
    path = runtime_dir() / "memory.db"
    return MemoryStore(path, readonly=False) if path.exists() else None


def _vault() -> Vault:
    settings = load_settings()
    if settings:
        return Vault(settings.root)
    from .config import runtime_dir
    return Vault(runtime_dir() / "vault")


@event_app.command("ingest")
def event_ingest() -> None:
    """Ingest one generic lifecycle event as JSON from stdin."""
    import json
    import sys
    from .domain import SessionLifecycleEvent
    from .episodes import EpisodeCollector

    event = SessionLifecycleEvent.model_validate(json.load(sys.stdin))
    store = _store()
    result = store.ingest_session_event(event)
    collector = EpisodeCollector(store)
    collector.collect(event.session_id, Path(event.transcript_path), None, event.source)
    if event.type == "pre_compact":
        collector.checkpoint(event)
    else:
        collector.request_finalize(event.session_id, end_kind="explicit", event_id=event.event_id)
    typer.echo(result)


@session_app.command("finalize")
def session_finalize(session_id: str) -> None:
    """Explicitly close the latest collected segment for a session."""
    import uuid
    from .episodes import EpisodeCollector

    receipt = EpisodeCollector(_store()).request_finalize(
        session_id, end_kind="manual", event_id=f"manual:{uuid.uuid4()}",
    )
    typer.echo(receipt.finalization_id)


@session_app.command("list")
def session_list() -> None:
    for episode in _store().list_episodes():
        typer.echo(f"{episode.session_id}\t{episode.segment}\t{episode.state}\t{episode.last_activity_at}")


@session_app.command("inspect")
def session_inspect(session_id: str) -> None:
    import json
    store = _store()
    episodes = [item.model_dump(mode="json") for item in store.list_episodes()
                if item.session_id == session_id]
    if not episodes:
        raise typer.BadParameter(f"session not found: {session_id}")
    typer.echo(json.dumps({"episodes": episodes,
                          "checkpoints": [item.model_dump(mode="json") for item in
                                          store.list_checkpoints(session_id)]},
                         indent=2, ensure_ascii=False))


@session_app.command("delete")
def session_delete(session_id: str) -> None:
    from .episodes import EpisodeCollector
    counts = EpisodeCollector(_store()).delete(session_id, _memory())
    typer.echo(" ".join(f"{key}={value}" for key, value in counts.items() if value))


@app.command("project-memories")
def project_memories() -> None:
    from .memory_projection import MemoryProjector
    memory = _memory()
    if memory is None:
        raise typer.BadParameter("memory.db not initialized")
    results = MemoryProjector(memory, _vault().root).project_pending()
    typer.echo(f"projected {len(results)}")


@app.command("precipitation-migrate")
def precipitation_migrate(apply: bool = typer.Option(False, "--apply"),
                          rollback: str = typer.Option("", "--rollback")) -> None:
    import json
    from .precipitation_migration import PrecipitationMigration
    memory = _memory()
    if memory is None:
        raise typer.BadParameter("memory.db not initialized")
    migration = PrecipitationMigration(memory)
    report = ({"restored": migration.rollback(rollback)} if rollback else
              (migration.apply() if apply else migration.plan()))
    payload = report if isinstance(report, dict) else report.__dict__
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


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
    from .memory import MemoryStore
    from .config import runtime_dir
    MemoryStore.initialize(runtime_dir() / "memory.db")
    typer.echo(f"wrote {cfg_path} (vault = {settings.root})")


@app.command("install-hook")
def install_hook(target: Path = typer.Option(None, "--target"),
                  uninstall: bool = typer.Option(False, "--uninstall"),
                  status: bool = typer.Option(False, "--status"),
                  self_test: bool = typer.Option(False, "--self-test")) -> None:
    """Install or remove the Claude Code SessionEnd/PreCompact/SessionStart hooks."""
    from . import install_hook as installer

    if status:
        import json
        typer.echo(json.dumps(installer.status(target), indent=2))
        return
    if self_test:
        import tempfile
        from .hooks.claude_code import handle_payload
        from .queue import JobStore
        with tempfile.TemporaryDirectory(prefix="forge-hook-test-") as folder:
            root = Path(folder)
            transcript = root / "self-test.jsonl"
            transcript.write_text('{}\n', encoding="utf-8")
            store = JobStore(root / "queue.db")
            try:
                event = handle_payload({"hook_event_name": "PreCompact", "session_id": "self-test",
                                        "transcript_path": str(transcript)}, store=store)
                ok = event is not None and len(store.list_checkpoints("self-test")) == 1
            finally:
                store.db.close()
        typer.echo("hook self-test passed" if ok else "hook self-test failed")
        if not ok:
            raise typer.Exit(1)
        return
    result = installer.uninstall(target) if uninstall else installer.install(target)
    if uninstall:
        typer.echo(f"removed {result['removed']} hook entries at {result['path']}")
    else:
        typer.echo(f"added {result['added']} hook entries at {result['path']}")


@app.command("sync")
def sync_cmd(dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    """Import vault content into memory.db (incremental, idempotent, one-way).

    Reads the vault and writes only memory.db. Re-running with no vault
    change imports nothing.
    """
    from . import runtime_sync

    report = runtime_sync.sync(_vault().root, dry_run=dry_run)
    typer.echo(f"created {report.created}, updated {report.updated}, "
               f"unchanged {report.unchanged}, protected {report.protected}")
    typer.echo(f"vault has {report.vault_documents} documents; "
               f"memory.db has {report.db_records}; snapshot_order {report.snapshot_order}")
    for line in report.diagnostics:
        typer.echo(f"! {line}")


@app.command("mcp-install")
def mcp_install_cmd(uninstall: bool = typer.Option(False, "--uninstall")) -> None:
    """Add or remove the Context Forge MCP server via the host's own CLI.

    Shells out to `claude mcp add/remove` (user scope) rather than writing the
    config file ourselves, so Claude Code owns that file's location (including
    `CLAUDE_CONFIG_DIR`), locking and credential-preservation. Re-running is
    idempotent. A stale entry in the inert `~/.claude/settings.json` is removed
    on a real install.
    """
    from . import mcp_install

    try:
        result = mcp_install.uninstall() if uninstall else mcp_install.install()
    except mcp_install.McpInstallError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1)
    if uninstall:
        typer.echo(f"removed {result['removed']} entry")
    elif result.get("already_installed"):
        typer.echo("context_forge already registered (user scope)")
    elif result.get("refreshed"):
        typer.echo("refreshed context_forge registration (stale entry replaced)")
    else:
        typer.echo("installed context_forge (user scope)")
    if result.get("migrated"):
        typer.echo(f"migrated stale entry out of {result['migrated']}")


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
        processed = process_one(_store(), _vault(), gateway, max_attempts, _memory())
        typer.echo("processed" if processed else "empty")
        return
    if interval <= 0:
        interval = settings.poll_interval_seconds if settings else 2.0
    run_loop(_store(), _vault(), gateway,
             interval_seconds=interval, max_attempts=max_attempts, memory_store=_memory())


@app.command("process-now")
def process_now() -> None:
    """Process queued jobs and write a summary to last_run.md.

    Intended to be run manually: `forge worker run --once`. Exits as
    soon as the queue is drained — no window, no toast, no sleep.
    """
    import sys

    settings = load_settings()
    provider = settings.model_provider if settings else "none"
    gateway = select_gateway(provider, settings)
    max_attempts = settings.max_attempts if settings else 3
    store = _store()
    vault = _vault()
    pending = sum(1 for j in store.list_jobs() if j["status"] == "queued")
    if pending == 0:
        sys.stdout.write("[context-forge] nothing to process\n")
        return
    sys.stdout.write(f"[context-forge] processing {pending} job(s)...\n")
    done = 0
    failures = 0
    while True:
        try:
            settings2 = load_settings()
            p2 = settings2.model_provider if settings2 else "none"
            gw = select_gateway(p2, settings2)
            processed = process_one(store, vault, gw, max_attempts, _memory())
        except Exception as exc:
            sys.stdout.write(f"[error] {type(exc).__name__}: {exc}\n")
            failures += 1
            processed = False
        if processed:
            done += 1
            sys.stdout.write(f"[context-forge] {done}/{pending} done\n")
        else:
            break
    jobs = store.list_jobs()
    still = sum(1 for j in jobs if j["status"] in ("queued", "running"))
    failed = sum(1 for j in jobs if j["status"] in ("failed", "dead_letter"))
    knowledge_count = len(list(vault.root.glob("knowledge/accepted/*.md")))
    rule_count = len(list(vault.root.glob("rules/proposals/*.md")))
    sys.stdout.write(
        f"[context-forge] finished: {done} ok, {failed} failed, "
        f"{still} pending\n"
    )
    sys.stdout.write(
        f"[context-forge] vault: {knowledge_count} knowledge, "
        f"{rule_count} rules\n"
    )

    from pathlib import Path
    from .config import runtime_dir
    home = runtime_dir()
    home.mkdir(parents=True, exist_ok=True)
    (home / "last_run.md").write_text(
        f"# Context Forge last run\n\n"
        f"- {done} session(s) processed\n"
        f"- {failed} failed, {still} pending\n"
        f"- {knowledge_count} knowledge file(s) in vault\n"
        f"- {rule_count} rule(s) in vault\n",
        encoding="utf-8",
    )


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
    """Run read-only diagnostics: config, vault, queue, index, provider,
    watcher, memory, memory-sync and mcp.

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


@app.command("migrate-memory")
def migrate_memory(apply: bool = typer.Option(False, "--apply")) -> None:
    """Split legacy `Scope` into the boundary trio (dry-run unless --apply).

    Rewrites what it does NOT revert: the v1 database is left untouched by
    the dry-run, and `--apply` takes an online backup next to the database
    first. Roll back by restoring that backup file.
    """
    from .config import runtime_dir
    from .memory import MemoryStore, MemoryStoreError

    path = runtime_dir() / "memory.db"
    if not path.exists():
        typer.echo("no memory database; nothing to migrate")
        return
    try:
        report = MemoryStore.migrate(path, dry_run=not apply)
    except MemoryStoreError as exc:
        typer.echo(f"failed: {exc}")
        raise typer.Exit(code=1)
    typer.echo(f"schema {report['from']} -> {report['to']}; "
               f"rewritten {report['rewritten']}, projections {report['projections']}")
    if report['backup']:
        typer.echo(f"backup: {report['backup']}")
    for line in report['diagnostics']:
        typer.echo(f"! {line}")


@app.command("memory-report")
def memory_report(days: int = typer.Option(90, "--days", min=0)) -> None:
    """Export the three P3 effectiveness reports; read-only, no side effects."""
    store = _memory()
    if store is None:
        typer.echo("no memory database")
        raise typer.Exit(code=1)
    from .memory_effectiveness import MemoryEffectiveness
    import json as _json

    typer.echo(_json.dumps(MemoryEffectiveness(store).report(days=days).as_dict(),
                           ensure_ascii=False, indent=2))


@app.command("hygiene")
def hygiene(days: int = typer.Option(90, "--days", min=0),
            min_sources: int = typer.Option(3, "--min-sources", min=1),
            threshold: float = typer.Option(0.9, "--threshold", min=0.0, max=1.0),
            apply: bool = typer.Option(False, "--apply")) -> None:
    """Report hygiene items; --apply runs only the two unattended tiers (G1, G2)."""
    store = _memory()
    if store is None:
        typer.echo("no memory database")
        raise typer.Exit(code=1)
    from .memory_lifecycle import MemoryLifecycle
    import json as _json

    lifecycle = MemoryLifecycle(store)
    if apply:
        actions = lifecycle.apply_auto_tier(merge_threshold=threshold)
        typer.echo(_json.dumps(actions, ensure_ascii=False, indent=2))
        typer.echo(f"exact duplicates archived {actions['exact_applied']}, "
                   f"left for a human {actions['exact_skipped']}, "
                   f"deferred by the cap {actions['exact_deferred']}; "
                   f"contradictions noted {actions['conflicts_annotated']}, "
                   f"already noted {actions['conflicts_skipped']}, "
                   f"deferred {actions['conflicts_deferred']}.")
        if actions['exact_applied']:
            typer.echo("every archive is a versioned event with `reason=exact duplicate "
                       "canonicalization`; there is no restore command yet (it lands with "
                       "`hygiene --review`).")
        return
    report = lifecycle.hygiene_report(days=days, min_sources=min_sources, threshold=threshold)
    typer.echo(_json.dumps(report.as_dict(), ensure_ascii=False, indent=2))


@app.command("retag-vault")
def retag_vault(apply: bool = typer.Option(False, "--apply"),
                output: Path = typer.Option(None, "--report")) -> None:
    """Retag vault documents with role / confidence / applicability tags.

    Dry-run by default and reports the basis for every change. `--apply`
    copies each changed file to `.retag-backup/` first; restore them with
    `forge retag-rollback`.
    """
    from .vault_retag import retag

    report = retag(_vault().root, dry_run=not apply)
    typer.echo(f"{report['documents']} documents; changed {report['changed']}, "
               f"unchanged {report['unchanged']}, paths collapsed {report['paths_collapsed']}")
    for line in report['diagnostics']:
        typer.echo(f"! {line}")
    if report.get('backup'):
        typer.echo(f"backup: {report['backup']}")
    if output:
        import json
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        typer.echo(f"report: {output}")


@app.command("retag-rollback")
def retag_rollback() -> None:
    """Restore vault files from `.retag-backup/` after `forge retag-vault --apply`."""
    from .vault_retag import rollback

    typer.echo(f"restored {rollback(_vault().root)} file(s)")


@app.command("rule-match")
def rule_match(project: str,
               changed_path: str = typer.Option("", "--path"),
               rule_limit: int = typer.Option(8, "--rule-limit", min=0),
               legacy_filter: bool = typer.Option(False, "--legacy-filter")) -> None:
    """Show which rules hit a project, ranked and capped at --rule-limit.

    `--rule-limit 0` lifts the cap. `--legacy-filter` reverts sort + cap
    only: it does not bring back the visibility fixes, so project-level
    rules stay reachable instead of vanishing again.
    """
    from .rules import RuleMatcher

    matches = RuleMatcher(_vault().root).match(
        project, changed_path or None, limit=rule_limit,
        legacy_filter=legacy_filter)
    for match in matches:
        typer.echo(f"- [{match.rule_id}] {match.reason}")
        typer.echo(f"  {match.instruction}")
    typer.echo(f"{len(matches)} rule(s)")


@app.command()
def search(query: str,
           project: str = typer.Option("", "--project"),
           legacy_filter: bool = typer.Option(False, "--legacy-filter")) -> None:
    """Full-text search over the vault (run `forge scan` first).

    `--project` narrows hits to one project. `--legacy-filter` reverts
    that narrowing only: it restores the old cross-project result mix,
    it does not change ranking or the empty-`paths` fix.
    """
    from .index import DocumentIndex

    scoped = None if legacy_filter or not project else project
    for row in DocumentIndex(_vault()._index_path()).search(query, project=scoped):
        typer.echo(f"{row['path']}\t{row['snippet']}")


@app.command("watch-transcripts")
def watch_transcripts(once: bool = typer.Option(False, "--once"),
                       interval: float = typer.Option(15.0, "--interval",
                                                       min=1.0),
                       settle: float = typer.Option(60.0, "--settle",
                                                     min=1.0),
                       all_history: bool = typer.Option(
                           False, "--all-history",
                           help="ignore the baseline and scan every "
                                "transcript you have ever recorded")) -> None:
    """Precipitate from Claude Code transcripts directly.

    Reads ~/.claude/projects/**/*.jsonl instead of relying on Claude
    Code hooks. Those files are outside any other tool's config, so
    this keeps working no matter how often cc-switch rewrites
    ~/.claude/settings.json or which model is active.

    Transcripts touched before the watcher's baseline are ignored, so
    installing never replays your history. `--once` runs a single pass.
    """
    from .watcher import baseline_path, run as watch_run, scan_once

    settings = load_settings()
    provider = settings.model_provider if settings else "none"
    max_attempts = settings.max_attempts if settings else 3
    try:
        gateway = select_gateway(provider, settings)
    except Exception as exc:
        typer.echo(f"gateway unavailable: {exc}")
        raise typer.Exit(code=1)

    baseline = 0.0 if all_history else None
    if all_history:
        typer.echo("[warn] --all-history reprocesses every past transcript")

    if once:
        n = scan_once(_store(), _vault(), gateway, settle_seconds=settle,
                      max_attempts=max_attempts, baseline=baseline, memory_store=_memory())
        typer.echo(f"processed {n} session(s)")
        return
    if baseline is not None:
        from .watcher import reset_baseline
        reset_baseline(baseline)
    typer.echo(f"baseline: {baseline_path()}")
    watch_run(_store(), _vault(), gateway, interval_seconds=interval,
              settle_seconds=settle, max_attempts=max_attempts, memory_store=_memory())


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
        mtime = max((p.stat().st_mtime for p in iter_vault_documents(vault.root)),
                    default=0.0)
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
