from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .domain import EpisodeCheckpoint, EpisodeRecord, FinalizationReceipt, SessionEvent, SessionLifecycleEvent


class EventConflict(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
          id TEXT PRIMARY KEY, project TEXT NOT NULL, cwd TEXT NOT NULL,
          transcript_path TEXT, transcript_hash TEXT, status TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
          id TEXT PRIMARY KEY, session_id TEXT NOT NULL, kind TEXT NOT NULL,
          payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
          UNIQUE(session_id, kind, payload_json)
        );
        CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload_json TEXT NOT NULL,
          idempotency_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
          attempts INTEGER NOT NULL DEFAULT 0, available_at TEXT NOT NULL,
          locked_at TEXT, lease_expires_at TEXT, last_error TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reviews (
          id TEXT PRIMARY KEY, session_id TEXT NOT NULL, project TEXT NOT NULL,
          path TEXT NOT NULL UNIQUE, status TEXT NOT NULL, content_hash TEXT NOT NULL,
          draft_hash TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS rule_feedback (
          id INTEGER PRIMARY KEY AUTOINCREMENT, rule_id TEXT NOT NULL,
          session_id TEXT, outcome TEXT NOT NULL, note TEXT,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS rule_hit_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, rule_id TEXT NOT NULL,
          project TEXT NOT NULL, changed_path TEXT, session_id TEXT,
          reason TEXT NOT NULL, occurred_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS session_state (
          session_id TEXT PRIMARY KEY,
          lines_processed INTEGER NOT NULL DEFAULT 0,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS session_lifecycle_events (
          event_id TEXT PRIMARY KEY, request_hash TEXT NOT NULL,
          type TEXT NOT NULL, source TEXT NOT NULL, session_id TEXT NOT NULL,
          payload_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          error TEXT, created_at TEXT NOT NULL, consumed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS session_episodes (
          session_id TEXT NOT NULL, segment INTEGER NOT NULL DEFAULT 1,
          project TEXT, transcript_path TEXT NOT NULL,
          collected_through_line INTEGER NOT NULL DEFAULT 0,
          transcript_digest TEXT NOT NULL, state TEXT NOT NULL,
          started_at TEXT NOT NULL, last_activity_at TEXT NOT NULL,
          end_kind TEXT, retention_deadline TEXT,
          host TEXT NOT NULL DEFAULT 'claude_code',
          PRIMARY KEY(session_id, segment)
        );
        CREATE TABLE IF NOT EXISTS scheduled_runs (
          step TEXT PRIMARY KEY, last_success_at TEXT, last_error TEXT,
          last_error_at TEXT, attempts INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS episode_checkpoints (
          checkpoint_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
          segment INTEGER NOT NULL, start_line INTEGER NOT NULL,
          end_line INTEGER NOT NULL, input_digest TEXT NOT NULL UNIQUE,
          snapshot_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS finalization_receipts (
          finalization_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
          segment INTEGER NOT NULL, episode_digest TEXT NOT NULL,
          end_kind TEXT NOT NULL, extraction_json TEXT, write_plan_json TEXT,
          status TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL, extraction_owner TEXT, extraction_lease_until TEXT,
          UNIQUE(session_id,segment,episode_digest)
        );
        CREATE TABLE IF NOT EXISTS finalization_operations (
          operation_key TEXT PRIMARY KEY, finalization_id TEXT NOT NULL,
          ordinal INTEGER NOT NULL, request_hash TEXT NOT NULL,
          memory_id TEXT NOT NULL, payload_json TEXT NOT NULL,
          status TEXT NOT NULL, result_json TEXT, error TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          UNIQUE(finalization_id,ordinal)
        );
        """)
        self.db.commit()
        # Migrations for tables created before newer columns were added.
        self._migrate()

    def _migrate(self) -> None:
        cur = self.db.execute("PRAGMA table_info(reviews)")
        columns = {row[1] for row in cur.fetchall()}
        if "draft_hash" not in columns:
            try:
                self.db.execute("ALTER TABLE reviews ADD COLUMN draft_hash TEXT")
                self.db.commit()
            except sqlite3.Error:
                pass
        receipt_columns = {row[1] for row in self.db.execute(
            "PRAGMA table_info(finalization_receipts)").fetchall()}
        for name in ("extraction_owner", "extraction_lease_until"):
            if name not in receipt_columns:
                self.db.execute(f"ALTER TABLE finalization_receipts ADD COLUMN {name} TEXT")
        episode_columns = {row[1] for row in self.db.execute(
            "PRAGMA table_info(session_episodes)").fetchall()}
        if "host" not in episode_columns:
            self.db.execute(
                "ALTER TABLE session_episodes ADD COLUMN host TEXT NOT NULL DEFAULT 'claude_code'")
        self.db.commit()

    def enqueue_event(self, event: SessionEvent) -> bool:
        payload = event.model_dump_json()
        key = f"review:{event.session_id}:{event.transcript_hash or 'unknown'}"
        now = _now()
        try:
            with self.db:
                self.db.execute(
                    "INSERT OR IGNORE INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (event.session_id, event.project, event.cwd, event.transcript_path,
                     event.transcript_hash, "captured", now),
                )
                self.db.execute(
                    "INSERT OR IGNORE INTO events VALUES (?, ?, ?, ?, ?)",
                    (event.event_id, event.session_id, event.event_type, payload, now),
                )
                result = self.db.execute(
                    "INSERT OR IGNORE INTO jobs VALUES (?, ?, ?, ?, ?, 0, ?, NULL, NULL, NULL, ?, ?)",
                    (key, "create_review", payload, key, "queued", now, now, now),
                )
            return result.rowcount == 1
        except sqlite3.Error:
            raise

    def ingest_session_event(self, event: SessionLifecycleEvent) -> str:
        import hashlib
        payload = event.model_dump_json()
        request_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        row = self.db.execute(
            "SELECT request_hash FROM session_lifecycle_events WHERE event_id=?",
            (event.event_id,),
        ).fetchone()
        if row:
            if row["request_hash"] != request_hash:
                raise EventConflict(event.event_id)
            return "existing"
        with self.db:
            self.db.execute(
                "INSERT INTO session_lifecycle_events "
                "(event_id,request_hash,type,source,session_id,payload_json,status,created_at) "
                "VALUES (?,?,?,?,?,?,'pending',?)",
                (event.event_id, request_hash, event.type, event.source,
                 event.session_id, payload, _now()),
            )
        return "created"

    def episode(self, session_id: str, segment: int = 1) -> EpisodeRecord | None:
        row = self.db.execute(
            "SELECT * FROM session_episodes WHERE session_id=? AND segment=?",
            (session_id, segment),
        ).fetchone()
        return EpisodeRecord(**dict(row)) if row else None

    def latest_episode(self, session_id: str) -> EpisodeRecord | None:
        row = self.db.execute(
            "SELECT * FROM session_episodes WHERE session_id=? ORDER BY segment DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return EpisodeRecord(**dict(row)) if row else None

    def active_episodes(self) -> list[EpisodeRecord]:
        rows = self.db.execute(
            "SELECT * FROM session_episodes WHERE state IN ('open','inferred_pending') "
            "ORDER BY last_activity_at,session_id,segment"
        ).fetchall()
        return [EpisodeRecord(**dict(row)) for row in rows]

    def list_episodes(self) -> list[EpisodeRecord]:
        return [EpisodeRecord(**dict(row)) for row in self.db.execute(
            "SELECT * FROM session_episodes ORDER BY last_activity_at DESC,session_id,segment")]

    def host_counts(self) -> dict[str, int]:
        """Episodes per host agent — the bucket 8a's silence alarm and P1's report read."""
        return {row["host"]: row["n"] for row in self.db.execute(
            "SELECT host, COUNT(*) AS n FROM session_episodes GROUP BY host ORDER BY host")}

    def scheduled_due(self, step: str, now: datetime, period_seconds: int) -> bool:
        row = self.db.execute(
            "SELECT last_success_at FROM scheduled_runs WHERE step=?", (step,)).fetchone()
        if row is None or not row["last_success_at"]:
            return True
        try:
            last = datetime.fromisoformat(row["last_success_at"])
        except ValueError:
            return True
        return now - last >= timedelta(seconds=period_seconds)

    def record_scheduled_success(self, step: str, now: datetime) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO scheduled_runs(step,last_success_at) VALUES (?,?) "
                "ON CONFLICT(step) DO UPDATE SET last_success_at=excluded.last_success_at, "
                "last_error=NULL, last_error_at=NULL, attempts=0",
                (step, now.isoformat()),
            )

    def record_scheduled_failure(self, step: str, now: datetime, error: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO scheduled_runs(step,last_error,last_error_at,attempts) "
                "VALUES (?,?,?,1) "
                "ON CONFLICT(step) DO UPDATE SET last_error=excluded.last_error, "
                "last_error_at=excluded.last_error_at, "
                "attempts=scheduled_runs.attempts+1",
                (step, error, now.isoformat()),
            )

    def scheduled_runs(self) -> list[dict]:
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM scheduled_runs ORDER BY step")]

    def save_episode(self, episode: EpisodeRecord) -> EpisodeRecord:
        values = episode.model_dump()
        with self.db:
            self.db.execute(
                "INSERT INTO session_episodes VALUES "
                "(:session_id,:segment,:project,:transcript_path,:collected_through_line,"
                ":transcript_digest,:state,:started_at,:last_activity_at,:end_kind,:retention_deadline,"
                ":host) "
                "ON CONFLICT(session_id,segment) DO UPDATE SET "
                "project=excluded.project,transcript_path=excluded.transcript_path,"
                "collected_through_line=excluded.collected_through_line,"
                "transcript_digest=excluded.transcript_digest,state=excluded.state,"
                "last_activity_at=excluded.last_activity_at,end_kind=excluded.end_kind,"
                "retention_deadline=excluded.retention_deadline,host=excluded.host",
                values,
            )
        return episode

    def save_checkpoint(self, checkpoint: EpisodeCheckpoint) -> EpisodeCheckpoint:
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO episode_checkpoints VALUES (?,?,?,?,?,?,?,?)",
                (checkpoint.checkpoint_id, checkpoint.session_id, checkpoint.segment,
                 checkpoint.start_line, checkpoint.end_line, checkpoint.input_digest,
                 json.dumps(checkpoint.snapshot, ensure_ascii=False), checkpoint.created_at),
            )
        return self.list_checkpoints(checkpoint.session_id)[-1]

    def list_checkpoints(self, session_id: str) -> list[EpisodeCheckpoint]:
        rows = self.db.execute(
            "SELECT * FROM episode_checkpoints WHERE session_id=? ORDER BY end_line,checkpoint_id",
            (session_id,),
        ).fetchall()
        return [EpisodeCheckpoint(
            checkpoint_id=row["checkpoint_id"], session_id=row["session_id"],
            segment=row["segment"], start_line=row["start_line"], end_line=row["end_line"],
            input_digest=row["input_digest"], snapshot=json.loads(row["snapshot_json"]),
            created_at=row["created_at"],
        ) for row in rows]

    def receipt(self, finalization_id: str) -> FinalizationReceipt | None:
        row = self.db.execute(
            "SELECT * FROM finalization_receipts WHERE finalization_id=?", (finalization_id,),
        ).fetchone()
        return FinalizationReceipt(**dict(row)) if row else None

    def create_receipt(self, receipt: FinalizationReceipt) -> FinalizationReceipt:
        by_id = self.db.execute(
            "SELECT * FROM finalization_receipts WHERE finalization_id=?",
            (receipt.finalization_id,),
        ).fetchone()
        if by_id:
            existing = FinalizationReceipt(**dict(by_id))
            if (existing.session_id, existing.segment, existing.episode_digest) != (
                    receipt.session_id, receipt.segment, receipt.episode_digest):
                raise EventConflict(receipt.finalization_id)
            return existing
        existing = self.db.execute(
            "SELECT * FROM finalization_receipts WHERE session_id=? AND segment=? AND episode_digest=?",
            (receipt.session_id, receipt.segment, receipt.episode_digest),
        ).fetchone()
        if existing:
            return FinalizationReceipt(**dict(existing))
        with self.db:
            self.db.execute(
                "INSERT INTO finalization_receipts "
                "(finalization_id,session_id,segment,episode_digest,end_kind,extraction_json,"
                "write_plan_json,status,error,created_at,updated_at,extraction_owner,extraction_lease_until) "
                "VALUES (:finalization_id,:session_id,:segment,:episode_digest,:end_kind,"
                ":extraction_json,:write_plan_json,:status,:error,:created_at,:updated_at,"
                ":extraction_owner,:extraction_lease_until)", receipt.model_dump())
        return receipt

    def claim_extraction(self, finalization_id: str, owner: str,
                         lease_seconds: int = 300) -> bool:
        now = datetime.now(timezone.utc)
        lease = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self.db:
            changed = self.db.execute(
                "UPDATE finalization_receipts SET extraction_owner=?,extraction_lease_until=?,updated_at=? "
                "WHERE finalization_id=? AND extraction_json IS NULL AND "
                "(extraction_owner IS NULL OR extraction_lease_until<? OR extraction_owner=?)",
                (owner, lease, now.isoformat(), finalization_id, now.isoformat(), owner),
            )
        return changed.rowcount == 1

    def freeze_finalization(self, finalization_id: str, extraction_json: str,
                            operations: list[dict]) -> FinalizationReceipt:
        with self.db:
            row = self.db.execute(
                "SELECT extraction_json FROM finalization_receipts WHERE finalization_id=?",
                (finalization_id,),
            ).fetchone()
            if row is None:
                raise KeyError(finalization_id)
            if row["extraction_json"] is not None and row["extraction_json"] != extraction_json:
                raise EventConflict(finalization_id)
            plan_json = json.dumps(operations, sort_keys=True, separators=(",", ":"))
            self.db.execute(
                "UPDATE finalization_receipts SET extraction_json=?,write_plan_json=?,status='planned',updated_at=?,"
                "extraction_owner=NULL,extraction_lease_until=NULL "
                "WHERE finalization_id=? AND status IN ('extracting','planned','failed')",
                (extraction_json, plan_json, _now(), finalization_id),
            )
            for operation in operations:
                payload = json.dumps(operation["payload"], sort_keys=True, separators=(",", ":"))
                self.db.execute(
                    "INSERT OR IGNORE INTO finalization_operations VALUES (?,?,?,?,?,?,'pending',NULL,NULL,?,?)",
                    (operation["operation_key"], finalization_id, operation["ordinal"],
                     operation["request_hash"], operation["memory_id"], payload, _now(), _now()),
                )
        result = self.receipt(finalization_id)
        assert result is not None
        return result

    def finalization_operations(self, finalization_id: str) -> list[dict]:
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM finalization_operations WHERE finalization_id=? ORDER BY ordinal",
            (finalization_id,),
        )]

    def pending_receipts(self) -> list[FinalizationReceipt]:
        return [FinalizationReceipt(**dict(row)) for row in self.db.execute(
            "SELECT * FROM finalization_receipts WHERE status IN ('extracting','planned') "
            "OR (status='committed' AND error IS NOT NULL) "
            "ORDER BY created_at,finalization_id")]

    def complete_finalization_operation(self, operation_key: str, result: dict) -> None:
        with self.db:
            self.db.execute(
                "UPDATE finalization_operations SET status='committed',result_json=?,error=NULL,updated_at=? "
                "WHERE operation_key=?",
                (json.dumps(result, sort_keys=True), _now(), operation_key),
            )

    def mark_receipt_committed(self, finalization_id: str) -> FinalizationReceipt:
        with self.db:
            row = self.db.execute(
                "SELECT session_id,segment FROM finalization_receipts WHERE finalization_id=?",
                (finalization_id,),
            ).fetchone()
            if row is None:
                raise KeyError(finalization_id)
            self.db.execute(
                "UPDATE finalization_receipts SET status='committed',updated_at=? WHERE finalization_id=?",
                (_now(), finalization_id),
            )
            self.db.execute(
                "UPDATE session_episodes SET state='finalized' WHERE session_id=? AND segment=?",
                (row["session_id"], row["segment"]),
            )
        result = self.receipt(finalization_id)
        assert result is not None
        return result

    def mark_receipt_projected(self, finalization_id: str) -> FinalizationReceipt:
        with self.db:
            self.db.execute(
                "UPDATE finalization_receipts SET status='projected',error=NULL,updated_at=? "
                "WHERE finalization_id=? AND status IN ('committed','projected')",
                (_now(), finalization_id),
            )
        result = self.receipt(finalization_id)
        if result is None:
            raise KeyError(finalization_id)
        return result

    def record_projection_error(self, finalization_id: str, error: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE finalization_receipts SET error=?,updated_at=? "
                "WHERE finalization_id=? AND status='committed'", (error, _now(), finalization_id))

    def claim(self, lease_seconds: int = 60) -> sqlite3.Row | None:
        now = datetime.now(timezone.utc)
        with self.db:
            self.db.execute(
                "UPDATE jobs SET status='queued', locked_at=NULL, lease_expires_at=NULL "
                "WHERE status='running' AND lease_expires_at < ?", (now.isoformat(),)
            )
            row = self.db.execute(
                "SELECT * FROM jobs WHERE status='queued' AND available_at <= ? "
                "ORDER BY created_at LIMIT 1", (now.isoformat(),)
            ).fetchone()
            if row is None:
                return None
            expires = (now + timedelta(seconds=lease_seconds)).isoformat()
            self.db.execute(
                "UPDATE jobs SET status='running', attempts=attempts+1, locked_at=?, "
                "lease_expires_at=?, updated_at=? WHERE id=? AND status='queued'",
                (now.isoformat(), expires, now.isoformat(), row["id"]),
            )
            return self.db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()

    def finish(self, job_id: str, error: str | None = None,
               max_attempts: int = 3) -> None:
        """Mark a job as succeeded, failed, or dead_letter.

        Once attempts reach `max_attempts`, the job transitions to
        `dead_letter` (§16) and stops being reaped from running. Failed
        jobs below the limit are still eligible for retry via
        `retry_job` — see `forge jobs retry <id>`.
        """
        row = self.db.execute(
            "SELECT attempts FROM jobs WHERE id=?", (job_id,),
        ).fetchone()
        attempts = row["attempts"] if row else 0
        if error and attempts >= max_attempts:
            status = "dead_letter"
        elif error:
            status = "failed"
        else:
            status = "succeeded"
        with self.db:
            self.db.execute(
                "UPDATE jobs SET status=?, last_error=?, updated_at=? WHERE id=?",
                (status, error, _now(), job_id),
            )

    def retry_job(self, job_id: str) -> bool:
        """Reset a failed/dead_letter job back to queued so worker picks it up."""
        with self.db:
            cur = self.db.execute(
                "UPDATE jobs SET status='queued', last_error=NULL, "
                "attempts=0, available_at=?, updated_at=? "
                "WHERE id=? AND status IN ('failed', 'dead_letter')",
                (_now(), _now(), job_id),
            )
            return cur.rowcount == 1

    def list_jobs(self) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM jobs ORDER BY created_at DESC"))

    def register_review(self, review_id: str, session_id: str, project: str,
                         path: Path, content_hash: str,
                         draft_hash: str | None = None) -> None:
        now = _now()
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO reviews VALUES (?, ?, ?, ?, 'draft', ?, ?, ?, ?)",
                (review_id, session_id, project, str(path), content_hash,
                 draft_hash or content_hash, now, now),
            )

    def review_user_hash(self, review_id: str) -> tuple[str, str] | None:
        """Return `(draft_hash, current_path)` for a review, or None."""
        row = self.db.execute(
            "SELECT draft_hash, path FROM reviews WHERE id=?", (review_id,),
        ).fetchone()
        if row is None:
            return None
        return (row["draft_hash"] or "", row["path"])

    def list_reviews(self, status: str = "draft") -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM reviews WHERE status=? ORDER BY created_at DESC", (status,)))

    def set_review_status(self, review_id: str, status: str) -> None:
        with self.db:
            self.db.execute("UPDATE reviews SET status=?, updated_at=? WHERE id=?", (status, _now(), review_id))

    def add_rule_feedback(self, rule_id: str, outcome: str, note: str | None = None,
                          session_id: str | None = None) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO rule_feedback(rule_id, session_id, outcome, note, created_at) VALUES (?, ?, ?, ?, ?)",
                (rule_id, session_id, outcome, note, _now()),
            )

    def record_rule_hit(self, rule_id: str, project: str, reason: str,
                         changed_path: str | None = None,
                         session_id: str | None = None) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO rule_hit_events(rule_id, project, changed_path, "
                "session_id, reason, occurred_at) VALUES (?, ?, ?, ?, ?, ?)",
                (rule_id, project, changed_path, session_id, reason, _now()),
            )

    def rule_hit_stats(self, rule_id: str) -> dict[str, int | str]:
        row = self.db.execute(
            "SELECT COUNT(*) AS hits, MAX(occurred_at) AS last_seen "
            "FROM rule_hit_events WHERE rule_id=?", (rule_id,)
        ).fetchone()
        feedback = self.db.execute(
            "SELECT outcome, COUNT(*) AS n FROM rule_feedback "
            "WHERE rule_id=? GROUP BY outcome", (rule_id,)
        ).fetchall()
        return {
            "rule_id": rule_id,
            "hits": row["hits"] or 0,
            "last_seen": row["last_seen"] or "",
            "feedback": {r["outcome"]: r["n"] for r in feedback},
        }

    def hits_for_session(self, session_id: str) -> list[str]:
        """Return the rule_ids that hit during a given session."""
        return [row["rule_id"] for row in self.db.execute(
            "SELECT rule_id FROM rule_hit_events WHERE session_id=?",
            (session_id,),
        )]

    def auto_record_feedback_for_session(self, session_id: str) -> int:
        """For every rule hit tagged with this session, record `unknown` if no
        explicit feedback has been given. Returns the number of rows inserted.
        """
        hits = list(self.db.execute(
            "SELECT rule_id FROM rule_hit_events WHERE session_id=?",
            (session_id,),
        ))
        if not hits:
            return 0
        recorded = 0
        now = _now()
        with self.db:
            for hit in hits:
                already = self.db.execute(
                    "SELECT 1 FROM rule_feedback "
                    "WHERE rule_id=? AND session_id=? LIMIT 1",
                    (hit["rule_id"], session_id),
                ).fetchone()
                if already:
                    continue
                self.db.execute(
                    "INSERT INTO rule_feedback(rule_id, session_id, outcome, "
                    "note, created_at) VALUES (?, ?, ?, ?, ?)",
                    (hit["rule_id"], session_id, "unknown",
                     "auto: session ended without explicit feedback", now),
                )
                recorded += 1
        return recorded

    def get_lines_processed(self, session_id: str) -> int:
        row = self.db.execute(
            "SELECT lines_processed FROM session_state WHERE session_id=?",
            (session_id,),
        ).fetchone()
        return row["lines_processed"] if row else 0

    def set_lines_processed(self, session_id: str, lines: int) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO session_state(session_id, lines_processed, "
                "updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "lines_processed=excluded.lines_processed, "
                "updated_at=excluded.updated_at",
                (session_id, lines, _now()),
            )

    def delete_session(self, session_id: str) -> dict[str, int]:
        """Remove a session and all its derived state from the queue.

        Returns a count of removed rows per table. Index rows are NOT
        deleted here because they reference files on disk; the caller
        should rebuild the FTS index after this returns.
        """
        counts = {"sessions": 0, "events": 0, "jobs": 0, "reviews": 0,
                  "rule_feedback": 0, "rule_hit_events": 0,
                  "session_lifecycle_events": 0, "session_episodes": 0,
                  "episode_checkpoints": 0, "finalization_receipts": 0,
                  "finalization_operations": 0}
        with self.db:
            receipt_ids = [row[0] for row in self.db.execute(
                "SELECT finalization_id FROM finalization_receipts WHERE session_id=?", (session_id,))]
            for finalization_id in receipt_ids:
                cur = self.db.execute("DELETE FROM finalization_operations WHERE finalization_id=?",
                                      (finalization_id,))
                counts["finalization_operations"] += cur.rowcount
            for table in ("episode_checkpoints", "finalization_receipts",
                          "session_lifecycle_events", "session_episodes"):
                cur = self.db.execute(f"DELETE FROM {table} WHERE session_id=?", (session_id,))
                counts[table] = cur.rowcount
            # sessions / events / reviews / rule_* all carry session_id
            for table in ("events", "reviews", "rule_feedback", "rule_hit_events"):
                cur = self.db.execute(
                    f"DELETE FROM {table} WHERE session_id=?", (session_id,),
                )
                counts[table] = cur.rowcount
            cur = self.db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            counts["sessions"] = cur.rowcount
            # jobs key is `review:{session_id}:...`; delete by prefix
            cur = self.db.execute(
                "DELETE FROM jobs WHERE idempotency_key LIKE ?",
                (f"review:{session_id}:%",),
            )
            counts["jobs"] = cur.rowcount
        return counts

    def episode_retention_preview(self, days: int, *, at: datetime | None = None) -> list[str]:
        moment = at or datetime.now(timezone.utc)
        cutoff = (moment - timedelta(days=days)).isoformat()
        rows = self.db.execute(
            "SELECT DISTINCT e.session_id FROM session_episodes e "
            "WHERE e.state='finalized' AND e.last_activity_at<=? "
            "AND NOT EXISTS (SELECT 1 FROM finalization_receipts r WHERE r.session_id=e.session_id "
            "AND r.status IN ('extracting','planned','failed')) ORDER BY e.session_id", (cutoff,),
        ).fetchall()
        return [row[0] for row in rows]

    def retention_preview(self, days: int) -> list[dict[str, str]]:
        """List sessions older than `days` that retention would delete.

        Each row has `session_id` and `created_at`. Markdown files in
        the vault are not represented here; callers must delete those
        separately or rely on the user to keep them.
        """
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = self.db.execute(
            "SELECT id, created_at FROM sessions WHERE created_at < ? "
            "ORDER BY created_at", (cutoff,),
        ).fetchall()
        return [{"session_id": r["id"], "created_at": r["created_at"]} for r in rows]

    def retention_apply(self, days: int, dry_run: bool = False) -> list[str]:
        """Delete sessions older than `days`. Returns the list of removed IDs."""
        targets = [r["session_id"] for r in self.retention_preview(days)]
        if dry_run:
            return targets
        for session_id in targets:
            self.delete_session(session_id)
        return targets

    def session_info(self, session_id: str) -> dict[str, object]:
        """Return a structured view of one session for debugging.

        Includes counters for events / jobs / reviews / hits / feedback and
        the current statuses. None if the session is not recorded.
        """
        row = self.db.execute(
            "SELECT id, project, cwd, transcript_path, transcript_hash, "
            "status, created_at FROM sessions WHERE id=?", (session_id,),
        ).fetchone()
        if row is None:
            return {}
        events = self.db.execute(
            "SELECT COUNT(*) AS n FROM events WHERE session_id=?",
            (session_id,),
        ).fetchone()["n"]
        jobs = list(self.db.execute(
            "SELECT id, status, attempts, last_error FROM jobs "
            "WHERE idempotency_key LIKE ? ORDER BY created_at",
            (f"review:{session_id}:%",),
        ))
        reviews = list(self.db.execute(
            "SELECT id, status, content_hash, created_at, updated_at "
            "FROM reviews WHERE session_id=?", (session_id,),
        ))
        hits = self.db.execute(
            "SELECT COUNT(*) AS n FROM rule_hit_events WHERE session_id=?",
            (session_id,),
        ).fetchone()["n"]
        feedback = list(self.db.execute(
            "SELECT rule_id, outcome, note, created_at FROM rule_feedback "
            "WHERE session_id=? ORDER BY created_at", (session_id,),
        ))
        return {
            "session": dict(row),
            "events": events,
            "jobs": [dict(r) for r in jobs],
            "reviews": [dict(r) for r in reviews],
            "rule_hits": hits,
            "rule_feedback": [dict(r) for r in feedback],
        }

    def metrics(self) -> dict[str, int | float]:
        """Aggregate the §11 first-slice indicators.

        Returns a flat dict suitable for `forge metrics` output.
        """
        sessions = self.db.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
        reviews_total = self.db.execute("SELECT COUNT(*) AS n FROM reviews").fetchone()["n"]
        reviews_draft = self.db.execute(
            "SELECT COUNT(*) AS n FROM reviews WHERE status='draft'"
        ).fetchone()["n"]
        reviews_approved = self.db.execute(
            "SELECT COUNT(*) AS n FROM reviews WHERE status='approved'"
        ).fetchone()["n"]
        reviews_rejected = self.db.execute(
            "SELECT COUNT(*) AS n FROM reviews WHERE status='rejected'"
        ).fetchone()["n"]
        jobs_total = self.db.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
        jobs_succeeded = self.db.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE status='succeeded'"
        ).fetchone()["n"]
        jobs_failed = self.db.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE status='failed'"
        ).fetchone()["n"]
        jobs_dead_letter = self.db.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE status='dead_letter'"
        ).fetchone()["n"]
        # Approval latency: avg(updated_at - created_at) in seconds for approved reviews
        rows = self.db.execute(
            "SELECT created_at, updated_at FROM reviews WHERE status='approved'"
        ).fetchall()
        deltas: list[float] = []
        for row in rows:
            try:
                a = datetime.fromisoformat(row["created_at"])
                b = datetime.fromisoformat(row["updated_at"])
                deltas.append(max(0.0, (b - a).total_seconds()))
            except ValueError:
                continue
        avg_delay = sum(deltas) / len(deltas) if deltas else 0.0
        return {
            "sessions": sessions,
            "reviews": reviews_total,
            "reviews_draft": reviews_draft,
            "reviews_approved": reviews_approved,
            "reviews_rejected": reviews_rejected,
            "jobs": jobs_total,
            "jobs_succeeded": jobs_succeeded,
            "jobs_failed": jobs_failed,
            "jobs_dead_letter": jobs_dead_letter,
            "discovery_rate": reviews_total / sessions if sessions else 0.0,
            "approval_rate": reviews_approved / reviews_total if reviews_total else 0.0,
            "duplicate_rate": 0.0,  # duplicate events return False without insert
            "avg_approval_delay_seconds": avg_delay,
        }
