# Memory-store verification

Date: 2026-09-22.

## Delivered boundary

The task adds a standalone SQLite memory foundation. It does not switch the current worker, vault, CLI or MCP runtime to `memory.db`, does not migrate live user data, and does not claim that the Agent learning loop is complete.

## Acceptance evidence

- Explicit schema initialization, read-only noncreating opens and unsupported-schema failures are covered.
- Version writes use optimistic concurrency and idempotency request hashes. Injected index failures roll back versions, sources, scopes, events and FTS together.
- Sources are immutable by identity; applicability preserves true/false/unknown; relations pin endpoint versions and can be revoked without deleting history.
- Search covers Chinese bigrams, ASCII normalization, literal FTS syntax, project/global/path visibility, discard and suppressed filtering, byte budgets and stable ordering.
- FTS can be dropped and transactionally rebuilt from authoritative records.
- Legacy preview/import is read-only at the source, preserves file bytes and mtime, keeps disabled/user-owned protection, namespaces session-local evidence, derives project scopes from rules, creates unambiguous rule-to-knowledge relations, and prevents stale snapshots from overwriting corrections.
- Legacy feedback is read without constructing `JobStore`; explicit rows remain distinct and scan-origin duplicates use source-aware idempotency without becoming verified evidence.
- SQLite backup/restore preserves versions, events, sources, relationships and search results. Markdown export is deterministic, atomic, traversal-safe and rejects symlink files/directories.
- Independent review found disabled-relation revocation and symlink-directory export gaps. Both were fixed with regression tests.

## Commands

`C:/Users/15532/AppData/Local/Programs/Python/Python312/python.exe -m pytest tests/test_memory_store.py tests/test_memory_import.py -q`

Result: 34 passed in 9.14s.

`C:/Users/15532/AppData/Local/Programs/Python/Python312/python.exe -m pytest tests/ -q`

Result: 80 passed in 17.89s.

`C:/Users/15532/AppData/Local/Programs/Python/Python312/python.exe -m compileall -q context_forge tests`

Result: exit 0.

`git diff --check`

Result: exit 0.

No project linter or static type-check configuration was found, so none was invented for this task.

## Baseline note

Before memory-store code existed, the first full run had 45 passed and one intermittent watcher failure. The isolated watcher test then passed. The final full run passed all 80 tests without modifying watcher behavior.
