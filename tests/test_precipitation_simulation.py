"""End-to-end simulations closing the two gaps left in implement.md 任务 8, spot 2.

The unit suites pin the individual gates; these two drive the *whole* pipeline the way the
2026-09-23 incident did, so a regression in the wiring (not just in one function) still shows:

  sim 1 -- a two-hour session appended to ~30 times, each append followed by >60 s of silence,
           must write zero long-term memory until it ends, then exactly one precipitation.
  sim 2 -- the 64-row flood corpus: rewrites that are not deterministic exact duplicates are
           reported, never silently collapsed (R6: 相似 ≠ 等价); only exact duplicates archive.

Everything lives under tmp_path; no production library is opened.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from context_forge import watcher
from context_forge.episodes import EpisodeCollector
from context_forge.finalization import FinalizationService
from context_forge.hooks.claude_code import handle_payload
from context_forge.memory import MemoryStore
from context_forge.memory_lifecycle import MemoryLifecycle
from context_forge.memory_merge import MemoryMerge
from context_forge.memory_models import MemoryDraft, Provenance, Source
from context_forge.queue import JobStore
from context_forge.vault import Vault
from context_forge.worker import process_pending_finalizations

APPENDS = 30                 # "30 次静默" from the PRD
SILENCE_SECONDS = 61.0       # each append is followed by more than the 60 s settle window
STEP_SECONDS = 240.0         # 30 * 4 min == the two simulated hours


class RecordingGateway:
    """A fake finalizer that counts calls; one candidate per episode."""

    def __init__(self) -> None:
        self.calls = 0

    def finalize_episode(self, payload: dict) -> dict:
        self.calls += 1
        session = payload["session_id"]
        source = Source(identity=f"episode:{session}:{payload['segment']}:1",
                        kind="observation", session_id=session, excerpt="session work")
        return {
            "schema_version": 1,
            "candidates": [{
                "candidate_id": "lesson",
                "evidence_refs": [source.identity],
                "draft": MemoryDraft(
                    body="invalidate every cached admin permission signal when the role changes",
                    status="active",
                    provenance=Provenance(sessions=(session,)),
                    sources=(source,),
                ).model_dump(mode="json"),
            }],
            "deferred_topics": [],
            "no_memory_reason": None,
        }


def _append_line(transcript: Path, index: int) -> None:
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "cwd": str(transcript.parent),
            "type": "user",
            "message": {"content": f"working on the permission cache, step {index}"},
        }) + "\n")


# ---------------------------------------------------------------------------------------
# sim 1 -- PRD AC #1: 30 silences over two hours, zero long-term memory before the end.
# ---------------------------------------------------------------------------------------

def test_two_hour_thirty_silence_session_writes_nothing_until_it_ends(tmp_path: Path,
                                                                      monkeypatch) -> None:
    projects = tmp_path / "projects" / "demo"
    projects.mkdir(parents=True)
    transcript = projects / "session-two-hour.jsonl"
    monkeypatch.setattr(watcher, "transcript_root", lambda: tmp_path / "projects")

    store = JobStore(tmp_path / "queue.db")
    vault = Vault(tmp_path / "vault")
    memory = MemoryStore.initialize(tmp_path / "memory.db")
    gateway = RecordingGateway()

    # Simulated wall clock; inject it instead of sleeping for two hours.
    clock = time.time()
    for index in range(APPENDS):
        _append_line(transcript, index)
        clock += STEP_SECONDS
        # The file was last touched more than the settle window ago: user is quiet.
        os.utime(transcript, (clock - SILENCE_SECONDS, clock - SILENCE_SECONDS))

        watcher.scan_once(store, vault, gateway, settle_seconds=60.0,
                          baseline=0, now=clock)

        # The old bug: 32 precipitations in 70 minutes. Nothing may reach long-term memory,
        # and the model must never be consulted while the session is merely quiet.
        assert memory.current_records() == []
        assert list(vault.root.rglob("*.md")) == []
        assert gateway.calls == 0
        assert store.pending_receipts() == []

    # Only the Episode collection watermark advanced; the session is still open.
    episode = store.latest_episode("session-two-hour")
    assert episode.collected_through_line == APPENDS
    assert episode.state == "open"
    assert store.active_episodes()[0].session_id == "session-two-hour"

    # Now the session actually ends -- exactly one receipt, exactly one precipitation.
    handle_payload({"hook_event_name": "SessionEnd", "session_id": "session-two-hour",
                    "transcript_path": str(transcript)}, store=store)
    assert len(store.pending_receipts()) == 1

    assert process_pending_finalizations(store, gateway, memory) == 1
    assert gateway.calls == 1
    assert len(memory.current_records()) == 1
    assert store.metrics()["episodes_inferred"] == 0     # explicit end, not a guess


# ---------------------------------------------------------------------------------------
# sim 2 -- PRD AC #7: the 64-row flood corpus. Two clauses, two assertion groups.
# ---------------------------------------------------------------------------------------

# 23-token core. Each rewrite appends one distinct token, so any pair keeps 23/25 = 0.92
# token overlap -- above the 0.90 hygiene-report threshold -- while every body (and therefore
# every canonical fingerprint) stays distinct.
LESSON_CORE = ("when a user changes their own role every cached admin permission signal must "
               "be invalidated and refetched before any authorization decision is made")


def _seed(store: MemoryStore, memory_id: str, body: str) -> None:
    store.put(memory_id, MemoryDraft(body=body, status="active"), expected_version=0,
              idempotency_key=f"seed:{memory_id}", reason="flood corpus replay")


def _flood_corpus() -> dict[str, str]:
    """One lesson as 31 rewrites + the odd unrelated item, padded to the incident's 64 rows."""
    corpus: dict[str, str] = {}
    for index in range(31):
        corpus[f"lesson-{index:02d}"] = f"{LESSON_CORE} variant{index}"
    corpus["proxy-asymmetry"] = "the dev and prod proxies disagree on forwarded headers"
    # A genuinely deterministic exact pair: same body, same empty boundary -> same fingerprint.
    corpus["dup-a"] = "rotate signing keys at least once a year"
    corpus["dup-b"] = "rotate signing keys at least once a year"
    fillers = [
        "never log raw tokens", "pin the toolchain version", "cap concurrent uploads",
        "reject symlinked config paths", "warm the cache before load tests",
        "index transcripts by session id", "retry idempotent network calls once",
        "store timestamps in UTC", "close sqlite connections after each write",
        "validate external input at the boundary", "prefer streaming over buffering",
        "fail open on telemetry only", "name migrations by date", "hash user emails at rest",
        "limit recall to a byte budget", "version every schema change",
        "never mutate a frozen snapshot", "sort output deterministically",
        "deduplicate before scoring", "cap report size with an explicit marker",
        "treat unknown liveness as unknown", "keep receipts for replay",
        "archive only exact duplicates", "report similar items for review",
        "derive ids from content not wording", "keep provenance out of fingerprints",
        "narrow access never widen it", "decay needs age and no fresh evidence",
        "a preference never expires by non-use", "annotate conflicts instead of deleting",
    ]
    for index, body in enumerate(fillers):
        corpus[f"filler-{index:02d}"] = body
    return corpus


