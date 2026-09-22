# Memory store

`context_forge.memory` provides a standalone SQLite storage foundation. The current worker, vault, CLI and MCP paths are unchanged. This API does not invoke a model or migrate live data automatically.

## Create, write, read

```python
from pathlib import Path
from context_forge.memory import MemoryStore
from context_forge.memory_models import (
    Applicability, MemoryDraft, Provenance, Source,
)

store = MemoryStore.initialize(Path("memory.db"))
record = store.put(
    "sqlite-transactions",
    MemoryDraft(
        body="Write the revision and its search projection in one transaction.",
        summary="Atomic memory updates",
        role="procedure",
        status="active",
        provenance=Provenance(projects=("example",), sessions=("test-run",)),
        sources=(Source(identity="test-run:123", kind="tool", excerpt="Rollback test passed"),),
        applicability=(Applicability(condition="SQLite storage", result="unknown"),),
    ),
    expected_version=0,
    idempotency_key="job:123:processing-v1",
    actor="tool",
    reason="Persist the observed rollback contract",
)
reader = MemoryStore(Path("memory.db"))
matches = reader.search("transaction", project="example")
original = reader.get(record.id, version=1)
```

Initialization is explicit and records `PRAGMA user_version=2`. `MemoryStore.migrate(path, dry_run=True)` is the explicit v1 → v2 upgrade: it splits the legacy `Scope` into `MemorySpace` / `AccessPolicy` / `Provenance` / `ApplicabilityScope`, takes an online backup first and rebuilds the projection tables. Ordinary opens never migrate. It is repeatable for a compatible database, initializes an empty database, and refuses unrelated or unsupported schemas. Future migrations must be explicit. Ordinary opens never create a missing database or migrate a schema. Errors are diagnostic `MemoryStoreError` exceptions, not empty search results. Each operation opens and closes its own connection; no `close()` is necessary. Existing writers use `MemoryStore(path, readonly=False)`.

`put` accepts a complete `MemoryDraft` snapshot, not a partial patch. Use the currently read version as `expected_version`; zero means create. A stale revision raises `VersionConflict`. A retry of the identical request and key returns its original revision, even after later corrections. Reusing the key with a changed request raises `IdempotencyConflict`. `user` normalizes to `human`. Automatic writes cannot change user-owned, suppressed, or disabled records; an explicit human revision can restore them. `history(id)` and `events(id)` preserve the preceding content and reasons.

## Sources, conditions and relationships

`Source.identity` identifies one immutable evidence observation. References to that identity reuse the same row; changed evidence under the same identity raises `SourceConflict`. Use a new identity for a new observation, and include the originating session/tool run in session-local identifiers. Content repetition alone is not proof of independent evidence. `observed_at`, `session_id`, reference, excerpt and a content hash survive queue deletion.

The `role` is one of observation, claim, hypothesis, preference or procedure; it is separate from the business `kind` (knowledge, rule or review). `memory_models.derive_level(draft)` derives `instance` / `pattern` / `principle` from `role` + `scope`; it is never stored. `observation` requires `provenance.projects` or `provenance.sessions`. Evidence defaults to `unknown`. Supported/contradicted levels require retained sources; verified requires a retained tool observation. Storage validates attribution structure, not whether a tool observation actually proves a claim; that judgment belongs to the later lifecycle layer. Model self-confidence and legacy feedback never automatically upgrade evidence.

`Applicability(condition=..., result="true" | "false" | "unknown", evidence_ids=(...))` retains tri-state conditions separately from access scopes. Conditions and relation evidence must identify sources in the version being written. Unknown conditions are returned as unknown; search does not interpret them as permission to apply a procedure.

Pass `relations=(Relation(target_id=..., target_version=..., kind=..., evidence_ids=(...), applicability=(...)),)` to `put`. Relations pin both endpoint versions and support derived_from, duplicate_of, overlaps, conflicts_with, supersedes, supports, contradicts, refines and exception_to. New revisions do not silently carry old relationships forward. `relations(id, version=None, include_revoked=False)` inspects both incoming and outgoing links. `revoke_relation(id, expected_version=..., idempotency_key=..., reason=..., actor="human")` records a revocation without deleting historical evidence. The expected version belongs to the relation's originating memory.

