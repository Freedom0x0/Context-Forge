import hashlib
import sys
import types
from itertools import combinations

import pytest

from context_forge.memory import MemoryStore
from context_forge.memory_merge import (
    MemoryMerge, _applicability_conflict, _boundary, _normalized, token_similarity,
)
from context_forge.memory_models import (
    Applicability, ApplicabilityScope, MemoryDraft, Provenance, Source,
)


def add(store, memory_id, body, *, kind="knowledge", project="demo", applicability=(), user_owned=False):
    return store.put(
        memory_id,
        MemoryDraft(
            kind=kind, body=body, status="active", user_owned=user_owned,
            provenance=Provenance(projects=(project,)), applicability=applicability,
            sources=(Source(identity=f"source:{memory_id}", kind="observation", excerpt=body),),
        ),
        expected_version=0, idempotency_key=f"put:{memory_id}", reason="test",
    )


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_report_is_read_only_deterministic_and_explicit_when_empty(tmp_path):
    path = tmp_path / "memory.db"
    store = MemoryStore.initialize(path)
    add(store, "one", "unique memory")
    before = file_hash(path), path.stat().st_mtime_ns
    first = MemoryMerge(store).report().as_dict()
    second = MemoryMerge(store).report().as_dict()
    assert first == second
    assert first["empty"] is True
    assert before == (file_hash(path), path.stat().st_mtime_ns)


