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
    buckets: tuple[dict, ...] = ()
    # Human verdicts on hygiene candidates live in queue.db, everything else here reads
    # memory.db. Counts only, no rates: the discipline the bucket report already follows is
    # that a small sample must not be turned into a percentage.
    verdicts: dict[str, object] = field(default_factory=dict)
    workflow_metrics: dict[str, object] = field(default_factory=dict)
    # Per-host collection health from queue.db: the silence duration beside the
    # scan-failure buckets (8a.3). Same side as verdicts — counts, not rates.
    host_health: dict[str, object] = field(default_factory=dict)
    # How many recalls each channel reached (bm25 / widened / semantic), from the
    # structured `channel` in retrieval events. Counts only, no rates (design §7).
    channels: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            'applications': list(self.applications),
            'anomaly_candidates': [_record_view(record) for record in self.anomaly_candidates],
            'tidy_candidates': [_record_view(record) for record in self.tidy_candidates],
            'exposure_vs_adoption': self.exposure_vs_adoption,
            'signals': self.signals,
            'buckets': list(self.buckets),
            'verdicts': self.verdicts,
            'workflow_metrics': self.workflow_metrics,
            'host_health': self.host_health,
            'channels': self.channels,
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


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


class MemoryEffectiveness:
    """Read-only application / anomaly / tidy reports over retrieval events."""

    def __init__(self, store: MemoryStore, queue=None) -> None:
        self.store = store
        # Optional: verdicts come from queue.db. A caller without a queue handle simply
        # gets no `verdicts` section rather than a misleading zero.
        self.queue = queue

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
            buckets=tuple(self.store.bucket_report(months=window)),
            verdicts=self._verdicts(),
            workflow_metrics=self.workflow_metrics(),
            host_health=self._host_health(),
            channels=self.store.channel_mix(),
        )

    def _host_health(self) -> dict[str, dict[str, object]]:
        """Per-host last-collected time beside its scan-failure buckets (8a.3).

        A host that only ever failed has no episode row, so failure-only hosts
        are merged in too — otherwise the very case the count exists to catch
        (format changed, nothing ever collected) would be invisible.
        """
        if self.queue is None:
            return {}
        activity = self.queue.host_activity()
        failures = self.queue.scan_failures()
        return {
            host: {
                'episodes': activity.get(host, {}).get('episodes', 0),
                'last_activity_at': activity.get(host, {}).get('last_activity_at'),
                'scan_failures': failures.get(host, {}),
            }
            for host in sorted(set(activity) | set(failures))
        }

    def workflow_metrics(self) -> dict[str, object]:
        """Return observable end-to-end workflow metrics.

        Rates deliberately include their denominators and use ``None`` when there is
        no sample.  This keeps an empty database distinguishable from a perfect one.
        """
        applications = self.store.applications()
        confirmed = sum(1 for item in applications if item.get('attribution') == 'confirmed')
        modified = sum(
            1 for item in applications
            if item.get('actual_outcome') and item.get('expected_outcome')
            and item['actual_outcome'].strip() != item['expected_outcome'].strip()
        )
        retrievals = self.store.retrieval_events()
        exposure = sum(1 for item in retrievals if item.get('stage') in EXPOSURE_STAGES)
        negative_feedback = [item for item in self.store.feedback()
                             if item.get('outcome') in {'irrelevant', 'harmful'}]

        precipitation_total = precipitation_approved = precipitation_modified = 0
        repeated_errors = 0
        harmful_events = 0
        false_recall = 0
        labelled_hits = 0
        revocation_hours: list[float] = []
        if self.queue is not None:
            verdicts = self.queue.db.execute(
                'SELECT decision,modified FROM precipitation_verdicts').fetchall()
            precipitation_total = len(verdicts)
            precipitation_approved = sum(1 for row in verdicts if row['decision'] == 'approved')
            precipitation_modified = sum(1 for row in verdicts if row['modified'])

            hits = self.queue.db.execute(
                'SELECT rule_id,session_id,occurred_at FROM rule_hit_events ORDER BY occurred_at').fetchall()
            feedback = self.queue.db.execute(
                "SELECT rule_id,session_id,outcome,request_id,created_at FROM rule_feedback "
                "WHERE outcome IN ('helpful','harmful','irrelevant') ORDER BY created_at"
            ).fetchall()
            # A feedback label is usable only when tied to a recall request and hit.
            # Collapse duplicate reports for a rule/request so retries do not inflate rates.
            hit_requests = {(row['rule_id'], row['session_id']) for row in hits if row['session_id']}
            labelled: dict[tuple[str, str], str] = {}
            labelled_times: dict[tuple[str, str], datetime] = {}
            for row in feedback:
                if not row['session_id'] or not row['request_id']:
                    continue
                key = (row['rule_id'], row['request_id'])
                if (row['rule_id'], row['session_id']) in hit_requests:
                    labelled[key] = row['outcome']
                    try:
                        labelled_times[key] = _parse_time(row['created_at'])
                    except (TypeError, ValueError):
                        pass
            labelled_hits = len(labelled)
            false_recall = sum(outcome == 'irrelevant' for outcome in labelled.values())
            harmful_keys = sorted(
                (key for key, outcome in labelled.items() if outcome == 'harmful'),
                key=lambda key: labelled_times.get(key, datetime.max.replace(tzinfo=timezone.utc)),
            )
            harmful_events = len(harmful_keys)
            harmed_rules: set[str] = set()
            for rule_id, _request_id in harmful_keys:
                repeated_errors += int(rule_id in harmed_rules)
                harmed_rules.add(rule_id)

            hits = self.queue.db.execute(
                'SELECT rule_id, occurred_at FROM rule_hit_events ORDER BY occurred_at').fetchall()
            hit_times: dict[str, list[datetime]] = {}
            for hit in hits:
                try:
                    hit_times.setdefault(hit['rule_id'], []).append(_parse_time(hit['occurred_at']))
                except (TypeError, ValueError):
                    continue
            revoked = self.queue.db.execute(
                "SELECT rule_id, created_at FROM rule_feedback WHERE outcome='revoked'"
            ).fetchall()
            for item in revoked:
                try:
                    end = _parse_time(item['created_at'])
                    starts = [stamp for stamp in hit_times.get(item['rule_id'], ()) if stamp <= end]
                    start = max(starts) if starts else None
                    if start is not None and end >= start:
                        revocation_hours.append((end - start).total_seconds() / 3600)
                except (TypeError, ValueError):
                    continue
        else:
            revocation_status = 'unavailable: queue store not provided'
        if self.queue is not None:
            revocation_status = 'available'

        def rate(n: int, d: int) -> float | None:
            return round(n / d, 6) if d else None

        return {
            'precipitation_confirmation': {
                'status': 'available' if self.queue is not None else 'unavailable: queue store not provided',
                'confirmed': precipitation_approved,
                'total': precipitation_total,
                'rate': rate(precipitation_approved, precipitation_total),
            },
            'application_attribution_confirmation': {'confirmed': confirmed, 'total': len(applications),
                                                      'rate': rate(confirmed, len(applications))},
            'modification': {'modified': modified, 'total': len(applications),
                             'rate': rate(modified, len(applications)),
                             'status': 'proxy: actual_outcome differs from expected_outcome',
                             'precipitation_modified': precipitation_modified,
                             'precipitation_total': precipitation_total,
                             'precipitation_rate': rate(precipitation_modified, precipitation_total)},
            'repeat_error': {'status': 'available' if self.queue is not None else
                             'unavailable: queue store not provided',
                             'repeated': repeated_errors, 'harmful_feedback_events': harmful_events,
                             'rate': rate(repeated_errors, harmful_events)},
            'false_recall': {'status': 'available' if self.queue is not None else
                             'unavailable: queue store not provided',
                             'false': false_recall, 'labelled_hits': labelled_hits,
                             'rate': rate(false_recall, labelled_hits), 'exposures': exposure},
            'rule_revocation_time': {
                'status': revocation_status,
                'samples': len(revocation_hours),
                'mean_hours': round(sum(revocation_hours) / len(revocation_hours), 6)
                if revocation_hours else None,
            },
        }

    def _verdicts(self) -> dict:
        if self.queue is None:
            return {}
        counts = self.queue.verdict_counts()
        return {'counts': counts, 'total': sum(counts.values())}
