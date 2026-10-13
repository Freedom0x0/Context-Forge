"""Host-neutral lifecycle adapter.

Other hosts do not need to imitate Claude's hook names. They emit the shared
SessionLifecycleEvent JSON contract and this adapter applies the same capture
and finalization pipeline.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..domain import SessionLifecycleEvent
from ..episodes import EpisodeCollector
from ..queue import JobStore


def handle_event(payload: dict, *, store: JobStore) -> SessionLifecycleEvent:
    """Ingest one host-neutral lifecycle event and schedule its next step."""
    event = SessionLifecycleEvent.model_validate(payload)
    transcript = Path(event.transcript_path).expanduser()
    if not transcript.is_file():
        raise ValueError("transcript_path must point to an existing file")
    store.ingest_session_event(event)
    collector = EpisodeCollector(store)
    collector.collect(event.session_id, transcript, payload.get("project"), event.source)
    if event.type == "pre_compact":
        collector.checkpoint(event)
    else:
        collector.request_finalize(event.session_id, end_kind="explicit", event_id=event.event_id)
    return event


def handle_json(text: str, *, store: JobStore) -> SessionLifecycleEvent:
    """Decode one JSON object from a host and delegate to :func:`handle_event`."""
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("host event must be one JSON object") from exc
    if not isinstance(payload, dict):
        raise ValueError("host event must be one JSON object")
    return handle_event(payload, store=store)
