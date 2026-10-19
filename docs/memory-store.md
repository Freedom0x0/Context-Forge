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

`search(query, project=..., changed_path=None, limit=8, byte_budget=65536, owner=..., technologies=..., domains=..., legacy_filter=False)` and `search_scored(...)` (same arguments, plus a `ScoreBreakdown` per record) return current full records, including applicability and sources. It excludes disabled/archived/candidate/stale states, suppressed records and `value="discard"`. Beyond that only three boundary eliminations apply: `space.owner` mismatch, `space.privacy="private"` across owners, and a `scope.exclusions` hit. **`project` is provenance, never a filter** — cross-project memories stay reachable, and the project-level access policy (`selected_projects` / `allowed_projects`) was removed in schema v12. Boundary filtering precedes result truncation. `limit=None` removes the count limit while retaining the byte budget. A record too large for the remaining budget is omitted whole, so its conditions are never silently stripped.

The boundary fields are read from `memory_access_projection` / `memory_applicability_projection` before any `snapshot_json` is parsed. A missing projection row raises `MemoryStoreError` (`run rebuild_projections()`); it is never a silent drop or a silent pass.

After the three eliminations the candidate pool is filtered by `admitted(record, ctx)`, then ranked by five weighted dimensions (`match_score` = applicability + provenance bonus, evidence, freshness, adoption feedback, normalised BM25) minus a conflict penalty. The pool filter is derived, never stored: `principle` always admits, `pattern` needs a `technologies`/`domains` hit, `instance` (an `observation`) needs a provenance project or path hit. **Admission is not an elimination** — it never touches the three boundary rules. Same-project and evidence-path provenance only add score; a cross-project record ranks lower, it is not deleted.

`legacy_filter=True` reverts admission, scoring and relevance top-K, falling back to BM25 order with the same count and byte caps. It does **not** revert the boundary trio, `project` no longer being a default filter, provenance retention, or the tri-state exclusion semantics — those are correctness fixes.

`exclusions` and `constraints` are evaluated as tri-state conditions with the same vocabulary as `Applicability.result`. A condition is decidable only as a path glob (matched against `changed_path`) or a bare name (matched against the `technologies ∪ domains ∪ project ∪ path segments` bag); anything else answers `unknown`. `unknown` never eliminates and never penalises: it surfaces as an unresolved risk. A decided exclusion eliminates; a decided constraint miss costs score and surfaces as risk.

Chinese bigrams and lowercase ASCII tokens share the same tokenizer. Query syntax is treated as data, with quoted generated MATCH terms. A single Chinese character uses a scoped literal fallback capped at 100 returned records and the byte budget. Empty/punctuation-only queries return no results. This is local lexical search, not semantic retrieval. `rebuild_fts()` recreates the derived index transactionally from authoritative versions, including when the FTS table has been dropped.

`get`, `history`, `events`, `relations` and `expand_relations` are trusted storage inspection APIs, **not authorization boundaries**. Downstream CLI/MCP tools must enforce scope on details and on each related node. `expand_relations(seed_ids, hops=1)` walks `derived_from` / `supports` / `refines` / `overlaps` one hop in both directions and returns neighbour ids for explanation and conflict context only; it never changes seed ranking and never feeds a weight. Conflict kinds, `duplicate_of` and `supersedes` are not expanded. `hops != 1` raises. Host retrieval-event recording is available (`retrieval_events` stages retrieved, tool_returned, presented, referenced, applied, validated) and is observational only: exposure never rewrites `evidence_level` and never adds weight. Only `applied` counts as reuse in the freshness dimension; `presented`/`referenced` and `proof_count` do not.

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

`context_forge.memory_recall.MemoryRecall` wraps scoped FTS results as complete recall units: current content, applicability, retained sources, active relationships, a `ScoreBreakdown`, the one-hop `expanded` neighbours and a four-part explanation. The four parts are 为什么命中 (which dimension matched), 来源与迁移 (origin project/session plus the transfer reason), 适用边界 (the true / false / **unknown** tri-state conditions; the unknown slot is always rendered) and 风险提示 (unresolved conditions, declared-scope mismatches, constraint misses, known counterexamples, conflicts). A hit with no applicability dimension match degrades the transfer reason to 未匹配任何适用维度 — 谨慎采用. 为什么命中 also names the channel that reached the record: a strict all-term BM25 hit carries no marker, while a widened one says which relaxation brought it in — 去掉 N 个匹配不上的词 (a term no document contains can never satisfy an AND) or 词并集兜底：命中其中一个词即可 (the union, used only as a last resort and capped by result count, never by terms dropped). Widening never bypasses the three boundary rules above. `search` takes `technologies` / `domains` for applicability scoring and pool admission, and `legacy_filter=True` as the rollback hatch. `search` can record `retrieved` or `tool_returned`; `mark` records later `presented`, `referenced`, `applied` and `validated` stages. These events are observational and never change memory confidence or versions.

