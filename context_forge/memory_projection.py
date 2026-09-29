from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .index import parse_frontmatter, yaml_scalar
from .memory import MemoryStore, now
from .memory_models import MemoryDraft


@dataclass(frozen=True)
class ProjectionResult:
    memory_id: str
    path: Path
    status: str
    version: int


class ProjectionConflict(RuntimeError):
    pass


class MemoryProjector:
    def __init__(self, store: MemoryStore, root: Path) -> None:
        self.store = store
        self.root = Path(root).resolve()

    @staticmethod
    def _hash(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    def _path(self, memory_id: str) -> Path:
        safe = hashlib.sha256(memory_id.encode()).hexdigest()[:16]
        return self.root / "memories" / f"{safe}.md"

    def _render(self, record) -> bytes:
        draft = record.draft
        values = {
            "id": record.id, "memory_version": record.version, "kind": draft.kind,
            "role": draft.role, "status": draft.status,
            "user_owned": str(draft.user_owned).lower(),
        }
        front = "\n".join(f"{key}: {yaml_scalar(value)}" for key, value in values.items())
        return f"---\n{front}\n---\n\n{draft.body.strip()}\n".encode("utf-8")

    def project(self, memory_id: str, *, force: bool = False) -> ProjectionResult:
        record = self.store.get(memory_id)
        if record is None:
            raise KeyError(memory_id)
        target = self._path(memory_id)
        content = self._render(record)
        with closing(sqlite3.connect(self.store.path)) as db:
            db.row_factory = sqlite3.Row
            prior = db.execute("SELECT * FROM memory_projections WHERE memory_id=?", (memory_id,)).fetchone()
            if target.exists() and not force:
                disk = target.read_bytes()
                disk_hash = self._hash(disk)
                front, _ = parse_frontmatter(disk.decode("utf-8", errors="replace"))
                if str(front.get("user_owned", "false")).lower() == "true" or not prior or disk_hash != prior["content_hash"]:
                    db.execute("INSERT INTO projection_conflicts(memory_id,path,expected_version,disk_hash,created_at) VALUES (?,?,?,?,?)",
                               (memory_id, str(target), record.version, disk_hash, now()))
                    db.commit()
                    raise ProjectionConflict(str(target))
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temp = tempfile.mkstemp(prefix=".forge-", dir=target.parent)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, target)
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)
            db.execute("INSERT INTO memory_projections VALUES (?,?,?,?,'projected',NULL,?) "
                       "ON CONFLICT(memory_id) DO UPDATE SET version=excluded.version,path=excluded.path,"
                       "content_hash=excluded.content_hash,status='projected',error=NULL,updated_at=excluded.updated_at",
                       (memory_id, record.version, str(target), self._hash(content), now()))
            db.commit()
        return ProjectionResult(memory_id, target, "projected", record.version)

    def project_pending(self) -> tuple[ProjectionResult, ...]:
        results = []
        for record in self.store.current_records():
            try:
                results.append(self.project(record.id))
            except ProjectionConflict:
                continue
        return tuple(results)

    def import_correction(self, path: Path, expected_version: int):
        path = Path(path).resolve()
        front, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        memory_id = str(front.get("id", ""))
        record = self.store.get(memory_id)
        if record is None:
            raise KeyError(memory_id)
        if record.version != expected_version:
            from .memory import VersionConflict
            raise VersionConflict(memory_id)
        draft = record.draft.model_copy(update={"body": body.strip(), "user_owned": True})
        corrected = self.store.put(memory_id, MemoryDraft.model_validate(draft),
                                   expected_version=expected_version,
                                   idempotency_key=f"projection-correction:{memory_id}:{self._hash(path.read_bytes())}",
                                   actor="human", reason="Markdown correction")
        self.project(memory_id, force=True)
        return corrected

    def resolve_keep_db(self, conflict_id: int) -> ProjectionResult:
        with closing(sqlite3.connect(self.store.path)) as db:
            row = db.execute("SELECT memory_id FROM projection_conflicts WHERE id=? AND status='open'",
                             (conflict_id,)).fetchone()
            if row is None:
                raise KeyError(conflict_id)
            result = self.project(row[0], force=True)
            db.execute("UPDATE projection_conflicts SET status='resolved',resolved_at=? WHERE id=?",
                       (now(), conflict_id))
            db.commit()
            return result
