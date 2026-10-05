"""Watch Claude Code transcripts directly — no hooks, no settings.json.

Claude Code appends to `~/.claude/projects/<slug>/<session>.jsonl`
continuously. Those files live outside any other tool's configuration,
so watching them keeps Context Forge working no matter how often
cc-switch rewrites `~/.claude/settings.json` or which model is active.

A transcript is processed only when it
  1. has complete lines past the stored offset, AND
  2. has been untouched for `settle_seconds` (so we never read a
     half-written line, and never hammer the model during active work).

Each pass reuses `worker.process_session`, so the watcher and the
hook-driven path behave identically.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .gateways import NoOpGateway, select_gateway
from .queue import JobStore
from .vault import Vault
from .worker import count_complete_lines

DEFAULT_INTERVAL = 15.0
DEFAULT_SETTLE = 60.0
_CWD_SCAN_LINES = 200


def transcript_root() -> Path:
    return Path.home() / ".claude" / "projects"


# A host adapter is this pair: the root to scan and the name to record. Adding a
# host must not mean adding another collection pipeline.
HOST = "claude_code"


def baseline_path() -> Path:
    from .config import runtime_dir
    return runtime_dir() / "watcher-baseline"


def ensure_baseline() -> float:
    """Epoch before which transcripts are ignored.

    Without this the watcher would replay the user's entire Claude Code
    history on first run — expensive and surprising. Created on first
    use, so precipitation starts from the moment the watcher appears.
    """
    p = baseline_path()
    if p.exists():
        try:
            return float(p.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            pass
    now = time.time()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(now), encoding="utf-8")
    except OSError:
        pass
    return now


def reset_baseline(when: float | None = None) -> float:
    """Move the baseline (0 = re-scan everything from now on)."""
    value = time.time() if when is None else when
    p = baseline_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(value), encoding="utf-8")
    return value


def read_session_meta(transcript: Path) -> tuple[str, str] | None:
    """Return (session_id, project) for a transcript, or None.

    `cwd` lives on ordinary records, not on the leading bookkeeping
    ones, so we scan a bounded prefix. The project name is the
    directory whose work the session belongs to.
    """
    try:
        with transcript.open("r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= _CWD_SCAN_LINES:
                    break
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                cwd = rec.get("cwd")
                if cwd:
                    return transcript.stem, Path(cwd).name
    except OSError:
        return None
    return None


def scan_once(store: JobStore, vault: Vault, gateway, *,
              settle_seconds: float = DEFAULT_SETTLE,
              max_attempts: int = 3,
              baseline: float | None = None,
              now: float | None = None,
              memory_store=None) -> int:
    """Process every transcript that has settled with new content.

    Transcripts last touched before `baseline` are ignored, so a fresh
    install never replays history. Returns the number of sessions
    actually processed.
    """
    root = transcript_root()
    if not root.exists():
        return 0
    current = time.time() if now is None else now
    floor = ensure_baseline() if baseline is None else baseline
    processed = 0
    new_activity = False
    for transcript in sorted(root.glob("*/*.jsonl")):
        try:
            mtime = transcript.stat().st_mtime
        except OSError:
            continue
        if mtime < floor:
            continue  # predates the watcher; never replay history
        if current - mtime < settle_seconds:
            continue  # still being written, or user is mid-session
        total = count_complete_lines(transcript)
        session_id = transcript.stem
        episode = store.latest_episode(session_id)
        if episode is None:
            new_activity = True
        collected = episode.collected_through_line if episode else 0
        if total <= collected:
            continue
        meta = read_session_meta(transcript)
        if meta is None:
            continue
        _, project = meta
        from .episodes import EpisodeCollector
        EpisodeCollector(store).collect(session_id, transcript, project, HOST)
        processed += 1
    # Sessions nobody reported the end of only close here; every pass is cheap
    # because the timestamp gate runs before any file is read.
    from .episodes import EpisodeCollector
    EpisodeCollector(store).advance_inferred(new_activity=new_activity)
    return processed


def heartbeat_path() -> Path:
    from .config import runtime_dir
    return runtime_dir() / "watcher-heartbeat"


def _beat() -> None:
    """Touch the heartbeat so `forge doctor` can tell we're alive."""
    import os
    try:
        p = heartbeat_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"pid": os.getpid(), "at": time.time()}), encoding="utf-8")
    except OSError:
        pass


def _sync_after_pass(vault_root: Path) -> None:
    """Mirror vault changes into memory.db after a pass that wrote documents.

    `runtime_sync.sync` owns the failure marker (clear on success, record on
    failure). Here a raised sync is printed — never swallowed — and never stops
    the watcher loop (the next pass retries).
    """
    from . import runtime_sync

    try:
        runtime_sync.sync(vault_root, dry_run=False)
    except Exception as exc:  # noqa: BLE001
        print(f"[watcher] memory sync failed: {type(exc).__name__}: {exc}")


def run(store: JobStore, vault: Vault, gateway=None, *,
        interval_seconds: float = DEFAULT_INTERVAL,
        settle_seconds: float = DEFAULT_SETTLE,
        max_attempts: int | None = None,
        memory_store=None) -> None:
    """Poll forever. Ctrl+C / SIGTERM exits cleanly."""
    import signal

    def _sigterm(signum, frame):  # noqa: ARG001
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, _sigterm)
    except (AttributeError, ValueError):
        pass

    if max_attempts is None:
        from .config import load_settings
        settings = load_settings()
        max_attempts = settings.max_attempts if settings else 3

    print(f"[watcher] watching {transcript_root()} "
          f"(every {interval_seconds:g}s, settle {settle_seconds:g}s)")
    try:
        while True:
            _beat()
            processed = 0
            try:
                processed = scan_once(store, vault, gateway,
                                      settle_seconds=settle_seconds,
                                      max_attempts=max_attempts,
                                      memory_store=memory_store)
                if memory_store is not None:
                    from .memory_projection import MemoryProjector
                    from .worker import process_pending_finalizations
                    process_pending_finalizations(
                        store, gateway or NoOpGateway(), memory_store,
                        MemoryProjector(memory_store, vault.root),
                    )
            except Exception as exc:  # noqa: BLE001
                print(f"[watcher] scan error: {type(exc).__name__}: {exc}")
            try:
                from . import scheduled
                for name in scheduled.run_due(store):
                    print(f"[scheduled] {name} ran")
            except Exception as exc:  # noqa: BLE001
                print(f"[scheduled] ledger error: {type(exc).__name__}: {exc}")
            time.sleep(interval_seconds)
    except KeyboardInterrupt:
        print("[watcher] interrupted; exiting")
