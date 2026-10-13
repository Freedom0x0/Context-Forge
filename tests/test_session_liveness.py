from __future__ import annotations

import sys
import types

from context_forge.hooks.liveness import process_alive_for_session
from context_forge.queue import JobStore


class FakeProcess:
    def __init__(self, pid, created, *, running=True):
        self.pid = pid
        self.created = created
        self.running = running

    def create_time(self):
        return self.created

    def is_running(self):
        return self.running

    def status(self):
        return "running" if self.running else "zombie"


def install_fake_psutil(monkeypatch, processes):
    fake = types.ModuleType("psutil")
    fake.STATUS_ZOMBIE = "zombie"
    fake.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
    fake.AccessDenied = type("AccessDenied", (Exception,), {})
    fake.ZombieProcess = type("ZombieProcess", (Exception,), {})
    fake.Process = lambda pid: processes[pid]
    monkeypatch.setitem(sys.modules, "psutil", fake)


def test_registered_session_process_is_checked_by_pid_and_creation_time(tmp_path, monkeypatch):
    store = JobStore(tmp_path / "queue.db")
    store.register_session_process("session-a", 321, 100.0)
    processes = {321: FakeProcess(321, 100.0)}
    install_fake_psutil(monkeypatch, processes)

    assert process_alive_for_session("session-a", store) is True
    processes[321] = FakeProcess(321, 101.0)
    assert process_alive_for_session("session-a", store) is False


def test_missing_mapping_or_process_library_is_unknown(tmp_path, monkeypatch):
    store = JobStore(tmp_path / "queue.db")
    assert process_alive_for_session("unknown", store) is None

    store.register_session_process("session-a", 321, 100.0)
    fake = types.ModuleType("psutil")
    fake.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
    fake.AccessDenied = type("AccessDenied", (Exception,), {})
    fake.ZombieProcess = type("ZombieProcess", (Exception,), {})

    def missing(_pid):
        raise fake.NoSuchProcess()

    fake.Process = missing
    fake.STATUS_ZOMBIE = "zombie"
    monkeypatch.setitem(sys.modules, "psutil", fake)
    assert process_alive_for_session("session-a", store) is False