The MCP server preserves the legacy vault tools and adds `context_forge_memory_search` and `context_forge_memory_get`. Search requires an explicit project, accepts an optional changed path, comma-separated `technologies` / `domains`, `legacy_filter`, uses `limit=0` for the bounded unlimited mode, and may receive a request ID for idempotent tool-return tracking. A missing or invalid authoritative database is reported as an error rather than an empty result.

## Merge analysis

`context_forge.memory_merge.MemoryMerge.report()` is read-only and deterministic for one database snapshot. It distinguishes exact duplicates from token-similarity candidates and reports broad global scopes. Similarity is only a review signal: it does not establish equivalence, contradiction or supersession.

`apply_exact` may archive only records whose normalized content and complete decision boundary match. Rule boundaries must match exactly, including project and path scopes. Sources remain attached to the archived version, prior outgoing relations are retained, and a version-pinned `duplicate_of` relation identifies the canonical record. Human-owned, disabled or suppressed records are not automatically changed. `undo_exact` restores the duplicate and revokes the relation while preserving history.


## Boundary trio and tech detection

`MemoryDraft` carries four separate boundary objects. They are different fields with different enums and must not be conflated:

| Object | Fields | Role |
|---|---|---|
| `MemorySpace` | `owner`, `privacy: shared\|private` | subject-level identity and privacy |
| `AccessPolicy` | `visibility: owner\|shared` | subject-level visibility; project authorisation removed in v12 |
| `Provenance` | `projects`, `sessions`, `paths` | where it came from; sorting bonus only |
| `ApplicabilityScope` | `technologies`, `domains`, `constraints`, `exclusions`, `visibility: global\|scoped` | when it applies |

`memory_access_projection` and `memory_applicability_projection` mirror these for fast filtering and are the input to `boundary_visible`; both are rebuildable from `snapshot_json` via `rebuild_projections()` / `rebuild_fts()`. There is no `generality` column — the level is derived. `scope.constraints` and `scope.visibility` are deliberately **not** projected: scoring and admission read them from the authoritative `snapshot_json`, and no pre-load query needs them.

`condition_result(condition, ctx)` evaluates one `exclusions` / `constraints` / `Applicability.condition` phrase in the three states of `Applicability.result`. Decidable forms are a path glob (needs `changed_path`) and a bare name with no spaces, glob or operator characters; everything else is `unknown`. An undecidable phrase is reported as risk and is never a silent hard filter.

`context_forge.tech_detect.detect(root)` returns a `TechProfile(technologies, domains, raw_evidence)` from manifests, lockfiles and import scanning. `context_forge.vault_retag.retag(root, dry_run=True)` retags a Gen1 vault with `role` / `confidence` / `technologies` / `domains` / `privacy` and reports the basis for every change; `rollback(root)` restores the `.retag-backup/` copies. Internal URLs are no longer tagged `selected_projects` (that policy is gone); credentials still set `privacy: private`. When it collapses a rule whose declared `paths` are evidence provenance rather than applicability, the globs are **moved** to `evidence_paths`, never deleted: `memory_import` merges `paths` and `evidence_paths` into `Provenance.paths`, so provenance survives either key.

An admission detail: because a constraints-only `pattern` has no technology or domain to hit, `admitted` also admits it on a **decided-true** constraint. Without that extension a draft whose only scope key is `constraints` could be written and never read back.

## Application feedback, weight and the three reports

`MemoryStore.record_application(MemoryApplication)` stores one attributable application. The model is fixed by the technical design: `application_id` (idempotency key), `task_id`, `memory_id`, `memory_version`, `action`, `expected_outcome`, `actual_outcome` (NULL when unknown — never invented), `validation_source_ids` and `attribution: confirmed|plausible|unknown`. `validation_source_ids` **must be a subset of that version's retained sources**, or the write is refused. Reusing an application id with different content raises `IdempotencyConflict`.

Only `attribution='confirmed'` lets an application affect anything. `plausible` and `unknown` are recorded and nothing more: they never move weight, never raise `evidence_level`, never trigger negative feedback and never mark `contradicted`.

`apply_feedback_weight(record, events)` is a **pure** function (it writes nothing). Its table is `applied +0.3`, `validated +0.5`, `revoked −0.8`, `harmful −1.0`, `irrelevant −0.2`. Exposure stages have no entry at all — being shown is not being used. Applied / validated stages only count once a **confirmed** application pins the action to an outcome; otherwise the gate drops them. A memory that was never adopted or validated accrues a small decay after `UNUSED_DECAY_DAYS`.

