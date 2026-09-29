SCHEMA_VERSION = 5

CORE = """
CREATE TABLE memories (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL,
 current_version INTEGER NOT NULL CHECK(current_version > 0),
 status TEXT NOT NULL CHECK(status IN ('candidate','active','disabled','archived','conflicted','stale')),
 user_owned INTEGER NOT NULL CHECK(user_owned IN (0,1)),
 suppressed INTEGER NOT NULL CHECK(suppressed IN (0,1)),
 weight REAL NOT NULL DEFAULT 1.0,
 FOREIGN KEY(id,current_version) REFERENCES memory_versions(memory_id,version)
 DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE memory_versions (
 memory_id TEXT NOT NULL REFERENCES memories(id), version INTEGER NOT NULL CHECK(version > 0),
 snapshot_json TEXT NOT NULL CHECK(json_valid(snapshot_json)), canonical_fingerprint TEXT NOT NULL,
 created_at TEXT NOT NULL,
 risk TEXT NOT NULL DEFAULT 'normal',
 PRIMARY KEY(memory_id,version)
);
CREATE TABLE memory_sources (
 identity TEXT PRIMARY KEY, content_hash TEXT NOT NULL,
 source_json TEXT NOT NULL CHECK(json_valid(source_json)), created_at TEXT NOT NULL
);
CREATE TABLE memory_version_sources (
 memory_id TEXT NOT NULL, version INTEGER NOT NULL, source_id TEXT NOT NULL REFERENCES memory_sources(identity),
 PRIMARY KEY(memory_id,version,source_id),
 FOREIGN KEY(memory_id,version) REFERENCES memory_versions(memory_id,version)
);
CREATE TABLE memory_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, idempotency_key TEXT NOT NULL UNIQUE,
 request_hash TEXT NOT NULL, memory_id TEXT NOT NULL, version INTEGER NOT NULL,
 kind TEXT NOT NULL, actor TEXT NOT NULL CHECK(actor IN ('human','model','tool','import','system')),
 payload_json TEXT NOT NULL CHECK(json_valid(payload_json)), created_at TEXT NOT NULL,
 FOREIGN KEY(memory_id,version) REFERENCES memory_versions(memory_id,version)
);
CREATE TABLE memory_relations (
 id TEXT PRIMARY KEY, memory_id TEXT NOT NULL, version INTEGER NOT NULL,
 target_id TEXT NOT NULL, target_version INTEGER NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('derived_from','duplicate_of','overlaps','conflicts_with',
 'supersedes','supports','contradicts','refines','exception_to')),
 applicability_json TEXT NOT NULL CHECK(json_valid(applicability_json)),
 created_event INTEGER NOT NULL REFERENCES memory_events(id),
 revoked_event INTEGER REFERENCES memory_events(id),
 FOREIGN KEY(memory_id,version) REFERENCES memory_versions(memory_id,version),
 FOREIGN KEY(target_id,target_version) REFERENCES memory_versions(memory_id,version)
);
CREATE TABLE memory_relation_sources (
 relation_id TEXT NOT NULL REFERENCES memory_relations(id),
 source_id TEXT NOT NULL REFERENCES memory_sources(identity),
 PRIMARY KEY(relation_id,source_id)
);
CREATE TABLE retrieval_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, idempotency_key TEXT NOT NULL UNIQUE,
 request_id TEXT NOT NULL, stage TEXT NOT NULL CHECK(stage IN
 ('retrieved','tool_returned','presented','referenced','applied','validated')),
 memory_id TEXT NOT NULL, version INTEGER NOT NULL, session_id TEXT,
 explanation_json TEXT NOT NULL CHECK(json_valid(explanation_json)), created_at TEXT NOT NULL,
 FOREIGN KEY(memory_id,version) REFERENCES memory_versions(memory_id,version)
);
CREATE TABLE memory_applications (
 application_id TEXT PRIMARY KEY,
 task_id TEXT NOT NULL, memory_id TEXT NOT NULL, memory_version INTEGER NOT NULL,
 action TEXT NOT NULL, expected_outcome TEXT NOT NULL, actual_outcome TEXT,
 validation_source_ids_json TEXT NOT NULL CHECK(json_valid(validation_source_ids_json)),
 attribution TEXT NOT NULL CHECK(attribution IN ('confirmed','plausible','unknown')),
 created_at TEXT NOT NULL,
 FOREIGN KEY(memory_id,memory_version) REFERENCES memory_versions(memory_id,version)
);
CREATE TABLE import_items (
 namespace TEXT NOT NULL, path TEXT NOT NULL, legacy_id TEXT NOT NULL,
 memory_id TEXT NOT NULL REFERENCES memories(id), content_hash TEXT NOT NULL,
 snapshot_order INTEGER NOT NULL CHECK(snapshot_order >= 0), imported_version INTEGER NOT NULL,
 PRIMARY KEY(namespace,path),
 FOREIGN KEY(memory_id,imported_version) REFERENCES memory_versions(memory_id,version)
);
CREATE TABLE import_receipts (
 namespace TEXT NOT NULL, path TEXT NOT NULL, content_hash TEXT NOT NULL,
 PRIMARY KEY(namespace,path,content_hash)
);
CREATE TABLE import_snapshots (
 namespace TEXT PRIMARY KEY, snapshot_order INTEGER NOT NULL CHECK(snapshot_order >= 0),
 manifest_hash TEXT NOT NULL
);
CREATE VIRTUAL TABLE memory_fts USING fts5(memory_id UNINDEXED, tokens);
CREATE INDEX memory_relations_target ON memory_relations(target_id,target_version);
CREATE INDEX memory_applications_memory ON memory_applications(memory_id,memory_version);
CREATE INDEX memory_versions_fingerprint ON memory_versions(canonical_fingerprint);
CREATE TABLE memory_projections (
 memory_id TEXT PRIMARY KEY REFERENCES memories(id), version INTEGER NOT NULL,
 path TEXT NOT NULL UNIQUE, content_hash TEXT NOT NULL, status TEXT NOT NULL,
 error TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE projection_conflicts (
 id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id TEXT NOT NULL REFERENCES memories(id),
 path TEXT NOT NULL, expected_version INTEGER NOT NULL, disk_hash TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL, resolved_at TEXT
);
CREATE TABLE source_tombstones (
 source_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, deleted_at TEXT NOT NULL,
 reason TEXT NOT NULL
);
CREATE TABLE precipitation_migrations (
 batch_id TEXT PRIMARY KEY, status TEXT NOT NULL, plan_json TEXT NOT NULL,
 created_at TEXT NOT NULL, rolled_back_at TEXT
);
"""

