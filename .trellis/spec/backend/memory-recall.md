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

## Hybrid recall: channels, and `text` is the max of lexical and semantic

Three channels feed one pipeline (BM25 strict-AND → keyword widening → optional semantic top-K); the union then goes through the existing boundary → admission → five-dimension scoring → explanation → byte budget. No second scoring path.

- **Every result carries its channel label** — `bm25` / `widened` / `semantic` — additively alongside the human-readable note (the Chinese widening notes are asserted by `tests/test_recall_widening.py`; do not rename or drop them). Both raw scores (lexical, semantic cosine) are shown.
- **The `text` dimension is `max(lexical_score, semantic_score)` per record**, where lexical is min-max normalised over **lexical ranks only** (never over the combined rank list — appending semantic rows would otherwise pollute the lexical score) and semantic is the **raw cosine (0..1)**. Do not invent a semantic normaliser; calibration waits for eval data.
- With no embedder configured, the combined list equals the lexical list, so behaviour is unchanged — the semantic channel must never be a prerequisite for an existing test.
- **Query-time semantic failure is not a silent downgrade** (design §5): when an embedder *is* configured but the semantic step fails (encode raised, or a wrong-width vector), the request still returns channels 1+2 **and every result's explanation carries `语义通道未启用（…）`** — the close is visible, not swallowed. The marker is emitted only in that failure case: with `embedder=None` (normal lexical mode) there is no marker, and the degrade never changes the returned ids or their order. (`legacy_filter=True`, the rollback hatch, is exempt — it predates the channel contract.)
- **The channel label is a structured, consumed fact, not prose** (design §7 / §9 step 8): `ScoreBreakdown.channel` (`bm25`/`widened`/`semantic`) is a field, mirrored by the `通道=` token in the explanation; `MemoryRecall.search` records it structurally in `retrieval_events.explanation_json`, and `MemoryStore.channel_mix()` counts recalls per channel. It is exposed as `EffectivenessReport.channels`. Counts only, never a rate; empty/absent values fold into `unknown`. This does **not** add a `signal_counts` enum (design §8) and does **not** change `bucket_report`'s grain (a channel is per-recall, a bucket is per-memory).
