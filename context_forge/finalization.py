from __future__ import annotations

import json
import uuid
from pathlib import Path

from .domain import FinalizationOutput
from .memory import MemoryStore, canonical_fingerprint, canonical_json, digest
from .memory_models import MemoryDraft, Provenance, Relation
from .queue import JobStore
from .transcript import sanitize_text


class FinalizationService:
    """Recoverable Episode -> long-term memory consolidation."""

    def __init__(self, queue: JobStore, memory: MemoryStore, gateway) -> None:
        self.queue = queue
        self.memory = memory
        self.gateway = gateway

    def finalize(self, finalization_id: str) -> list[str]:
        receipt = self.queue.receipt(finalization_id)
        if receipt is None:
            raise KeyError(finalization_id)
        episode = self.queue.episode(receipt.session_id, receipt.segment)
        if episode is None or episode.transcript_digest != receipt.episode_digest:
            raise ValueError("receipt episode no longer matches its frozen digest")
        if receipt.extraction_json is None:
            owner = str(uuid.uuid4())
            if not self.queue.claim_extraction(finalization_id, owner):
                return []
            transcript = sanitize_text(Path(episode.transcript_path).read_text(
                encoding="utf-8", errors="replace"))
            existing = self.memory.search(transcript[:1000], project=episode.project, limit=20)
            raw = self.gateway.finalize_episode({
                "schema_version": 1, "session_id": episode.session_id,
                "segment": episode.segment, "episode_digest": episode.transcript_digest,
                "transcript": transcript,
                "existing_candidates": [{"id": item.id, "version": item.version,
                                          "body": item.draft.body[:500]} for item in existing],
                "checkpoints": [item.model_dump(mode="json") for item in
                                self.queue.list_checkpoints(episode.session_id)],
            })
            output = FinalizationOutput.model_validate(raw)
            self._validate_output(output, episode.session_id, {item.id for item in existing})
            extraction_json = output.model_dump_json()
            operations = self._plan(receipt.finalization_id, output, episode.session_id,
                                    episode.project, episode.host)
            receipt = self.queue.freeze_finalization(finalization_id, extraction_json, operations)
        operations = self.queue.finalization_operations(finalization_id)
        results: list[str] = []
        for operation in operations:
            payload = json.loads(operation["payload_json"])
            if digest(canonical_json(payload)) != operation["request_hash"]:
                raise ValueError("frozen operation hash mismatch")
            draft = MemoryDraft.model_validate(payload["draft"])
            if operation["status"] != "committed":
                record = self.memory.put(
                    operation["memory_id"], draft,
                    expected_version=payload["expected_version"],
                    idempotency_key=operation["operation_key"], actor="model",
                    reason="episode finalization",
                    relations=tuple(Relation.model_validate(item)
                                    for item in payload.get("relations", [])),
                )
                self.queue.complete_finalization_operation(
                    operation["operation_key"], {"memory_id": record.id, "version": record.version},
                )
            results.append(operation["memory_id"])
        self.queue.mark_receipt_committed(finalization_id)
        return results

    @staticmethod
    def _validate_output(output: FinalizationOutput, session_id: str,
                         allowed_relation_targets: set[str] | None = None) -> None:
        allowed_relation_targets = allowed_relation_targets or set()
        ids = set()
        for candidate in output.candidates:
            if candidate.candidate_id in ids:
                raise ValueError("duplicate candidate_id")
            ids.add(candidate.candidate_id)
            source_ids = {source.identity for source in candidate.draft.sources}
            if sanitize_text(candidate.draft.body) != candidate.draft.body or any(
                    sanitize_text(source.excerpt) != source.excerpt for source in candidate.draft.sources):
                raise ValueError("candidate contains material requiring redaction")
            if not set(candidate.evidence_refs) <= source_ids:
                raise ValueError("unknown evidence ref")
            if candidate.draft.role == "observation" and session_id not in candidate.draft.provenance.sessions:
                raise ValueError("observation must reference the finalized session")
            for relation in candidate.relations:
                if relation.target_id not in allowed_relation_targets:
                    raise ValueError("relation target is not in the retrieved candidate set")
                if not set(relation.evidence_ids) <= source_ids:
                    raise ValueError("relation evidence must reference candidate sources")

    def _plan(self, finalization_id: str, output: FinalizationOutput,
              session_id: str, project: str | None, host: str = 'unknown') -> list[dict]:
        operations = []
        for ordinal, candidate in enumerate(output.candidates):
            draft = candidate.draft
            projects = tuple(sorted(set(draft.provenance.projects) | ({project} if project else set())))
            sessions = tuple(sorted(set(draft.provenance.sessions) | {session_id}))
            draft = draft.model_copy(update={
                "provenance": Provenance(projects=projects, sessions=sessions,
                                           paths=draft.provenance.paths, host=host),
            })
            exact = self.memory.find_exact(canonical_fingerprint(draft))
            memory_id = exact.id if exact else str(uuid.uuid5(uuid.NAMESPACE_URL,
                                                               f"context-forge:{finalization_id}:{ordinal}"))
            expected = exact.version if exact else 0
            if exact:
                merged_sources = {source.identity: source for source in exact.draft.sources}
                merged_sources.update({source.identity: source for source in draft.sources})
                draft = exact.draft.model_copy(update={
                    "sources": tuple(merged_sources.values()),
                    "provenance": draft.provenance,
                })
            payload = {"draft": draft.model_dump(mode="json"), "expected_version": expected,
                       "relations": [item.model_dump(mode="json") for item in candidate.relations]}
            operation_key = f"{finalization_id}:memory:{ordinal}"
            operations.append({
                "operation_key": operation_key, "ordinal": ordinal,
                "memory_id": memory_id, "payload": payload,
                "request_hash": digest(canonical_json(payload)),
            })
        return operations
