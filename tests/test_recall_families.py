from context_forge.memory import MemoryStore
from context_forge.memory_models import AccessPolicy, MemoryDraft, MemorySpace
from context_forge.memory_recall import MemoryRecall, group_for_presentation


def put(store, memory_id, body, access=AccessPolicy(), space=MemorySpace()):
    return store.put(memory_id, MemoryDraft(body=body, status="active", access=access, space=space),
                     expected_version=0, idempotency_key=memory_id, reason="test")


def test_family_grouping_is_after_candidates_and_boundary_compatible(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    put(store, "a", "permission cache invalidation must happen together")
    put(store, "b", "permission cache invalidation must happen together")
    put(store, "private", "permission cache invalidation must happen together",
        space=MemorySpace(privacy="private"))
    response = MemoryRecall(store).search("permission cache invalidation", project="demo")
    assert len(response) == 3
    assert ("a", "b") in response.families
    assert response.candidate_truncated is False


def test_presentation_grouping_is_anchor_based_not_transitive(tmp_path):
    store = MemoryStore.initialize(tmp_path / "memory.db")
    records = [put(store, str(i), body) for i, body in enumerate((
        "a b c d", "a b c d e", "b c d e",
    ))]
    fake_score = object()
    families = group_for_presentation([(record, fake_score) for record in records], threshold=.75)
    assert [[item[0].id for item in family.members] for family in families] == [["0", "1"], ["2"]]
