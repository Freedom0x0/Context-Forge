# Forge precipitation dedup TDD evidence

Date: 2026-09-24

## Scope

This report covers Episode collection, lifecycle hooks, explicit/inferred finalization state, extraction ownership, recoverable operation replay, memory schema v4, DB-authoritative Markdown projection, staged recall families, privacy retention, and exact-only precipitation migration.

## RED/GREEN checkpoints

The implementation started from a `199 passed` baseline. RED tests were added before each implementation slice for Episode behavior, lifecycle events, finalization recovery, projection conflicts, presentation families, privacy deletion, and migration rollback. The committed early checkpoints are `18e99bd`, `8962fc3`, `cdb1ed3`, `6e9df46`, `e1a94ed`, and `774121a`.

Fresh verification:

```text
python -m pytest -q
221 passed in 69.86s

python -m pytest tests/test_memory_projection.py --cov=context_forge.memory_projection --cov-report=term-missing -q
3 passed; memory_projection.py 86%

Selected new-module coverage run
22 passed; aggregate 88%
episodes.py 96%, finalization.py 88%, memory_projection.py 79% before the final keep-DB test, precipitation_migration.py 94%
```

## Fault and migration evidence

- Crash after `memory.db` commit but before queue operation acknowledgement replays the same operation key and does not call the model twice.
- Two concurrent workers use a leased extraction CAS; only one model invocation occurs.
- Projection failure leaves the receipt committed and eligible for projection retry.
- v1/v2/v3 migration uses explicit dry-run and online backup; legacy backup restoration remains covered by `test_memory_boundary.py`.
- Precipitation dry-run leaves database bytes unchanged. Apply archives exact fingerprints only, is batch-idempotent, and rollback restores effective state by adding a version without changing IDs.
- Session deletion redacts both source rows and embedded historical snapshot excerpts, retaining only `source_deleted` tombstones.

## Launch state

Lifecycle hook installation no longer enables watcher autostart. The watcher remains an explicit operator action after `forge doctor`, migration dry-run, provider validation, and the full test gate pass. CodeGraph was unavailable for this implementation because the repository index contained no usable graph generation; discovery used targeted source reads and `rg` as the documented fallback.
