"""Concurrent claims must hand each job to exactly one process.

Windows starts child processes by re-importing the module, so the database path
travels through the environment instead of a module-level temporary directory.
"""

import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from multiprocessing import Process, Queue
from pathlib import Path

from context_forge.queue import JobStore

CLAIM_PROBE_HOME = 'CLAIM_PROBE_HOME'


def _moment() -> str:
    return datetime.now(timezone.utc).isoformat()


def seed(db: Path, jobs: int) -> None:
    store = JobStore(db)
    moment = _moment()
    with store.db:
        for index in range(jobs):
            store.db.execute(
                "INSERT INTO jobs(id,kind,payload_json,idempotency_key,status,attempts,"
                "available_at,created_at,updated_at) VALUES (?,?,?,?,'queued',0,?,?,?)",
                (f'job-{index}', 'session_end', '{}', f'key-{index}', moment, moment, moment))
    store.db.close()


def claim_worker(out: Queue, per_process: int) -> None:  # noqa: ARG001 - Queue typed at call site
    db = Path(os.environ[CLAIM_PROBE_HOME]) / 'queue.db'
    store = JobStore(db)
    claimed: list[str] = []
    try:
        for _ in range(per_process * 6):
            if len(claimed) >= per_process:
                break
            row = store.claim(lease_seconds=600)
            if row is None:
                continue
            claimed.append(row['id'])
        out.put({'claimed': claimed})
    except Exception as exc:  # noqa: BLE001 - report instead of failing silently
        out.put({'error': f'{type(exc).__name__}: {exc}'})


def test_concurrent_claims_never_hand_out_the_same_job(tmp_path, monkeypatch):
    monkeypatch.setenv(CLAIM_PROBE_HOME, str(tmp_path))
    monkeypatch.setenv('CONTEXT_FORGE_HOME', str(tmp_path))
    processes, per_process = 3, 15
    jobs = processes * per_process              # demand exactly equals supply: nothing is left over
    seed(tmp_path / 'queue.db', jobs)
    out: Queue = Queue()

    workers = [Process(target=claim_worker, args=(out, per_process)) for _ in range(processes)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    results = []
    while not out.empty():
        results.append(out.get())
    assert not [item for item in results if 'error' in item], results
    claimed = [job for item in results for job in item['claimed']]

    assert len(claimed) == processes * per_process      # every process got its share
    assert len(set(claimed)) == len(claimed)            # and no job was claimed twice
    with sqlite3.connect(tmp_path / 'queue.db') as connection:
        running = connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE status='running'").fetchone()[0]
    assert running == len(claimed)