PROJECTIONS = """
CREATE TABLE IF NOT EXISTS memory_access_projection (
 memory_id TEXT NOT NULL, version INTEGER NOT NULL,
 owner TEXT NOT NULL,
 privacy TEXT NOT NULL CHECK(privacy IN ('shared','private')),
 visibility TEXT NOT NULL CHECK(visibility IN ('owner','selected_projects','shared')),
 allowed_projects_json TEXT NOT NULL CHECK(json_valid(allowed_projects_json)),
 PRIMARY KEY(memory_id,version),
 FOREIGN KEY(memory_id,version) REFERENCES memory_versions(memory_id,version)
);
CREATE TABLE IF NOT EXISTS memory_applicability_projection (
 memory_id TEXT NOT NULL, version INTEGER NOT NULL,
 technologies_json TEXT NOT NULL CHECK(json_valid(technologies_json)),
 domains_json TEXT NOT NULL CHECK(json_valid(domains_json)),
 exclusions_json TEXT NOT NULL CHECK(json_valid(exclusions_json)),
 PRIMARY KEY(memory_id,version),
 FOREIGN KEY(memory_id,version) REFERENCES memory_versions(memory_id,version)
);
"""

# v2 -> v3 delta, applied by the explicit migrate() step. The weight ALTER is
# guarded by a table_info probe in migrate(); the rest are IF NOT EXISTS.
V3 = """
CREATE TABLE IF NOT EXISTS memory_applications (
 application_id TEXT PRIMARY KEY,
 task_id TEXT NOT NULL, memory_id TEXT NOT NULL, memory_version INTEGER NOT NULL,
 action TEXT NOT NULL, expected_outcome TEXT NOT NULL, actual_outcome TEXT,
 validation_source_ids_json TEXT NOT NULL CHECK(json_valid(validation_source_ids_json)),
 attribution TEXT NOT NULL CHECK(attribution IN ('confirmed','plausible','unknown')),
 created_at TEXT NOT NULL,
 FOREIGN KEY(memory_id,memory_version) REFERENCES memory_versions(memory_id,version)
);
CREATE INDEX IF NOT EXISTS memory_applications_memory ON memory_applications(memory_id,memory_version);
"""

V4 = """
CREATE INDEX IF NOT EXISTS memory_versions_fingerprint ON memory_versions(canonical_fingerprint);
CREATE TABLE IF NOT EXISTS memory_projections (
 memory_id TEXT PRIMARY KEY REFERENCES memories(id), version INTEGER NOT NULL,
 path TEXT NOT NULL UNIQUE, content_hash TEXT NOT NULL, status TEXT NOT NULL,
 error TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projection_conflicts (
 id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id TEXT NOT NULL REFERENCES memories(id),
 path TEXT NOT NULL, expected_version INTEGER NOT NULL, disk_hash TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL, resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS source_tombstones (
 source_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, deleted_at TEXT NOT NULL,
 reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS precipitation_migrations (
 batch_id TEXT PRIMARY KEY, status TEXT NOT NULL, plan_json TEXT NOT NULL,
 created_at TEXT NOT NULL, rolled_back_at TEXT
);
"""

SCHEMA = CORE + PROJECTIONS
