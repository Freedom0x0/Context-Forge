from __future__ import annotations

import hashlib
from pathlib import Path

from .domain import EpisodeCheckpoint, EpisodeRecord, SessionLifecycleEvent
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

    def collect(self, session_id: str, transcript_path: Path, project: str | None) -> EpisodeRecord:
        text, lines = _complete_text(Path(transcript_path))
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        current = self.store.episode(session_id)
        if current and current.collected_through_line == lines and current.transcript_digest == digest:
            return current
        moment = _now()
        record = EpisodeRecord(
            session_id=session_id, project=project,
            transcript_path=str(Path(transcript_path).resolve()),
            collected_through_line=lines, transcript_digest=digest,
            state="reopened" if current and current.state == "finalized" else (current.state if current else "open"),
            started_at=current.started_at if current else moment,
            last_activity_at=moment, end_kind=current.end_kind if current else None,
            retention_deadline=current.retention_deadline if current else None,
        )
        return self.store.save_episode(record)

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
