# Memory Lifecycle Contracts

`MemoryLifecycle` is the application layer between session extraction and the authoritative `MemoryStore`.

- A session may produce zero, one, or multiple candidates.
- Candidate sources identify the session and retained observations; model output is not tool verification.
- Automatic publication is idempotent and makes a candidate searchable, but never bypasses `MemoryStore` version checks.
- Human revisions use expected versions and are the only path allowed to change protected memories.
- Suppression, disabled status, and user ownership cannot be silently reversed by background publication.
- Scope correction changes visibility without deleting history.
- Supersession creates a new versioned relation and marks the old understanding stale.
- Model and prompt versions are metadata, not evidence strength.

Do not call model gateways inside storage transactions, treat repeated summaries as independent support, or use lifecycle APIs to execute remembered rules.

## Inferred session finalization

`EpisodeCollector.advance_inferred()` may finalize only when both `process_alive=False` and
`active_tools=False` are explicit for that session, and the existing silence/grace and transcript
checks pass. Unknown is not false. Claude's SessionStart hook registers the exact ancestor PID and
process creation time; the watcher validates both to detect PID reuse. SessionStart,
UserPromptSubmit, Stop, PreToolUse, PostToolUse and PostToolUseFailure hooks maintain liveness and
active-tool evidence in `queue.db`. Tool evidence expires after its TTL; stale or incomplete state
is unknown. If optional `psutil` is absent or the hook cannot identify its Claude ancestor, automatic
inferred finalization remains blocked and explicit SessionEnd remains available.
