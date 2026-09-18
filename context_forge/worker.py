"""Worker: claim a job, extract session artifacts, write vault files.

The auto-loop is intentional: worker calls the model, gets one
`SessionArtifacts` per session, and writes:

  vault/knowledge/accepted/{k_id}.md       auto-published knowledge
  vault/rules/proposals/rule-{k_id}.md     enabled rule

User actions are file-based, never command-based: edit, archive,
or change frontmatter `status`. `user_owned: true` blocks auto-
overwrite of hand-written entries.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from .config import load_settings
from .domain import (
    EventType,
    LlmGateway,
    SessionArtifacts,
    SessionEvent,
)
from .gateways import NoOpGateway, select_gateway
from .queue import JobStore
from .vault import Vault


def _read_transcript_slice(transcript_path: str, start_line: int
                              ) -> tuple[str, int]:
    """Return (slice_text, total_lines). Slices transcript from start_line.

    On resume, only the new portion is passed to the model so already-
    processed turns don't get re-precipitated.
    """
    if not transcript_path:
        return "", 0
    p = Path(transcript_path)
    if not p.exists():
        return "", 0
    total = 0
    with p.open("rb") as f:
        for _ in f:
            total += 1
    if start_line > total:
        start_line = 0  # transcript was rotated/truncated
    if start_line >= total:
        return "", total
    lines: list[str] = []
    with p.open("r", encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            if i < start_line:
                continue
            lines.append(line.rstrip("\n"))
    return "\n".join(lines), total


def _sigterm_to_keyboard_interrupt(signum, frame):  # noqa: ARG001
    raise KeyboardInterrupt


def process_one(store: JobStore, vault: Vault,
                gateway: LlmGateway | None = None,
                max_attempts: int | None = None) -> bool:
    """Process a single queued job. Returns True if a job was handled.

    Side effects on success:
    - vault/knowledge/accepted/{k_id}.md if knowledge is non-null
    - vault/rules/proposals/rule-{k_id}.md if rule_candidate is non-null

    Files flagged `user_owned: true` are skipped (not overwritten).
    """
    from . import status as status_mod

    job = store.claim()
    if job is None:
        return False
    gw = gateway or NoOpGateway()
    if max_attempts is None:
        settings = load_settings()
        max_attempts = settings.max_attempts if settings else 3
    try:
        event = SessionEvent.model_validate_json(job["payload_json"])

        # Read only the NEW portion of the transcript (since last
        # processing of this session). On first call, reads everything.
        start_line = store.get_lines_processed(event.session_id)
        slice_text, total_lines = _read_transcript_slice(
            event.transcript_path, start_line,
        )

        # If the slice is empty (worker already processed the full file),
        # skip the model call but still mark the job succeeded.
        if not slice_text.strip():
            store.set_lines_processed(event.session_id, total_lines)
            store.finish(job["id"], max_attempts=max_attempts)
            return True

        # Stage the slice as a temp file so the gateway reads it as if
        # it were a real transcript. Clean up after.
        import tempfile
        tmp = Path(tempfile.gettempdir()) / f"cf-slice-{event.session_id}.jsonl"
        tmp.write_text(slice_text, encoding="utf-8")
        try:
            artifacts = gw.extract_session(
                str(tmp), event.transcript_hash, event.session_id,
            )
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

        # Re-run domain validators so gateways that bypass Pydantic at
        # construction time still have to satisfy the contract.
        artifacts = SessionArtifacts.model_validate(artifacts.model_dump())
        if artifacts.should_save:
            _write_session_artifacts(vault, event, artifacts)
        if event.event_type is EventType.SESSION_END:
            store.auto_record_feedback_for_session(event.session_id)
        # Always advance the offset, even on should_save=false, so we
        # don't re-read the same lines next time.
        store.set_lines_processed(event.session_id, total_lines)
        store.finish(job["id"], max_attempts=max_attempts)
    except Exception as exc:
        store.finish(job["id"], error=f"{type(exc).__name__}: {exc}",
                     max_attempts=max_attempts)
    finally:
        try:
            status_mod.write_status_file(
                status_mod.status_path(),
                status_mod.collect(store),
            )
        except Exception:
            pass
    return True


def _write_session_artifacts(vault: Vault, event: SessionEvent,
                              artifacts: SessionArtifacts) -> None:
    """Write knowledge + rule candidate.

    Each write is best-effort: a single failure (e.g. user-owned file)
    must not block the other writes.
    """

    if artifacts.knowledge is not None:
        try:
            vault.write_knowledge_auto(
                event.session_id, artifacts.knowledge, artifacts,
            )
        except FileExistsError as exc:
            # user_owned file -- skip, do not overwrite
            print(f"[worker] knowledge skip: {exc}")
        except Exception as exc:
            print(f"[worker] knowledge write failed: {exc}")

    if artifacts.rule_candidate is not None and artifacts.knowledge is not None:
        try:
            vault.write_rule_candidate(
                event.session_id,
                artifacts.knowledge.id,
                event.project,
                artifacts.rule_candidate,
            )
        except FileExistsError as exc:
            print(f"[worker] rule skip: {exc}")
        except Exception as exc:
            print(f"[worker] rule write failed: {exc}")


def run_loop(store: JobStore, vault: Vault,
             gateway: LlmGateway | None = None,
             interval_seconds: float = 2.0,
             max_attempts: int = 3,
             stop_after: int | None = None) -> int:
    """Long-running worker. `stop_after` lets tests terminate cleanly."""
    import signal
    import time
    try:
        signal.signal(signal.SIGTERM, _sigterm_to_keyboard_interrupt)
    except (AttributeError, ValueError):
        pass
    processed_total = 0
    print(f"[worker] loop started; interval={interval_seconds}s "
          f"max_attempts={max_attempts}")
    try:
        while True:
            try:
                processed = process_one(store, vault, gateway, max_attempts)
            except Exception as exc:  # noqa: BLE001
                print(f"[worker] loop error: {type(exc).__name__}: {exc}")
                processed = False
            if processed:
                processed_total += 1
                if stop_after is not None and processed_total >= stop_after:
                    break
            else:
                if stop_after is not None and processed_total >= stop_after:
                    break
                time.sleep(interval_seconds)
    except KeyboardInterrupt:
        print("[worker] interrupted; exiting")
    return processed_total


__all__ = [
    "NoOpGateway",
    "process_one",
    "run_loop",
    "select_gateway",
]
