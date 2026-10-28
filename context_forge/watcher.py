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
import os
import time
from pathlib import Path

from .runtime_log import log_path, report
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

# Working directories that belong to a tool, not to the user's work.
#
# `~/.claude/projects/` is keyed by working directory, and a sibling tool that is
# itself a Claude Code client files its own sessions in the same tree: claude-mem
# runs its compressor as a Claude Code session under
# `~/.claude-mem/observer-sessions`, so 304 of this machine's 346 transcripts were
# claude-mem's internal logs. Precipitating those writes the tool's own prompts
# into the user's library and spends a model call per file. claaude-mem excludes
# the same folder from its own import (`src/shared/should-track-project.ts`).
EXCLUDED_SESSION_DIRS = frozenset({'.claude-mem', '.claude'})


def excluded_cwd(cwd: str) -> bool:
    """True when the session ran in a tool's own directory rather than a project."""
    return any(part in EXCLUDED_SESSION_DIRS for part in Path(cwd).parts)


def liveness_evidence(*, process_alive: bool | None = None,
                      active_tools: bool | None = None) -> tuple[bool | None, bool | None]:
    """Normalize host-probe values without treating missing evidence as false.

    The current Claude transcript watcher has no session-scoped process/tool
    probe. Generic host integrations may supply both values explicitly; until
    then, the safe default is unknown and inferred finalization remains blocked.
    """
    return (
        process_alive if isinstance(process_alive, bool) else None,
        active_tools if isinstance(active_tools, bool) else None,
    )


def _session_process_alive(session_id: str, store: JobStore) -> bool | None:
    """Check the exact process registered by this session's SessionStart hook."""
    from .hooks.liveness import process_alive_for_session
    return process_alive_for_session(session_id, store)


def probe_session_liveness(transcript: Path, session_id: str, store: JobStore, *,
                           now: float | None = None,
                           ttl_seconds: float = 86400.0) -> tuple[bool | None, bool | None]:
    """Use exact PID identity and fresh hook-maintained tool evidence.

    Transcript shape is deliberately ignored: a quiet file cannot distinguish
    an idle turn from a tool call that has not yet produced a result.
    """
    process_alive = _session_process_alive(session_id, store)
    state = store.session_liveness(session_id)
    if state is None:
        return process_alive, None
    observed = float(state["observed_at"])
    current = time.time() if now is None else now
    if current - observed > ttl_seconds:
        return process_alive, None
    tools = state["active_tools"]
    return process_alive, bool(tools) if isinstance(tools, bool) else None


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


