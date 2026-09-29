# Memory-store acceptance matrix

This task delivers the storage foundation only. The current worker and MCP read/write paths are not switched to the new database. Model learning quality and host integration remain downstream acceptance items.

| Contract | Required evidence |
|---|---|
| Explicit initialization | Opening a missing database for reading fails without creating it; initialization records a schema version and rejects unsupported schemas. |
| Atomic persistence | Failure after version/event insertion rolls back the entire write, including the current pointer and FTS projection. |
| Idempotency | Retrying the same operation returns its original result; reusing a key for a different payload fails. |
| Concurrent correction | Two writers with the same expected version cannot both succeed; the loser does not change history. |
| Provenance | Sources and conditions round-trip; repeated source identities do not create independent evidence. |
| Legacy provenance | Session-local evidence labels are scoped by their origin; rule-to-knowledge links survive migration; missing or ambiguous projects remain isolated. |
| Relations | Endpoints refer to existing versions; condition unknown remains distinct from false; historical relationships remain inspectable. |
| Scoped search | Chinese and ASCII queries work without raw MATCH syntax injection; unknown and other-project records cannot leak through limit/order handling. |
| Rebuildable index | Dropped or corrupted derived index can be rebuilt from authoritative records with equivalent search results. |
| Read-only migration | Dry-run leaves both source and destination unchanged; import preserves disabled/user-owned state and source hashes. |
| Reimport safety | Repeating an imported snapshot is a no-op; an old snapshot cannot overwrite a later correction. |
| Feedback import | Existing feedback is read without constructing JobStore, modifying the legacy database, or treating imported feedback as verified evidence. |
| Backup/restore | A live WAL database restores into a new target with content, versions, events, sources and relations intact. |
| Export | Repeated export produces the same bytes; arbitrary identifiers cannot escape the export directory; export failure cannot alter authoritative data. |
| Regression | Existing tests and new isolated storage tests pass using the available project Python runtime. |

Implementation and review results will be recorded in verification.md after execution. No live vault migration or destructive database cleanup is authorized by a test run.
