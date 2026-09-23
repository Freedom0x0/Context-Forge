"""P2.e5 -- the P2 gate was partly a constant; P3 replaces the proxies.

Pre-registered thresholds (fixed before this file was written):

    A1  confirmed-application wrong-transfer rate (S2) <= 0.20   hard gate
    A2  warning coverage for detectable wrongs (S2) == 1.00      hard gate
    A3  help recall (S2) >= help recall (S1)                     recorded
    A4  returned bytes (S2) <= 1.5 * (S1)                        recorded

A1 replaces the two P2 proxies (T1 silent-wrong rate, T3 raw-wrong rate). Both
were constants on the old corpus: every group held 6 labelled items, below
`limit=8`, so *all* labelled items were returned for any implementation and the
rates could not move. A1 is measured over real `MemoryApplication` records with
`attribution='confirmed'`: a wrong transfer is an application later revoked.

Group G4 adds 12 labelled items (> `limit=8`) whose wrong ids sort before the
right ones, so the *returned set* — and therefore A1 — depends on ranking.

Gate power, recorded by mutation:

  Gate   Mutation that should RED it                         Result
  -------------------------------------------------------------------
  A1     neutralise scoring (relevance total := 0)           RED  (0.15 -> 0.31)
  A2     drop the per-`why_wrong` warning mapping            RED
  A3/A4  (recorded, not gates)                               —
"""

from __future__ import annotations

from context_forge.memory import (
    MemoryStore, ScoreBreakdown, derive_level,
)
from context_forge.memory_models import (
    ApplicabilityScope, MemoryApplication, MemoryDraft, Provenance, Relation, Source,
)
from context_forge.memory_recall import MemoryRecall
from recall_eval_cases import GROUPS, WARNINGS, Group

SIGNALS = ('未匹配任何适用维度', '技术栈未命中', '问题域未命中', '约束未满足', '已知反例', 'conflicts_with')

A1_MAX_WRONG_TRANSFER = 0.20
A2_MIN_COVERAGE = 1.00


def build(store: MemoryStore, group: Group) -> dict[str, object]:
    labels = {}
    for case in group.memories:
        source = Source(identity=f'tool:{case.memory_id}', kind='tool', excerpt=case.body)
        relations = ()
        if case.conflicts_with:
            relations = (Relation(target_id=case.conflicts_with, target_version=1,
                                  kind='conflicts_with', evidence_ids=(source.identity,)),)
        store.put(case.memory_id, MemoryDraft(
            body=case.body, summary=case.memory_id, role=case.role, status='active',
            evidence_level=case.evidence_level,
            provenance=Provenance(projects=case.origin, sessions=case.sessions),
            scope=ApplicabilityScope(technologies=case.technologies, domains=case.domains,
                                     constraints=case.constraints),
            sources=(source,)), expected_version=0,
            idempotency_key=f'put:{case.memory_id}', reason='eval corpus', relations=relations)
        labels[case.memory_id] = case
    for index in range(group.noise):
        memory_id = f'noise-{group.task.project}-{index}'
        store.put(memory_id, MemoryDraft(
            body=f'{group.task.query} project filler {index}', summary=memory_id,
            role='claim', status='active', evidence_level='unverified',
            provenance=Provenance(projects=(group.task.project,)),
            scope=ApplicabilityScope(technologies=('flash-legacy',)),
            sources=(Source(identity=f'tool:{memory_id}', kind='tool', excerpt='filler'),)),
            expected_version=0, idempotency_key=f'put:{memory_id}', reason='eval noise')
    return labels


def s1(store: MemoryStore, group: Group) -> list:
    """Baseline: the project hard filter with no ranking and no cap."""
    hits = store.search(group.task.query, project=group.task.project,
                        legacy_filter=True, limit=None)
    return [record for record in hits if group.task.project in record.draft.provenance.projects]


def s2(store: MemoryStore, group: Group):
    return MemoryRecall(store).search(
        group.task.query, project=group.task.project,
        technologies=frozenset(group.task.technologies), domains=frozenset(group.task.domains),
        limit=8)