def read_session_meta(transcript: Path) -> tuple[str, str, str] | None:
    """Return (session_id, project, cwd) for a transcript, or None.

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
                    return transcript.stem, Path(cwd).name, str(cwd)
    except OSError:
        return None
    return None


def scan_once(store: JobStore, vault: Vault, gateway, *,
              settle_seconds: float = DEFAULT_SETTLE,
              max_attempts: int = 3,
              baseline: float | None = None,
              now: float | None = None,
              memory_store=None,
              process_alive: bool | None = None,
              active_tools: bool | None = None) -> int:
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
    session_liveness: dict[str, tuple[bool | None, bool | None]] = {}
    for transcript in sorted(root.glob("*/*.jsonl")):
        try:
            mtime = transcript.stat().st_mtime
        except OSError:
            store.record_scan_failure(HOST, 'unreadable')
            continue
        if mtime < floor:
            continue  # predates the watcher; never replay history
        if current - mtime < settle_seconds:
            continue  # still being written, or user is mid-session
        total = count_complete_lines(transcript)
        session_id = transcript.stem
        process, tools = probe_session_liveness(transcript, session_id, store,
                                                now=current)
        session_liveness[session_id] = (process, tools)
        episode = store.latest_episode(session_id)
        collected = episode.collected_through_line if episode else 0
        if total <= collected:
            if total == 0:
                # No complete line ever: a host that changed its transcript
                # format looks exactly like this, and like "user stopped".
                store.record_scan_failure(HOST, 'empty')
            continue
        meta = read_session_meta(transcript)
        if meta is None:
            store.record_scan_failure(HOST, 'no-meta')
            continue
        _, project, cwd = meta
        if excluded_cwd(cwd):
            continue
        from .episodes import EpisodeCollector
        EpisodeCollector(store).collect(session_id, transcript, project, HOST)
        new_activity = True
        processed += 1
    # Probe active episodes even when their transcript has no new lines.  This
    # is what lets a quiet, exited Claude process eventually be inferred closed.
    known = {tx.stem for tx in root.glob("*/*.jsonl")}
    for episode in store.active_episodes():
        if episode.session_id in known:
            tx = Path(episode.transcript_path)
            if tx.is_file():
                process, tools = probe_session_liveness(
                    tx, episode.session_id, store, now=current)
                session_liveness[episode.session_id] = (process, tools)
    # Sessions nobody reported the end of only close here; every pass is cheap
    # because the timestamp gate runs before any file is read.
    from .episodes import EpisodeCollector
    alive, tools_active = liveness_evidence(
        process_alive=process_alive, active_tools=active_tools)
    EpisodeCollector(store).advance_inferred(
        new_activity=new_activity, process_alive=alive, active_tools=tools_active,
        liveness_by_session=session_liveness)
    return processed


class WatcherAlreadyRunning(RuntimeError):
    """Another process already holds the watcher lock for this runtime dir."""

    def __init__(self, pid: int | None) -> None:
        self.pid = pid
        super().__init__(f"已有实例在运行（pid={pid if pid is not None else '未知'}）")


def lock_path() -> Path:
    from .config import runtime_dir
    return runtime_dir() / "watcher.lock"


def _read_pid(path: Path) -> int | None:
    # Byte 0 is the locked region; on Windows a read there is denied, so the
    # writer puts the pid from offset 1 and we read from there too.
    try:
        handle = os.fdopen(os.open(path, os.O_RDWR), "r+b", buffering=0)
    except OSError:
        return None
    try:
        handle.seek(1)
        return int(handle.read().strip())
    except (OSError, ValueError):
        return None
    finally:
        handle.close()


def _lock_byte(handle) -> None:
    """msvcrt.locking locks a byte range, so byte 0 must exist."""
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"0")


def _write_pid(handle) -> None:
    handle.seek(1)
    handle.write(str(os.getpid()).ljust(20).encode("utf-8"))


def _try_lock(handle) -> None:
    """Take an exclusive lock on byte 0; raise OSError if already held."""
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class WatcherLock:
    """Exclusive, per-runtime-dir lock held for the process's lifetime.

    The lock is a real file handle, not a pid file: the OS releases it when
    the process dies, so crashes leave no stale lock to guess about.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or lock_path()
        self._handle = None

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = os.fdopen(
            os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600), "r+b", buffering=0)
        try:
            _lock_byte(handle)
            _try_lock(handle)
        except OSError as exc:  # held by another process
            holder = _read_pid(self._path)
            handle.close()
            raise WatcherAlreadyRunning(holder) from exc
        _write_pid(handle)
        self._handle = handle

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            _unlock(handle)
        except OSError:
            pass
        finally:
            handle.close()


def lock_holder_pid() -> int | None:
    """Pid currently holding the lock, or None when nobody holds it.

    A leftover lock file with no live holder reads as None — the file is not
    the lock, the open handle is. Probes by trying to take the lock itself.
    """
    path = lock_path()
    if not path.exists():
        return None
    try:
        handle = os.fdopen(os.open(path, os.O_RDWR), "r+b", buffering=0)
    except OSError:
        return None
    try:
        try:
            _try_lock(handle)
        except OSError:
            return _read_pid(path)  # someone else holds it
        _unlock(handle)             # free: hand it straight back
        return None
    finally:
        handle.close()


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


def run(store: JobStore, vault: Vault, gateway=None, *,
        interval_seconds: float = DEFAULT_INTERVAL,
        settle_seconds: float = DEFAULT_SETTLE,
        max_attempts: int | None = None,
        memory_store=None) -> None:
    """Poll forever. Ctrl+C / SIGTERM exits cleanly.

    One resident instance per runtime dir: a second launch fails loudly with
    the holder's pid and a non-zero exit instead of running in parallel. Both
    `forge watch-transcripts` and the VBS launcher reach this same acquire.
    """
    import signal

    lock = WatcherLock()
    try:
        lock.acquire()
    except WatcherAlreadyRunning as exc:
        # A duplicate launcher retries every 30 seconds: print each time, log hourly.
        report(f"[watcher] {exc}", dedupe_seconds=3600)
        raise SystemExit(1)

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

    report(f"[watcher] watching {transcript_root()} "
           f"(every {interval_seconds:g}s, settle {settle_seconds:g}s); "
           f"log {log_path()}")
    try:
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
                    report(f"[watcher] scan error: {type(exc).__name__}: {exc}")
                try:
                    from . import scheduled
                    for name in scheduled.run_due(store, memory_store=memory_store):
                        report(f"[scheduled] {name} ran")
                except Exception as exc:  # noqa: BLE001
                    report(f"[scheduled] ledger error: {type(exc).__name__}: {exc}")
                time.sleep(interval_seconds)
        except KeyboardInterrupt:
            report("[watcher] interrupted; exiting")
    finally:
        lock.release()
