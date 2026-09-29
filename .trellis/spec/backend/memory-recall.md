# Memory Recall Contracts

- Recall filters current status, suppression and the four boundary eliminations (owner, privacy, project authorisation, exclusions) before returning the requested number of records. Source project is provenance, not a filter.
- A result unit retains the conclusion together with applicability, sources, relationships and unresolved conflicts.
- `get` may inspect historical versions; user-facing search returns current active or conflicted records only.
- Retrieval stages are separate events: retrieved, tool-returned, presented, referenced, applied and validated.
- Exposure events do not update evidence strength, value or memory versions.
- Event writes are request-idempotent and reject reuse for a different event.
- MCP keeps legacy vault tools while exposing explicit long-term-memory search/get tools.
- Database failures remain failures; only optional event recording may be treated as a non-result-changing side channel by a host.

Do not treat tool return as presentation, model self-report as application, or task success as validation without attributable evidence.
