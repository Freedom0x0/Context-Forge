"""P3 -- application attribution, feedback weight and the three reports.

Gate power (recorded before the assertions below, per the quality guideline
"A gate you cannot fail is not a gate"). Each guard was mutated and the result
recorded:

  Guard    Mutation that should RED it                       Result
  ------------------------------------------------------------------------
  t1       change WEIGHT_TABLE['applied'] to 0.0             RED
  t2       add `evidence_level='verified'` write in          RED
           record_retrieval
  t3       let `_weight_events` select the exposure stages   RED
           (`presented`/`referenced`) as weight events
  t5b      drop the `if confirmed:` gate in                  RED
           _weight_events
  t5c      set evidence_level='contradicted' in              RED
           record_feedback
  t5d      skip the validation_source_ids subset check       RED
  t6       raise the support() cap to 'verified'             RED
  P3.6     pass feedback=0.0 in search_scored                RED
"""

from __future__ import annotations

import inspect
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from context_forge.memory import (
    DEFAULT_WEIGHT, MemoryStore, WeightEvent, apply_feedback_weight,
)
from context_forge.memory_effectiveness import MemoryEffectiveness
from context_forge.memory_lifecycle import Candidate, MemoryLifecycle
from context_forge.memory_models import (
    Applicability, ApplicabilityScope, MemoryApplication, MemoryDraft, Provenance,
    Relation, Source,
)

TOOL = Source(identity="tool:m", kind="tool", excerpt="observed behaviour")


def seed(store, memory_id, body="proxy timeout policy", *, role="claim", scope=None,
         provenance=None, sources=(TOOL,), relations=(), **overrides):
    draft = MemoryDraft(
        body=body, summary=memory_id, role=role, status="active",
        scope=scope or ApplicabilityScope(), provenance=provenance or Provenance(),
        sources=sources, **overrides)
    return store.put(memory_id, draft, expected_version=0,
                     idempotency_key=f"put:{memory_id}", reason="test", relations=relations)


def application(memory_id, *, attribution="confirmed", version=1, app_id="app-1",
                source_ids=("tool:m",), actual="fixed"):
    return MemoryApplication(
        application_id=app_id, task_id="task-1", memory_id=memory_id,
        memory_version=version, action="applied the rule",
        expected_outcome="the timeout disappears", actual_outcome=actual,
        validation_source_ids=source_ids, attribution=attribution)


def applied_stage(store, memory_id, *, version=1, request="req"):
    store.record_retrieval(memory_id, version, request_id=request, stage="applied",
                           idempotency_key=f"{request}:applied:{memory_id}:{version}")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# P3.t1 -- the weight table, one entry at a time, plus the revocation rule
