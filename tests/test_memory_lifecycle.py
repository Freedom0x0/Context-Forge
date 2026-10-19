from context_forge.domain import Condition, RuleExtraction, SessionArtifacts, KnowledgeItem
from context_forge.memory import MemoryStore, ProtectedMemory, VersionConflict
from context_forge.memory_lifecycle import MemoryLifecycle
from context_forge.memory_models import AccessPolicy, Applicability


def test_zero_and_multiple_candidates(tmp_path):
    service = MemoryLifecycle(MemoryStore.initialize(tmp_path / "memory.db"), model="deepseek-v4-flash", prompt_version="p1")
    empty = service.candidates(SessionArtifacts(title="empty", should_save=False), session_id="s1")
    assert empty == ()
    artifacts = SessionArtifacts(title="lesson", knowledge=KnowledgeItem(id="k", body="use tests"),
                                 rule_candidate=RuleExtraction(instruction="run tests", confidence="high"))
    candidates = service.candidates(artifacts, session_id="s1", project="demo")
    assert {candidate.draft.kind for candidate in candidates} == {"knowledge", "rule"}
    assert all(candidate.draft.model == "deepseek-v4-flash" for candidate in candidates)
    assert all(candidate.draft.evidence_level == "unverified" for candidate in candidates)
    assert {c.draft.confidence for c in candidates if c.draft.kind == "rule"} == {"high"}


def test_every_extraction_field_reaches_the_draft(tmp_path):
    service = MemoryLifecycle(MemoryStore.initialize(tmp_path / "memory.db"))
    condition = Condition(condition="antd 5", result="true")
    artifacts = SessionArtifacts(title="lesson", knowledge=KnowledgeItem(
        id="k", body="use tests", role="hypothesis", technologies=["antd"], domains=["search"],
        applicability=[condition], counterexamples=["antd 6"]),
        rule_candidate=RuleExtraction(
            instruction="run tests", role="procedure", technologies=["pytest"], domains=["testing"],
            applicability=[condition], counterexamples=["offline"]))
    by_kind = {c.draft.kind: c.draft for c in service.candidates(artifacts, session_id="s1")}
    for draft in by_kind.values():
        assert draft.applicability == (Applicability(condition="antd 5", result="true"),)
    assert (by_kind["knowledge"].scope.technologies, by_kind["knowledge"].scope.domains,
            by_kind["knowledge"].scope.exclusions) == (("antd",), ("search",), ("antd 6",))
    assert (by_kind["rule"].scope.technologies, by_kind["rule"].scope.domains,
            by_kind["rule"].scope.exclusions) == (("pytest",), ("testing",), ("offline",))


def test_publish_is_idempotent_and_human_suppression_wins(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    service = MemoryLifecycle(store)
    candidate = service.candidates(SessionArtifacts(title="lesson", knowledge=KnowledgeItem(id="k", body="keep")), session_id="s1")[0]
    first = service.publish(candidate)
    assert service.publish(candidate).version == first.version
    disabled = service.suppress(first.id, expected_version=first.version, idempotency_key="suppress")
    assert disabled.draft.status == "disabled"
    try:
        service.publish(candidate)
    except ProtectedMemory:
        pass
    else:
        raise AssertionError("suppressed memory was revived")


def test_revision_and_stale_worker_are_version_checked(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    service = MemoryLifecycle(store)
    candidate = service.candidates(SessionArtifacts(title="lesson", knowledge=KnowledgeItem(id="k", body="old")), session_id="s1")[0]
    record = service.publish(candidate)
    revised = service.revise(record.id, body="corrected", expected_version=record.version,
                              idempotency_key="human-1", reason="clarified")
    assert revised.draft.body == "corrected"
    try:
        service.revise(record.id, body="stale", expected_version=record.version,
                       idempotency_key="worker-stale", reason="background update")
    except VersionConflict:
        pass
    else:
        raise AssertionError("stale update overwrote human correction")


def test_scope_correction_changes_visibility(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    service = MemoryLifecycle(store)
    candidate = service.candidates(SessionArtifacts(title="lesson", knowledge=KnowledgeItem(id="k", body="boundary")), session_id="s1", project="one")[0]
    record = service.publish(candidate)
    scoped = service.scope(
        record.id,
        AccessPolicy(visibility="owner"),
        expected_version=record.version, idempotency_key="scope-1")
    assert store.get(record.id).draft.access.visibility == "owner"
    # Project-level access is gone: changing the project no longer changes visibility.
    assert store.search("boundary", project="one")[0].version == scoped.version
    assert store.search("boundary", project="two")[0].version == scoped.version
