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


def test_hygiene_apply_runs_only_the_two_unattended_tiers(tmp_path, monkeypatch):
    """The default stays a read-only report; `--apply` is what writes, and it writes G1 and G2
    only. The JSON block is machine output, so the prose note is read from after its `}`."""
    monkeypatch.setenv("CONTEXT_FORGE_HOME", str(tmp_path))
    store = MemoryStore.initialize(tmp_path / "memory.db")
    seed(store, "keep", body="always validate external input")
    seed(store, "twin", body="always validate external input")
    seed(store, "solo", body="rotate signing keys yearly")

    import context_forge.cli as cli
    from typer.testing import CliRunner

    untouched = CliRunner().invoke(cli.app, ["hygiene"])
    assert json.loads(untouched.output)["merges"] != []
    assert store.get("twin").draft.status == "active"

    applied = CliRunner().invoke(cli.app, ["hygiene", "--apply"])
    assert applied.exit_code == 0, applied.output
    payload = json.loads(applied.output[:applied.output.index("\n}") + 2])
    assert (payload["exact_applied"], payload["conflicts_annotated"]) == (1, 0)
    assert store.get("twin").draft.status == "archived"
    assert store.get("solo").draft.status == "active"
    assert "forge hygiene-undo" in applied.output[applied.output.rindex("}") + 1:]
    assert store.get("keep").draft.status == "active"
    assert store.get("solo").draft.status == "active"

    rerun = CliRunner().invoke(cli.app, ["hygiene", "--apply"])
    assert json.loads(rerun.output[:rerun.output.index("\n}") + 2])["exact_applied"] == 0


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


def _trio(store):
    for memory_id in ('a', 'b', 'c'):
        seed(store, memory_id, body='always validate external input')


def test_auto_pass_canonicalizes_a_three_way_group_onto_one_anchor(tmp_path):
    """(a,b), (a,c) and (b,c) all come out of one group; (b,c) must not re-anchor onto the
    already-archived b, or the library grows a version that points at a withheld memory."""
    store = MemoryStore.initialize(tmp_path / 'memory.db')
    _trio(store)

    report = MemoryLifecycle(store).apply_auto_tier()

    assert report['exact_applied'] == 2
    assert store.get('a').draft.status == 'active'
    assert [store.get(x).draft.status for x in ('b', 'c')] == ['archived', 'archived']
    for memory_id in ('b', 'c'):
        noted = [(item['kind'], item['target_id']) for item in store.relations(memory_id)]
        assert ('duplicate_of', 'a') in noted, memory_id
        assert ('duplicate_of', 'b') not in noted, memory_id
    # c was archived once, by (a,c); the later (b,c) pair must not re-archive it onto b.
    assert store.get('c').version == 2


def test_a_second_auto_pass_writes_nothing_new(tmp_path):
    store = MemoryStore.initialize(tmp_path / 'memory.db')
    _trio(store)
    MemoryLifecycle(store).apply_auto_tier()
    versions = {memory_id: store.get(memory_id).version for memory_id in ('a', 'b', 'c')}

    again = MemoryLifecycle(store).apply_auto_tier()

    assert again['exact_applied'] == 0 and again['conflicts_annotated'] == 0
    assert {memory_id: store.get(memory_id).version for memory_id in ('a', 'b', 'c')} == versions


def test_auto_pass_keeps_the_human_row_and_guards_by_provenance(tmp_path):
    """Two different guards in one pass: a human-owned memory is never the one archived, so its
    model-written twin goes; and a pair whose provenance differs is reported but not touched."""
    store = MemoryStore.initialize(tmp_path / 'memory.db')
    seed(store, 'mine', body='prefer the smallest working change', user_owned=True)
    seed(store, 'theirs', body='prefer the smallest working change')
    seed(store, 'alpha', body='rotate signing keys yearly', project='alpha')
    seed(store, 'beta', body='rotate signing keys yearly', project='beta')

    report = MemoryLifecycle(store).apply_auto_tier()

    assert report['exact_applied'] == 1
    assert report['exact_skipped'] == 1
    assert store.get('mine').draft.status == 'active'        # the human row survives untouched
    assert store.get('theirs').draft.status == 'archived'    # the model copy is what goes
    assert store.get('mine').draft.user_owned is True
    assert [store.get(x).draft.status for x in ('alpha', 'beta')] == ['active', 'active']


