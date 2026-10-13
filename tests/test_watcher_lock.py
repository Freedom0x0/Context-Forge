"""Only one resident watcher may hold a given runtime dir's lock.

Mutual exclusion is proven across real processes, not asserted on library
shape: delete the flock/msvcrt branch and the second process's acquire
succeeds, turning this suite red. Windows spawns children by re-importing
this module, so the runtime dir travels through CONTEXT_FORGE_HOME.
"""

import os
import time
from multiprocessing import Process, Queue
from pathlib import Path

from context_forge.watcher import (
    WatcherAlreadyRunning,
    WatcherLock,
    lock_holder_pid,
    lock_path,
)


def hold_worker(out: Queue, hold_seconds: float) -> None:
    lock = WatcherLock()
    try:
        lock.acquire()
    except WatcherAlreadyRunning as exc:
        out.put({"acquired": False, "holder": exc.pid})
        return
    out.put({"acquired": True, "pid": os.getpid()})
    time.sleep(hold_seconds)
    lock.release()


def test_second_instance_fails_and_names_holder(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONTEXT_FORGE_HOME", str(tmp_path))
    out: Queue = Queue()
    first = Process(target=hold_worker, args=(out, 2.0), daemon=True)
    first.start()
    try:
        started = out.get(timeout=15)
        assert started["acquired"] is True       # holder is up

        second = WatcherLock()
        try:
            second.acquire()
            raise AssertionError("second acquire must fail while the holder lives")
        except WatcherAlreadyRunning as exc:
            assert exc.pid == started["pid"]     # failure names the holder
    finally:
        first.join(timeout=15)
    assert not first.is_alive()                  # holder exited; OS dropped its lock

    # The lock is free again once the holder is gone.
    reclaimed = WatcherLock()
    reclaimed.acquire()
    reclaimed.release()


def test_distinct_runtime_dirs_do_not_block(tmp_path: Path) -> None:
    a = WatcherLock(tmp_path / "a" / "watcher.lock")
    b = WatcherLock(tmp_path / "b" / "watcher.lock")
    a.acquire()
    try:
        b.acquire()                              # per-library: independent locks
        b.release()
    finally:
        a.release()


def test_holder_pid_reports_live_holder(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONTEXT_FORGE_HOME", str(tmp_path))
    lock = WatcherLock()
    lock.acquire()
    try:
        assert lock_holder_pid() == os.getpid()
    finally:
        lock.release()


def test_stale_lock_file_is_not_a_holder(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONTEXT_FORGE_HOME", str(tmp_path))
    lock = WatcherLock()
    lock.acquire()
    lock.release()
    assert lock_path().exists()                  # file left on disk...
    assert lock_holder_pid() is None             # ...but nobody holds the lock


def run_worker(out: Queue) -> None:
    from context_forge import watcher
    try:
        watcher.run(None, None, interval_seconds=0.05)
    except SystemExit as exc:
        out.put(("exit", exc.code))
    except BaseException as exc:  # noqa: BLE001 - report instead of vanishing
        out.put(("raised", repr(exc)))
    else:
        out.put(("returned", None))


def test_run_refuses_while_lock_held(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONTEXT_FORGE_HOME", str(tmp_path))
    out: Queue = Queue()
    holder = Process(target=hold_worker, args=(out, 2.0), daemon=True)
    holder.start()
    try:
        assert out.get(timeout=15)["acquired"] is True

        run_out: Queue = Queue()
        runner = Process(target=run_worker, args=(run_out,), daemon=True)
        runner.start()
        try:
            runner.join(timeout=15)
            # A lock-less run() would enter the polling loop and never exit here.
            assert not runner.is_alive(), \
                "run() entered the polling loop despite the lock"
            assert run_out.get_nowait() == ("exit", 1)
        finally:
            if runner.is_alive():
                runner.terminate()
                runner.join(timeout=5)
    finally:
        holder.join(timeout=15)
