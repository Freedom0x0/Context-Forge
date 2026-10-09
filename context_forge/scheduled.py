"""Daily maintenance steps, run by the transcript watcher.

Every step is accounted for on its own in `scheduled_runs`: a failing step is
recorded and skipped, it never rolls back or blocks the others. One transaction
per step is deliberate -- the steps touch different data planes (library content,
aggregates, deletions), and a shared transaction would let a later failure undo a
real content change.

Writes made from here must derive idempotency keys from the *candidate identity*
(`hygiene:exact:{a}:{b}@{version}`), never from the date or the run number, or the same pair
would be re-applied every day.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable

from .memory import MemoryStore
from .queue import JobStore

DAILY_SECONDS = 86400


@dataclass(frozen=True)
class Context:
    """What a step may touch. Steps get the queue always and memory when it exists."""

    queue: JobStore
    memory: MemoryStore | None = None


def _retention(ctx: Context) -> None:
    from .config import load_settings

    settings = load_settings()
    days = settings.retention_days if settings else 30
    ctx.queue.retention_apply(days, dry_run=False)


def _month_keys(today: date | None = None) -> tuple[str, str]:
    """Current and previous month, as 'YYYY-MM'."""
    today = today or date.today()
    previous = (today.replace(day=1) - timedelta(days=1)).strftime('%Y-%m')
    return today.strftime('%Y-%m'), previous


def _signals(ctx: Context) -> None:
    """Roll the aggregate forward, then let it license pruning raw events.

    Recomputing two whole months is cheaper than tracking deltas and is idempotent,
    so a crash mid-step is fixed by the next run rather than leaving half-counted data.
    """
    if ctx.memory is None:
        return
    for month in _month_keys():
        ctx.memory.aggregate_signals(month)
    ctx.memory.prune_retrieval_events()


def _hygiene(ctx: Context) -> None:
    """Canonicalize exact duplicates and note contradictions: the two unattended tiers.

    G3 and G4 stay proposals. The pass is capped, and a cap that bit is printed rather than
    swallowed, because a library with more duplicates than one pass can take is exactly the
    situation where silence would look like health.
    """
    if ctx.memory is None:
        return
    from .memory_lifecycle import MemoryLifecycle

    actions = MemoryLifecycle(ctx.memory).apply_auto_tier()
    changed = actions['exact_applied'] + actions['conflicts_annotated']
    deferred = actions['exact_deferred'] + actions['conflicts_deferred']
    if changed or deferred:
        print(f'[scheduled] hygiene archived {actions["exact_applied"]} duplicate(s), '
              f'noted {actions["conflicts_annotated"]} contradiction(s), '
              f'deferred {deferred} for the next pass')


STEPS: dict[str, Callable[[Context], None]] = {
    'retention': _retention, 'signals': _signals, 'hygiene': _hygiene}

# Steps that have nothing to do without the memory database. Skipping is not the same as
# succeeding: recording `signals` as run while no memory.db existed would make a silent
# gap look like a healthy ledger.
MEMORY_STEPS = frozenset({'signals', 'hygiene'})


def run_due(store: JobStore, now: datetime | None = None, *,
            period_seconds: int = DAILY_SECONDS,
            steps: dict[str, Callable[[Context], None]] | None = None,
            memory_store: MemoryStore | None = None) -> list[str]:
    """Run every overdue step once and return the names that succeeded."""
    ctx = Context(store, memory_store)
    steps = steps if steps is not None else STEPS
    moment = now or datetime.now().astimezone()
    executed: list[str] = []
    for name, step in steps.items():
        if name in MEMORY_STEPS and memory_store is None:
            continue
        if not store.scheduled_due(name, moment, period_seconds):
            continue
        try:
            step(ctx)
        except Exception as exc:  # noqa: BLE001 - one step must not stop the rest
            store.record_scheduled_failure(name, moment, f'{type(exc).__name__}: {exc}')
            print(f'[scheduled] {name} failed: {type(exc).__name__}: {exc}')
            continue
        store.record_scheduled_success(name, moment)
        executed.append(name)
    return executed
