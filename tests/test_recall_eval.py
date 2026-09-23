"""P2.e2-e4 -- S1 (project hard filter) versus S2 (scored recall) on G1/G2/G3.

Thresholds were fixed in `implement.md` before any of this was written and are
repeated here so a reader cannot quietly relax them:

    T1 silent wrong-transfer rate (S2) <= 0.10   hard gate
    T2 warning coverage for detectable wrongs == 1.00   hard gate
    T3 raw wrong-transfer rate (S2) <= 0.30   hard gate
    T4 help recall (S2) >= help recall (S1)   recorded
    T5 returned bytes (S2) <= 1.5 * (S1)   recorded

"Wrong transfer" is proxied from the scoring table because a deterministic
corpus has no `applied -> revoked` events. The true rate needs P3's
`MemoryApplication`; see the report for that limitation.
"""

from __future__ import annotations

from context_forge.memory import MemoryStore
from context_forge.memory_models import (
    ApplicabilityScope, MemoryDraft, Provenance, Relation, Source,
)
from context_forge.memory_recall import MemoryRecall
from recall_eval_cases import GROUPS, WARNINGS, Group

SIGNALS = ('未匹配任何适用维度', '技术栈未命中', '问题域未命中', '约束未满足', '已知反例', 'conflicts_with')

T1_MAX_SILENT = 0.10
T2_MIN_COVERAGE = 1.00
T3_MAX_RAW = 0.30


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
              'bytes_s1': 0, 'bytes_s2': 0, 'help_s1': 0, 'help_s2': 0, 'groups': {}}
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
        'T1_silent_wrong': totals['silent'] / totals['returned'],
        'T2_warning_coverage': (totals['detectable_warned'] / totals['detectable']
                                if totals['detectable'] else 1.0),
        'T3_raw_wrong': totals['wrong'] / totals['returned'],
        'T4_help_recall_s1': totals['help_s1'] / totals['available_right'],
        'T4_help_recall_s2': totals['help_s2'] / totals['available_right'],
        'T5_bytes_ratio': totals['bytes_s2'] / totals['bytes_s1'],
        'first_success_rate': totals['first_success'] / totals['tasks'],
    }
    return totals


# P2.e3/e4 -- the four indicators and the pre-registered gate
def test_scored_recall_beats_the_project_filter_within_the_preregistered_gate(tmp_path):
    metrics = measure(MemoryStore.initialize(tmp_path / 'memory.db'))
    rates = metrics['rates']
    assert rates['T1_silent_wrong'] <= T1_MAX_SILENT
    assert rates['T2_warning_coverage'] >= T2_MIN_COVERAGE
    assert rates['T3_raw_wrong'] <= T3_MAX_RAW
    assert rates['T4_help_recall_s2'] >= rates['T4_help_recall_s1']
    assert rates['T5_bytes_ratio'] <= 1.5
    assert metrics['silent'] == 1
    assert metrics['detectable_warned'] == metrics['detectable'] == 3


# P2.e1 -- the corpus itself must stay shaped the way the gate assumes
def test_eval_corpus_is_the_shape_the_gate_assumes():
    labels = [(case.label, case.why_wrong) for group in GROUPS for case in group.memories]
    assert sum(1 for label, _ in labels if label == 'wrong') == 4
    assert sum(1 for label, why in labels if label == 'wrong' and why == 'none') == 1
    assert sum(1 for label, why in labels if label == 'wrong' and why != 'none') == 3
    assert all(why in WARNINGS for _, why in labels if why != 'none')
