# Memory Merge Contracts

- Reports are read-only and deterministic for a fixed database snapshot and threshold.
- Exact automatic duplicates require equal normalized bodies and equal kind, role, decision boundary (`access` + `provenance` + `scope`) and applicability.
- Similarity only creates an ambiguous candidate; it never proves equivalence, conflict or replacement.
- Rule comparison never crosses unequal project/path boundaries.
- Automatic canonicalization archives the duplicate through a versioned write and records `duplicate_of`.
- Existing sources and outgoing relationships are retained on the archived revision.
- Protected memories are never automatically archived.
- Undo restores an active revision and revokes the duplicate relation without deleting history.
- Quality reports separate automatic, ambiguous and broad-scope counts; fewer memories alone is not a success metric.