`memories.weight` is an operational cache (`DEFAULT_WEIGHT + apply_feedback_weight`), not authoritative truth — the content lives in `snapshot_json` and the input events live in `retrieval_events` / `memory_applications` / `memory_events`. It is recomputed on the writes that change its inputs (`put`, `record_retrieval`, `record_application`, `record_feedback`) and can be rebuilt in bulk with `rebuild_weights()`. `search_scored` feeds `weight − 1.0` into the fifth scoring dimension. **Rollback: `UPDATE memories SET weight=1.0`** returns every record to the neutral weight; reports are read-only.

`record_feedback(memory_id, outcome=..., idempotency_key=...)` records `revoked` / `harmful` / `irrelevant` as `memory_events` (`kind='feedback'`), version-pinned. `harmful` flips a **non-protected** record to `conflicted` (pending review); a `user_owned` / `suppressed` / `disabled` record is left untouched but still carries the negative weight. Revocation and correction record a reason and a negative weight only — they **never** set `evidence_level='contradicted'`.

Evidence direction is dispatched without silently overwriting (technical design §4.4): `MemoryLifecycle.support` appends a source, raises `proof_count`, and caps the level at `supported` (never `verified` without a new tool excerpt); `weaken` appends a source and a decided-`false` `Applicability`; `extend` adds a `refines` / `exception_to` link without rewriting the target; `overturn` creates a contradicting memory with a `contradicts` relation and **never deletes the refuted one**.

`context_forge.memory_effectiveness.MemoryEffectiveness` produces the three read-only reports: `application_report(memory_id)` → `{referenced, applied, validated, revoked}`; `anomaly_candidates()` → `verified` records with zero `applied` (labelled 未使用 / 待复核, never a hallucination verdict); `tidy_candidates(days=90)` → long-unused records (a `preference` is never declared wrong by non-use). `report(days=90)` bundles them with an `exposure_vs_adoption` ratio. Exposed via `forge memory-report` and the MCP tool `context_forge_memory_effectiveness`; `forge doctor` prints a weight/coverage line.

Adding `memories.weight` and `memory_applications` bumps `SCHEMA_VERSION` to 3. The explicit `migrate()` now chains v1 → 3 (legacy `Scope` split **and** the v3 delta) and v2 → 3 (the delta alone); ordinary opens never migrate.

## Memory hygiene

`context_forge.memory_lifecycle.MemoryLifecycle` owns consolidation and decay; `context_forge.memory_merge.MemoryMerge` owns merge and conflict proposals. All are derived, not stored, and none add a column or a migration.

`consolidation_candidates(min_sources=3)` groups `observation` records by normalized-text similarity and returns one `ConsolidationProposal` per cluster that spans at least `min_sources` distinct (project, session) origins. The proposal is a **new** memory — `role` becomes `claim` (or `procedure` when every episode is a `rule`) — unioning every episode's `provenance` (projects/sessions/paths) and `sources`, and converging `ApplicabilityScope` to the technologies/domains/constraints **every** episode shares (exclusions are unioned, not dropped). Merged tri-state `applicability` conditions stay `unknown` once two episodes disagree, so a later agreeing episode cannot resurrect a decided value and erase a recorded counterexample. Access is the narrowest among the episodes; a cluster with mixed owners yields no proposal rather than a widened one. `consolidate(proposal)` writes it with `derived_from` links to every source at its current version; the episodes are never rewritten. `undo_consolidation(memory_id, ...)` archives the abstraction and revokes those links.

`decay_candidates(days=90)` delegates to `MemoryEffectiveness.tidy_candidates`: long unused **and** without a source update (a new version refreshes `created_at`). `mark_stale(memory_id, ..., reason)` requires a stated basis and is reversible via `restore`; it refuses a `preference`, whose time is judged by `last_confirmed_at` (refreshed on a human `revise`) and whose history changes through `supersede`, never through non-use.

`merge_candidates(threshold=0.9)` splits highly-similar same-kind pairs three ways: fingerprint-identical → `duplicate_of`; compatible but not identical → `overlaps` (review); contradictory applicability (opposite decisions on the same condition, or an exclusion naming what the other claims applies) → `conflicts_with`. Exactness is `canonical_fingerprint` — the same predicate the write gate uses — so which project learned a rule never forks it, and `normalized_text`/`_boundary` are no longer the duplicate test. A `duplicate_of` carries `automatic=True` **only when provenance matches too**: `apply_exact` accepts nothing else, because archiving the loser of a cross-project pair would drop that project from recall. A human may still merge it, via `merge_pair`, which unions the loser's projects/sessions/paths and sources onto the anchor first. `apply_conflict(candidate, idempotency_key=...)` writes a revocable `conflicts_with` relation without rewriting or hiding either side.