## Search and inspection boundaries

`search(query, project=..., changed_path=None, limit=8, byte_budget=65536, owner=...)` returns current full records, including applicability and sources. It excludes disabled/archived/candidate/stale states, suppressed records and `value="discard"`. Beyond that only four boundary eliminations apply: `space.owner` mismatch, `space.privacy="private"` across owners, `access.visibility="selected_projects"` without the current project in `allowed_projects`, and a `scope.exclusions` hit. **`project` is provenance, never a default filter** — cross-project memories stay reachable and only `AccessPolicy` restricts them. Boundary filtering precedes result truncation; ranking uses ascending BM25, then ID. `limit=None` removes the count limit while retaining the byte budget. A record too large for the remaining budget is omitted whole, so its conditions are never silently stripped.

Chinese bigrams and lowercase ASCII tokens share the same tokenizer. Query syntax is treated as data, with quoted generated MATCH terms. A single Chinese character uses a scoped literal fallback capped at 100 returned records and the byte budget. Empty/punctuation-only queries return no results. This is local lexical search, not semantic retrieval. `rebuild_fts()` recreates the derived index transactionally from authoritative versions, including when the FTS table has been dropped.

`get`, `history`, `events`, and `relations` are trusted storage inspection APIs, **not authorization boundaries**. Downstream CLI/MCP tools must enforce scope on details and on each related node. Relationship expansion and host retrieval-event recording belong to later tasks; the schema reserves `retrieval_events` stages retrieved, tool_returned, presented, referenced, applied and validated.

## Import an old vault

```python
from context_forge.memory_import import preview_vault

snapshot = preview_vault(Path("old-vault"), feedback_db=Path("queue.db"))
report = store.import_vault(Path("old-vault"), feedback_db=Path("queue.db"))
store.backup(Path("before-import.db"))
result = store.import_vault(
    Path("old-vault"), feedback_db=Path("queue.db"),
    dry_run=False, namespace="legacy-installation-a", snapshot_order=1,
)
```

`preview_vault` needs no destination database. `import_vault` defaults to a read-only dry-run and reports created, updated, unchanged, protected, feedback counts, diagnostics and path-to-ID mappings. Use the same namespace for the same logical source, even if its snapshot directory moves. The default namespace is the resolved vault path. No importer calls `Vault.scan` or constructs `JobStore`; file bytes/mtime and the feedback database remain unchanged.

`snapshot_order` is an explicit nonnegative monotonic revision, not an inferred file timestamp. The first snapshot can use zero. A changed snapshot must use a strictly larger revision. Older/equal-but-changed snapshots are rejected. Previously seen content never replaces a newer imported snapshot. Updated imports also check the last imported version; subsequent corrections, disabled records and user-owned records remain protected. Use explicit human `put` corrections for edited exports, not automatic two-way synchronization.

Legacy IDs, paths, raw hashes, metadata and source references are retained. Each document gets a stable namespaced ID; duplicate legacy IDs remain separate and ambiguous feedback references are reported. Unknown project remains unknown. Knowledge without a project can inherit explicit project scopes from referencing rules, never implicit global access. Imported rules retain versioned `derived_from` knowledge links when the referenced ID resolves unambiguously.

Feedback rows preserve original row ID, session, outcome, note and timestamp in events, without changing evidence level. Explicit feedback rows remain distinct even if their text matches. Repeated legacy frontmatter-scan rows deduplicate by rule/session/outcome/note; unknown-session scans cannot demonstrate independent observations. Missing/corrupt feedback input fails explicitly rather than creating an empty database. A complete import, including receipts and feedback, commits in one transaction.

## Backup, restore and export

`backup(new_path)` uses SQLite's online backup API and checks database integrity. It refuses an existing target. `MemoryStore.restore(backup_path, new_target)` restores to a new database and returns a writable store. This preserves versions, events, sources, conditions, relationships and import receipts, including committed data still in WAL. Never copy just an active main database file as a backup.