def test_auto_pass_is_capped_and_the_remainder_survives_to_the_next_pass(tmp_path):
    store = MemoryStore.initialize(tmp_path / 'memory.db')
    for memory_id in ('a', 'b', 'c', 'd'):
        seed(store, memory_id, body='index transcripts by session id')

    first = MemoryLifecycle(store).apply_auto_tier(limit=2)

    assert (first['exact_applied'], first['exact_deferred']) == (2, 4)   # six pairs, cap of two
    assert store.get('d').draft.status == 'active'

    second = MemoryLifecycle(store).apply_auto_tier()

    assert second['exact_applied'] == 1
    assert store.get('d').draft.status == 'archived'


def test_auto_pass_notes_a_contradiction_once_and_again_after_new_evidence(tmp_path):
    """The relation id derives from the idempotency key, so the key has to carry the version the
    note was written on: without it a new evidence version makes the next pass collide with its
    own earlier event instead of restoring the note the update dropped."""
    store = MemoryStore.initialize(tmp_path / 'memory.db')
    seed(store, 'x', body='prefer sqlite for local storage',
         applicability=[Applicability(condition='windows only', result='true')])
    seed(store, 'y', body='prefer sqlite for local storage',
         applicability=[Applicability(condition='windows only', result='false')])

    first = MemoryLifecycle(store).apply_auto_tier()
    second = MemoryLifecycle(store).apply_auto_tier()

    assert first['conflicts_annotated'] == 1
    assert second['conflicts_annotated'] == 0 and second['conflicts_skipped'] >= 1

    current = store.get('x')
    store.put('x', current.draft, expected_version=current.version,
              idempotency_key='evidence-again', reason='new evidence lands')
    third = MemoryLifecycle(store).apply_auto_tier()

    assert third['conflicts_annotated'] == 1
    assert store.get('x').draft.status == 'active'          # a note never changes the content


def _cross_project_pair(tmp_path, name='verdict.db'):
    from context_forge.memory_merge import MemoryMerge

    store = MemoryStore.initialize(tmp_path / name)
    seed(store, 'alpha-one', body='never log raw tokens', project='alpha')
    seed(store, 'beta-one', body='never log raw tokens', project='beta')
    candidate = next(item for item in MemoryMerge(store).report().exact_duplicates
                     if not item.automatic)
    return store, candidate


def test_a_keep_verdict_changes_nothing_but_gives_the_rate_a_denominator(tmp_path):
    from context_forge.memory_lifecycle import adjudicate
    from context_forge.queue import JobStore

    store, candidate = _cross_project_pair(tmp_path)
    queue = JobStore(tmp_path / 'queue.db')

    result = adjudicate(candidate, 'keep', store=store, queue=queue)

    assert result['changed_id'] is None
    assert [store.get(x).draft.status for x in ('alpha-one', 'beta-one')] == ['active', 'active']
    assert queue.verdict_counts() == {'keep': 1}


def test_a_merge_verdict_moves_the_loser_s_project_onto_the_anchor(tmp_path):
    """The pair the automatic tier refuses is exactly the pair a human may settle, and merging
    must carry beta over or the beta library silently loses the rule it already had."""
    from context_forge.memory_lifecycle import adjudicate
    from context_forge.queue import JobStore

    store, candidate = _cross_project_pair(tmp_path, 'merge.db')
    queue = JobStore(tmp_path / 'queue.db')

    result = adjudicate(candidate, 'merge', store=store, queue=queue)

    anchor = store.get('alpha-one')
    assert result['changed_id'] == 'beta-one'
    assert set(anchor.draft.provenance.projects) == {'alpha', 'beta'}
    assert store.get('beta-one').draft.status == 'archived'
    assert [record.id for record in store.search('never log raw tokens', project='beta')] \
        == ['alpha-one']
    assert queue.verdict_counts() == {'merge': 1}

    again = adjudicate(candidate, 'merge', store=store, queue=queue)
    assert again['changed_version'] == store.get('beta-one').version == 2
    assert store.get('alpha-one').version == 2        # a re-run mints no versions