def measure(store: MemoryStore) -> dict:
    totals = {'returned': 0, 'wrong': 0, 'silent': 0, 'right': 0, 'available_right': 0,
              'detectable': 0, 'detectable_warned': 0, 'first_success': 0, 'tasks': 0,
              'bytes_s1': 0, 'bytes_s2': 0, 'help_s1': 0, 'help_s2': 0,
              'confirmed': 0, 'revoked': 0, 'groups': {}}
    for group in GROUPS:
        labels = build(store, group)
        baseline = s1(store, group)
        recalled = s2(store, group)
        returned = [result for result in recalled if result.memory.id in labels]
        wrong = [result for result in returned if labels[result.memory.id].label == 'wrong']
        right = [result for result in returned if labels[result.memory.id].label == 'right']
        silent = [result for result in wrong
                  if not any(signal in result.explanation for signal in SIGNALS)]
        detectable = [result for result in wrong if labels[result.memory.id].why_wrong != 'none']
        warned = [result for result in detectable
                  if WARNINGS[labels[result.memory.id].why_wrong] in result.explanation]
        available = sum(1 for case in group.memories if case.label == 'right')
        baseline_right = sum(1 for record in baseline if record.id in labels
                             and labels[record.id].label == 'right')
        first_ok = 0
        if right and not wrong:
            first_ok = 1
        elif right and wrong:
            order = [result.memory.id for result in returned]
            first_ok = int(min(order.index(result.memory.id) for result in right)
                           < min(order.index(result.memory.id) for result in wrong))
        for result in returned:
            case = labels[result.memory.id]
            store.record_application(MemoryApplication(
                application_id=f'app:{group.task.group}:{case.memory_id}',
                task_id=group.task.group, memory_id=case.memory_id,
                memory_version=result.memory.version, action='applied a recalled memory',
                expected_outcome='task succeeds',
                actual_outcome='ok' if case.label == 'right' else 'failed',
                validation_source_ids=(f'tool:{case.memory_id}',), attribution='confirmed'))
            totals['confirmed'] += 1
            if case.label == 'wrong':
                store.record_feedback(case.memory_id, outcome='revoked',
                                      idempotency_key=f'revoked:{case.memory_id}',
                                      note='wrong transfer')
                totals['revoked'] += 1
        totals['groups'][group.task.group] = {
            'returned': len(returned), 'right': len(right), 'wrong': len(wrong),
            'silent': len(silent), 'detectable': len(detectable), 'warned': len(warned),
            'baseline_returned': len(baseline), 'first_success': first_ok,
            'bytes_s1': sum(len(record.model_dump_json().encode('utf-8')) for record in baseline),
            'bytes_s2': sum(len(result.memory.model_dump_json().encode('utf-8')) for result in recalled),
        }
        totals['returned'] += len(returned)
        totals['wrong'] += len(wrong)
        totals['silent'] += len(silent)
        totals['right'] += len(right)
        totals['available_right'] += available
        totals['detectable'] += len(detectable)
        totals['detectable_warned'] += len(warned)
        totals['first_success'] += first_ok
        totals['help_s1'] += baseline_right
        totals['help_s2'] += len(right)
        totals['bytes_s1'] += sum(len(record.model_dump_json().encode('utf-8')) for record in baseline)
        totals['bytes_s2'] += sum(len(result.memory.model_dump_json().encode('utf-8')) for result in recalled)
        totals['tasks'] += 1
    totals['rates'] = {
        'A1_wrong_transfer': totals['revoked'] / totals['confirmed'],
        'A2_warning_coverage': (totals['detectable_warned'] / totals['detectable']
                                if totals['detectable'] else 1.0),
        'A3_help_recall_s1': totals['help_s1'] / totals['available_right'],
        'A3_help_recall_s2': totals['help_s2'] / totals['available_right'],
        'A4_bytes_ratio': totals['bytes_s2'] / totals['bytes_s1'],
        'first_success_rate': totals['first_success'] / totals['tasks'],
        'silent_wrong': totals['silent'] / totals['returned'],
    }
    return totals


# P2.e5 -- attributable application data replaces the two P2 rate proxies
def test_scored_recall_beats_the_project_filter_within_the_preregistered_gate(tmp_path):
    metrics = measure(MemoryStore.initialize(tmp_path / 'memory.db'))
    rates = metrics['rates']
    assert rates['A1_wrong_transfer'] <= A1_MAX_WRONG_TRANSFER
    assert rates['A2_warning_coverage'] >= A2_MIN_COVERAGE
    assert rates['A3_help_recall_s2'] >= rates['A3_help_recall_s1']
    assert rates['A4_bytes_ratio'] <= 1.5
    assert metrics['detectable_warned'] == metrics['detectable'] == 3
    assert metrics['revoked'] == 4 and metrics['confirmed'] == 26


# P2.e5 gate power -- neutralising scoring must push A1 over its threshold
def test_a1_moves_when_scoring_is_neutralised(tmp_path, monkeypatch):
    def flat(record, ctx, weights=None, **kwargs):
        return ScoreBreakdown(total=0.0, match=0.0, applicability=0.0, provenance_bonus=0.0,
                              evidence=0.0, freshness=0.0, feedback=0.0, text=0.0,
                              conflict_penalty=0.0, admitted_by=derive_level(record.draft),
                              explanation='')
    monkeypatch.setattr('context_forge.memory.relevance', flat)
    metrics = measure(MemoryStore.initialize(tmp_path / 'memory.db'))
    assert metrics['rates']['A1_wrong_transfer'] > A1_MAX_WRONG_TRANSFER
    assert metrics['rates']['A1_wrong_transfer'] > 0.25


# P2.e1 -- the corpus itself must stay shaped the way the gate assumes
def test_eval_corpus_is_the_shape_the_gate_assumes():
    labels = [(case.label, case.why_wrong) for group in GROUPS for case in group.memories]
    assert sum(1 for label, _ in labels if label == 'wrong') == 8
    g4 = next(group for group in GROUPS if group.task.group.startswith('G4'))
    assert len(g4.memories) == 12 > 8
    assert sum(1 for case in g4.memories if case.label == 'wrong') == 4
    assert all(case.memory_id.startswith('g4-a-wrong') for case in g4.memories
               if case.label == 'wrong')
    assert all(case.memory_id.startswith('g4-z-right') for case in g4.memories
               if case.label == 'right')
    assert all(why in WARNINGS for _, why in labels if why != 'none')