def test_weight_table_entries(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    record = seed(store, "m")
    expected = {"applied": 0.3, "validated": 0.5, "revoked": -0.8,
                "harmful": -1.0, "irrelevant": -0.2}
    for kind, value in expected.items():
        assert apply_feedback_weight(record, [WeightEvent(kind)]) == pytest.approx(value)
    assert apply_feedback_weight(record, []) == 0.0
    revised = store.record_feedback("m", outcome="revoked", idempotency_key="fb:1",
                                    note="undo the rule")
    assert revised.draft.evidence_level != "contradicted"
    assert store.get("m").draft.evidence_level != "contradicted"
    assert store.weight("m") == pytest.approx(DEFAULT_WEIGHT - 0.8)


# P3.t2 -- GUARD: no exposure path may write evidence_level='verified'
def test_exposure_path_never_writes_verified():
    for function in (MemoryStore.record_retrieval, MemoryStore._weight_events,
                     apply_feedback_weight, MemoryStore._recompute_weight):
        source = inspect.getsource(function)
        assert "'verified'" not in source and "evidence_level" not in source


# P3.t3 -- GUARD: presented / referenced never produce positive weight
def test_exposure_stages_never_reinforce(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    seed(store, "noisy")
    store.record_application(application("noisy"))
    for stage in ("retrieved", "tool_returned", "presented", "referenced"):
        for number in range(5):
            store.record_retrieval("noisy", 1, request_id=f"r{number}", stage=stage,
                                   idempotency_key=f"{stage}:{number}")
    assert store.weight("noisy") == DEFAULT_WEIGHT
    scores = {r.id: score for r, score in store.search_scored("proxy", project="demo")}
    assert scores["noisy"].feedback == 0.0
    applied_stage(store, "noisy")
    assert store.weight("noisy") == pytest.approx(DEFAULT_WEIGHT + 0.3)


# P3.t4 -- the four evidence directions; an overturned memory is not deleted
def test_evidence_directions_dispatch(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    lifecycle = MemoryLifecycle(store)

    seed(store, "supported", evidence_level="unverified")
    supported = lifecycle.support("supported", Source(identity="tool:s2", kind="tool", excerpt="again"),
                                  reason="more evidence", expected_version=1, idempotency_key="ev:s")
    assert supported.version == 2
    assert supported.draft.evidence_level == "supported"
    assert supported.draft.proof_count == 1

    seed(store, "narrowed")
    narrowed = lifecycle.weaken("narrowed", Source(identity="tool:w", kind="tool", excerpt="only on linux"),
                                "windows", reason="counterexample", expected_version=1,
                                idempotency_key="ev:w")
    assert Applicability(condition="windows", result="false", evidence_ids=("tool:w",)) in narrowed.draft.applicability

    seed(store, "refiner")
    seed(store, "refined-target")
    extended = lifecycle.extend("refiner", target_id="refined-target", kind="refines",
                                source=Source(identity="tool:x", kind="tool", excerpt="narrower case"),
                                reason="extend", expected_version=1, idempotency_key="ev:x")
    assert extended.version == 2
    kinds = {(item["kind"], item["target_id"]) for item in store.relations("refiner")}
    assert ("refines", "refined-target") in kinds

    seed(store, "refuted")
    replacement = Candidate("replacement", MemoryDraft(body="proxy timeout policy", role="claim",
                                                       status="active", evidence_level="unverified",
                                                       sources=(Source(identity="tool:r", kind="tool",
                                                                       excerpt="different root cause"),)))
    created = lifecycle.overturn("refuted", replacement,
                                 Source(identity="tool:o", kind="tool", excerpt="refutes"),
                                 reason="refutation", expected_version=1, idempotency_key="ev:o")
    assert store.get("refuted") is not None
    assert store.get("refuted").version == 1
    assert {(item["kind"], item["target_id"]) for item in store.relations("replacement")} == {
        ("contradicts", "refuted")}


# P3.t5 -- the application report counts a known event sequence
def test_application_report_counts_known_sequence(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    seed(store, "m")
    store.record_retrieval("m", 1, request_id="r", stage="referenced", idempotency_key="e:ref")
    store.record_retrieval("m", 1, request_id="r", stage="referenced", idempotency_key="e:ref2")
    applied_stage(store, "m")
    store.record_retrieval("m", 1, request_id="r", stage="validated", idempotency_key="e:val")
    store.record_feedback("m", outcome="revoked", idempotency_key="fb:r", note="undo")
    assert MemoryEffectiveness(store).application_report("m") == {
        "referenced": 2, "applied": 1, "validated": 1, "revoked": 1}


# P3.t5b -- GUARD: a non-confirmed application changes nothing
def test_unconfirmed_application_changes_nothing(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    seed(store, "m", evidence_level="unverified")
    store.record_application(application("m", attribution="plausible"))
    applied_stage(store, "m")
    assert store.weight("m") == DEFAULT_WEIGHT
    assert store.get("m").draft.evidence_level == "unverified"
    assert store.feedback("m") == []

    store.record_application(application("m", attribution="unknown", app_id="app-2"))
    store.record_retrieval("m", 1, request_id="r", stage="validated", idempotency_key="e:val")
    assert store.weight("m") == DEFAULT_WEIGHT
    assert store.get("m").draft.evidence_level != "contradicted"


# P3.t5c -- GUARD: revocation must not declare a memory contradicted
def test_revocation_never_marks_contradicted(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    seed(store, "m", evidence_level="supported")
    store.record_feedback("m", outcome="revoked", idempotency_key="fb:r", note="corrected")
    revised = store.record_feedback("m", outcome="irrelevant", idempotency_key="fb:i", note="no use")
    assert revised.draft.evidence_level == "supported"
    assert store.relations("m") == []


# P3.t5d -- a validation source outside the version is refused
def test_application_write_validation(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    seed(store, "m")
    with pytest.raises(ValueError):
        store.record_application(application("m", source_ids=("tool:absent",)))
    assert store.applications("m") == []
    store.record_application(application("m", source_ids=("tool:m",)))
    assert len(store.applications("m")) == 1


# P3.t6 -- supporting evidence caps at `supported`, never `verified`
def test_support_never_reaches_verified(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    lifecycle = MemoryLifecycle(store)
    seed(store, "m", evidence_level="supported")
    promoted = lifecycle.support("m", Source(identity="tool:plain", kind="observation", excerpt="noised"),
                                 reason="more evidence", expected_version=1, idempotency_key="ev:1")
    assert promoted.draft.evidence_level == "supported"
    again = lifecycle.support("m", TOOL, reason="more evidence", expected_version=2, idempotency_key="ev:2")
    assert again.draft.evidence_level != "verified"


# P3.t7 -- long-unused memories are listed; non-use is not a hallucination verdict
def test_tidy_and_anomaly_candidates(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    seed(store, "verified-unused", evidence_level="verified")
    seed(store, "preference-unused", role="preference", provenance=Provenance(projects=("demo",)))
    seed(store, "used", evidence_level="verified")
    applied_stage(store, "used")
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE memory_versions SET created_at=?",
                           ("2020-01-01T00:00:00+00:00",))
    report = MemoryEffectiveness(store)
    anomaly = {record.id for record in report.anomaly_candidates()}
    assert anomaly == {"verified-unused"}
    tidy = {record.id for record in report.tidy_candidates(days=90)}
    assert tidy == {"verified-unused"}
    assert "preference-unused" not in tidy


# P3.6 -- the feedback dimension carries the persisted weight (not the default 0)
def test_weight_feeds_the_feedback_dimension(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    seed(store, "aa-rewarded")
    seed(store, "zz-plain")
    store.record_application(application("aa-rewarded"))
    applied_stage(store, "aa-rewarded")
    scores = {r.id: score for r, score in store.search_scored("proxy", project="demo")}
    assert scores["aa-rewarded"].feedback == pytest.approx(0.3)
    assert scores["zz-plain"].feedback == 0.0
    assert scores["aa-rewarded"].total > scores["zz-plain"].total


# P3 -- a harmful verdict flips a non-protected record to `conflicted`
def test_harmful_feedback_flags_conflicted(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    seed(store, "m")
    revised = store.record_feedback("m", outcome="harmful", idempotency_key="fb:h", note="broke prod")
    assert revised.draft.status == "conflicted"
    assert store.get("m").draft.status == "conflicted"
    assert store.weight("m") == pytest.approx(DEFAULT_WEIGHT - 1.0)


# P3 -- protected memories are never flipped by automatic feedback
def test_harmful_feedback_leaves_protected_records(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    seed(store, "m", user_owned=True)
    revised = store.record_feedback("m", outcome="harmful", idempotency_key="fb:h", note="broke prod")
    assert revised.draft.status == "active"
    assert store.weight("m") == pytest.approx(DEFAULT_WEIGHT - 1.0)


# 必做 C2 -- a constraints-only pattern is admitted on a decided constraint hit
def test_constraints_only_pattern_is_admitted(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    seed(store, "constraint-pattern", scope=ApplicabilityScope(constraints=("strip",)))
    assert {r.id for r in store.search("proxy", project="demo", domains=frozenset({"strip"}))} == {
        "constraint-pattern"}
    assert store.search("proxy", project="demo", domains=frozenset({"other"})) == []


# P3 export -- the three reports come out through the CLI as JSON
def test_cli_exports_the_three_reports(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXT_FORGE_HOME", str(tmp_path))
    store = MemoryStore.initialize(tmp_path / "memory.db")
    seed(store, "m", evidence_level="verified")
    import context_forge.cli as cli
    from typer.testing import CliRunner

    result = CliRunner().invoke(cli.app, ["memory-report"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert set(payload) == {"applications", "anomaly_candidates", "tidy_candidates",
                            "exposure_vs_adoption"}
    assert payload["exposure_vs_adoption"]["adoption"] == 0