def test_re_deciding_a_candidate_overwrites_the_ledger_row(tmp_path):
    from context_forge.memory_lifecycle import adjudicate
    from context_forge.queue import JobStore

    store, candidate = _cross_project_pair(tmp_path, 'rethink.db')
    queue = JobStore(tmp_path / 'queue.db')
    adjudicate(candidate, 'keep', store=store, queue=queue)

    adjudicate(candidate, 'archive', store=store, queue=queue, target_id='beta-one')

    assert queue.verdict_counts() == {'archive': 1}
    assert len(queue.verdicts()) == 1
    assert store.get('beta-one').draft.status == 'archived'


def test_a_verdict_target_outside_the_pair_is_refused_and_never_ledgered(tmp_path):
    from context_forge.memory_lifecycle import adjudicate
    from context_forge.queue import JobStore

    store, candidate = _cross_project_pair(tmp_path, 'bad_target.db')
    queue = JobStore(tmp_path / 'queue.db')

    with pytest.raises(ValueError, match='one of the two memories'):
        adjudicate(candidate, 'archive', store=store, queue=queue, target_id='alpha-one-fake')
    with pytest.raises(ValueError, match='unknown verdict'):
        adjudicate(candidate, 'delete', store=store, queue=queue)

    assert queue.verdict_counts() == {}
    assert store.get('alpha-one').draft.status == 'active'


def test_suppress_and_archive_differ_in_what_the_model_may_do_afterwards(tmp_path):
    """Archived keeps absorbing evidence silently; disabled refuses the write outright."""
    from context_forge.memory_lifecycle import adjudicate
    from context_forge.queue import JobStore

    store, candidate = _cross_project_pair(tmp_path, 'suppress.db')
    queue = JobStore(tmp_path / 'queue.db')

    adjudicate(candidate, 'suppress', store=store, queue=queue, target_id='beta-one')

    assert store.get('beta-one').draft.status == 'disabled'
    current = store.get('beta-one')
    with pytest.raises(ProtectedMemory):
        store.put('beta-one', current.draft, expected_version=current.version,
                  idempotency_key='model-again', actor='model', reason='re-precipitated')


class _TtyIn:
    def isatty(self):
        return True


def _review_setup(tmp_path, monkeypatch, name, answers):
    import context_forge.cli as cli
    from context_forge.memory_lifecycle import MemoryLifecycle

    monkeypatch.setenv("CONTEXT_FORGE_HOME", str(tmp_path))
    store, _candidate = _cross_project_pair(tmp_path, name)
    remaining = list(answers)
    monkeypatch.setattr("sys.stdin", _TtyIn())
    monkeypatch.setattr(cli, "_ask",
                        lambda prompt: remaining.pop(0) if remaining else "q")
    return cli, store, MemoryLifecycle(store)


def test_review_refuses_a_pipe_because_a_model_must_not_judge_its_own_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXT_FORGE_HOME", str(tmp_path))
    _cross_project_pair(tmp_path, 'memory.db')          # `_memory()` reads the runtime dir
    import context_forge.cli as cli
    from typer.testing import CliRunner

    result = CliRunner().invoke(cli.app, ["hygiene", "--review"], input="k\nk\n")

    assert result.exit_code == 2
    assert "needs a terminal" in result.output


def test_review_ledgers_a_named_archive(tmp_path, monkeypatch, capsys):
    cli, store, lifecycle = _review_setup(tmp_path, monkeypatch, 'walk.db', ["a", "beta-one"])

    cli._adjudicate_interactively(store, lifecycle, threshold=0.9, days=90)

    out = capsys.readouterr().out
    assert "refused" not in out
    assert cli._store().verdict_counts() == {"archive": 1}
    assert store.get("beta-one").draft.status == "archived"


def test_an_unread_answer_is_not_recorded_as_a_decision(tmp_path, monkeypatch, capsys):
    cli, store, lifecycle = _review_setup(tmp_path, monkeypatch, 'garbage.db', ["x"])

    cli._adjudicate_interactively(store, lifecycle, threshold=0.9, days=90)

    out = capsys.readouterr().out
    assert "left unledgered" in out
    assert cli._store().verdict_counts() == {}
    assert store.get("beta-one").draft.status == "active"


