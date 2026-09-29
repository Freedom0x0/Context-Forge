"""Daily maintenance steps, run by the transcript watcher.

Every step is accounted for on its own in `scheduled_runs`: a failing step is
recorded and skipped, it never rolls back or blocks the others. One transaction
per step is deliberate -- the steps touch different data planes (library content,
aggregates, deletions), and a shared transaction would let a later failure undo a
real content change.

Writes made from here must derive idempotency keys from the *candidate identity*
(`daily:g1:{a}:{b}`), never from the date or the run number, or the same pair
would be re-applied every day.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from .queue import JobStore

DAILY_SECONDS = 86400


def _retention(store: JobStore) -> None:
    from .config import load_settings

    settings = load_settings()
    days = settings.retention_days if settings else 30
    store.retention_apply(days, dry_run=False)


STEPS: dict[str, Callable[[JobStore], None]] = {'retention': _retention}


def run_due(store: JobStore, now: datetime | None = None, *,
            period_seconds: int = DAILY_SECONDS,
            steps: dict[str, Callable[[JobStore], None]] | None = None) -> list[str]:
    """Run every overdue step once and return the names that succeeded."""
    steps = steps if steps is not None else STEPS
    moment = now or datetime.now().astimezone()
    executed: list[str] = []
    for name, step in steps.items():
        if not store.scheduled_due(name, moment, period_seconds):
            continue
        try:
            step(store)
        except Exception as exc:  # noqa: BLE001 - one step must not stop the rest
            store.record_scheduled_failure(name, moment, f'{type(exc).__name__}: {exc}')
            print(f'[scheduled] {name} failed: {type(exc).__name__}: {exc}')
            continue
        store.record_scheduled_success(name, moment)
        executed.append(name)
    return executed
