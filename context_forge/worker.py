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


def count_complete_lines(path: Path) -> int:
    """Count newline-terminated lines only.

    A transcript may be mid-append, leaving a partial final line. We
    never advance the offset past a partial line, or the next pass
    would skip content that was still being written.
    """
    try:
        with path.open("rb") as f:
            return f.read().count(b"\n")
    except OSError:
        return 0


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
    total = count_complete_lines(p)
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
    return "\n".join(lines[: total - start_line]), total


def _sigterm_to_keyboard_interrupt(signum, frame):  # noqa: ARG001
    raise KeyboardInterrupt


def process_session(store: JobStore, vault: Vault,
                    gateway: LlmGateway,
                    session_id: str,
                    transcript_path: str | None,
                    transcript_hash: str | None,
                    project: str,
                    max_attempts: int = 3,
                    memory_store=None) -> str:
    """Process the new transcript lines for one session.

    Returns "processed", "skipped" (nothing new / nothing readable) or
    "failed: <error>". Shared by the queue worker and the transcript
    watcher so both paths behave identically.
    """
    try:
        start_line = store.get_lines_processed(session_id)
        slice_text, total_lines = _read_transcript_slice(
            transcript_path, start_line,
        )
        if not slice_text.strip():
            store.set_lines_processed(session_id, total_lines)
            return "skipped"

        # Feed the model a readable conversation, not raw JSONL.
        # CC transcripts are full of tool results and thinking blocks;
        # raw input makes lighter models improvise the output shape.
        from .transcript import extract_conversation, sanitize_text
        conversation = sanitize_text(
            extract_conversation(slice_text.splitlines())
        )
        if not conversation.strip():
            store.set_lines_processed(session_id, total_lines)
            return "skipped"

        import tempfile
        tmp = Path(tempfile.gettempdir()) / f"cf-slice-{session_id}.jsonl"
        tmp.write_text(conversation, encoding="utf-8")
        try:
            artifacts = gateway.extract_session(
                str(tmp), transcript_hash, session_id,
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
            _write_session_artifacts(vault, session_id, project, artifacts)
            if memory_store is not None:
                from .memory_lifecycle import MemoryLifecycle
                from .provider_http import PROMPT_VERSION
                model = getattr(gateway, "model", None) or getattr(gateway, "name", None)
                episode = store.latest_episode(session_id)
                MemoryLifecycle(memory_store, model=model, prompt_version=PROMPT_VERSION,
                                host=episode.host if episode else 'unknown').publish_session(
                    artifacts, session_id=session_id, project=project,
                    source_reference=transcript_path or "",
                )
        # Advance the offset even when nothing was saved, so the next
        # pass doesn't re-read the same lines.
        store.set_lines_processed(session_id, total_lines)
        return "processed"
    except Exception as exc:
        from .provider_http import RateLimited
        if isinstance(exc, RateLimited):
            # Offset intentionally left un-advanced: the next pass
            # retries the same slice. Nothing is lost.
            return "ratelimited"
        return f"failed: {type(exc).__name__}: {exc}"


def process_one(store: JobStore, vault: Vault,
                gateway: LlmGateway | None = None,
                max_attempts: int | None = None,
                memory_store=None) -> bool:
    """Claim one queued job and process it. True if a job was handled."""
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
        result = process_session(
            store, vault, gw, event.session_id, event.transcript_path,
            event.transcript_hash, event.project, max_attempts,
            memory_store,
        )
        if event.event_type is EventType.SESSION_END:
            store.auto_record_feedback_for_session(event.session_id)
        if result.startswith("failed:"):
            store.finish(job["id"], error=result[len("failed:"):].strip(),
                         max_attempts=max_attempts)
        else:
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


def process_pending_finalizations(store: JobStore, gateway, memory_store,
                                  projector=None) -> int:
    if memory_store is None:
        return 0
    from .finalization import FinalizationService
    from .provider_http import PROMPT_VERSION
    processed = 0
    service = FinalizationService(store, memory_store, gateway,
                                  model=getattr(gateway, "model", None),
                                  prompt_version=PROMPT_VERSION)
    for receipt in store.pending_receipts():
        try:
            memory_ids = service.finalize(receipt.finalization_id)
        except Exception as exc:
            with store.db:
                store.db.execute(
                    "UPDATE finalization_receipts SET status='failed',error=?,updated_at=? "
                    "WHERE finalization_id=?", (f"{type(exc).__name__}: {exc}",
                                                datetime.now(timezone.utc).isoformat(),
                                                receipt.finalization_id))
            continue
        if projector is not None:
            try:
                for memory_id in memory_ids:
                    projector.project(memory_id)
                store.mark_receipt_projected(receipt.finalization_id)
            except Exception as exc:
                store.record_projection_error(
                    receipt.finalization_id, f"projection: {type(exc).__name__}: {exc}")
        processed += 1
    return processed


def _write_session_artifacts(vault: Vault, session_id: str, project: str,
                              artifacts: SessionArtifacts) -> None:
    """Write knowledge + rule candidate.

    Each write is best-effort: a single failure (e.g. user-owned file)
    must not block the other writes.
    """
    if artifacts.knowledge is not None:
        try:
            vault.write_knowledge_auto(session_id, artifacts.knowledge, artifacts)
        except FileExistsError as exc:
            print(f"[worker] knowledge skip: {exc}")
        except Exception as exc:
            print(f"[worker] knowledge write failed: {exc}")

    if artifacts.rule_candidate is not None and artifacts.knowledge is not None:
        try:
            vault.write_rule_candidate(
                session_id, artifacts.knowledge.id, project,
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
             stop_after: int | None = None,
             memory_store=None) -> int:
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
                processed = process_one(store, vault, gateway, max_attempts, memory_store)
                process_pending_finalizations(store, gateway or NoOpGateway(), memory_store)
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
