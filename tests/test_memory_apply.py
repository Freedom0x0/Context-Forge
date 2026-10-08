"""Attribution must be derived from evidence, never declared by the host (F5)."""

import sqlite3
from pathlib import Path

import pytest

from context_forge.memory import MemoryStore
from context_forge.memory_models import MemoryDraft, Provenance, Source

BODY = 'always re-run the migration smoke test before deploying'
SOURCE_ID = 'm1:tool:1'


@pytest.fixture
def store(tmp_path: Path):
    memory = MemoryStore.initialize(tmp_path / 'memory.db')
    memory.put('m1', MemoryDraft(body=BODY, status='active', role='procedure',
                                 provenance=Provenance(projects=('demo',)),
                                 sources=(Source(identity=SOURCE_ID, kind='tool', excerpt=BODY),)),
               expected_version=0, idempotency_key='put-1', actor='tool', reason='fixture')
    return memory


def mark(memory, request_id, stage):
    memory.record_retrieval('m1', 1, request_id=request_id, stage=stage,
                            idempotency_key=f'{request_id}:{stage}:m1:1')


def apply(store, **overrides):
    values = {'memory_id': 'm1', 'task_id': 'task-1', 'action': 'followed the rule',
              'expected_outcome': 'deploy passed', 'request_id': 'req-1'}
    values.update(overrides)
    return store.record_host_application(**values)


def test_outcome_without_evidence_is_plausible_not_confirmed(store):
    application, withheld = apply(store, actual_outcome='deploy passed')

    assert application.attribution == 'plausible'
    assert withheld == 'no evidence identities named'


def test_outcome_and_evidence_still_need_a_validated_report(store):
    """The whole point: a host cannot mint its own `confirmed` by quoting its own sources."""
    application, withheld = apply(store, actual_outcome='deploy passed',
                                  validation_source_ids=(SOURCE_ID,))

    assert application.attribution == 'plausible'
    assert withheld == 'no validated report under request req-1'


def test_missing_outcome_is_unknown(store):
    application, withheld = apply(store)

    assert application.attribution == 'unknown'
    assert withheld == 'no outcome reported yet'


def test_full_chain_reaches_confirmed_and_moves_weight(store):
    before = store.weight('m1')
    mark(store, 'req-1', 'applied')
    mark(store, 'req-1', 'validated')

    application, withheld = apply(store, actual_outcome='deploy passed',
                                  validation_source_ids=(SOURCE_ID,))
    after = store.weight('m1')

    assert withheld == '' and application.attribution == 'confirmed'
    assert after != before, (before, after)     # the F5 gap: marks alone moved nothing


def test_a_second_report_for_the_same_task_reuses_one_application_row(store, tmp_path):
    mark(store, 'req-1', 'validated')
    first, _ = apply(store, actual_outcome='deploy passed', validation_source_ids=(SOURCE_ID,))
    second, _ = apply(store, actual_outcome='deploy passed', validation_source_ids=(SOURCE_ID,))

    assert first.application_id == second.application_id
    with sqlite3.connect(tmp_path / 'memory.db') as connection:
        assert connection.execute('SELECT COUNT(*) FROM memory_applications').fetchone()[0] == 1


def test_a_later_better_report_adds_evidence_instead_of_colliding(store):
    """A host that first reports, then validates, must be able to strengthen its record."""
    early, _ = apply(store, actual_outcome='deploy passed', validation_source_ids=(SOURCE_ID,))
    mark(store, 'req-1', 'validated')

    late, withheld = apply(store, actual_outcome='deploy passed', validation_source_ids=(SOURCE_ID,))

    assert early.attribution == 'plausible' and late.attribution == 'confirmed'
    assert early.application_id != late.application_id
    assert len(store.applications('m1')) == 2
    assert store.weight('m1') != 1.0


def test_evidence_from_another_version_is_refused(store):
    mark(store, 'req-1', 'validated')

    with pytest.raises(ValueError, match='validation sources'):
        apply(store, actual_outcome='deploy passed', validation_source_ids=('m1:tool:999',))


@pytest.mark.parametrize('missing', ['task_id', 'action', 'expected_outcome', 'request_id'])
def test_required_fields_are_enforced(store, missing):
    with pytest.raises(ValueError, match='required'):
        apply(store, **{missing: ''})


def test_unknown_memory_raises(store):
    with pytest.raises(KeyError):
        apply(store, memory_id='absent')