def test_exact_duplicate_requires_same_decision_boundary(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    add(store, "a", "Use transaction boundaries")
    add(store, "b", " use  transaction boundaries ")
    add(store, "different-project", "Use transaction boundaries", project="other")
    add(store, "different-condition", "Use transaction boundaries", applicability=(
        Applicability(condition="external API only", result="unknown"),
    ))
    report = MemoryMerge(store).report(similarity_threshold=0.5)
    assert [(item.left_id, item.right_id) for item in report.exact_duplicates] == [("a", "b")]
    assert all(not item.automatic for item in report.possible_overlaps)


def test_rules_do_not_compare_across_project_boundaries(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    add(store, "a", "always validate external input", kind="rule", project="one")
    add(store, "b", "always validate external inputs", kind="rule", project="two")
    assert MemoryMerge(store).report(similarity_threshold=0.1).possible_overlaps == ()


def test_apply_exact_is_versioned_preserves_sources_and_can_be_undone(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    add(store, "a", "same")
    add(store, "b", "same")
    merge = MemoryMerge(store)
    candidate = merge.report().exact_duplicates[0]
    archived = merge.apply_exact(candidate, idempotency_key="merge-1")
    assert merge.apply_exact(candidate, idempotency_key="merge-1").version == archived.version
    assert archived.id == "b"
    assert archived.draft.status == "archived"
    assert archived.draft.sources[0].identity == "source:b"
    relation = next(item for item in store.relations("b") if item["kind"] == "duplicate_of")
    restored = merge.undo_exact("b", expected_version=archived.version, relation_id=relation["id"],
                                idempotency_key="undo-1")
    assert restored.draft.status == "active"
    assert store.relations("b", version=archived.version) == []


def test_automatic_merge_does_not_archive_human_owned_memory(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    add(store, "a", "same")
    add(store, "b", "same", user_owned=True)
    candidate = MemoryMerge(store).report().exact_duplicates[0]
    result = MemoryMerge(store).apply_exact(candidate, idempotency_key="merge")
    assert result.draft.status == "active"


def test_mcp_report_is_read_only(tmp_path, monkeypatch):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    add(store, "a", "same")
    add(store, "b", "same")

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
    server = mcp_server.create_server()
    report = server.tools["context_forge_memory_report"]()
    assert report["quality"]["automatic_candidates"] == 1
    assert store.get("b").draft.status == "active"


def _put(store, memory_id, body_text, *, kind='knowledge', project='demo', status='active',
         suppressed=False, visibility='scoped', applicability=()):
    """A wider fixture than `add`: the equivalence corpus needs every branch the filter reads."""
    store.put(memory_id, MemoryDraft(
        kind=kind, body=body_text, status=status, suppressed=suppressed,
        scope=ApplicabilityScope(visibility=visibility),
        applicability=applicability,
        provenance=Provenance(projects=(project,)),
        sources=(Source(identity=f'source:{memory_id}', kind='observation', excerpt=body_text),),
    ), expected_version=0, idempotency_key=f'put:{memory_id}', reason='test')


def _corpus(tmp_path):
    """Fifteen records reaching every branch the two loops read: case and whitespace variants,
    all three kinds, differing projects, rule boundaries, an applicability contradiction,
    global scope, and the two records the live filter is supposed to drop."""
    store = MemoryStore.initialize(tmp_path / 'memory.db')
    conflict = (Applicability(condition='windows only', result='true'),)
    opposite = (Applicability(condition='windows only', result='false'),)
    _put(store, 'dup-a', 'invalidate permission caches together')
    _put(store, 'dup-b', ' Invalidate  Permission  Caches TOGETHER ')
    _put(store, 'dup-c', 'invalidate permission caches together', project='other')
    _put(store, 'near-a', 'invalidate the session cache on logout')
    _put(store, 'near-b', 'invalidate the session cache on login')
    _put(store, 'rule-same', 'always validate external input', kind='rule')
    _put(store, 'rule-same-2', 'always validate external inputs', kind='rule')
    _put(store, 'rule-other', 'always validate external input', kind='rule', project='other')
    _put(store, 'conflict-a', 'prefer sqlite for local storage', applicability=conflict)
    _put(store, 'conflict-b', 'prefer sqlite for local storage', applicability=opposite)
    _put(store, 'global-one', 'never log raw tokens', visibility='global')
    _put(store, 'archived-one', 'invalidate permission caches together', status='archived')
    _put(store, 'suppressed-one', 'invalidate permission caches together', suppressed=True)
    _put(store, 'review-one', 'check the recall explanation by hand', kind='review')
    _put(store, 'candidate-one', 'check the recall explanation by hand', kind='review',
         status='candidate')
    return store


def _brute_force(records, similarity_threshold):
    """The pre-batching algorithm, verbatim: every pair recomputes text and boundary."""
    exact, overlaps = [], []
    for left, right in combinations(records, 2):
        if left.draft.kind != right.draft.kind:
            continue
        same_text = _normalized(left) == _normalized(right)
        same_boundary = _boundary(left) == _boundary(right)
        if same_text and same_boundary:
            exact.append((left.id, right.id))
            continue
        if left.draft.kind == 'rule' and not same_boundary:
            continue
        if token_similarity(_normalized(left), _normalized(right)) >= similarity_threshold:
            overlaps.append((left.id, right.id))
    return exact, overlaps


def _brute_force_proposals(records, threshold):
    proposals = []
    for left, right in combinations(records, 2):
        if left.draft.kind != right.draft.kind:
            continue
        score = token_similarity(_normalized(left), _normalized(right))
        if score < threshold:
            continue
        if _applicability_conflict(left, right):
            kind = 'conflicts_with'
        elif _normalized(left) == _normalized(right) and _boundary(left) == _boundary(right):
            kind = 'duplicate_of'
        else:
            kind = 'overlaps'
        proposals.append((left.id, right.id, kind, round(score, 6)))
    return proposals


def _live(store):
    return [record for record in store.current_records()
            if record.draft.status not in {'archived', 'disabled'} and not record.draft.suppressed]


@pytest.mark.parametrize('threshold', [0.1, 0.5, 0.6, 0.9, 1.0])
def test_batched_report_equals_the_pairwise_reference_including_order(tmp_path, threshold):
    """The batching rewrite must change cost only: same pairs, same order, same scores."""
    store = _corpus(tmp_path)
    records = _live(store)
    expected_exact, expected_overlaps = _brute_force(records, threshold)

    report = MemoryMerge(store).report(similarity_threshold=threshold)

    assert report.total == len(records)
    assert [(item.left_id, item.right_id) for item in report.exact_duplicates] == expected_exact
    assert all(item.automatic and item.score == 1.0 for item in report.exact_duplicates)
    assert [(item.left_id, item.right_id) for item in report.possible_overlaps] == expected_overlaps
    assert report.broad_scopes == tuple(record.id for record in records
                                        if record.draft.scope.visibility == 'global')


def test_exact_duplicates_keep_record_order_across_groups(tmp_path):
    """A three-member group emits (0,7), (0,8), (7,8); a two-member group in between emits (1,2).
    Group order puts (7,8) first, record order must put (1,2) first — only if a group has three
    members does this differ, so that is the shape this corpus has."""
    store = MemoryStore.initialize(tmp_path / 'memory.db')
    _put(store, 'a-trio-one', 'prune finished episodes nightly')
    _put(store, 'b-pair-one', 'prefer the smallest working change')
    _put(store, 'c-pair-two', 'prefer the smallest working change')
    _put(store, 'd-solo-one', 'index transcripts by session id')
    _put(store, 'e-solo-two', 'rotate signing keys annually')
    _put(store, 'f-solo-three', 'keep raw transcripts for a week')
    _put(store, 'g-solo-four', 'label unverified claims as guesses')
    _put(store, 'h-trio-two', 'prune finished episodes nightly')
    _put(store, 'i-trio-three', ' prune  finished  episodes nightly ')
    records = _live(store)
    assert [record.id for record in records] == ['a-trio-one', 'b-pair-one', 'c-pair-two',
                                                 'd-solo-one', 'e-solo-two', 'f-solo-three',
                                                 'g-solo-four', 'h-trio-two', 'i-trio-three']
    expected, _ = _brute_force(records, 0.5)

    found = [(item.left_id, item.right_id) for item in
             MemoryMerge(store).report(similarity_threshold=0.5).exact_duplicates]

    assert found == expected
    assert found == [('a-trio-one', 'h-trio-two'), ('a-trio-one', 'i-trio-three'),
                     ('b-pair-one', 'c-pair-two'), ('h-trio-two', 'i-trio-three')]


@pytest.mark.parametrize('threshold', [0.5, 0.9, 1.0])
def test_batched_proposals_equals_the_pairwise_reference_including_order(tmp_path, threshold):
    store = _corpus(tmp_path)
    records = _live(store)
    expected = _brute_force_proposals(records, threshold)

    found = MemoryMerge(store).merge_candidates(threshold=threshold)

    assert [(item.left_id, item.right_id, item.kind, item.score)
            for item in found] == expected


def test_overlaps_keep_record_order_across_buckets(tmp_path):
    """Bucket order is not record order: the lone knowledge record opens its bucket but pairs
    with nothing, so the rule pair (positions 1,2) must still precede the knowledge pair (3,4)."""
    store = MemoryStore.initialize(tmp_path / 'memory.db')
    _put(store, 'a-knowledge-alone', 'back up the vault before upgrading')
    _put(store, 'b-rule-one', 'always validate input before saving', kind='rule')
    _put(store, 'c-rule-two', 'always validate inputs before saving', kind='rule')
    _put(store, 'd-knowledge-one', 'rotate api keys every quarter')
    _put(store, 'e-knowledge-two', 'rotate api keys every month')
    records = _live(store)
    assert [record.id for record in records] == ['a-knowledge-alone', 'b-rule-one', 'c-rule-two',
                                                 'd-knowledge-one', 'e-knowledge-two']
    _, expected = _brute_force(records, 0.5)

    found = [(item.left_id, item.right_id) for item in
             MemoryMerge(store).report(similarity_threshold=0.5).possible_overlaps]

    assert found == expected
    assert found == [('b-rule-one', 'c-rule-two'), ('d-knowledge-one', 'e-knowledge-two')]
