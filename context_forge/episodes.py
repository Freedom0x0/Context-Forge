from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .domain import EpisodeCheckpoint, EpisodeRecord, FinalizationReceipt, SessionLifecycleEvent
from .queue import JobStore, _now
from .transcript import sanitize_text


def _complete_text(path: Path) -> tuple[str, int]:
    data = path.read_bytes()
    complete = data.rsplit(b"\n", 1)[0] if not data.endswith(b"\n") else data[:-1]
    count = data.count(b"\n")
    return complete.decode("utf-8", errors="replace"), count


class EpisodeCollector:
    def __init__(self, store: JobStore) -> None:
        self.store = store

    def collect(self, session_id: str, transcript_path: Path, project: str | None,
                host: str) -> EpisodeRecord:
        text, lines = _complete_text(Path(transcript_path))
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        current = self.store.latest_episode(session_id)
        if (current and current.collected_through_line == lines
                and current.transcript_digest == digest):
            return current
        moment = _now()
        segment = current.segment if current else 1
        start_line = 0
        if current and current.state == "finalized":
            segment += 1
            start_line = current.collected_through_line
            sliced = "\n".join(text.splitlines()[start_line:])
            digest = hashlib.sha256(sliced.encode("utf-8")).hexdigest()
        record = EpisodeRecord(
            session_id=session_id, segment=segment, project=project, host=host,
            transcript_path=str(Path(transcript_path).resolve()),
            collected_through_line=lines, transcript_digest=digest,
            state="open", started_at=moment if segment != (current.segment if current else 1) else (current.started_at if current else moment),
            last_activity_at=moment,
        )
        return self.store.save_episode(record)

    def advance_inferred(self, now: datetime | None = None, *, process_alive: bool,
                         active_tools: bool, pending_after: timedelta = timedelta(minutes=30),
                         grace: timedelta = timedelta(minutes=30)) -> list[FinalizationReceipt]:
        """Advance conservative abnormal-end detection by one state transition."""
        if process_alive or active_tools:
            return []
        now = now or datetime.now(timezone.utc)
        receipts: list[FinalizationReceipt] = []
        for episode in self.store.active_episodes():
            last = datetime.fromisoformat(episode.last_activity_at)
            if episode.state == "open" and now - last >= pending_after:
                self.store.save_episode(episode.model_copy(update={
                    "state": "inferred_pending", "last_activity_at": now.isoformat(),
                }))
            elif episode.state == "inferred_pending" and now - last >= grace:
                event_id = f"inferred:{episode.session_id}:{episode.segment}:{episode.transcript_digest}"
                receipts.append(self.request_finalize(
                    episode.session_id, end_kind="inferred", event_id=event_id,
                ))
        return receipts

    def checkpoint(self, event: SessionLifecycleEvent) -> EpisodeCheckpoint:
        episode = self.store.episode(event.session_id)
        if episode is None:
            raise KeyError(event.session_id)
        text, _ = _complete_text(Path(episode.transcript_path))
        snapshot = sanitize_text(text)
        digest = hashlib.sha256(snapshot.encode("utf-8")).hexdigest()
        checkpoint = EpisodeCheckpoint(
            checkpoint_id=f"checkpoint-{digest[:24]}", session_id=episode.session_id,
            segment=episode.segment, start_line=0,
            end_line=min(event.final_line_count or episode.collected_through_line,
                         episode.collected_through_line),
            input_digest=digest, snapshot=snapshot, created_at=_now(),
        )
        return self.store.save_checkpoint(checkpoint)

    def request_finalize(self, session_id: str, *, end_kind: str,
                         event_id: str) -> FinalizationReceipt:
        episode = self.store.latest_episode(session_id)
        if episode is None:
            raise KeyError(session_id)
        moment = _now()
        receipt = self.store.create_receipt(FinalizationReceipt(
            finalization_id=f"finalize:{event_id}", session_id=session_id,
            segment=episode.segment, episode_digest=episode.transcript_digest,
            end_kind=end_kind, status="extracting", created_at=moment, updated_at=moment,
        ))
        if receipt.status == "extracting":
            updated = episode.model_copy(update={"state": "finalizing", "end_kind": end_kind})
            self.store.save_episode(updated)
        return receipt

    def delete(self, session_id: str, memory_store=None) -> dict[str, int]:
        tombstones = memory_store.tombstone_session_sources(session_id) if memory_store else 0
        counts = self.store.delete_session(session_id)
        counts["source_tombstones"] = tombstones
        return counts

    def apply_retention(self, days: int, *, memory_store=None,
                        now: datetime | None = None) -> list[str]:
        targets = self.store.episode_retention_preview(days, at=now)
        for session_id in targets:
            self.delete(session_id, memory_store)
        return targets
