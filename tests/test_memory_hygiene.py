"""P4 -- memory hygiene: consolidation, decay, merge/conflict and protection.

Gate power (recorded before the assertions, per the quality guideline
"A gate you cannot fail is not a gate"). Each guard was mutated and the result
recorded:

  Guard                Mutation that should RED it                     Result
  ---------------------------------------------------------------------------
  consolidation        drop the `len(_origins) < min_sources` gate      RED
  origin independence  count origins as projects only                  RED
  provenance retained  build the abstraction without unioned projects  RED
  decay / preference   remove the `role == 'preference'` skip in          RED
                       tidy_candidates
  merge split          drop the `_applicability_conflict` branch        RED
  disputed condition   un-latch the applicability-conflict set         RED
  access widening      delete `access_broader` in _put                  RED
  privacy widening     drop the privacy clause of `access_broader`      RED
  protected refusal    delete the protected check in _put               RED
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from context_forge.memory import MemoryStore, ProtectedMemory
from context_forge.memory_lifecycle import Candidate, MemoryLifecycle
from context_forge.memory_merge import MemoryMerge
from context_forge.memory_models import (
    AccessPolicy, Applicability, ApplicabilityScope, MemoryDraft, MemorySpace,
    Provenance, Source,
)

OBSERVED = Source(identity="tool:obs", kind="tool", excerpt="observed behaviour")


def seed(store, memory_id, body="shared policy", *, role="claim", status="active",
         project="demo", session=None, technologies=(), domains=(), exclusions=(),
         applicability=(), sources=(OBSERVED,), access=None, space=None,
         user_owned=False, suppressed=False, **overrides):
    provenance = Provenance(projects=(project,) if project else (),
                            sessions=(session,) if session else ())
    draft = MemoryDraft(
        body=body, summary=memory_id, role=role, status=status, provenance=provenance,
        scope=ApplicabilityScope(technologies=tuple(technologies), domains=tuple(domains),
                                 exclusions=tuple(exclusions)),
        applicability=tuple(applicability), sources=sources, user_owned=user_owned,
        suppressed=suppressed, access=access or AccessPolicy(),
        space=space or MemorySpace(), **overrides)
    return store.put(memory_id, draft, expected_version=0,
                     idempotency_key=f"put:{memory_id}", reason="test")


def backdate(store, memory_id, stamp="2020-01-01T00:00:00+00:00"):
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE memory_versions SET created_at=? WHERE memory_id=?",
                           (stamp, memory_id))


def episodes(store):
    body = "Always set filterOption false for server-side search"
    return [seed(store, f"ep{index}", body, role="observation", project=project,
                 session=f"s{index}", technologies=("antd",))
            for index, project in enumerate(("alpha", "beta", "gamma"))]


# P4.t1 -- independent episodes consolidate into a new, un-rewritten abstraction
def test_episodes_consolidate_into_an_abstraction(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    episodes(store)
    lifecycle = MemoryLifecycle(store)
    proposals = lifecycle.consolidation_candidates(min_sources=3)
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.draft.role == "claim"
    assert set(proposal.draft.provenance.projects) == {"alpha", "beta", "gamma"}
    assert set(proposal.source_ids) == {"ep0", "ep1", "ep2"}
    assert lifecycle.consolidation_candidates(min_sources=4) == []

    for index in range(3):
        assert store.get(f"ep{index}").draft.role == "observation"
        assert store.get(f"ep{index}").version == 1

    created = lifecycle.consolidate(proposal)
    assert created.draft.role == "claim"
    assert created.draft.evidence_level == "supported"
    assert {relation["kind"] for relation in store.relations(created.id)} == {"derived_from"}
    assert {relation["target_id"] for relation in store.relations(created.id)} == {
        "ep0", "ep1", "ep2"}
    assert store.expand_relations([created.id]) == ["ep0", "ep1", "ep2"]


# P4.t1 -- independence is the (project, session) pair, not the project alone
def test_consolidation_requires_independent_origins(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    body = "Always set filterOption false for server-side search"
    for index in range(3):
        seed(store, f"same-project-{index}", body, role="observation",
             project="one", session=f"s{index}", technologies=("antd",))
    lifecycle = MemoryLifecycle(store)
    assert len(lifecycle.consolidation_candidates(min_sources=3)) == 1

    same_session = MemoryStore.initialize(tmp_path / "same.db")
    for index in range(3):
        seed(same_session, f"same-session-{index}", body, role="observation",
             project="one", session="only", technologies=("antd",))
    assert MemoryLifecycle(same_session).consolidation_candidates(min_sources=3) == []


# P4.t1 -- two episodes disagreeing on one condition keep it `unknown`, not decided
def test_consolidation_keeps_a_disputed_condition_unknown(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    body = "Always set filterOption false for server-side search"
    decisions = (("true", "a"), ("false", "b"), ("true", "c"))
    for index, (result, project) in enumerate(decisions):
        seed(store, f"dis{index}", body, role="observation", project=project,
             session=f"s{index}", technologies=("antd",),
             applicability=(Applicability(condition="antd+server-paging", result=result),))
    proposal = MemoryLifecycle(store).consolidation_candidates(min_sources=3)[0]
    merged = {condition.condition: condition.result for condition in proposal.draft.applicability}
    assert merged["antd+server-paging"] == "unknown"


# P4.4 -- the abstraction is revocable; the episodes are untouched
def test_consolidation_is_reversible(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    episodes(store)
    lifecycle = MemoryLifecycle(store)
    created = lifecycle.consolidate(lifecycle.consolidation_candidates(min_sources=3)[0])
    undone = lifecycle.undo_consolidation(created.id, expected_version=created.version,
                                          idempotency_key="undo:1")
    assert undone.draft.status == "archived"
    assert store.expand_relations([created.id]) == []
    assert all(store.get(f"ep{index}").draft.status == "active" for index in range(3))


# P4.t3 -- decay needs age AND no source update; new evidence refreshes the clock
def test_decay_candidates_need_age_and_no_source_update(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    seed(store, "aged")
    seed(store, "fresh")
    backdate(store, "aged")
    lifecycle = MemoryLifecycle(store)
    assert {record.id for record in lifecycle.decay_candidates(days=90)} == {"aged"}

    lifecycle.support("aged", Source(identity="tool:new", kind="tool", excerpt="more"),
                      reason="fresh evidence", expected_version=1, idempotency_key="ev:1")
    assert lifecycle.decay_candidates(days=90) == []


# P4.t2 -- a preference is never declared expired by non-use
def test_preference_is_never_decayed_by_non_use(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    seed(store, "pref", role="preference")
    backdate(store, "pref")
    lifecycle = MemoryLifecycle(store)
    assert lifecycle.decay_candidates(days=90) == []
    with pytest.raises(ValueError):
        lifecycle.mark_stale("pref", expected_version=1, idempotency_key="stale:pref",
                             reason="long unused")


# P4.t2 -- decayed records are reversible; user_owned is not decayed at all
def test_mark_stale_is_reversible_and_respects_ownership(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    seed(store, "m")
    lifecycle = MemoryLifecycle(store)
    stale = lifecycle.mark_stale("m", expected_version=1, idempotency_key="stale:m",
                                 reason="180d without use or update")
    assert stale.draft.status == "stale"
    restored = lifecycle.restore("m", expected_version=2, idempotency_key="restore:m",
                                 reason="still valid")
    assert restored.draft.status == "active"

    seed(store, "owned", user_owned=True)
    with pytest.raises(ProtectedMemory):
        lifecycle.mark_stale("owned", expected_version=1, idempotency_key="stale:owned",
                             reason="decay")


# P4.t2 -- human confirmation refreshes last_confirmed_at
def test_human_revision_refreshes_last_confirmed_at(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    seed(store, "pref", role="preference")
    lifecycle = MemoryLifecycle(store)
    assert store.get("pref").draft.last_confirmed_at is None
    revised = lifecycle.revise("pref", body="still my preference", reason="reconfirmed",
                               expected_version=1, idempotency_key="rc")
    assert revised.draft.last_confirmed_at is not None


# P4.t2 -- supersedes expresses an old/new preference without deleting history
def test_supersede_expresses_a_new_preference(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    seed(store, "old", role="preference", body="prefer spaces")
    replacement = Candidate("new-pref", MemoryDraft(
        body="prefer tabs", role="preference", status="active",
        provenance=Provenance(projects=("demo",)),
        sources=(Source(identity="tool:p", kind="tool", excerpt="user said tabs"),)))
    created = MemoryLifecycle(store).supersede(
        "old", replacement, expected_version=1, idempotency_key="sup:1", reason="changed mind")
    assert store.get("old").draft.status == "stale"
    assert ("supersedes", created.id) in {
        (relation["kind"], relation["target_id"]) for relation in store.relations("old")}


# P4.t4 -- high similarity splits into merge and conflict, never a blind merge
def test_merge_candidates_split_conflicts_from_compatible(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    seed(store, "a", body="always validate external input")
    seed(store, "b", body="always validate external inputs")
    seed(store, "c", body="strip the payload", applicability=(
        Applicability(condition="payload", result="true"),))
    seed(store, "d", body="strip the payload", applicability=(
        Applicability(condition="payload", result="false"),))
    proposals = MemoryMerge(store).merge_candidates(threshold=0.5)
    assert {proposal.kind for proposal in proposals if "c" in (proposal.left_id, proposal.right_id)
            and "d" in (proposal.left_id, proposal.right_id)} == {"conflicts_with"}
    compatible = {proposal.left_id for proposal in proposals
                  if proposal.kind in ("duplicate_of", "overlaps")}
    assert compatible & {"a", "b"}


# P4.t4 -- a conflict annotation is a revocable relation, not a deletion
def test_apply_conflict_is_revocable(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    seed(store, "c", body="strip the payload", applicability=(
        Applicability(condition="payload", result="true"),))
    seed(store, "d", body="strip the payload", applicability=(
        Applicability(condition="payload", result="false"),))
    merge = MemoryMerge(store)
    candidate = next(item for item in merge.merge_candidates(threshold=0.5)
                     if item.kind == "conflicts_with")
    result = merge.apply_conflict(candidate, idempotency_key="cf:1")
    relation = next(item for item in store.relations(candidate.left_id)
                    if item["kind"] == "conflicts_with")
    assert relation["target_id"] == candidate.right_id
    assert store.get(candidate.right_id).version == 1

    store.revoke_relation(relation["id"], expected_version=result.version,
                          idempotency_key="cf:1:r", reason="resolved", actor="human")
    assert store.relations(candidate.left_id) == []


# P4.t5 -- background actors may not widen access, ever
def test_automatic_writes_cannot_widen_access(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    seed(store, "m")
    lifecycle = MemoryLifecycle(store)
    lifecycle.scope("m", AccessPolicy(visibility="selected_projects", allowed_projects=("one",)),
                    expected_version=1, idempotency_key="narrow")
    widened = Candidate("m", MemoryDraft(body="changed", role="claim", status="active",
                                         sources=(OBSERVED,)))
    with pytest.raises(ProtectedMemory):
        lifecycle.publish(widened)

    narrow = store.get("m").draft.model_copy(
        update={"access": AccessPolicy(visibility="selected_projects", allowed_projects=("one",))})
    assert store.put("m", narrow, expected_version=2, idempotency_key="same",
                     actor="system", reason="rewrite").version == 3


def test_automatic_writes_cannot_widen_privacy(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    seed(store, "m", space=MemorySpace(privacy="private"))
    shared = store.get("m").draft.model_copy(update={"space": MemorySpace(privacy="shared")})
    with pytest.raises(ProtectedMemory):
        store.put("m", shared, expected_version=1, idempotency_key="widen",
                  actor="system", reason="leak")


# P4.t5 -- protected records reject every automatic change; no hard delete exists
def test_protected_records_reject_automatic_changes(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    seed(store, "owned", user_owned=True)
    seed(store, "sup", suppressed=True)
    seed(store, "dis", status="disabled")
    lifecycle = MemoryLifecycle(store)
    for memory_id in ("owned", "sup", "dis"):
        with pytest.raises(ProtectedMemory):
            lifecycle.mark_stale(memory_id, expected_version=1,
                                 idempotency_key=f"stale:{memory_id}", reason="decay")


def test_no_permanent_delete_path(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    seed(store, "m")
    assert not hasattr(MemoryStore, "delete") and not hasattr(MemoryStore, "forget")
    assert store.get("m") is not None


# P4.5 -- one batched report, exported through the CLI
def test_hygiene_report_batches_and_cli_exports(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXT_FORGE_HOME", str(tmp_path))
    store = MemoryStore.initialize(tmp_path / "memory.db")
    episodes(store)
    seed(store, "aged")
    backdate(store, "aged")
    payload = MemoryLifecycle(store).hygiene_report().as_dict()
    assert set(payload) == {"consolidation", "decay", "merges", "conflicts"}
    assert len(payload["consolidation"]) == 1
    assert [item["id"] for item in payload["decay"]] == ["aged"]

    import context_forge.cli as cli
    from typer.testing import CliRunner

    result = CliRunner().invoke(cli.app, ["hygiene"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["consolidation"][0]["role"] == "claim"


# P4.5 -- the same batched report is exposed as an MCP tool
def test_mcp_hygiene_tool(tmp_path, monkeypatch):
    import sys
    import types

    store = MemoryStore.initialize(tmp_path / "memory.db")
    episodes(store)

    class FakeMCP:
        def __init__(self, name):
            self.tools = {}

        def tool(self):
            def register(function):
                self.tools[function.__name__] = function
                return function
            return register

    fastmcp = types.ModuleType("mcp.server.fastmcp")
    fastmcp.FastMCP = FakeMCP
    monkeypatch.setitem(sys.modules, "mcp", types.ModuleType("mcp"))
    monkeypatch.setitem(sys.modules, "mcp.server", types.ModuleType("mcp.server"))
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fastmcp)
    import context_forge.mcp_server as mcp_server
    monkeypatch.setattr(mcp_server, "_memory", lambda: store)
    tools = mcp_server.create_server().tools
    payload = tools["context_forge_memory_hygiene"]()
    assert set(payload) == {"consolidation", "decay", "merges", "conflicts"}
    assert len(payload["consolidation"]) == 1
