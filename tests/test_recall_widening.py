"""Keyword widening: the third channel must relax without crossing boundaries."""

from context_forge.memory import MemoryStore
from context_forge.memory_models import AccessPolicy, MemoryDraft, Provenance, Source


def add(store, memory_id, body, *, project='demo', access=None):
    store.put(memory_id, MemoryDraft(
        body=body, status='active',
        provenance=Provenance(projects=(project,), sessions=(f'session-{memory_id}',)),
        access=access or AccessPolicy(),
        sources=(Source(identity=f'source:{memory_id}', kind='observation', excerpt=body),),
    ), expected_version=0, idempotency_key=f'put:{memory_id}', reason='test')


def seed(tmp_path):
    store = MemoryStore.initialize(tmp_path / 'memory.db')
    for index in range(8):
        add(store, f'plain-{index}', 'alpha beta gamma')
    add(store, 'target', 'alpha beta gamma delta')
    return store


def scored(store, query, **kwargs):
    kwargs.setdefault('project', None)
    kwargs.setdefault('limit', 20)
    return store.search_scored(query, **kwargs)


def hits(store, query, **kwargs):
    return {record.id: breakdown for record, breakdown in scored(store, query, **kwargs)}


def test_a_term_that_matches_nothing_is_dropped_first(tmp_path):
    """Terms with no matching document are always out; keeping them makes widening useless."""
    store = seed(tmp_path)

    found = hits(store, 'alpha beta gamma delta zulu')

    assert 'target' in found
    assert '去掉 1 个匹配不上的词' in found['target'].explanation


def test_many_unknown_terms_still_recover_without_a_round_budget(tmp_path):
    """Measured cliff: 4+ unseen terms recovered 0% while the cap counted every drop."""
    store = seed(tmp_path)
    query = 'alpha beta gamma delta ' + ' '.join(f'zulu{index}' for index in range(2, 9))

    assert 'target' in hits(store, query)


def test_strict_matches_are_left_unannotated(tmp_path):
    store = seed(tmp_path)

    found = hits(store, 'alpha beta gamma')

    assert len(found) == 9
    assert not any('去掉' in breakdown.explanation or '兜底' in breakdown.explanation
                   for breakdown in found.values())


def test_widening_still_enforces_the_boundary(tmp_path):
    store = seed(tmp_path)
    add(store, 'hidden', 'alpha beta gamma delta zeta', project='other',
        access=AccessPolicy(visibility='selected_projects', allowed_projects=('other',)))

    found = hits(store, 'alpha beta gamma delta zeta zulu', project='demo')

    # 'zeta' lives only in the other project's memory, so no AND can reach 'target' —
    # the union is what brings it in, and the union reaches 'hidden' too.
    assert 'target' in found          # reachable via widening
    assert 'hidden' not in found      # visible only to its own project


def test_each_hit_labels_the_channel_that_reached_it(tmp_path):
    """Two different relaxations, two different honest labels — and no doc gets both."""
    store = seed(tmp_path)
    add(store, 'solo', 'alpha zeta')

    found = hits(store, 'alpha beta gamma delta zulu')

    # 'target' is reachable as soon as 'zulu', which no document contains, is shed;
    # 'solo' shares two terms with the question, so only the union can reach it.
    assert '去掉 1 个匹配不上的词' in found['target'].explanation
    assert '兜底' not in found['target'].explanation
    assert '词并集兜底' in found['solo'].explanation
    assert '去掉' not in found['solo'].explanation


def test_the_union_fallback_is_the_last_resort_and_says_so(tmp_path):
    """No matching term is ever sacrificed: the last resort switches the operator instead."""
    store = seed(tmp_path)
    add(store, 'solo', 'alpha zeta')

    found = hits(store, 'alpha beta gamma delta zeta')

    # 'solo' shares two terms with the query but no document holds all five, so only a
    # union can reach it.
    assert 'solo' in found
    assert '词并集兜底' in found['solo'].explanation


def test_the_union_fallback_ranks_by_bm25_not_by_how_many_terms_were_asked_for(tmp_path):
    """Recorded so a future change cannot quietly assume term-count ordering here."""
    store = seed(tmp_path)
    add(store, 'solo', 'alpha zeta')
    add(store, 'weak', 'alpha')

    order = list(hits(store, 'alpha beta gamma delta zeta'))

    assert order[-1] == 'weak'        # one common term is the weakest evidence
    assert order.index('solo') < order.index('weak')


def test_single_token_queries_do_not_widen(tmp_path):
    store = seed(tmp_path)

    assert list(hits(store, 'zulu')) == []
