# Memory Lifecycle Contracts

`MemoryLifecycle` is the application layer between session extraction and the authoritative `MemoryStore`.

- A session may produce zero, one, or multiple candidates.
- Candidate sources identify the session and retained observations; model output is not tool verification.
- Automatic publication is idempotent and makes a candidate searchable, but never bypasses `MemoryStore` version checks.
- Human revisions use expected versions and are the only path allowed to change protected memories.
- Suppression, disabled status, and user ownership cannot be silently reversed by background publication.
- Scope correction changes visibility without deleting history.
- Supersession creates a new versioned relation and marks the old understanding stale.
- A committed receipt drops its segment's checkpoint bodies; `start_line` / `end_line` / `input_digest` stay. A body is a backup of the host log, so keeping it after the extraction is frozen only duplicates the transcript — and copying the whole log per PreCompact is quadratic in episode length.
- The finalizer receives the extracted conversation, not the raw transcript, capped at a **token** budget (60% of the model's context). Raw log lines make the model improvise, and a long session does not fit the context at all. The budget is counted in tokens because a character budget cannot stand in for one: the same character count is half the tokens in English and twice them in Chinese, and this library's text is Chinese.
- **Source identities are content-addressed at plan time.** The model names a source by file (`<session>/transcript`) and then quotes different passages in two candidates; two candidates then claim one identity with two contents and `memory_sources` refuses the second. `_plan` renames each source to `<identity>#<excerpt digest>` and remaps `applicability.evidence_ids`, so a genuine repeat still de-duplicates and a different quote can never collide.
- **One candidate's failed write does not void its siblings.** Each operation is its own transaction, so the writes before a failure are already committed; failing the whole receipt threw those away as well. The failing operation is marked `failed` and the receipt still commits — `doctor` reports the count. Only the candidate write is caught: a ledger write failing *after* the memory landed is recoverable by replaying, so it must still surface.
- **A failed attempt is retried until the cap.** Bounding the provider wait without a retry would trade "blocks the watcher forever" for "loses the memory": a transient failure leaves the receipt `planned` (attempts < 3) and it is picked up again; a permanent one fails immediately.
- A failed finalization returns its episode to `open`. `active_episodes()` offers only `open` and `inferred_pending`, so an episode left in `finalizing` is never offered again and never cleaned up — a zombie holding its transcript forever.
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

A session whose working directory belongs to a tool rather than a project is not collected at all.
`~/.claude/projects/` is keyed by working directory, so a sibling tool that is itself a Claude Code
client files its internal sessions in the same tree — claude-mem's compressor alone accounted for
304 of 346 transcripts on one machine. Those transcripts are the tool's own logs, and precipitating
them writes the tool's prompts into the user's library.
