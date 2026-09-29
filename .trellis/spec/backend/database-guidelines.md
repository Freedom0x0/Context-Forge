# Database Guidelines

> Database patterns and conventions for this project.

---

## Overview

- **Engine**: SQLite (stdlib `sqlite3`), no ORM. Every operation opens and closes its own connection.
- **Two databases**, different authority levels:

| DB | Role | Authority |
|----|------|-----------|
| `~/.context-forge/memory.db` | Long-term memory (versions, sources, relations, retrieval events) | **Authoritative** |
| `~/.context-forge/index-*.db`, `queue.db` | FTS index, job queue | **Derived** — rebuildable, never authoritative |

- **Authoritative representation** is `memory_versions.snapshot_json` (a full `MemoryDraft` snapshot). Anything needed for fast filtering/sorting goes into a **rebuildable projection table**, never into a second authoritative JSON column.
- Schema version is `PRAGMA user_version`. `MemoryStore.initialize` / explicit `migrate` is the **only** way to change schema. Opening an ordinary connection against a stale/unrelated DB raises `MemoryStoreError` — it never migrates implicitly.

---

## Query Patterns

### Scenario: FTS5 `snippet()` column reference

#### 1. Scope / Trigger

Infra integration (SQLite FTS5). Any call to `snippet()` / `highlight()` / `offsets()`.

#### 2. Signatures

```python
# sqlite3 — FTS5 auxiliary functions
snippet(fts_table, column_index: int, open, close, ellipsis, max_tokens) -> str
```

`column_index` is **0-based, integer only**.

#### 3. Contracts

| Arg | Type | Constraint |
|-----|------|-----------|
| `fts_table` | str | FTS5 virtual table name |
| `column_index` | **int** | 0-based offset into the table's column list **including UNINDEXED columns** |
| `open` / `close` / `ellipsis` | str | marker strings |
| `max_tokens` | int | token budget for the snippet |

#### 4. Validation & Error Matrix

| Condition | Result |
|-----------|--------|
| `column_index` is an int in range | Correct column's text is snippeted |
| `column_index` is a **string column name** | **No error.** Silently coerced via `atoi` (`"content"` → `0`) and returns **column 0** (often `id`) |
| `column_index` out of range | SQLite error at query time |

#### 5. Good/Base/Bad Cases

For `documents(id, path, project, content)` (4 columns, `project` is `UNINDEXED`):

- **Good**: `snippet(documents, 3, …)` → snippets `content`
- **Base**: `snippet(documents, 2, …)` → snippets `project`
- **Bad**: `snippet(documents, 'content', …)` → returns `id` values, no error

#### 6. Tests Required

`tests/test_recall_limits.py::test_search_project_filter` asserts the returned snippet body contains query text (not an id/path). When adding a column to `documents`, **this test must stay green** — a wrong column index surfaces as wrong snippet text, not a crash.

#### 7. Wrong vs Correct

##### Wrong
```python
# Looks readable, silently returns column 0.
cur.execute("SELECT snippet(documents, 'content', '[', ']', '…', 8) FROM documents …")
```

##### Correct
```python
# Column list is (id, path, project, content) → content is index 3.
# Keep a one-line note of the derivation; do not "simplify" back to the named form.
cur.execute("SELECT snippet(documents, 3, '[', ']', '…', 8) FROM documents …")
```

> **Warning**: The failure mode is silent. Never "clean up" a numeric column index back to a column name — verify against the live `PRAGMA table_info` column order first, **including UNINDEXED columns**, which still consume an index slot.

---

### Scenario: Derived index shape upgrades and single path source

#### 1. Scope / Trigger

Infra integration: changing an FTS/derived table's columns, or resolving where an index file lives.

#### 2. Signatures

```python
# context_forge/vault.py — single source of truth for index location
class Vault:
    def _index_path(self) -> Path: ...   # runtime_dir() / f"index-{digest(root)[:16]}.db"

# context_forge/index.py
class DocumentIndex:
    def _ensure_documents(self) -> None: ...   # rebuild on column-shape mismatch
```

#### 3. Contracts

| Item | Contract |
|------|----------|
| Index path derivation | **Exactly one implementation**: `Vault._index_path`. Callers must use it. |
| Shape detection | `PRAGMA table_info(documents)` column-set comparison against the expected tuple |
| Upgrade action | `DROP TABLE documents` + recreate — FTS5 drop clears all shadow tables (`_data/_idx/_content/_docsize/_config`) |
| Empty DB | `columns == set()` must **not** trigger a drop |

#### 4. Validation & Error Matrix

| Condition | Result |
|-----------|--------|
| Column set matches | No-op |
| Column set differs (e.g. missing `project`) | DROP + recreate; index is now empty |
| Table absent / empty set | Create only, no DROP |
| Two call sites derive the path independently | Forbidden — see Wrong vs Correct |

#### 5. Good/Base/Bad Cases

- **Good**: add `project` column, `_ensure_documents` rebuilds old shapes, `rebuild_history` untouched
- **Base**: fresh DB creates 4-column shape directly
- **Bad**: a second copy of the digest recipe elsewhere drifts → queries silently return 0 rows

#### 6. Tests Required

`tests/test_recall_limits.py::test_search_upgrades_old_index_shape` — builds a legacy 3-column `documents`, asserts rebuild happens and search still works. Without it the DROP branch is unguarded.