`hygiene_report(days=90, min_sources=3, threshold=0.9)` bundles all four lists for batch review — no per-record interruption — and is exposed via `forge hygiene` and the MCP tool `context_forge_memory_hygiene`. Automatic writes may only **narrow** access: `MemoryStore._put` raises `ProtectedMemory('automatic writes may not widen access')` when a non-human actor would broaden `visibility` or relax `privacy` from `private` to `shared`. There is no hard-delete API — archival (reversible) is the only removal.

`issue_confirmation` / `open_confirmation` / `pending_confirmations` / `approve_confirmation` / `consume_confirmation` / `confirmation_audit_trail` hold the high-risk approval ledger (`pending_confirmations`, schema v9; `replay_attempts` arrives with v10). A token is a server-side uuid4, so a host cannot mint or recompute one; answering requires a terminal, expires in 10 minutes, is one-time, and dies the moment the memory gains a version — the join is on `current_version`, so a stale request stops being about the memory as it now stands without any cleanup job. `finalize` parks a high-risk rule as `candidate` and issues the request in the same step, which is why the gate cannot strand a rule in the dark.

Recall enforces the other half — for rules published before the deposit gate, and for rules that never went through it (`memory_import` maps a vault `accepted` file straight to `active`, and `MemoryLifecycle.revise` writes whatever body a human types). `MemoryRecall._withheld` is the read-side gate: `search()` and `get()` both pass through it, and a withheld item serializes as exactly `{id, version, kind, role, status, evidence_level, summary, body: null, confirmation_required, token, expires_at, note}` — no `sources`, `applicability`, `scope`, `provenance`, `explanation`, `score`, `relations` or `expanded`. `open_confirmation` returns the handle already open for that version instead of inserting a row per recall. `consume_confirmation(token)` releases the text after four checks, in order: `used_at IS NULL`; `approved_at` set **and** `approved_from_tty`; `expires_at > now()`; `version == memories.current_version`. Every refusal writes nothing except a replay on a used handle, which increments `replay_attempts` in the same transaction that raises; `forge doctor` prints the total. Release is keyed on `(memory_id, version)`, so an approval covers that version's later recalls and a new version is withheld again. Why those fields, the rejected alternatives, and what this gate does not claim: `docs/adr/0001-withhold-unapproved-high-risk-rules.md`.

Two tiers run unattended and two never do. `MemoryLifecycle.apply_auto_tier()` canonicalizes exact duplicates (`apply_exact`) and annotates contradictions (`apply_conflict`), each tier capped at 100 per pass with whatever it did not reach reported as deferred; it is wired to `forge hygiene --apply` and to the daily `hygiene` maintenance step, which **skips rather than succeeds** when no `memory.db` exists. G3 and G4 stay human: `adjudicate(candidate, keep|merge|archive|suppress, ...)` and `adjudicate_decay(memory_id, keep|stale, ...)` are reached from `forge hygiene --review`, which refuses a non-terminal stdin outright — a model must not adjudicate its own memory. Every verdict, including `keep`, is ledgered in `queue.db` (`hygiene_verdicts`, keyed by candidate identity and overwritten on a change of mind) and surfaces in the effectiveness report as a `verdicts` section with counts only, never a rate. `forge hygiene-undo <memory-id>` reverses one automatic change, reading the library to decide which kind: a `duplicate_of` outgoing edge unarchives via `undo_exact`, a `derived_from` edge unwinds via `undo_consolidation`; it prints the plan until `--apply`.

## Episode consolidation (schema v4)

The production write path is DB-authoritative:

1. `forge watch-transcripts` reads only newline-complete transcript data into `queue.db` Episodes. A 60-second quiet period never publishes long-term memory.
2. Claude Code `SessionEnd`, `forge session finalize`, or the conservative inferred state machine creates one idempotent finalization receipt.
3. The finalizer freezes validated extraction JSON and a deterministic per-memory write plan before writing `memory.db`. A retry replays operation keys and never calls the model again after extraction succeeds.
4. `memory.db` schema v4 stores stable memory IDs and a separate exact `canonical_fingerprint` for the assertion plus its full decision boundary.
5. Markdown under `memories/` is a projection. A changed or `user_owned` file creates a conflict; use an explicit correction import or keep-DB resolution.

Operational commands:

```text
forge install-hook
forge install-hook --status
forge install-hook --self-test
forge session list
forge session inspect SESSION_ID
forge session finalize SESSION_ID
forge session delete SESSION_ID
forge project-memories
forge precipitation-migrate
forge precipitation-migrate --apply
forge precipitation-migrate --rollback BATCH_ID
```

`forge install-hook` installs lifecycle hooks only. It intentionally does not enable watcher autostart. Enable the watcher separately only after `forge doctor` and the launch-gate tests pass.