def test_flood_corpus_replay_reports_rewrites_and_archives_only_exact_duplicates(
        tmp_path: Path) -> None:
    store = MemoryStore.initialize(tmp_path / "memory.db")
    corpus = _flood_corpus()
    assert len(corpus) == 64
    for memory_id, body in corpus.items():
        _seed(store, memory_id, body)

    # R6 / 相似 ≠ 等价: distinct wording is never silently collapsed on the write path.
    assert len(store.current_records()) == 64

    lesson_ids = {f"lesson-{index:02d}" for index in range(31)}
    hygiene = MemoryLifecycle(store).hygiene_report()

    # Clause A -- 存量 31 条进入卫生报告. The lifecycle report (merge_candidates at 0.90) must
    # surface every rewrite; they are listed as `overlaps`, never as exact duplicates.
    reported = {candidate.left_id for candidate in hygiene.merges}
    reported |= {candidate.right_id for candidate in hygiene.merges}
    assert lesson_ids <= reported
    assert all(candidate.kind == "overlaps" for candidate in hygiene.merges
               if lesson_ids & {candidate.left_id, candidate.right_id})

    # The bare merge report agrees: the only exact pair is the deterministic duplicate pair.
    report = MemoryMerge(store).report(similarity_threshold=0.6)
    exact_pairs = {frozenset((c.left_id, c.right_id)) for c in report.exact_duplicates}
    assert exact_pairs == {frozenset(("dup-a", "dup-b"))}
    assert not any(lesson_ids & {c.left_id, c.right_id} for c in report.exact_duplicates)

    # Clause B -- 只有确定性精确重复可自动可逆归档. One pair archives; the 31 rewrites stay.
    applied = MemoryLifecycle(store).apply_auto_tier()
    assert applied["exact_applied"] == 1
    assert store.get("dup-b").draft.status == "archived"
    assert store.get("dup-a").draft.status == "active"
    for memory_id in lesson_ids:
        assert store.get(memory_id).draft.status == "active"
    # Nothing was deleted -- rows survive as versions; only the exact twin left recall.
    assert len(store.current_records()) == 64


# ---------------------------------------------------------------------------------------
# sim 2, companion -- "no single session produces 31 separate long-term entities" when the
# lesson is re-precipitated with a *deterministic identical* body: exact reuse keeps one row.
# ---------------------------------------------------------------------------------------

def test_repeated_identical_precipitation_stays_one_entity(tmp_path: Path) -> None:
    queue = JobStore(tmp_path / "queue.db")
    memory = MemoryStore.initialize(tmp_path / "memory.db")
    collector = EpisodeCollector(queue)
    gateway = RecordingGateway()

    written: list[str] = []
    for index in range(31):
        session = f"session-{index:02d}"
        transcript = tmp_path / f"{session}.jsonl"
        transcript.write_text('{"message":"permission cache work"}\n', encoding="utf-8")
        collector.collect(session, transcript, "demo", "claude_code")
        receipt = collector.request_finalize(session, end_kind="manual",
                                             event_id=f"manual-{session}")
        written.append(FinalizationService(queue, memory, gateway).finalize(
            receipt.finalization_id)[0])

    assert len({*written}) == 1                      # one entity, thirty-one precipitations
    assert gateway.calls == 31                       # the model still ran; identity is stable
    records = memory.current_records()
    assert len(records) == 1
    assert records[0].version == 31
    assert len(records[0].draft.provenance.sessions) == 31
