import hashlib
import random
import sys
import types
from itertools import combinations

import pytest

from context_forge.memory import MemoryStore, canonical_fingerprint
from context_forge.memory_merge import (
    OVERLAP_REPORT_LIMIT, MemoryMerge, MergeCandidate, _applicability_conflict, _boundary, normalized_text,
    token_similarity,
)
from context_forge.memory_models import (
    Applicability, ApplicabilityScope, MemoryDraft, MemorySpace, Provenance, Relation, Source,
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


def test_exact_duplicate_is_the_write_gate_fingerprint_not_the_source_project(tmp_path):
    """Which project learned a rule no longer makes it a second memory — but only a human may
    canonicalize the pair, because archiving the loser without a union deletes that project."""
    store = MemoryStore.initialize(tmp_path / "memory.db")
    add(store, "a", "Use transaction boundaries")
    add(store, "b", " use  transaction boundaries ")
    add(store, "different-project", "Use transaction boundaries", project="other")
    add(store, "different-condition", "Use transaction boundaries", applicability=(
        Applicability(condition="external API only", result="unknown"),
    ))

    report = MemoryMerge(store).report(similarity_threshold=0.5)

    assert [(item.left_id, item.right_id, item.automatic)
            for item in report.exact_duplicates] == [
        ("a", "b", True),
        ("a", "different-project", False),
        ("b", "different-project", False),
    ]
    # A declared condition is part of the boundary, so this pair is still only a review signal.
    assert all("different-condition" not in (item.left_id, item.right_id)
               for item in report.exact_duplicates)
    assert all(not item.automatic for item in report.possible_overlaps)


def test_differing_privacy_is_never_an_auto_mergeable_duplicate(tmp_path):
    """The hole this closes: identical wording used to be an exact duplicate across owner and
    privacy, so canonicalizing it could quietly move a private memory into the shared space."""
    store = MemoryStore.initialize(tmp_path / "memory.db")
    for memory_id, privacy in (("shared-one", "shared"), ("private-one", "private")):
        store.put(memory_id, MemoryDraft(
            kind="rule", body="生产环境禁止直接改 schema", status="active",
            space=MemorySpace(owner="default", privacy=privacy),
            provenance=Provenance(projects=("demo",)),
            sources=(Source(identity=f"source:{memory_id}", kind="observation",
                            excerpt="生产环境禁止直接改 schema"),),
        ), expected_version=0, idempotency_key=f"put:{memory_id}", reason="test")

    report = MemoryMerge(store).report()

    assert report.exact_duplicates == ()
    # Still worth a human look, but never something the merge executor will accept.
    assert [(item.left_id, item.right_id, item.kind, item.automatic)
            for item in MemoryMerge(store).merge_candidates(threshold=1.0)] \
        == [("private-one", "shared-one", "overlaps", False)]


def test_apply_exact_refuses_a_cross_project_pair_even_if_marked_automatic(tmp_path):
    """A candidate built from an older report can go stale after provenance was edited."""
    store = MemoryStore.initialize(tmp_path / "memory.db")
    add(store, "a", "Use transaction boundaries")
    add(store, "b", "Use transaction boundaries", project="other")
    stale = MergeCandidate("a", "b", "duplicate_of", 1.0, 'from an earlier report', True)

    with pytest.raises(ValueError, match='provenance'):
        MemoryMerge(store).apply_exact(stale, idempotency_key="refuse")

    assert store.get("b").draft.status == "active"


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
    """The rule as written down, applied to every pair without buckets or memoisation.

    Exactness is the write gate's fingerprint, so which project learned a rule no longer
    makes it a different memory; `automatic` still requires the provenance to agree, because
    archiving the loser of a cross-project pair would remove that project from recall.
    """
    exact, overlaps = [], []
    for left, right in combinations(records, 2):
        if canonical_fingerprint(left.draft) == canonical_fingerprint(right.draft):
            exact.append((left.id, right.id,
                          left.draft.provenance == right.draft.provenance))
            continue
        if left.draft.kind != right.draft.kind:
            continue
        if left.draft.kind == 'rule' and _boundary(left) != _boundary(right):
            continue
        if token_similarity(normalized_text(left), normalized_text(right)) >= similarity_threshold:
            overlaps.append((left.id, right.id))
    return exact, overlaps


def _brute_force_proposals(records, threshold):
    proposals = []
    for left, right in combinations(records, 2):
        if left.draft.kind != right.draft.kind:
            continue
        score = token_similarity(normalized_text(left), normalized_text(right))
        if score < threshold:
            continue
        if _applicability_conflict(left, right):
            kind = 'conflicts_with'
        elif canonical_fingerprint(left.draft) == canonical_fingerprint(right.draft):
            kind = 'duplicate_of'
        else:
            kind = 'overlaps'
        automatic = (kind == 'duplicate_of'
                     and left.draft.provenance == right.draft.provenance)
        proposals.append((left.id, right.id, kind, round(score, 6), automatic))
    return proposals


def _live(store):
    return [record for record in store.current_records()
            if record.draft.status not in {'archived', 'disabled'} and not record.draft.suppressed]


@pytest.mark.parametrize('threshold', [0.1, 0.5, 0.6, 0.9, 1.0])
def test_batched_report_equals_the_pairwise_reference_including_order(tmp_path, threshold):
    """Buckets may change the cost, never the answer: same pairs, same order, same flags."""
    store = _corpus(tmp_path)
    records = _live(store)
    expected_exact, expected_overlaps = _brute_force(records, threshold)

    report = MemoryMerge(store).report(similarity_threshold=threshold)

    assert report.total == len(records)
    assert [(item.left_id, item.right_id, item.automatic)
            for item in report.exact_duplicates] == expected_exact
    assert all(item.score == 1.0 for item in report.exact_duplicates)
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

    assert found == [pair[:2] for pair in expected]
    assert found == [('a-trio-one', 'h-trio-two'), ('a-trio-one', 'i-trio-three'),
                     ('b-pair-one', 'c-pair-two'), ('h-trio-two', 'i-trio-three')]


@pytest.mark.parametrize('threshold', [0.5, 0.9, 1.0])
def test_batched_proposals_equals_the_pairwise_reference_including_order(tmp_path, threshold):
    store = _corpus(tmp_path)
    records = _live(store)
    expected = _brute_force_proposals(records, threshold)

    found = MemoryMerge(store).merge_candidates(threshold=threshold)

    assert [(item.left_id, item.right_id, item.kind, item.score, item.automatic)
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


def test_blocking_never_loses_a_pair_the_brute_force_finds(tmp_path):
    """Seeded random corpora against the pairwise oracle.

    The prefix filter is a claim about math, so it gets a claim-shaped test: for every
    threshold, the blocked pass must return exactly the pairs the all-pairs pass returns —
    including their order.
    """
    for seed in range(8):
        rng = random.Random(seed)
        store = MemoryStore.initialize(tmp_path / f'fuzz-{seed}.db')
        vocabulary = [f'w{index}' for index in range(rng.randint(4, 12))]
        for index in range(rng.randint(3, 14)):
            words = rng.sample(vocabulary, rng.randint(1, len(vocabulary)))
            _put(store, f'r-{index}', ' '.join(words),
                 kind=rng.choice(['knowledge', 'rule', 'review']))
        records = _live(store)
        for threshold in (0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0):
            expected_exact, expected_overlaps = _brute_force(records, threshold)
            report = MemoryMerge(store).report(similarity_threshold=threshold,
                                               overlap_limit=10 ** 6)
            assert [(item.left_id, item.right_id, item.automatic)
                    for item in report.exact_duplicates] == expected_exact, (seed, threshold)
            assert [(item.left_id, item.right_id)
                    for item in report.possible_overlaps] == expected_overlaps, (seed, threshold)
            expected = _brute_force_proposals(records, threshold)
            assert [(item.left_id, item.right_id, item.kind, item.score, item.automatic)
                    for item in MemoryMerge(store).merge_candidates(threshold=threshold)] \
                == expected, (seed, threshold)


def _many_overlaps(tmp_path):
    """Ten memories sharing most of their wording, so the pair count exceeds any small cap."""
    store = MemoryStore.initialize(tmp_path / 'cap.db')
    for index in range(10):
        _put(store, f'c-{index}', f'review the recall explanation every {index} days')
    return store


def test_report_caps_overlaps_and_reports_how_many_it_held_back(tmp_path):
    store = _many_overlaps(tmp_path)
    uncapped = MemoryMerge(store).report(similarity_threshold=0.5, overlap_limit=10 ** 6)
    assert uncapped.overlaps_found == len(uncapped.possible_overlaps) > 5

    capped = MemoryMerge(store).report(similarity_threshold=0.5, overlap_limit=5)
    quality = capped.as_dict()['quality']

    assert len(capped.possible_overlaps) == 5
    assert [item.left_id for item in capped.possible_overlaps] \
        == [item.left_id for item in uncapped.possible_overlaps[:5]]
    assert capped.overlaps_found == uncapped.overlaps_found
    assert (quality['ambiguous_candidates'], quality['ambiguous_shown'], quality['overlap_limit']) \
        == (capped.overlaps_found, 5, 5)
    assert quality['truncated'] is True
    assert capped.as_dict()['empty'] is False


def test_an_oversized_library_still_returns_a_readable_report(tmp_path):
    store = _many_overlaps(tmp_path)
    report = MemoryMerge(store).report(similarity_threshold=0.5)
    assert len(report.possible_overlaps) <= OVERLAP_REPORT_LIMIT
    assert report.overlap_limit == OVERLAP_REPORT_LIMIT


@pytest.mark.parametrize('bad_limit', [0, -3])
def test_a_limit_below_one_is_refused_instead_of_silently_hiding_everything(tmp_path, bad_limit):
    store = _many_overlaps(tmp_path)
    with pytest.raises(ValueError, match='at least one'):
        MemoryMerge(store).report(similarity_threshold=0.5, overlap_limit=bad_limit)


def test_undoing_a_canonicalization_keeps_the_other_notes(tmp_path):
    """A `put` replaces the version's relation set. If undo carried nothing forward, revoking
    the duplicate edge would also delete a human's `refines` note that happened to sit on it."""
    store = MemoryStore.initialize(tmp_path / "memory.db")
    add(store, "a", "same")
    add(store, "b", "same")
    anchor, duplicate = store.get("a"), store.get("b")
    store.put("b", duplicate.draft, expected_version=duplicate.version,
              idempotency_key="note-b", actor="human", reason="a human note",
              relations=(Relation(target_id=anchor.id, target_version=anchor.version,
                                  kind="refines"),))

    merge = MemoryMerge(store)
    archived = merge.apply_exact(merge.report().exact_duplicates[0], idempotency_key="merge-1")
    duplicate_edge = next(item for item in store.relations("b", archived.version)
                          if item["kind"] == "duplicate_of")

    restored = merge.undo_exact("b", expected_version=archived.version,
                                relation_id=duplicate_edge["id"], idempotency_key="undo-1")

    kinds = [item["kind"] for item in store.relations("b", restored.version)]
    assert kinds == ["refines"]
    assert store.get("b").draft.status == "active"
    assert [item["kind"] for item in store.relations("a")
            if item["target_id"] == "b"] == []          # the anchor was never rewritten
