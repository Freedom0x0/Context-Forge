import inspect
import json
import sqlite3
from typing import get_args

from context_forge.memory import (
    CONFLICT_KINDS, EXPANSION_KINDS, BoundaryView, MemoryStore, MemoryStoreError,
    RetrievalContext, admitted, boundary_view, boundary_visible,
)
from context_forge.memory_models import (
    AccessPolicy, Applicability, ApplicabilityScope, MemoryDraft, MemoryRecord,
    Provenance, Relation, Source, derive_level,
)
from context_forge.memory_recall import MemoryRecall


def seed(store, memory_id, body, *, role="claim", scope=None, provenance=None,
         sources=None, relations=(), **overrides):
    draft = MemoryDraft(
        body=body, summary=memory_id, role=role, status="active",
        scope=scope or ApplicabilityScope(),
        provenance=provenance or Provenance(),
        sources=sources or (Source(identity=f"source:{memory_id}", kind="observation", excerpt=body),),
        **overrides)
    return store.put(memory_id, draft, expected_version=0,
                     idempotency_key=f"put:{memory_id}", reason="test", relations=relations)


def explain(memory_id, query="proxy", **kwargs):
    recall = kwargs.pop("recall")
    for result in recall.search(query, project=kwargs.pop("project", "demo"), **kwargs):
        if result.memory.id == memory_id:
            return result.explanation
    raise AssertionError(f"{memory_id} was not recalled")


def record(memory_id, **overrides):
    return MemoryRecord(id=memory_id, version=1, created_at="2026-09-23T00:00:00+00:00",
                        draft=MemoryDraft(body="proxy timeout policy", **overrides))