def test_undo_reverses_an_archived_duplicate_and_does_nothing_on_a_dry_run(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXT_FORGE_HOME", str(tmp_path))
    store = MemoryStore.initialize(tmp_path / 'memory.db')
    seed(store, 'a', body='shared policy')
    seed(store, 'b', body='shared policy')
    merge = MemoryMerge(store)
    merge.apply_exact(merge.report().exact_duplicates[0], idempotency_key='merge-1')
    assert store.get('b').draft.status == 'archived'
    archived_version = store.get('b').version

    import context_forge.cli as cli
    from typer.testing import CliRunner

    dry = CliRunner().invoke(cli.app, ['hygiene-undo', 'b'])
    assert dry.exit_code == 0, dry.output
    assert json.loads(dry.output[:dry.output.rindex('}') + 1])['dry_run'] is True
    assert store.get('b').draft.status == 'archived'          # nothing applied

    applied = CliRunner().invoke(cli.app, ['hygiene-undo', 'b', '--apply'])
    assert applied.exit_code == 0, applied.output
    assert json.loads(applied.output)['status'] == 'active'
    assert store.get('b').draft.status == 'active'
    noted = store.relations('b', version=archived_version, include_revoked=True)
    assert [item for item in noted if item['kind'] == 'duplicate_of'
            and item['revoked_event'] is not None]

    again = CliRunner().invoke(cli.app, ['hygiene-undo', 'b', '--apply'])
    assert again.exit_code != 0                                # nothing left to reverse


def test_undo_reverses_a_consolidation(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXT_FORGE_HOME", str(tmp_path))
    store = MemoryStore.initialize(tmp_path / 'memory.db')
    episodes(store)
    lifecycle = MemoryLifecycle(store)
    created = lifecycle.consolidate(lifecycle.consolidation_candidates(min_sources=3)[0])
    assert store.get(created.id).draft.status == 'active'

    import context_forge.cli as cli
    from typer.testing import CliRunner

    applied = CliRunner().invoke(cli.app, ['hygiene-undo', created.id, '--apply'])
    assert applied.exit_code == 0, applied.output

    assert json.loads(applied.output)['status'] == 'archived'
    assert store.get(created.id).draft.status == 'archived'
    # The abstraction's `derived_from` edges are revoked, not silently left pointing at members.
    noted = store.relations(created.id, version=created.version, include_revoked=True)
    assert [item for item in noted if item['kind'] == 'derived_from'
            and item['revoked_event'] is not None]


def test_a_decay_verdict_stales_the_memory_and_keeps_it_out_of_recall(tmp_path):
    from context_forge.memory_lifecycle import adjudicate_decay
    from context_forge.queue import JobStore

    store = MemoryStore.initialize(tmp_path / 'g4.db')
    seed(store, 'aged', body='shared policy')
    queue = JobStore(tmp_path / 'queue.db')

    adjudicate_decay('aged', 'stale', store=store, queue=queue)

    assert store.get('aged').draft.status == 'stale'
    assert store.search('shared policy', project='demo') == []
    assert queue.verdict_counts() == {'stale': 1}


def test_a_preference_is_refused_a_stale_verdict_and_nothing_is_ledgered(tmp_path):
    """'Nobody used it' does not apply to a taste: only confirmation counts, so the verdict
    is refused rather than quietly accepted — and a refused verdict must not be counted."""
    from context_forge.memory_lifecycle import adjudicate_decay
    from context_forge.queue import JobStore

    store = MemoryStore.initialize(tmp_path / 'pref.db')
    seed(store, 'taste', body='dark mode please', role='preference')
    queue = JobStore(tmp_path / 'queue.db')

    with pytest.raises(ValueError, match='preference time is judged'):
        adjudicate_decay('taste', 'stale', store=store, queue=queue)
    with pytest.raises(ValueError, match='unknown decay verdict'):
        adjudicate_decay('taste', 'delete', store=store, queue=queue)

    assert queue.verdict_counts() == {}
    assert store.get('taste').draft.status == 'active'


def test_review_walks_decay_items_after_merge_candidates(tmp_path, monkeypatch, capsys):
    cli, store, lifecycle = _review_setup(tmp_path, monkeypatch, 'g4loop.db', ["k", "s"])
    seed(store, 'aged', body='aged policy')
    backdate(store, 'aged')

    cli._adjudicate_interactively(store, lifecycle, threshold=0.9, days=90)

    out = capsys.readouterr().out
    assert '[G3 1/1]' in out and '[G4 1/1]' in out
    assert cli._store().verdict_counts() == {'keep': 1, 'stale': 1}
    assert store.get('aged').draft.status == 'stale'