`export_markdown(directory)` writes deterministic current-record snapshots with all draft metadata and sources. Filenames are SHA-256 hashes of IDs, preventing path traversal; existing file symlinks are rejected and files are replaced atomically. Export errors never roll back or alter authoritative data. Markdown is a one-way current view, **not a complete historical backup**. Export does not remove obsolete files in a reused directory; use a fresh directory when a clean snapshot is needed.

All tests use temporary directories. No dependency additions, runtime switch, live migration, lifecycle automation or model-quality claim is part of this storage implementation.

## Lifecycle service

`context_forge.memory_lifecycle.MemoryLifecycle` is the application layer for the next integration stage. It converts validated `SessionArtifacts` into zero or more `candidate` memories, preserving session evidence and model/prompt metadata. Publishing promotes automatic candidates to `active`; it does not treat model self-confidence or repeated summaries as independent verification.

Human corrections use version-pinned writes through `revise`, `suppress`, `restore`, `scope` and `supersede`. A stale background proposal fails with `VersionConflict`, and a model proposal cannot revive a disabled, suppressed or user-owned memory. The service does not execute remembered rules, call a model inside a database transaction, or replace host retrieval behavior until the lifecycle integration task explicitly switches those paths.

## Recall service

`context_forge.memory_recall.MemoryRecall` wraps scoped FTS results as complete recall units: current content, applicability, retained sources, active relationships and an explanation of unresolved conditions or conflicts. `search` can record `retrieved` or `tool_returned`; `mark` records later `presented`, `referenced`, `applied` and `validated` stages. These events are observational and never change memory confidence or versions.

The MCP server preserves the legacy vault tools and adds `context_forge_memory_search` and `context_forge_memory_get`. Search requires an explicit project, accepts an optional changed path, uses `limit=0` for the bounded unlimited mode, and may receive a request ID for idempotent tool-return tracking. A missing or invalid authoritative database is reported as an error rather than an empty result.

## Merge analysis

`context_forge.memory_merge.MemoryMerge.report()` is read-only and deterministic for one database snapshot. It distinguishes exact duplicates from token-similarity candidates and reports broad global scopes. Similarity is only a review signal: it does not establish equivalence, contradiction or supersession.

`apply_exact` may archive only records whose normalized content and complete decision boundary match. Rule boundaries must match exactly, including project and path scopes. Sources remain attached to the archived version, prior outgoing relations are retained, and a version-pinned `duplicate_of` relation identifies the canonical record. Human-owned, disabled or suppressed records are not automatically changed. `undo_exact` restores the duplicate and revokes the relation while preserving history.


## Boundary trio and tech detection

`MemoryDraft` carries four separate boundary objects. They are different fields with different enums and must not be conflated:

| Object | Fields | Role |
|---|---|---|
| `MemorySpace` | `owner`, `privacy: shared\|private` | subject-level identity and privacy |
| `AccessPolicy` | `visibility: owner\|selected_projects\|shared`, `allowed_projects` | explicit project authorisation |
| `Provenance` | `projects`, `sessions`, `paths` | where it came from; sorting bonus only |
| `ApplicabilityScope` | `technologies`, `domains`, `constraints`, `exclusions`, `visibility: global\|scoped` | when it applies |

`memory_access_projection` and `memory_applicability_projection` mirror these for fast filtering; both are rebuildable from `snapshot_json` via `rebuild_projections()` / `rebuild_fts()`. There is no `generality` column — the level is derived.

`context_forge.tech_detect.detect(root)` returns a `TechProfile(technologies, domains, raw_evidence)` from manifests, lockfiles and import scanning. `context_forge.vault_retag.retag(root, dry_run=True)` retags a Gen1 vault with `role` / `confidence` / `technologies` / `domains` / `access_visibility` / `allowed_projects` / `privacy` and reports the basis for every change; `rollback(root)` restores the `.retag-backup/` copies.