#### 7. Wrong vs Correct

##### Wrong
```python
# doctor.py / cli.py / anywhere else — a second hash recipe
def _index_path(self) -> Path:
    return runtime_dir() / f"index-{hashlib.sha256(str(root).encode()).hexdigest()[:16]}.db"
```

##### Correct
```python
def check_index(self) -> ...:
    return DocumentIndex(self._vault()._index_path())   # delegate to the one true derivation
```

**Why**: three copies of the derivation existed (`Vault._index_path`, `cli._index_path`, `doctor.check_index` inline). Any drift makes lookups silently empty — no error, just zero results.

---

## Migrations

- `SCHEMA_VERSION` bump + explicit `migrate()`. Migration must: take an online backup first, support `dry_run=True` (default), be auto-executable, and be reversible.
- New authoritative fields go into `snapshot_json`. New **columns** only for rebuildable projections (`memory_access_projection`, `memory_applicability_projection`) or operational caches (`memories.weight`).
- **Never** add a second authoritative column for data already in `snapshot_json` (double-truth). `proof_count` lives in `snapshot_json` only.

### Scenario: A migration backup must be restorable — without a schema gate

#### 1. Scope / Trigger

Database schema/migration change. Any `migrate()` that claims "roll back by restoring the backup".

#### 2. Signatures

```python
class MemoryStore:
    @classmethod
    def initialize(cls, path) -> MemoryStore: ...   # only place schema is created
    @classmethod
    def migrate(cls, path, *, dry_run=True, ...) -> dict: ...
    def backup(self, target) -> Path: ...
    @classmethod
    def restore(cls, backup, target) -> MemoryStore: ...

def _copy_database(source: Path, target: Path) -> None:
    """Raw SQLite online copy. No schema gate. Used by both backup and restore."""
```

#### 3. Contracts

| Item | Contract |
|------|----------|
| Backup content | Full DB at the **pre-migration** schema version |
| Restore path | Must copy bytes / use a raw online backup — **never** construct `cls(backup)` |
| Schema gate | `__init__` raises `MemoryStoreError` on `user_version != SCHEMA_VERSION` |
| Post-restore | Re-running `migrate()` must reproduce the migration deterministically |

#### 4. Validation & Error Matrix

| Condition | Result |
|-----------|--------|
| `restore()` implemented as `cls(backup)` where backup is v1 and `SCHEMA_VERSION==2` | **Raises `unsupported memory schema 1`** → rollback path is a lie |
| `restore()` uses `_copy_database` | Restores bytes regardless of schema version |
| Restore then `MemoryStore(target)` | Raises until `migrate()`/`initialize()` is run — expected |
| Restore then `migrate(target)` | Deterministic re-application, `dry_run` reports `changed: 0` on the second pass |

#### 5. Good/Base/Bad Cases

- **Good**: `_copy_database(backup, live)` then `migrate(live)` → original behaviour recovered
- **Base**: `backup()` produces `*.pre-v<N>-<ts>.db` with `user_version` still old and `integrity_check ok`
- **Bad**: `restore()` calls `cls(backup)` — the documented rollback fails on the exact backups `migrate()` creates

#### 6. Tests Required

`tests/test_memory_boundary.py::test_migration_backup_is_restorable` — constructs a pre-migration DB, runs `migrate()`, restores from the produced backup, asserts the file is usable again. **Without this test the branch is unguarded** (it was: deleting the privacy-style safety net left everything green).

Also required: `test_implicit_migration_is_refused` (opening a v1 file raises **and leaves its bytes unchanged**).

#### 7. Wrong vs Correct

##### Wrong
```python
@classmethod
def restore(cls, backup: Path, target: Path) -> "MemoryStore":
    cls(backup).backup(target)        # ← cls() gates on SCHEMA_VERSION: v1 backup unusable
    return cls.initialize(target)
```

##### Correct
```python
@classmethod
def restore(cls, backup: Path, target: Path) -> "MemoryStore":
    _copy_database(Path(backup).resolve(), Path(target).resolve())   # no schema gate
    return cls.initialize(target)                                    # explicit re-init
```

**Docstring rule**: if you document "roll back by restoring the backup", the restore path must work on the backups your own migration writes. State the *actual* rollback command (`cp *.pre-v2-*.db memory.db` then `migrate()`) rather than an abstraction that cannot execute.


## Naming Conventions

| Kind | Pattern | Examples |
|------|---------|----------|
| Tables | plural snake_case | `memories`, `memory_versions`, `retrieval_events`, `documents` |
| Projection tables | `memory_<aspect>_projection` | `memory_access_projection` |
| JSON columns | `<name>_json` + `CHECK(json_valid(...))` | `allowed_projects_json`, `snapshot_json` |
| Event idempotency | `idempotency_key TEXT PRIMARY KEY` | `retrieval_events` |

## Common Mistakes

1. **Passing a column name to `snippet()`** — silently returns column 0 (see scenario above).
2. **Copying an index path derivation** — must delegate to `Vault._index_path`.
3. **Opening a derived index and treating empty results as "no matches"** — after a shape upgrade the index is empty until `rebuild()` runs. Distinguish "index empty" from "query matched nothing".
4. **Migrating implicitly on open** — ordinary connections must raise, never migrate.
