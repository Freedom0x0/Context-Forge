# Baseline before memory-store implementation

Date: 2026-09-21.

Command: `C:/Users/15532/AppData/Local/Programs/Python/Python312/python.exe -m pytest tests/ -q`.

Result: 45 passed, 1 failed in 15.53s. The pre-existing failure is `test_scan_once_incremental_on_append` at `tests/test_context_forge.py:807`: the second scan produced no second extraction (`len(seen)` was 1, expected 2). No new memory-store code existed during this run. Do not attribute this failure to the storage implementation or modify the watcher as part of this task.

An isolated follow-up run of the same failing test passed (1 passed in 0.58s), so the baseline failure is intermittent. No watcher changes are included in this task.

The bundled Codex Python can execute Trellis scripts but does not have pytest. The Python312 interpreter has pytest 9.1.1 and is used for project verification.

Graph discovery: the graph service lists only two unrelated repositories; this repository has no available project generation or coverage result. Relevant source was inspected directly. No graph index was created.

Existing vault scan clears the feedback frontmatter after inserting feedback and writes index files. Migration must not call it or instantiate JobStore against the source database. The queue retention path can delete sessions, so memory provenance cannot depend solely on a queue foreign key.
