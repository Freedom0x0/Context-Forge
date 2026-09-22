SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE memories (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL,
 current_version INTEGER NOT NULL CHECK(current_version > 0),
 status TEXT NOT NULL CHECK(status IN ('candidate','active','disabled','archived','conflicted','stale')),
 user_owned INTEGER NOT NULL CHECK(user_owned IN (0,1)),
 suppressed INTEGER NOT NULL CHECK(suppressed IN (0,1)),
 FOREIGN KEY(id,current_version) REFERENCES memory_versions(memory_id,version)
 DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE memory_versions (
 memory_id TEXT NOT NULL REFERENCES memories(id), version INTEGER NOT NULL CHECK(version > 0),
 snapshot_json TEXT NOT NULL CHECK(json_valid(snapshot_json)), created_at TEXT NOT NULL,
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
CREATE TABLE memory_scopes (
 memory_id TEXT NOT NULL, version INTEGER NOT NULL, ordinal INTEGER NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('global','project','unknown')), project TEXT,
 paths_json TEXT NOT NULL CHECK(json_valid(paths_json)),
 CHECK((kind='project' AND project IS NOT NULL AND length(project)>0 AND project<>'unknown')
 OR (kind<>'project' AND project IS NULL)),
 PRIMARY KEY(memory_id,version,ordinal),
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
CREATE INDEX memory_scope_lookup ON memory_scopes(kind,project,memory_id,version);
CREATE INDEX memory_relations_target ON memory_relations(target_id,target_version);
"""
