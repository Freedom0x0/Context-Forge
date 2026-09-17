from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .domain import SessionEvent


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
                  "rule_feedback": 0, "rule_hit_events": 0}
        with self.db:
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
