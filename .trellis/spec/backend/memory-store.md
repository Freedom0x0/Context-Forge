# Memory Store Contracts

## Scope / Trigger

Applies to the standalone long-term memory foundation in `context_forge/memory*.py`. The existing worker/vault remains active until the later integration task explicitly switches it. Do not initialize or migrate a database as a side effect of a read tool.

## Signatures

- `MemoryStore.initialize(path)` explicitly creates the versioned schema and returns a writable store.
- `MemoryStore(path, readonly=True)` opens an existing compatible database without creating it.
- `put(id, draft, expected_version=..., idempotency_key=..., actor=..., reason=..., relations=...)` atomically persists a revision.
- `get(id, version=None)`, `history(id)`, `events(id)` and `relations(id, ...)` inspect durable state.
- `search(query, project=..., changed_path=None, limit=8, byte_budget=65536, owner=...)` returns visible current records.
- `MemoryStore.migrate(path, dry_run=True)` is the only schema upgrade path besides `initialize`; ordinary opens raise.
- Workflow metrics use attributable ledgers: precipitation confirmation/modification comes from
  explicit queue verdicts, while recall error labels require a matching rule-hit session and
  request-linked feedback. Missing or unlinked labels are excluded from rates.
- `rebuild_fts()`, `backup(target)`, `restore(backup, target)` and `export_markdown(target)` manage derived data and recovery.
- `import_vault(root, feedback_db=None, dry_run=True, namespace=None, snapshot_order=0)` provides explicit legacy migration.

## Contracts

The database is authoritative. Markdown export is a view, not a complete database backup. Use the SQLite backup API for live WAL databases; restore into a new destination.

Each mutation updates the current pointer, version, event, source links, relationships and search projection in one transaction. Model calls must stay outside transactions. A stale expected version must fail rather than overwrite a correction. Idempotency keys identify complete requests, not just memory IDs.

Source identity identifies immutable evidence. Reusing it with different content must fail. Repeated references to one identity are not independent evidence. Applicability is `true`, `false` or `unknown`; unknown is not false or true. Boundary concerns are four separate objects (`MemorySpace`, `AccessPolicy`, `Provenance`, `ApplicabilityScope`); `privacy` and `visibility` are different fields with different enums.

Search normalizes both indexed text and queries, quotes generated FTS terms, orders by BM25 ascending, and applies the four boundary eliminations before returning a limited result set. `project` is provenance and never a default filter; only `AccessPolicy` restricts projects. Direct `get`/history methods are internal storage inspection, not an authorization boundary; user-facing tools must enforce scope separately.

## Validation & Error Matrix

| Input/state | Required behavior |
|---|---|
| Missing/incompatible database | Explicit error, no implicit read-time schema creation |
| Stale expected version | `VersionConflict`, no durable mutation |
| Changed request under reused key | `IdempotencyConflict` |
| Changed evidence under reused source identity | `SourceConflict` |
| Automatic overwrite of protected memory | `ProtectedMemory` |
| Invalid relation target | Transaction rollback |
| Missing/corrupt FTS | Explicit failure until rebuild; never claim there were no matching memories |
| Existing backup target | Refuse overwrite |

## Good / Base / Bad Cases

Good: retry the identical request with the same key; inspect its original revision even if the current memory has advanced.

Base: initialize a temporary database, write an explicitly scoped draft, then query through a read-only handle.

Bad: instantiate the old `JobStore` or call `Vault.scan()` when reading migration input; both can write to the source.

## Tests Required

Use temporary directories for every database/vault. Assert rollback after an injected failure, competing writers, duplicate request semantics, source identity collisions, scope filtering, literal FTS syntax handling, index rebuild, import idempotency and source file hashes/mtime. Verify backup restoration of history and relationships, not only current text.

## Wrong vs Correct

Wrong: copy only the active SQLite main file and call it a complete backup.

Correct: use SQLite's backup API, check the result, restore to a new target and verify versions, events, sources, boundary fields and relationships.

## Folding tri-state conditions: conflict is an *absorbing* state

**Rule**: when consolidating/merging N records' `Applicability(condition, result ∈ {true,false,unknown})` entries for the same condition, **once any pair disagrees the result is `unknown` and stays `unknown`**. A later agreeing record must never revive it.

This shipped as a real defect:

```python
# episodes: true, false, true   (a recorded counterexample sits in the middle)
for ep in episodes:
    if previous is None:      previous = ep.result
    elif previous == ep.result: pass
    elif previous == 'unknown': previous = ep.result     # ← BUG: 3rd episode revives 'true'
    else:                     previous = 'unknown'
# result: 'true' — the counterexample is silently erased
```

**Why it matters**: the whole point of retaining `false` conditions is that they are recorded counterexamples (R1.4). Folding them away means recall later reports a condition as unconditionally true when a contradicting observation exists. That is precisely the "宁可少而真，不要多而假" violation the product exists to prevent — and it is silent.

**Correct**:

```python
conflicted: set[str] = set()
for ep in episodes:
    if condition in conflicted:            # absorbing: never leaves 'unknown'
        continue
    if ep.result == 'unknown':
        conflicted.add(condition)
    elif running is None:
        running = ep.result
    elif running != ep.result:
        conflicted.add(condition)
        running = 'unknown'
```

**Tests required**: `test_consolidation_keeps_a_disputed_condition_unknown` — a `true/false/true` sequence must yield `unknown`. Mutation: drop the `conflicted` guard → RED.

**Generalisation**: any fold over a tri-state (or any lattice with a "don't know" element) must ask *"is the pessimistic state absorbing?"* Min/max folds are naturally absorbing; a "last write wins unless equal" fold is not.

## Mutation records must be reproducible

A guard's mutation note is part of the evidence. Record a mutation that **cannot actually go red** and the note becomes worse than nothing — later readers will assume the guard was verified.

Shipped example: a note claimed "`_origins` using `projects ∪ sessions` → RED". But the fixture's three episodes each had a *distinct* project **and** a distinct session, so `projects`-only, `sessions`-only and the union all counted 3 — every count cleared the `min_sources` threshold and nothing moved. The mutation was tautological.

**Rule**: for each recorded mutation, run it. If it stays green, either the guard is decorative or the **fixture can't distinguish the variants** — and the fix is usually a fixture that makes the variants differ:

```python
# fixture that actually distinguishes "independent origins"
# 3 episodes, SAME project, DIFFERENT sessions  → projects-only counts 1 → proposal suppressed
# 3 episodes, SAME project, SAME session       → union counts 1     → 0 proposals
```

Add that fixture, then re-run the mutation and record what actually happened.