# P2.t1 -- the same query must produce the same top-K in the same order
def test_top_k_is_reproducible(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    for index in range(12):
        seed(store, f"m{index:02d}", f"proxy timeout policy {index}")
    first = store.search_scored("proxy", project="demo")
    second = store.search_scored("proxy", project="demo")
    assert len(first) == 8
    assert [item.id for item, _ in first] == [item.id for item, _ in second]
    assert [round(score.total, 9) for _, score in first] == [round(score.total, 9) for _, score in second]


# P2.t2 -- a pattern enters the pool only on a declared dimension hit
def test_pattern_needs_a_dimension_hit(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    seed(store, "pattern", "proxy timeout policy",
         scope=ApplicabilityScope(technologies=("umi",), domains=("proxy",)))
    seed(store, "principle", "proxy timeout policy")
    assert {r.id for r in store.search("proxy", project="demo", technologies=frozenset({"antd"}))} == {"principle"}
    assert {r.id for r in store.search("proxy", project="demo", technologies=frozenset({"umi"}))} == {
        "pattern", "principle"}
    assert {r.id for r in store.search("proxy", project="demo", domains=frozenset({"proxy"}))} == {
        "pattern", "principle"}


# P2.t3 -- a decided counterexample eliminates even an always-admitted principle
def test_exclusions_eliminate_before_admission(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    seed(store, "excluded", "proxy timeout policy", scope=ApplicabilityScope(exclusions=("legacy",)))
    seed(store, "prose", "proxy timeout policy",
         scope=ApplicabilityScope(exclusions=("monorepo 根目录不适用",)))
    seed(store, "kept", "proxy timeout policy")
    assert [r.id for r in store.search("proxy", project="legacy")] == ["kept", "prose"]
    assert {r.id for r in store.search("proxy", project="demo")} == {"excluded", "prose", "kept"}
    assert "monorepo 根目录不适用" in explain("prose", recall=MemoryRecall(store))


# P2.t4 -- four sections, and the unknown slot never disappears
def test_explanation_carries_four_parts_and_the_unknown_slot(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    seed(store, "bounded", "proxy timeout policy", applicability=(
        Applicability(condition="umi@4.x", result="true"),
        Applicability(condition="umi@5.x", result="false"),
        Applicability(condition="React 19", result="unknown"),
    ))
    seed(store, "bare", "proxy timeout policy")
    recall = MemoryRecall(store)
    bounded = explain("bounded", recall=recall)
    for label in ("为什么命中", "来源与迁移", "适用边界", "风险提示"):
        assert label in bounded
    assert 'true="umi@4.x"' in bounded
    assert 'false="umi@5.x"' in bounded
    assert 'unknown="React 19"' in bounded
    assert 'unknown="' in explain("bare", recall=recall)


# P2.t5 -- a cross-project hit must say where it came from and why it transfers
def test_cross_project_result_carries_origin_and_migration_reason(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    seed(store, "from-platform", "antd select server search",
         provenance=Provenance(projects=("platform-web-main",), sessions=("c0f909b9",)),
         scope=ApplicabilityScope(technologies=("antd",), domains=("search",)))
    text = explain("from-platform", query="select", recall=MemoryRecall(store), project="context-forge",
                   technologies=frozenset({"antd"}), domains=frozenset({"search"}))
    assert "platform-web-main" in text and "c0f909b9" in text
    assert "适用技术" in text and "问题域" in text
    assert "谨慎采用" not in text


# P2.t6 -- GUARD: text-only relevance must degrade the migration reason
def test_text_only_match_degrades_to_caution(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    seed(store, "bare", "proxy timeout policy",
         provenance=Provenance(projects=("elsewhere",), sessions=("s1",)))
    text = explain("bare", recall=MemoryRecall(store))
    assert "仅文本相关" in text
    assert "未匹配任何适用维度" in text
    assert "谨慎采用" in text


# P2.t7 -- GUARD: a conflicting pair is returned whole and cross-annotated
def test_conflicting_pair_is_returned_and_cross_annotated(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    left_source = Source(identity="tool:strip", kind="tool", excerpt="strip wins")
    right_source = Source(identity="tool:passthrough", kind="tool", excerpt="passthrough wins")
    seed(store, "strip-rule", "api payload whitespace policy", sources=(left_source,))
    seed(store, "passthrough-rule", "api payload whitespace policy", sources=(right_source,),
         relations=(Relation(target_id="strip-rule", target_version=1, kind="conflicts_with",
                             evidence_ids=(right_source.identity,)),))
    results = MemoryRecall(store).search("policy", project="demo")
    assert {result.memory.id for result in results} == {"strip-rule", "passthrough-rule"}
    by_id = {result.memory.id: result.explanation for result in results}
    assert "conflicts_with" in by_id["strip-rule"] and "需按版本分流" in by_id["strip-rule"]
    assert "conflicts_with" in by_id["passthrough-rule"] and "需按版本分流" in by_id["passthrough-rule"]
    assert "passthrough-rule" in by_id["strip-rule"]
    assert "strip-rule" in by_id["passthrough-rule"]


# P2.t8 -- the hatch restores the binary filter but not the boundary fixes
def test_legacy_filter_restores_the_binary_filter(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    seed(store, "instance", "proxy timeout policy", role="observation",
         provenance=Provenance(projects=("elsewhere",), sessions=("s1",)))
    seed(store, "principle", "proxy timeout policy")
    seed(store, "locked", "proxy timeout policy",
         access=AccessPolicy(visibility="selected_projects", allowed_projects=("other",)))
    assert [r.id for r in store.search("proxy", project="demo")] == ["principle"]
    assert {r.id for r in store.search("proxy", project="demo", legacy_filter=True)} == {
        "instance", "principle"}
    assert {r.id for r in store.search("proxy", project="demo", legacy_filter=True,
                                      technologies=frozenset({"umi"}))} == {"instance", "principle"}
    assert "locked" not in {r.id for r in store.search("proxy", project="demo", legacy_filter=True)}


# P2.t9 -- one hop along positive relations, and it never feeds a score
def test_expand_relations_walks_positive_kinds_only(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    hub_source = Source(identity="tool:hub", kind="tool", excerpt="hub")
    hub = seed(store, "hub", "shared policy wording", sources=(hub_source,))
    linked = []
    for kind in EXPANSION_KINDS:
        source = Source(identity=f"tool:{kind}", kind="tool", excerpt=kind)
        seed(store, f"pos-{kind}", "shared policy wording", sources=(source,),
             relations=(Relation(target_id="hub", target_version=hub.version, kind=kind,
                                 evidence_ids=(source.identity,)),))
        linked.append(f"pos-{kind}")
    for kind in ("conflicts_with", "contradicts", "exception_to", "duplicate_of", "supersedes"):
        source = Source(identity=f"tool:neg-{kind}", kind="tool", excerpt=kind)
        seed(store, f"neg-{kind}", "shared policy wording", sources=(source,),
             relations=(Relation(target_id="hub", target_version=hub.version, kind=kind,
                                 evidence_ids=(source.identity,)),))
    assert store.expand_relations(["hub"]) == sorted(linked)
    assert set(CONFLICT_KINDS).isdisjoint(EXPANSION_KINDS)

    control = MemoryStore.initialize(tmp_path / "control.db")
    seed(control, "hub", "shared policy wording")
    assert (store.search_scored("policy", project="demo")[0][1].total
            == control.search_scored("policy", project="demo")[0][1].total)


# P2.t10 -- GUARD: exposure events are observations, never learning signals
def test_exposure_events_do_not_reinforce(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    seed(store, "noisy", "proxy timeout policy", proof_count=9)
    seed(store, "plain", "proxy timeout policy")
    for stage in ("tool_returned", "presented", "referenced"):
        for number in range(5):
            store.record_retrieval("noisy", 1, request_id=f"r-{number}", stage=stage,
                                   idempotency_key=f"exposure:{stage}:{number}")
    scores = {r.id: score for r, score in store.search_scored("proxy", project="demo")}
    assert scores["noisy"].total == scores["plain"].total
    assert scores["noisy"].freshness == scores["plain"].freshness
    assert store.relations("noisy") == []
    assert store.expand_relations(["noisy", "plain"]) == []


# P2.t11 -- every role admits without raising, and preference is always a principle
def test_admitted_handles_every_role():
    context = RetrievalContext(query="proxy", project="demo", technologies=frozenset({"umi"}),
                               domains=frozenset({"proxy"}))
    scoped = ApplicabilityScope(technologies=("umi",))
    for role in get_args(MemoryDraft.model_fields["role"].annotation):
        provenance = Provenance(projects=("demo",)) if role == "observation" else Provenance()
        plain = record(role, role=role, provenance=provenance)
        tagged = record(role, role=role, provenance=provenance, scope=scoped)
        assert isinstance(admitted(plain, context), bool)
        assert isinstance(admitted(tagged, context), bool)
        assert derive_level(tagged.draft) in {"principle", "pattern", "instance"}
        if role == "preference":
            assert derive_level(plain.draft) == derive_level(tagged.draft) == "principle"
            assert admitted(plain, context) and admitted(tagged, context)
        if role == "hypothesis":
            assert derive_level(tagged.draft) == derive_level(record("claim", role="claim", scope=scoped).draft)


# P2.12 -- elimination and admission are different stages with different inputs
def test_admission_and_elimination_are_separate_stages():
    elimination = inspect.getsource(boundary_visible)
    admission = inspect.getsource(admitted)
    for word in ("derive_level", "admitted", "principle", "pattern", "instance"):
        assert word not in elimination
    for word in ("owner", "privacy", "allowed_projects", "exclusions"):
        assert word not in admission


# P2.3 -- the P1 window: an instance without a provenance hit stays out of the pool
def test_instance_without_a_provenance_hit_is_not_admitted(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    seed(store, "elsewhere", "proxy timeout policy", role="observation",
         provenance=Provenance(projects=("other",), sessions=("s1",)))
    seed(store, "located", "proxy timeout policy", role="observation",
         provenance=Provenance(sessions=("s2",), paths=("src/config/*.py",)))
    assert store.search("proxy", project="demo") == []
    assert store.search("proxy", project="other")[0].id == "elsewhere"
    hit = store.search("proxy", project="demo", changed_path="src/config/app.py")
    assert [r.id for r in hit] == ["located"]
    assert store.search("proxy", project="demo", legacy_filter=True)[0].id in {"elsewhere", "located"}


# P2.t13 -- GUARD: freshness counts attributable adoptions only
def test_freshness_counts_attributable_adoptions_only(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    # ids are ordered so the winner is last in id order: a pure-id mutation must go red
    seed(store, "zz-adopted", "proxy timeout policy")
    seed(store, "aa-noisy", "proxy timeout policy", proof_count=9)
    seed(store, "mm-plain", "proxy timeout policy")
    for number in range(5):
        store.record_retrieval("aa-noisy", 1, request_id=f"p{number}", stage="presented",
                               idempotency_key=f"presented:{number}")
        store.record_retrieval("aa-noisy", 1, request_id=f"r{number}", stage="referenced",
                               idempotency_key=f"referenced:{number}")
        store.record_retrieval("zz-adopted", 1, request_id=f"a{number}", stage="applied",
                               idempotency_key=f"applied:{number}")
    scores = {r.id: score for r, score in store.search_scored("proxy", project="demo")}
    assert scores["zz-adopted"].freshness > scores["mm-plain"].freshness
    assert scores["aa-noisy"].freshness == scores["mm-plain"].freshness
    assert [r.id for r, _ in store.search_scored("proxy", project="demo")] == [
        "zz-adopted", "aa-noisy", "mm-plain"]


# P2 -- the boundary projections are the filter input, not a write-only table
def test_boundary_projection_mirrors_the_draft_and_is_required(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    written = seed(store, "one", "proxy timeout policy",
                   scope=ApplicabilityScope(technologies=("umi",), domains=("proxy",),
                                            exclusions=("legacy",)),
                   access=AccessPolicy(visibility="selected_projects", allowed_projects=("a",)))
    view = boundary_view(written.draft)
    with sqlite3.connect(store.path) as connection:
        access = connection.execute("SELECT * FROM memory_access_projection").fetchone()
        applicability = connection.execute("SELECT * FROM memory_applicability_projection").fetchone()
        connection.execute("DELETE FROM memory_access_projection")
    assert (access[2], access[3], access[4], tuple(json.loads(access[5]))) == (
        view.owner, view.privacy, view.visibility, view.allowed_projects)
    assert tuple(json.loads(applicability[4])) == view.exclusions
    try:
        store.search("proxy", project="demo")
    except MemoryStoreError as error:
        assert "boundary projection missing" in str(error)
    else:
        raise AssertionError("a missing projection must be loud, not silent")
