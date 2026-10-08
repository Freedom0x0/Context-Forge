from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .memory import MemoryStore
from .memory_models import MemoryRecord

EXPOSURE_STAGES = ('retrieved', 'tool_returned', 'presented', 'referenced')
ADOPTION_STAGES = ('applied', 'validated')


@dataclass(frozen=True)
class EffectivenessReport:
    """The three read-only P3 reports. Nothing here mutates a memory."""

    applications: tuple[dict, ...]
    anomaly_candidates: tuple[MemoryRecord, ...]
    tidy_candidates: tuple[MemoryRecord, ...]
    exposure_vs_adoption: dict[str, float]
    # Roll-up coverage from signal_counts, so the reader knows whether these numbers came
    # from raw events that are still kept or from an aggregate that outlives them.
    signals: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            'applications': list(self.applications),
            'anomaly_candidates': [_record_view(record) for record in self.anomaly_candidates],
            'tidy_candidates': [_record_view(record) for record in self.tidy_candidates],
            'exposure_vs_adoption': self.exposure_vs_adoption,
            'signals': self.signals,
        }


def _record_view(record: MemoryRecord) -> dict:
    return {
        'id': record.id,
        'version': record.version,
        'role': record.draft.role,
        'status': record.draft.status,
        'evidence_level': record.draft.evidence_level,
        'summary': record.draft.summary,
    }


def _age_days(created_at: str) -> int:
    created = datetime.fromisoformat(created_at)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created).days


class MemoryEffectiveness:
    """Read-only application / anomaly / tidy reports over retrieval events."""

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def _counts(self) -> tuple[dict[str, dict[str, int]], dict[str, int]]:
        stages: dict[str, dict[str, int]] = {}
        for event in self.store.retrieval_events():
            bucket = stages.setdefault(event['memory_id'], {})
            bucket[event['stage']] = bucket.get(event['stage'], 0) + 1
        revoked: dict[str, int] = {}
        for event in self.store.feedback():
            if event.get('outcome') == 'revoked':
                revoked[event['memory_id']] = revoked.get(event['memory_id'], 0) + 1
        return stages, revoked

    def application_report(self, memory_id: str) -> dict[str, int]:
        """引用 / 采纳 / 验证 / 撤销 counts for one memory (observation only)."""
        stages, revoked = self._counts()
        bucket = stages.get(memory_id, {})
        return {
            'referenced': bucket.get('referenced', 0),
            'applied': bucket.get('applied', 0),
            'validated': bucket.get('validated', 0),
            'revoked': revoked.get(memory_id, 0),
        }

    def anomaly_candidates(self) -> list[MemoryRecord]:
        """`verified` but never `applied`: 未使用 / 待复核 — never a hallucination verdict."""
        stages, _ = self._counts()
        return [record for record in self.store.current_records()
                if record.draft.evidence_level == 'verified'
                and record.draft.status not in {'archived', 'disabled'}
                and not record.draft.suppressed
                and stages.get(record.id, {}).get('applied', 0) == 0]

    def tidy_candidates(self, days: int = 90) -> list[MemoryRecord]:
        """Long unused: 整理候选. Non-use never declares a preference wrong."""
        stages, _ = self._counts()
        result = []
        for record in self.store.current_records():
            if record.draft.role == 'preference':
                continue
            if record.draft.status in {'archived', 'disabled'} or record.draft.suppressed:
                continue
            if stages.get(record.id, {}).get('applied', 0):
                continue
            if _age_days(record.created_at) < days:
                continue
            result.append(record)
        return result

    def report(self, *, days: int = 90) -> EffectivenessReport:
        stages, revoked = self._counts()
        applications = []
        exposure = adoption = 0
        for record in self.store.current_records():
            bucket = stages.get(record.id, {})
            exposure += sum(bucket.get(stage, 0) for stage in EXPOSURE_STAGES)
            adoption += sum(bucket.get(stage, 0) for stage in ADOPTION_STAGES)
            if bucket or record.id in revoked:
                applications.append({
                    'memory_id': record.id,
                    **self.application_report(record.id),
                })
        ratio = adoption / exposure if exposure else 0.0
        window = max(1, -(-days // 30))                # the day window expressed in months
        return EffectivenessReport(
            applications=tuple(applications),
            anomaly_candidates=tuple(self.anomaly_candidates()),
            tidy_candidates=tuple(self.tidy_candidates(days)),
            exposure_vs_adoption={'exposure': exposure, 'adoption': adoption, 'ratio': round(ratio, 6)},
            signals={'aggregated_through': self.store.aggregated_through(),
                     'window_months': window,
                     'totals': self.store.signal_totals(window)},
        )
