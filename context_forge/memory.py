from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import exp, log
from pathlib import Path
from typing import Iterator, Literal, NamedTuple, Sequence

from .memory_models import (
    DEFAULT_OWNER, Actor, Level, MemoryApplication, MemoryDraft, MemoryRecord,
    Relation, Scope, derive_level, split_legacy_scopes,
)
from .memory_schema import PROJECTIONS, SCHEMA, SCHEMA_VERSION, V3, V4, V6, V9, V11
from .risk import requires_confirmation, risk_of
from .rules import glob_match
from .embedding import unpack_vector
from .transcript import sanitize_text


class MemoryStoreError(RuntimeError):
    pass


class VersionConflict(MemoryStoreError):
    pass


class IdempotencyConflict(MemoryStoreError):
    pass


class SourceConflict(MemoryStoreError):
    pass


class ProtectedMemory(MemoryStoreError):
    pass


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def folded_body(body: str) -> str:
    """The one definition of "same wording": casefolded, whitespace collapsed.

    The write gate's fingerprint and the merge report both need it, and a split between
    them would mean two answers to "is this the same assertion".
    """
    return ' '.join(body.casefold().split())


def canonical_fingerprint(draft: MemoryDraft) -> str:
    """Exact assertion + decision-boundary fingerprint; never an entity ID."""
    value = {
        'kind': draft.kind, 'role': draft.role, 'body': folded_body(draft.body),
        'applicability': [item.model_dump(mode='json') for item in draft.applicability],
        'scope': draft.scope.model_dump(mode='json'),
        'space': draft.space.model_dump(mode='json'),
        'access': draft.access.model_dump(mode='json'),
    }
    return digest(canonical_json(value))


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _exec_script(connection: sqlite3.Connection, script: str) -> None:
    # executescript() commits first, which would end a surrounding transaction
    for statement in script.split(';'):
        if statement.strip():
            connection.execute(statement)


GLOB_MARKERS = '*?/\\'
_SLUG = re.compile(r'^[^\s*?/\\<>=%:]+$')
EXPANSION_KINDS = ('derived_from', 'supports', 'refines', 'overlaps')
CONFLICT_KINDS = ('conflicts_with', 'contradicts', 'exception_to')
RETRIEVAL_STAGES = ('retrieved', 'tool_returned', 'presented', 'referenced', 'applied', 'validated')
# Being shown a memory is not using it: the first three stages are written by the recall
# path itself, so a host reporting back may only name what it did with what it was shown.
MARKABLE_STAGES = ('referenced', 'applied', 'validated')
# How long a high-risk confirmation stays answerable. Long enough to walk to a terminal,
# short enough that an old approval cannot be dug up for a new request.
CONFIRMATION_TTL_SECONDS = 600
# Widening is bounded by which operator is allowed, never by how many terms may be
# dropped: a term that matches no document is shed for free, and matching terms are only
# ever given up together by switching the whole query to a union.
WIDEN_RESULT_CAP = 100


def _fts_match(terms: Sequence[str], operator: str = ' AND ') -> str:
    return operator.join('"' + term.replace('"', '""') + '"' for term in terms)


def _token_frequency(connection: sqlite3.Connection, term: str) -> int:
    """How many indexed documents contain a term — decides which token is safe to drop."""
    return connection.execute('SELECT count(*) FROM memory_fts WHERE memory_fts MATCH ?',
                              (_fts_match([term]),)).fetchone()[0]


def _approved_pairs(connection: sqlite3.Connection,
                    pairs: Sequence[tuple[str, int]]) -> set[tuple[str, int]]:
    """Which of these (memory_id, version) pairs a human has already answered.

    Keyed on the version, not the token: approval is a property of the text, so every later
    recall of that same text is released by the same one reading, and a new version is not.
    A row saying "approved" without a terminal behind it answers nothing — recall and the
    exchange must refuse it the same way, or the weaker door becomes the way through.
    """
    if not pairs:
        return set()
    ids = sorted({memory_id for memory_id, _ in pairs})
    rows = connection.execute(
        'SELECT memory_id,version FROM pending_confirmations WHERE approved_at IS NOT NULL '
        'AND approved_from_tty '
        f'AND memory_id IN ({",".join("?" * len(ids))})', ids).fetchall()
    return {(row['memory_id'], row['version']) for row in rows} & set(pairs)

EVIDENCE_SCORE = {'verified': 1.0, 'supported': 0.7, 'unverified': 0.3,
                  'unknown': 0.1, 'contradicted': -0.5}
FRESHNESS_HALF_LIFE_DAYS = 180.0
FRESHNESS_DECAY = log(2) / FRESHNESS_HALF_LIFE_DAYS
REUSE_BOOST = 0.3
CONFLICT_PENALTY = 0.5
UNRESOLVED_SCORE = 0.3
UNRESOLVED_PENALTY = 0.4
SCOPE_GLOBAL_SCORE = 1.0
SCOPE_TECHNOLOGY_SCORE = 0.8
SCOPE_DOMAIN_SCORE = 0.7
PROVENANCE_PROJECT_BONUS = 0.20
PROVENANCE_PATH_BONUS = 0.15

# P3 feedback weight table (technical design 4.4). Exposure stages carry no
# entry on purpose: being shown is not being used.
WEIGHT_TABLE = {'applied': 0.3, 'validated': 0.5, 'revoked': -0.8,
                'harmful': -1.0, 'irrelevant': -0.2}
UNUSED_DECAY_DAYS = 90
UNUSED_DECAY_WEIGHT = -0.1
FEEDBACK_OUTCOMES = ('revoked', 'harmful', 'irrelevant')
DEFAULT_WEIGHT = 1.0


@dataclass(frozen=True)
class WeightEvent:
    """One normalized, attributable signal feeding `apply_feedback_weight`."""

    kind: Literal['applied', 'validated', 'revoked', 'harmful', 'irrelevant']
    at: str | None = None


def apply_feedback_weight(record: MemoryRecord, events: Sequence[WeightEvent],
                          *, at: datetime | None = None) -> float:
    """Pure weight over attributable events; exposure never appears here.

    Only the adopted / validated / negative-feedback kinds exist in
    `WEIGHT_TABLE`. Long-term non-use accrues a small decay unless the memory
    was ever adopted or validated.
    """
    total = sum(WEIGHT_TABLE[event.kind] for event in events)
    if not any(event.kind in {'applied', 'validated'} for event in events):
        created = datetime.fromisoformat(record.created_at)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        moment = at or datetime.now(timezone.utc)
        if (moment - created).days >= UNUSED_DECAY_DAYS:
            total += UNUSED_DECAY_WEIGHT
    return total


@dataclass(frozen=True)
class RetrievalContext:
    query: str = ''
    project: str | None = None
    changed_path: str | None = None
    technologies: frozenset[str] = frozenset()
    domains: frozenset[str] = frozenset()

    def bag(self) -> frozenset[str]:
        parts = {value.casefold() for value in self.technologies | self.domains}
        if self.project:
            parts.add(self.project.casefold())
        path = (self.changed_path or '').replace('\\', '/')
        parts.update(segment.casefold() for segment in path.split('/') if segment)
        return frozenset(parts)


class BoundaryView(NamedTuple):
    owner: str
    privacy: str
    visibility: str
    allowed_projects: tuple[str, ...]
    exclusions: tuple[str, ...]


@dataclass(frozen=True)
class ScoreWeights:
    match: float = 1.0
    evidence: float = 1.2
    freshness: float = 0.6
    feedback: float = 0.8
    text: float = 1.0


DEFAULT_WEIGHTS = ScoreWeights()


@dataclass(frozen=True)
class ScoreBreakdown:
    total: float
    match: float
    applicability: float
    provenance_bonus: float
    evidence: float
    freshness: float
    feedback: float
    text: float
    conflict_penalty: float
    admitted_by: Level
    explanation: str
    # Which channel reached this record (bm25 / widened / semantic), structured so a consumer
    # never has to parse the Chinese explanation string (design §7).
    channel: str = 'bm25'


@dataclass(frozen=True)
class CandidatePool:
    items: tuple[tuple[MemoryRecord, ScoreBreakdown], ...]
    truncated: bool


def boundary_view(draft: MemoryDraft) -> BoundaryView:
    return BoundaryView(draft.space.owner, draft.space.privacy, draft.access.visibility,
                        tuple(draft.access.allowed_projects), tuple(draft.scope.exclusions))


def condition_result(condition: str, ctx: RetrievalContext) -> str:
    """Evaluate one condition phrase against the retrieval context.

    Answers in the three states of `Applicability.result`. Decidable forms are
    a path glob (needs a changed path) and a bare name with no spaces, glob or
    operator characters. Everything else answers `unknown`: an undecidable
    condition is reported as risk and never acts as a silent hard filter.
    """
    text = condition.strip()
    if not text:
        return 'unknown'
    if any(marker in text for marker in GLOB_MARKERS):
        path = (ctx.changed_path or '').replace('\\', '/')
        return 'unknown' if not path else ('true' if glob_match(path, text) else 'false')
    if not _SLUG.match(text):
        return 'unknown'
    return 'true' if text.casefold() in ctx.bag() else 'false'


def paths_hit(paths: Sequence[str], changed_path: str | None) -> bool:
    if not changed_path or not paths:
        return False
    path = changed_path.replace('\\', '/')
    return any(glob_match(path, pattern) for pattern in paths)


def violated(constraints: Sequence[str], ctx: RetrievalContext) -> bool:
    return any(condition_result(entry, ctx) == 'false' for entry in constraints)


_VISIBILITY_RANK = {'owner': 0, 'selected_projects': 1, 'shared': 2}


def access_broader(previous: MemoryDraft, current: MemoryDraft) -> bool:
    """True when `current` reaches a subject or project `previous` did not.

    Automatic writes may only narrow access; widening it is a human-only
    action (technical design 2.3).
    """
    if _VISIBILITY_RANK[current.access.visibility] > _VISIBILITY_RANK[previous.access.visibility]:
        return True
    if current.access.visibility == previous.access.visibility == 'selected_projects':
        if not set(current.access.allowed_projects) <= set(previous.access.allowed_projects):
            return True
    return previous.space.privacy == 'private' and current.space.privacy == 'shared'


def exclusion_outcome(exclusions: Sequence[str], ctx: RetrievalContext) -> tuple[bool, tuple[str, ...]]:
    """Counterexamples eliminate only when the context decides they hold."""
    unresolved: list[str] = []
    for entry in exclusions:
        result = condition_result(entry, ctx)
        if result == 'true':
            return True, ()
        if result == 'unknown':
            unresolved.append(entry)
    return False, tuple(unresolved)


def boundary_visible(view: BoundaryView, *, owner: str, ctx: RetrievalContext) -> bool:
    """The four boundary eliminations, and nothing else.

    `project` resolves AccessPolicy scope only; it never matches against
    provenance, so cross-project memories stay reachable. Candidate-pool
    admission is a separate stage and must not be folded in here.
    """
    if view.owner != owner:
        return False
    # ponytail: subsumed by the owner check while one subject owns the store;
    # kept so a future shared space cannot leak private records
    if view.privacy == 'private' and view.owner != owner:
        return False
    if view.visibility == 'selected_projects' and ctx.project not in view.allowed_projects:
        return False
    eliminate, _unresolved = exclusion_outcome(view.exclusions, ctx)
    return not eliminate


def admitted(record: MemoryRecord, ctx: RetrievalContext) -> bool:
    """Candidate-pool admission by derived level. Never an elimination."""
    draft = record.draft
    level = derive_level(draft)
    if level == 'principle':
        return True
    if level == 'pattern':
        if set(draft.scope.technologies) & set(ctx.technologies):
            return True
        if set(draft.scope.domains) & set(ctx.domains):
            return True
        # A constraints-only pattern has no technology or domain to hit; a
        # decidable, satisfied constraint is its only admission evidence.
        return any(condition_result(entry, ctx) == 'true' for entry in draft.scope.constraints)
    return bool(set(draft.provenance.projects) & {ctx.project}
                or paths_hit(draft.provenance.paths, ctx.changed_path))


def applicability_score(memory: MemoryRecord, ctx: RetrievalContext) -> float:
    scope = memory.draft.scope
    applicability = 0.0
    applicability += SCOPE_GLOBAL_SCORE if scope.visibility == 'global' else 0.0
    applicability += SCOPE_TECHNOLOGY_SCORE if set(scope.technologies) & set(ctx.technologies) else 0.0
    applicability += SCOPE_DOMAIN_SCORE if set(scope.domains) & set(ctx.domains) else 0.0
    if not (scope.technologies or scope.domains or scope.constraints or scope.visibility == 'global'):
        applicability = UNRESOLVED_SCORE
    applicability -= UNRESOLVED_PENALTY if violated(scope.constraints, ctx) else 0.0
    return applicability


def provenance_bonus(memory: MemoryRecord, ctx: RetrievalContext) -> float:
    bonus = 0.0
    bonus += PROVENANCE_PROJECT_BONUS if ctx.project in memory.draft.provenance.projects else 0.0
    bonus += PROVENANCE_PATH_BONUS if paths_hit(memory.draft.provenance.paths, ctx.changed_path) else 0.0
    return bonus


def match_score(memory: MemoryRecord, ctx: RetrievalContext) -> float:
    """适用条件 + 来源加分. Provenance only ever adds, it never eliminates."""
    return applicability_score(memory, ctx) + provenance_bonus(memory, ctx)


def freshness_score(memory: MemoryRecord, applied: int = 0, at: datetime | None = None) -> float:
    """Reused memories hold up; `applied` counts attributable adoptions only.

    `applied` is the number of `retrieval_events` at the `applied` stage for
    this version. Exposure stages and `proof_count` are deliberately not
    inputs: being shown is not being used.
    """
    created = datetime.fromisoformat(memory.created_at)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    moment = at or datetime.now(timezone.utc)
    days = max(0, int((moment - created).total_seconds() // 86400))
    return exp(-FRESHNESS_DECAY * days) * (1.0 + REUSE_BOOST * applied)


def conflict_penalty(conflicts: int) -> float:
    return CONFLICT_PENALTY * conflicts


def text_scores(ranks: Sequence[float]) -> list[float]:
    """Scale-free BM25 normalisation; equal ranks stay equal."""
    if not ranks:
        return []
    low, high = min(ranks), max(ranks)
    if high == low:
        return [1.0] * len(ranks)
    return [(value - low) / (high - low) for value in ranks]


def _explain(draft: MemoryDraft, ctx: RetrievalContext, *, applicability: float,
             bonus: float, evidence: float, freshness: float, feedback: float,
             text: float, risks: Sequence[str], match_note: str = '', channel: str = '',
             lexical_score: float | None = None, semantic_score: float | None = None) -> str:
    scope = draft.scope
    hits = []
    if scope.visibility == 'global':
        hits.append(f'全局适用 +{SCOPE_GLOBAL_SCORE}')
    shared_technologies = sorted(set(scope.technologies) & set(ctx.technologies))
    shared_domains = sorted(set(scope.domains) & set(ctx.domains))
    if shared_technologies:
        hits.append(f"技术:{','.join(shared_technologies)} +{SCOPE_TECHNOLOGY_SCORE}")
    if shared_domains:
        hits.append(f"领域:{','.join(shared_domains)} +{SCOPE_DOMAIN_SCORE}")
    if not hits:
        hits.append(f'未解析 +{UNRESOLVED_SCORE}' if applicability == UNRESOLVED_SCORE else f'约束惩罚 {applicability:.2f}')
    provenance = []
    if ctx.project and ctx.project in draft.provenance.projects:
        provenance.append(f'同项目 +{PROVENANCE_PROJECT_BONUS:.2f}')
    if paths_hit(draft.provenance.paths, ctx.changed_path):
        provenance.append(f'证据路径 +{PROVENANCE_PATH_BONUS:.2f}')
    # Channel + both raw scores are additive to the free-text note: the note says which
    # relaxation reached the record, the structured parts say through which channel and with
    # what lexical / cosine numbers (design §7), which the layer-2 report can bucket on.
    match_parts = []
    if channel:
        match_parts.append(f'通道={channel}')
    if lexical_score is not None:
        match_parts.append(f'词法={lexical_score:.2f}')
    if semantic_score is not None:
        match_parts.append(f'语义={semantic_score:.2f}')
    if match_note:
        match_parts.append(match_note)
    match_suffix = f' 匹配[{" ".join(match_parts)}]' if match_parts else ''
    line_hit = ('为什么命中: 适用[' + ', '.join(hits) + '] 来源[' + (', '.join(provenance) or '无加分')
                + f'] 证据[{draft.evidence_level} {evidence:.2f}] 时效[{freshness:.2f}] '
                + f'反馈[{feedback:.2f}] 文本[{text:.2f}]' + match_suffix)

    projects = ', '.join(draft.provenance.projects) or '未声明项目'
    sessions = draft.provenance.sessions
    origin = f'来自 {projects}' + (f" / session {', '.join(sessions)}" if sessions else '')
    migration = []
    if shared_technologies:
        migration.append(f"适用技术 [{', '.join(shared_technologies)}] 与当前技术栈重合")
    if shared_domains:
        migration.append(f"问题域 [{', '.join(shared_domains)}] 命中")
    if scope.visibility == 'global':
        migration.append('声明为全局适用')
    if not migration:
        if provenance:
            migration.append('未匹配任何适用维度（仅来源相关） — 谨慎采用')
        elif ctx.query:
            migration.append('仅文本相关, 未匹配任何适用维度 — 谨慎采用')
        else:
            migration.append('未匹配任何适用维度 — 谨慎采用')
    line_origin = f"来源与迁移: {origin}; 迁移理由: {'; '.join(migration)}"

    buckets: dict[str, list[str]] = {'true': [], 'false': [], 'unknown': []}
    for condition in draft.applicability:
        buckets[condition.result].append(condition.condition)
    undecidable = [entry for entry in (*scope.exclusions, *scope.constraints)
                   if condition_result(entry, ctx) == 'unknown']
    buckets['unknown'].extend(undecidable)
    unresolved = len(buckets['unknown'])
    if not buckets['unknown']:
        buckets['unknown'].append('未穷举适用条件')
    line_bounds = '适用边界: ' + '; '.join(
        f'{name}="' + ' | '.join(values) + '"' if values else f'{name}=（无）'
        for name, values in (('true', buckets['true']), ('false', buckets['false']),
                             ('unknown', buckets['unknown'])))

    notes = list(risks)
    if scope.technologies and not shared_technologies:
        notes.append(f"技术栈未命中 [{', '.join(sorted(scope.technologies))}]")
    if scope.domains and not shared_domains:
        notes.append(f"问题域未命中 [{', '.join(sorted(scope.domains))}]")
    unmet = [entry for entry in scope.constraints if condition_result(entry, ctx) == 'false']
    if unmet:
        notes.append(f"约束未满足 [{', '.join(unmet)}]")
    if buckets['false']:
        notes.append('已知反例: ' + ' | '.join(buckets['false']))
    if unresolved:
        notes.append(f'未解析 {unresolved} 项')
    line_risk = '风险提示: ' + ('; '.join(notes) if notes else '无')
    return '\n'.join((line_hit, line_origin, line_bounds, line_risk))


def relevance(memory: MemoryRecord, ctx: RetrievalContext,
              weights: ScoreWeights = DEFAULT_WEIGHTS, *, text: float = 0.0,
              applied: int = 0, feedback: float = 0.0, conflicts: int = 0,
              risks: Sequence[str] = (), match_note: str = '', channel: str = '',
              lexical_score: float | None = None, semantic_score: float | None = None) -> ScoreBreakdown:
    """Five weighted dimensions minus the conflict penalty.

    `feedback` is the w4 calibration hook (P3's `apply_feedback_weight`);
    there is no attributable application data before P3, so exposure events
    are never fed in here.
    """
    applicability = applicability_score(memory, ctx)
    bonus = provenance_bonus(memory, ctx)
    match = applicability + bonus
    evidence = EVIDENCE_SCORE[memory.draft.evidence_level]
    freshness = freshness_score(memory, applied=applied)
    penalty = conflict_penalty(conflicts)
    total = (weights.match * match + weights.evidence * evidence
             + weights.freshness * freshness + weights.feedback * feedback
             + weights.text * text - penalty)
    return ScoreBreakdown(
        total=total, match=match, applicability=applicability, provenance_bonus=bonus,
        evidence=evidence, freshness=freshness, feedback=feedback, text=text,
        conflict_penalty=penalty, admitted_by=derive_level(memory.draft), channel=channel,
        explanation=_explain(memory.draft, ctx, applicability=applicability, bonus=bonus,
                             evidence=evidence, freshness=freshness, feedback=feedback,
                             text=text, risks=risks, match_note=match_note, channel=channel,
                             lexical_score=lexical_score, semantic_score=semantic_score))


def _split_snapshot(raw: str, *, owner: str, privacy: str) -> tuple[str, MemoryDraft]:
    data = json.loads(raw)
    scopes = tuple(Scope.model_validate(item) for item in data.pop('scopes', ()))
    split = split_legacy_scopes(scopes, owner=owner, privacy=privacy)  # type: ignore[arg-type]
    data['space'] = split.space.model_dump(mode='json')
    data['access'] = split.access.model_dump(mode='json')
    data['provenance'] = split.provenance.model_dump(mode='json')
    data['scope'] = split.scope.model_dump(mode='json')
    data['applicability'] = list(data.get('applicability') or ()) + [
        condition.model_dump(mode='json') for condition in split.applicability]
    data.setdefault('proof_count', 0)
    data.setdefault('last_confirmed_at', None)
    if data.get('role') == 'observation' and not (split.provenance.projects or split.provenance.sessions):
        data['role'] = 'claim'
    draft = MemoryDraft.model_validate(data)
    return canonical_json(draft.model_dump(mode='json')), draft


def _insert_projections(connection: sqlite3.Connection, memory_id: str, version: int,
                        draft: MemoryDraft) -> None:
    view = boundary_view(draft)
    connection.execute('INSERT OR REPLACE INTO memory_access_projection VALUES (?,?,?,?,?,?)',
                       (memory_id, version, view.owner, view.privacy, view.visibility,
                        canonical_json(list(view.allowed_projects))))
    connection.execute('INSERT OR REPLACE INTO memory_applicability_projection VALUES (?,?,?,?,?)',
                       (memory_id, version, canonical_json(list(draft.scope.technologies)),
                        canonical_json(list(draft.scope.domains)), canonical_json(list(view.exclusions))))


def tokenize(text: str) -> list[str]:
    tokens = []
    for match in re.finditer(r'[a-zA-Z0-9_]+|[\u3400-\u9fff\U00020000-\U0002fa1f]+', text):
        word = match.group().lower()
        if word.isascii():
            tokens.append(word)
        elif len(word) > 1:
            tokens.extend(word[index:index + 2] for index in range(len(word) - 1))
    return tokens


def _actor(actor: Actor) -> str:
    if actor not in {'human', 'user', 'model', 'tool', 'import', 'system'}:
        raise ValueError('unknown actor')
    return 'human' if actor == 'user' else actor


class MemoryStore:
    """An existing database handle; connections live for one operation only."""

    def __init__(self, path: Path, *, readonly: bool = True, embedder=None):
        self.path = Path(path).resolve()
        self.readonly = readonly
        self.embedder = embedder
        with self._connection():
            pass

    @classmethod
    def initialize(cls, path: Path) -> MemoryStore:
        path = Path(path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=5)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA foreign_keys=ON')
            version = connection.execute('PRAGMA user_version').fetchone()[0]
            # Every older schema migrates here: `migrate()` already supports each version
            # at or above 1, and `restore()` re-runs this path on a `.pre-vN-` backup so a
            # rollback copy of any vintage is restorable. The constructor still refuses to
            # open an un-migrated database, so this stays the one explicit entry point.
            if 1 <= version < SCHEMA_VERSION:
                connection.close()
                connection = None
                cls.migrate(path, dry_run=False)
                connection = sqlite3.connect(path, timeout=5)
                connection.execute('PRAGMA foreign_keys=ON')
                version = connection.execute('PRAGMA user_version').fetchone()[0]
            if version == 0:
                if connection.execute("SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchone():
                    raise MemoryStoreError('refusing to initialize an unrelated database')
                connection.executescript('BEGIN IMMEDIATE;\n' + SCHEMA + f'\nPRAGMA user_version={SCHEMA_VERSION};\nCOMMIT;')
            elif version != SCHEMA_VERSION:
                raise MemoryStoreError(f'unsupported memory schema {version}; expected {SCHEMA_VERSION}')
            connection.execute('PRAGMA journal_mode=WAL')
        except sqlite3.Error as error:
            if connection is not None:
                connection.rollback()
            raise MemoryStoreError(f'cannot initialize memory database {path}: {error}') from error
        finally:
            if connection is not None:
                connection.close()
        return cls(path, readonly=False)

    @classmethod
    def migrate(cls, path: Path, *, dry_run: bool = True,
                owner: str = DEFAULT_OWNER, privacy: str = 'shared') -> dict:
        """Explicit migration chain up to the current schema.

        v1 splits legacy `Scope` into the boundary trio; v2 adds the weight
        cache and `memory_applications`. Takes an online backup before touching
        anything. `dry_run=True` reports without writing. To roll back, copy the
        `.pre-v<N>-*.db` file over the live database: `restore()` on that file
        would re-run this migration via `initialize()`, so it is disaster
        recovery, not a rollback. Projections are rebuildable from
        snapshot_json.
        """
        path = Path(path).resolve()
        report = {'dry_run': dry_run, 'from': None, 'to': SCHEMA_VERSION,
                  'rewritten': 0, 'unchanged': 0, 'backup': None,
                  'projections': 0, 'diagnostics': [], 'mapping': {}}
        connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)
        try:
            connection.row_factory = sqlite3.Row
            version = connection.execute('PRAGMA user_version').fetchone()[0]
        finally:
            connection.close()
        report['from'] = version
        if version == SCHEMA_VERSION:
            report['unchanged'] = -1
            return report
        if not 1 <= version < SCHEMA_VERSION:
            raise MemoryStoreError(f'cannot migrate memory schema {version} to {SCHEMA_VERSION}')
        if not dry_run:
            report['backup'] = str(cls._online_backup(path))
        rewritten = []
        if version == 1:
            rows = []
            connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)
            try:
                connection.row_factory = sqlite3.Row
                rows = [dict(row) for row in connection.execute(
                    'SELECT memory_id,version,snapshot_json FROM memory_versions ORDER BY memory_id,version')]
            finally:
                connection.close()
            for row in rows:
                try:
                    snapshot, draft = _split_snapshot(row['snapshot_json'], owner=owner, privacy=privacy)
                except Exception as error:
                    report['diagnostics'].append(f"{row['memory_id']}@{row['version']}: {error}")
                    continue
                report['projections'] += 1
                if snapshot == row['snapshot_json']:
                    report['unchanged'] += 1
                    continue
                report['rewritten'] += 1
                report['mapping'][f"{row['memory_id']}@{row['version']}"] = {
                    'projects': list(draft.provenance.projects),
                    'paths': list(draft.provenance.paths),
                    'visibility': draft.access.visibility,
                    'scope_visibility': draft.scope.visibility,
                    'role': draft.role,
                }
                rewritten.append((row['memory_id'], row['version'], snapshot, draft))
        if dry_run:
            return report
        connection = sqlite3.connect(path, timeout=5)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('BEGIN IMMEDIATE')
            _exec_script(connection, PROJECTIONS)
            for memory_id, version_no, snapshot, draft in rewritten:
                connection.execute('UPDATE memory_versions SET snapshot_json=? WHERE memory_id=? AND version=?',
                                   (snapshot, memory_id, version_no))
                _insert_projections(connection, memory_id, version_no, draft)
            connection.execute('DROP TABLE IF EXISTS memory_scopes')
            columns = {row[1] for row in connection.execute('PRAGMA table_info(memories)')}
            if 'weight' not in columns:
                connection.execute('ALTER TABLE memories ADD COLUMN weight REAL NOT NULL DEFAULT 1.0')
            _exec_script(connection, V3)
            columns = {row[1] for row in connection.execute('PRAGMA table_info(memory_versions)')}
            if 'canonical_fingerprint' not in columns:
                connection.execute("ALTER TABLE memory_versions ADD COLUMN canonical_fingerprint TEXT NOT NULL DEFAULT ''")
            if 'risk' not in columns:
                connection.execute("ALTER TABLE memory_versions ADD COLUMN risk TEXT NOT NULL DEFAULT 'normal'")
            for row in connection.execute('SELECT memory_id,version,snapshot_json FROM memory_versions').fetchall():
                value = MemoryDraft.model_validate_json(row['snapshot_json'])
                connection.execute('UPDATE memory_versions SET canonical_fingerprint=?,risk=? '
                                   'WHERE memory_id=? AND version=?',
                                   (canonical_fingerprint(value), risk_of(value),
                                    row['memory_id'], row['version']))
            _exec_script(connection, V4)
            memory_columns = {row[1] for row in connection.execute('PRAGMA table_info(memories)')}
            for name, ddl in (
                ('host', "ALTER TABLE memories ADD COLUMN host TEXT NOT NULL DEFAULT 'unknown'"),
                ('model', "ALTER TABLE memories ADD COLUMN model TEXT NOT NULL DEFAULT ''"),
                ('prompt_version', "ALTER TABLE memories ADD COLUMN prompt_version TEXT NOT NULL DEFAULT ''"),
                ('evidence_level', "ALTER TABLE memories ADD COLUMN evidence_level TEXT NOT NULL DEFAULT 'unknown'"),
            ):
                if name not in memory_columns:
                    connection.execute(ddl)
            _exec_script(connection, V6)
            _exec_script(connection, V9)
            confirmation_columns = {row[1] for row in connection.execute(
                'PRAGMA table_info(pending_confirmations)')}
            if 'replay_attempts' not in confirmation_columns:
                connection.execute('ALTER TABLE pending_confirmations ADD COLUMN replay_attempts '
                                   'INTEGER NOT NULL DEFAULT 0')
            version_columns = {row[1] for row in connection.execute('PRAGMA table_info(memory_versions)')}
            if 'embedding_model' not in version_columns:
                connection.execute("ALTER TABLE memory_versions ADD COLUMN embedding_model TEXT NOT NULL DEFAULT ''")
            if 'embedding' not in version_columns:
                connection.execute("ALTER TABLE memory_versions ADD COLUMN embedding BLOB")
            for row in connection.execute(
                'SELECT m.id, v.snapshot_json FROM memories m '
                'JOIN memory_versions v ON v.memory_id=m.id AND v.version=m.current_version').fetchall():
                value = MemoryDraft.model_validate_json(row['snapshot_json'])
                connection.execute('UPDATE memories SET host=?,model=?,prompt_version=?,evidence_level=? WHERE id=?',
                                   (value.provenance.host, value.model or '',
                                    value.prompt_version or '', value.evidence_level, row['id']))
            connection.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
            connection.execute('COMMIT')
            connection.execute('PRAGMA journal_mode=WAL')
        except sqlite3.Error as error:
            connection.rollback()
            raise MemoryStoreError(f'cannot migrate memory database {path}: {error}') from error
        finally:
            connection.close()
        return report

    @staticmethod
    def _copy_database(source: Path, target: Path) -> Path:
        """Online-backup `source` into a new `target`, whatever schema it holds."""
        source = Path(source).resolve()
        target = Path(target).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb'):
            pass
        try:
            reader = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=5)
            try:
                writer = sqlite3.connect(target)
                try:
                    reader.backup(writer)
                    if writer.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise MemoryStoreError('backup integrity check failed')
                finally:
                    writer.close()
            finally:
                reader.close()
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return target

    @staticmethod
    def _online_backup(path: Path) -> Path:
        target = path.with_name(path.name + f'.pre-v{SCHEMA_VERSION}-{now().replace(":", "").replace("-", "")}.db')
        return MemoryStore._copy_database(path, target)

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        if write and self.readonly:
            raise MemoryStoreError('store was opened read-only')
        connection = None
        try:
            mode = 'rw' if write else 'ro'
            connection = sqlite3.connect(self.path.as_uri() + f'?mode={mode}', uri=True, timeout=5)
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('PRAGMA busy_timeout=5000')
            version = connection.execute('PRAGMA user_version').fetchone()[0]
            if version != SCHEMA_VERSION:
                raise MemoryStoreError(f'unsupported memory schema {version}; explicitly initialize/migrate first')
            connection.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            connection.execute('SELECT id,current_version,status,user_owned,suppressed FROM memories LIMIT 0')
            yield connection
            if write:
                connection.commit()
        except sqlite3.Error as error:
            raise MemoryStoreError(f'memory database {self.path}: {error}') from error
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _record(connection: sqlite3.Connection, memory_id: str, version: int | None = None) -> MemoryRecord | None:
        row = connection.execute(
            'SELECT v.* FROM memory_versions v JOIN memories m ON m.id=v.memory_id '
            'WHERE m.id=? AND v.version=COALESCE(?,m.current_version)', (memory_id, version),
        ).fetchone()
        if row is None:
            return None
        return MemoryRecord(id=memory_id, version=row['version'],
                            draft=MemoryDraft.model_validate_json(row['snapshot_json']),
                            created_at=row['created_at'])

    def get(self, memory_id: str, version: int | None = None) -> MemoryRecord | None:
        with self._connection() as connection:
            return self._record(connection, memory_id, version)

    def history(self, memory_id: str) -> list[MemoryRecord]:
        with self._connection() as connection:
            versions = connection.execute('SELECT version FROM memory_versions WHERE memory_id=? ORDER BY version', (memory_id,))
            return [self._record(connection, memory_id, row['version']) for row in versions]

    def current_records(self) -> list[MemoryRecord]:
        with self._connection() as connection:
            return [self._record(connection, row['id'])
                    for row in connection.execute('SELECT id FROM memories ORDER BY id')]

    def reindex_embeddings(self, embedder) -> dict[str, int]:
        """Rebuild current-version local vectors without changing memory versions."""
        with self._connection() as connection:
            rows = connection.execute(
                'SELECT m.id,v.version,v.snapshot_json FROM memories m '
                'JOIN memory_versions v ON v.memory_id=m.id AND v.version=m.current_version '
                'ORDER BY m.id').fetchall()
        prepared: list[tuple[str, int, bytes]] = []
        failed = 0
        for row in rows:
            try:
                draft = MemoryDraft.model_validate_json(row['snapshot_json'])
                text = sanitize_text(draft.summary + '\n' + draft.body)
                prepared.append((row['id'], row['version'], embedder.encode(text)))
            except Exception:
                failed += 1
        updated = 0
        with self._connection(write=True) as connection:
            for memory_id, version, vector in prepared:
                result = connection.execute(
                    'UPDATE memory_versions SET embedding_model=?,embedding=? '
                    'WHERE memory_id=? AND version=? AND EXISTS '
                    '(SELECT 1 FROM memories WHERE id=? AND current_version=?)',
                    (embedder.identity, vector, memory_id, version, memory_id, version),
                )
                updated += result.rowcount
        return {'total': len(rows), 'updated': updated, 'failed': failed,
                'stale': len(prepared) - updated}

    def find_exact(self, fingerprint: str) -> MemoryRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                'SELECT v.memory_id,v.version FROM memory_versions v JOIN memories m ON m.id=v.memory_id '
                'WHERE v.canonical_fingerprint=? AND v.version=m.current_version ORDER BY v.memory_id LIMIT 1',
                (fingerprint,),
            ).fetchone()
            return self._record(connection, row['memory_id'], row['version']) if row else None

    def issue_confirmation(self, memory_id: str, version: int, *, reason: str,
                           request_id: str | None = None, session_id: str | None = None,
                           ttl_seconds: int = CONFIRMATION_TTL_SECONDS) -> str:
        """Hand out an approval handle for one memory version.

        A uuid4, never a digest of content: a token the model can compute is a token the model
        can approve for itself. The row is the server-side state F3 asked for — an answer that
        lives only in the response text can be replayed, forged or never read by a human.
        """
        token = str(uuid.uuid4())
        moment = now()
        expiry = (datetime.fromisoformat(moment) + timedelta(seconds=ttl_seconds)).isoformat()
        with self._connection(write=True) as connection:
            if connection.execute('SELECT 1 FROM memory_versions WHERE memory_id=? AND version=?',
                                  (memory_id, version)).fetchone() is None:
                raise KeyError((memory_id, version))
            connection.execute(
                'INSERT INTO pending_confirmations (token,memory_id,version,request_id,session_id,'
                'reason,issued_at,expires_at) VALUES (?,?,?,?,?,?,?,?)',
                (token, memory_id, version, request_id, session_id, reason, moment, expiry))
        return token

    def open_confirmation(self, memory_id: str, version: int, *, reason: str,
                         request_id: str | None = None, session_id: str | None = None,
                         ttl_seconds: int = CONFIRMATION_TTL_SECONDS) -> dict:
        """Return one live handle for this version, issuing a new row only if none is open.

        Recall can fire many times a minute for the same parked rule. Handing out a fresh token
        each time would bury the human under identical requests and grow the ledger without
        bound, so an unanswered, unexpired handle is reused; a re-issue happens once it lapses
        or is answered, which is when the old one stops meaning anything.
        """
        with self._connection(write=True) as connection:
            row = connection.execute(
                'SELECT token,expires_at FROM pending_confirmations WHERE memory_id=? AND version=? '
                'AND approved_at IS NULL AND used_at IS NULL AND expires_at > ? '
                'ORDER BY issued_at DESC LIMIT 1', (memory_id, version, now())).fetchone()
            if row is not None:
                return {'token': row['token'], 'expires_at': row['expires_at']}
        token = self.issue_confirmation(memory_id, version, reason=reason, request_id=request_id,
                                        session_id=session_id, ttl_seconds=ttl_seconds)
        with self._connection() as connection:
            expiry = connection.execute('SELECT expires_at FROM pending_confirmations WHERE token=?',
                                        (token,)).fetchone()['expires_at']
        return {'token': token, 'expires_at': expiry}

    def approved_versions(self, pairs: Sequence[tuple[str, int]]) -> set[tuple[str, int]]:
        """Which of these (memory_id, version) pairs a human has already answered."""
        if not pairs:
            return set()
        with self._connection() as connection:
            return _approved_pairs(connection, pairs)

    def needs_confirmation(self, record: MemoryRecord) -> bool:
        """A high-risk rule whose text nobody has read yet."""
        if not requires_confirmation(record.draft):
            return False
        return (record.id, record.version) not in self.approved_versions([(record.id, record.version)])

    def pending_confirmations(self) -> list[dict]:
        """Open requests joined to the text a human has to read before answering.

        Joining on `current_version` is what makes a version change expire the request: the row
        stops being about the memory as it now stands, and no cleanup job is involved.
        """
        with self._connection() as connection:
            rows = connection.execute(
                'SELECT c.token,c.memory_id,c.version,c.reason,c.issued_at,c.expires_at,'
                'c.session_id,v.snapshot_json FROM pending_confirmations c '
                'JOIN memories m ON m.id=c.memory_id AND m.current_version=c.version '
                'JOIN memory_versions v ON v.memory_id=c.memory_id AND v.version=c.version '
                'WHERE c.approved_at IS NULL AND c.used_at IS NULL AND c.expires_at > ? '
                'ORDER BY c.issued_at,c.token', (now(),)).fetchall()
        opened = []
        for row in rows:
            draft = MemoryDraft.model_validate_json(row['snapshot_json'])
            opened.append({'token': row['token'], 'memory_id': row['memory_id'],
                           'version': row['version'], 'reason': row['reason'],
                           'issued_at': row['issued_at'], 'expires_at': row['expires_at'],
                           'session_id': row['session_id'], 'kind': draft.kind,
                           'risk': risk_of(draft), 'summary': draft.summary or draft.body[:120],
                           'body': draft.body})
        return opened

    def approve_confirmation(self, token: str, *, from_tty: bool,
                            idempotency_key: str | None = None) -> MemoryRecord:
        """A human answers a request; a publication approval also lifts it out of candidate.

        Every check is server-side: the row exists and is still the current version, nobody has
        answered it, it has not expired, and the answer came from a terminal. Approval never
        touches a record that is no longer candidate, so answering late cannot stomp a newer state.
        """
        if not from_tty:
            raise PermissionError('an approval must be typed in a terminal, not piped')
        with self._connection(write=True) as connection:
            row = connection.execute(
                'SELECT c.token,c.memory_id,c.version,c.approved_at,c.used_at,c.expires_at,'
                'm.current_version FROM pending_confirmations c '
                'JOIN memories m ON m.id=c.memory_id WHERE c.token=?', (token,)).fetchone()
            if row is None:
                raise KeyError(token)
            if row['approved_at'] is not None or row['used_at'] is not None:
                raise PermissionError('this token was already answered')
            if row['expires_at'] <= now():
                raise PermissionError('this token expired; run the recall again to get a new one')
            if row['current_version'] != row['version']:
                raise PermissionError('the memory changed version; this approval no longer applies')
            record = self._record(connection, row['memory_id'], row['version'])
            if record is None:
                raise KeyError((row['memory_id'], row['version']))
            if record.draft.status == 'candidate':
                record = self._put(connection, row['memory_id'],
                                   record.draft.model_copy(update={'status': 'active'}),
                                   expected_version=record.version,
                                   idempotency_key=idempotency_key or f'approve:{token}',
                                   actor='human', reason='high-risk rule approved in a terminal')
            connection.execute('UPDATE pending_confirmations SET approved_at=?,approved_from_tty=1 '
                               'WHERE token=?', (now(), token))
            return record

    def consume_confirmation(self, token: str) -> dict:
        """Give the withheld text to whoever holds a handle a human actually answered.

        Approval is the load-bearing check, not possession of the token: recall hands the token
        to the model, so an exchange gated on the token alone would be the model approving itself.
        The handle is single-use, and a second attempt on a used one is counted in the same
        transaction that reports it — a host that keeps re-exchanging one handle is behaving
        exactly as F3 predicted and should surface in a report, not vanish into an exception.
        """
        released = None
        denied = None
        with self._connection(write=True) as connection:
            row = connection.execute(
                'SELECT c.token,c.memory_id,c.version,c.approved_at,c.approved_from_tty,'
                'c.used_at,c.expires_at,m.current_version FROM pending_confirmations c '
                'JOIN memories m ON m.id=c.memory_id WHERE c.token=?', (token,)).fetchone()
            if row is None:
                raise KeyError(token)
            if row['used_at'] is not None:
                connection.execute('UPDATE pending_confirmations SET replay_attempts='
                                   'replay_attempts + 1 WHERE token=?', (token,))
                denied = ('this token was already exchanged; run the recall again for a new one')
            elif row['approved_at'] is None:
                denied = ('no human has answered this handle yet: run '
                          'forge approve --token <handle> --apply in a terminal')
            elif not row['approved_from_tty']:
                denied = 'this approval was not recorded from a terminal, so it does not release the text'
            elif row['expires_at'] <= now():
                denied = 'this token expired; run the recall again to get a new one'
            elif row['current_version'] != row['version']:
                denied = 'the memory changed version; this approval no longer applies'
            else:
                record = self._record(connection, row['memory_id'], row['version'])
                if record is None:
                    raise KeyError((row['memory_id'], row['version']))
                connection.execute('UPDATE pending_confirmations SET used_at=? WHERE token=?',
                                   (now(), token))
                released = {'memory_id': record.id, 'version': record.version,
                            'body': record.draft.body, 'kind': record.draft.kind,
                            'risk': record.draft.risk, 'status': record.draft.status,
                            'approved_at': row['approved_at']}
        if denied is not None:
            raise PermissionError(denied)
        return released

    def confirmation_audit_trail(self, limit: int = 50) -> list[dict]:
        """Recent answers, with the tty flag: what was approved, when and how."""
        with self._connection() as connection:
            return [dict(row) for row in connection.execute(
                'SELECT token,memory_id,version,reason,issued_at,expires_at,approved_at,'
                'approved_from_tty,used_at,replay_attempts FROM pending_confirmations '
                'ORDER BY issued_at DESC LIMIT ?', (max(1, limit),))]

    def tombstone_session_sources(self, session_id: str, *, reason: str = 'session deleted') -> int:
        """Remove retained excerpts for a deleted session while preserving identities."""
        changed = 0
        with self._connection(write=True) as connection:
            rows = connection.execute('SELECT identity,source_json FROM memory_sources').fetchall()
            for row in rows:
                source = json.loads(row['source_json'])
                if source.get('session_id') != session_id:
                    continue
                source['excerpt'] = ''
                source['reference'] = 'source_deleted'
                rendered = canonical_json(source)
                connection.execute('UPDATE memory_sources SET source_json=?,content_hash=? WHERE identity=?',
                                   (rendered, digest(''), row['identity']))
                connection.execute('INSERT OR REPLACE INTO source_tombstones VALUES (?,?,?,?)',
                                   (row['identity'], session_id, now(), reason))
                changed += 1
            if changed:
                versions = connection.execute(
                    'SELECT memory_id,version,snapshot_json FROM memory_versions').fetchall()
                for version in versions:
                    snapshot = json.loads(version['snapshot_json'])
                    touched = False
                    for source in snapshot.get('sources', []):
                        if source.get('session_id') == session_id:
                            source['excerpt'] = ''
                            source['reference'] = 'source_deleted'
                            touched = True
                    if touched:
                        connection.execute(
                            'UPDATE memory_versions SET snapshot_json=? WHERE memory_id=? AND version=?',
                            (canonical_json(snapshot), version['memory_id'], version['version']))
        return changed

    def events(self, memory_id: str) -> list[dict]:
        with self._connection() as connection:
            return [dict(row) for row in connection.execute('SELECT * FROM memory_events WHERE memory_id=? ORDER BY id', (memory_id,))]

    def record_retrieval(self, memory_id: str, version: int, *, request_id: str,
                         stage: str, idempotency_key: str, session_id: str | None = None,
                         explanation: dict | None = None) -> None:
        if stage not in RETRIEVAL_STAGES:
            raise ValueError('invalid retrieval stage')
        if not request_id or not idempotency_key:
            raise ValueError('request and idempotency key required')
        payload = canonical_json(explanation or {})
        with self._connection(write=True) as connection:
            prior = connection.execute('SELECT memory_id,version,request_id,stage,session_id,explanation_json '
                                       'FROM retrieval_events WHERE idempotency_key=?', (idempotency_key,)).fetchone()
            values = (memory_id, version, request_id, stage, session_id, payload)
            if prior:
                if tuple(prior) != values:
                    raise IdempotencyConflict('retrieval idempotency key was used for a different event')
                return
            if self._record(connection, memory_id, version) is None:
                raise KeyError((memory_id, version))
            connection.execute('INSERT INTO retrieval_events(idempotency_key,request_id,stage,memory_id,version,session_id,explanation_json,created_at) '
                               'VALUES (?,?,?,?,?,?,?,?)',
                               (idempotency_key, request_id, stage, memory_id, version, session_id, payload, now()))
            self._recompute_weight(connection, memory_id)

    def retrieval_events(self, request_id: str | None = None) -> list[dict]:
        with self._connection() as connection:
            if request_id is None:
                rows = connection.execute('SELECT * FROM retrieval_events ORDER BY id')
            else:
                rows = connection.execute('SELECT * FROM retrieval_events WHERE request_id=? ORDER BY id', (request_id,))
            return [dict(row) for row in rows]

    def weight(self, memory_id: str) -> float:
        with self._connection() as connection:
            row = connection.execute('SELECT weight FROM memories WHERE id=?', (memory_id,)).fetchone()
        if row is None:
            raise KeyError(memory_id)
        return row['weight']

    def rebuild_weights(self) -> int:
        """Recompute the operational weight cache for every memory."""
        with self._connection(write=True) as connection:
            ids = [row['id'] for row in connection.execute('SELECT id FROM memories ORDER BY id')]
            for memory_id in ids:
                self._recompute_weight(connection, memory_id)
            return len(ids)

    def _recompute_weight(self, connection: sqlite3.Connection, memory_id: str) -> None:
        record = self._record(connection, memory_id)
        if record is None:
            return
        weight = DEFAULT_WEIGHT + apply_feedback_weight(
            record, self._weight_events(connection, memory_id, record.version))
        connection.execute('UPDATE memories SET weight=? WHERE id=?', (weight, memory_id))

    @staticmethod
    def _weight_events(connection: sqlite3.Connection, memory_id: str,
                       version: int) -> list[WeightEvent]:
        """Attributable signals only; exposure stages are filtered out here.

        Applied / validated stages count once a confirmed application pins the
        action to an outcome. Negative feedback is recorded explicitly.
        """
        events: list[WeightEvent] = []
        confirmed = connection.execute(
            "SELECT 1 FROM memory_applications WHERE memory_id=? AND memory_version=? "
            "AND attribution='confirmed' LIMIT 1", (memory_id, version)).fetchone()
        if confirmed:
            events.extend(WeightEvent(row['stage']) for row in connection.execute(
                "SELECT stage FROM retrieval_events WHERE memory_id=? AND version=? "
                "AND stage IN ('applied','validated') ORDER BY id", (memory_id, version)))
        events.extend(WeightEvent(json.loads(row['payload_json'])['outcome']) for row in connection.execute(
            "SELECT payload_json FROM memory_events WHERE memory_id=? AND version=? AND kind='feedback' "
            "ORDER BY id", (memory_id, version)))
        return events

    def record_application(self, application: MemoryApplication) -> None:
        """Store one attributable application.

        `validation_source_ids` must be a subset of the version's retained
        sources; anything else is refused rather than silently under-attributed.
        """
        application = MemoryApplication.model_validate(application.model_dump())
        values = (application.application_id, application.task_id, application.memory_id,
                  application.memory_version, application.action, application.expected_outcome,
                  application.actual_outcome,
                  canonical_json(list(application.validation_source_ids)), application.attribution)
        with self._connection(write=True) as connection:
            prior = connection.execute('SELECT * FROM memory_applications WHERE application_id=?',
                                       (application.application_id,)).fetchone()
            if prior:
                stored = (prior['application_id'], prior['task_id'], prior['memory_id'],
                          prior['memory_version'], prior['action'], prior['expected_outcome'],
                          prior['actual_outcome'], prior['validation_source_ids_json'],
                          prior['attribution'])
                if stored != values:
                    raise IdempotencyConflict('application id was used for a different request')
                return
            if self._record(connection, application.memory_id, application.memory_version) is None:
                raise KeyError((application.memory_id, application.memory_version))
            sources = {row['source_id'] for row in connection.execute(
                'SELECT source_id FROM memory_version_sources WHERE memory_id=? AND version=?',
                (application.memory_id, application.memory_version))}
            if not set(application.validation_source_ids) <= sources:
                raise ValueError('validation sources must reference this version sources')
            connection.execute('INSERT INTO memory_applications VALUES (?,?,?,?,?,?,?,?,?,?)',
                               (*values, now()))
            self._recompute_weight(connection, application.memory_id)

    def record_host_application(self, *, memory_id: str, task_id: str, action: str,
                                expected_outcome: str, request_id: str,
                                version: int | None = None, actual_outcome: str = '',
                                validation_source_ids: Sequence[str] = ()) -> tuple[MemoryApplication, str]:
        """Record one host-reported application with attribution derived, never declared.

        Returns `(application, withheld_reason)`. `confirmed` needs three things a host
        cannot grant itself: a reported outcome, evidence identities that belong to this
        version, and an earlier `validated` report under the same request. Without the
        third one the claim is only self-assertion, so it lands as `plausible` and does
        not move weight.
        """
        if not task_id or not action or not expected_outcome or not request_id:
            raise ValueError('task_id, action, expected_outcome and request_id are required')
        record = self.get(memory_id, version)
        if record is None:
            raise KeyError((memory_id, version))
        with self._connection() as connection:
            validated = connection.execute(
                "SELECT 1 FROM retrieval_events WHERE request_id=? AND memory_id=? AND version=? "
                "AND stage='validated' LIMIT 1", (request_id, record.id, record.version)).fetchone()
        if not actual_outcome:
            withheld = 'no outcome reported yet'
        elif not tuple(validation_source_ids):
            withheld = 'no evidence identities named'
        elif not validated:
            withheld = f'no validated report under request {request_id}'
        else:
            withheld = ''
        attribution = 'confirmed' if not withheld else ('plausible' if actual_outcome else 'unknown')
        application = MemoryApplication(
            # The attribution is part of the identity so a later, better-evidenced report
            # strengthens the record instead of colliding with the weaker one. Weight only
            # asks whether a confirmed row exists, so the earlier row cannot double-count.
            application_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f'context-forge:application:'
                                                               f'{task_id}:{record.id}:{record.version}'
                                                               f':{attribution}')),
            task_id=task_id, memory_id=record.id, memory_version=record.version,
            action=action, expected_outcome=expected_outcome,
            actual_outcome=actual_outcome or None,
            validation_source_ids=tuple(validation_source_ids), attribution=attribution)
        self.record_application(application)
        return application, withheld

    def applications(self, memory_id: str | None = None, version: int | None = None) -> list[dict]:
        with self._connection() as connection:
            if memory_id is None:
                rows = connection.execute('SELECT * FROM memory_applications ORDER BY application_id')
            elif version is None:
                rows = connection.execute('SELECT * FROM memory_applications WHERE memory_id=? '
                                          'ORDER BY application_id', (memory_id,))
            else:
                rows = connection.execute('SELECT * FROM memory_applications WHERE memory_id=? AND memory_version=? '
                                          'ORDER BY application_id', (memory_id, version))
            return [dict(row) for row in rows]

    def aggregate_signals(self, month: str) -> dict:
        """Rebuild one month of `signal_counts` from the raw events.

        Overwrite, never delta: that is what makes the daily step safe to re-run after a
        crash and what eventually lets raw events be pruned. `month` is 'YYYY-MM', taken
        from the event timestamps.
        """
        counts: dict[tuple[str, int, str], int] = {}
        with self._connection() as connection:
            for row in connection.execute(
                    'SELECT memory_id,version,stage FROM retrieval_events '
                    "WHERE substr(created_at,1,7)=?", (month,)):
                key = (row['memory_id'], row['version'], f"stage:{row['stage']}")
                counts[key] = counts.get(key, 0) + 1
            for row in connection.execute(
                    "SELECT memory_id,version,payload_json FROM memory_events "
                    "WHERE kind='feedback' AND substr(created_at,1,7)=?", (month,)):
                outcome = json.loads(row['payload_json']).get('outcome', 'unknown')
                key = (row['memory_id'], row['version'], f'vote:{outcome}')
                counts[key] = counts.get(key, 0) + 1
        with self._connection(write=True) as connection:
            connection.execute('DELETE FROM signal_counts WHERE month=?', (month,))
            connection.executemany('INSERT INTO signal_counts VALUES (?,?,?,?,?)',
                                   [(month, memory_id, version, signal, events)
                                    for (memory_id, version, signal), events in sorted(counts.items())])
        return {'month': month, 'rows': len(counts)}

    def aggregated_through(self) -> str | None:
        """Latest month the aggregate covers — the only thing that makes pruning safe."""
        with self._connection() as connection:
            row = connection.execute('SELECT MAX(month) AS month FROM signal_counts').fetchone()
        return row['month'] if row and row['month'] else None

    def prune_retrieval_events(self, retention_days: int = 180) -> int:
        """Drop raw events only once an aggregate covers their month *and* retention expired.

        With no aggregate rows at all this deletes nothing: the summary must exist before
        the detail it stands in for goes away.
        """
        covered = self.aggregated_through()
        if covered is None:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()[:7]
        with self._connection(write=True) as connection:
            changed = connection.execute(
                'DELETE FROM retrieval_events WHERE substr(created_at,1,7) <= ? '
                'AND substr(created_at,1,7) < ?', (covered, cutoff))
            return changed.rowcount

    def signal_totals(self, months: int = 12) -> dict[str, int]:
        """Aggregate event counts per signal over the most recent months, newest included."""
        with self._connection() as connection:
            rows = connection.execute(
                'SELECT signal, SUM(events) AS events FROM signal_counts '
                'WHERE month IN (SELECT DISTINCT month FROM signal_counts '
                'ORDER BY month DESC LIMIT ?) GROUP BY signal ORDER BY signal', (months,)).fetchall()
        return {row['signal']: row['events'] for row in rows}

    BUCKET_AXES = (('host', 'host'),
                   ('model+prompt_version', "model || '@' || prompt_version"),
                   ('evidence_level', 'evidence_level'))

    def bucket_report(self, months: int = 6, min_sample: int = 20) -> list[dict]:
        """Opposition / support counts per reporting bucket, sample size included.

        A rate is withheld below `min_sample`: "1 of 2 opposed" is noise, and printing a
        percentage there invites acting on it. The denominator is every memory in the
        bucket, suppressed or not, so shrinking the sample cannot make the rate look better.
        """
        rows: list[dict] = []
        with self._connection() as connection:
            window = [row['month'] for row in connection.execute(
                'SELECT DISTINCT month FROM signal_counts ORDER BY month DESC LIMIT ?', (months,))]
            if not window:
                return rows
            placeholders = ','.join('?' * len(window))
            for axis, expression in self.BUCKET_AXES:
                grouped = connection.execute(
                    f'SELECT {expression} AS bucket, COUNT(*) AS memories, '
                    'SUM(CASE WHEN signals.against_events > 0 THEN 1 ELSE 0 END) AS against_memories, '
                    'COALESCE(SUM(signals.against_events), 0) AS against_events, '
                    'COALESCE(SUM(signals.support_events), 0) AS support_events '
                    'FROM memories LEFT JOIN ('
                    '  SELECT memory_id, '
                    "    SUM(CASE WHEN signal LIKE 'vote:%' THEN events ELSE 0 END) AS against_events, "
                    "    SUM(CASE WHEN signal IN ('stage:applied','stage:validated') THEN events ELSE 0 END) "
                    '    AS support_events '
                    f'  FROM signal_counts WHERE month IN ({placeholders}) GROUP BY memory_id'
                    ') AS signals ON signals.memory_id = memories.id '
                    'GROUP BY bucket ORDER BY bucket', (*window,)).fetchall()
                for row in grouped:
                    memories = row['memories']
                    enough = memories >= min_sample
                    rows.append({'axis': axis, 'bucket': row['bucket'], 'memories': memories,
                                 'against_memories': row['against_memories'] or 0,
                                 'against_events': row['against_events'],
                                 'support_events': row['support_events'],
                                 'against_rate': (round((row['against_memories'] or 0) / memories, 6)
                                                 if enough and memories else None),
                                 'note': '' if enough else f'样本不足（<{min_sample}），只给绝对数'})
        return rows

    def channel_mix(self) -> dict[str, int]:
        """Recall-row counts per channel, read from the structured retrieval-event payload.

        Counts only, never a rate: a handful of recalls is not a distribution — the same
        discipline `bucket_report` follows. Rows recorded before the channel field existed —
        or via the legacy rollback hatch, which writes an empty string — fall under
        `unknown` rather than being silently dropped.
        """
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT COALESCE(NULLIF(json_extract(explanation_json,'$.channel'),''),'unknown') "
                'AS channel, '
                'COUNT(*) AS n FROM retrieval_events GROUP BY channel ORDER BY channel').fetchall()
        return {row['channel']: row['n'] for row in rows}

    def record_feedback(self, memory_id: str, *, outcome: str, idempotency_key: str,
                        note: str = '', actor: Actor = 'human') -> MemoryRecord:
        """Record human / attributable negative feedback on a memory.

        `harmful` flips a non-protected active record to `conflicted` (pending
        review). Revocation never sets `contradicted`: it records a reason and a
        negative weight, nothing more.
        """
        if outcome not in FEEDBACK_OUTCOMES:
            raise ValueError('unknown feedback outcome')
        actor = _actor(actor)
        if not idempotency_key:
            raise ValueError('idempotency key required')
        with self._connection(write=True) as connection:
            record = self._record(connection, memory_id)
            if record is None:
                raise KeyError(memory_id)
            payload = {'outcome': outcome, 'note': note, 'version': record.version}
            request_hash = digest(canonical_json({**payload, 'actor': actor}))
            prior = connection.execute('SELECT kind,request_hash FROM memory_events WHERE idempotency_key=?',
                                       (idempotency_key,)).fetchone()
            if prior:
                if prior['kind'] != 'feedback' or prior['request_hash'] != request_hash:
                    raise IdempotencyConflict(idempotency_key)
                return record
            protected = record.draft.user_owned or record.draft.suppressed or record.draft.status == 'disabled'
            if outcome == 'harmful' and record.draft.status != 'conflicted' and not protected:
                conflicted = record.draft.model_copy(update={'status': 'conflicted'})
                record = self._put(connection, memory_id, conflicted, expected_version=record.version,
                                   idempotency_key=idempotency_key + ':status', actor='system',
                                   reason='harmful feedback: pending review')
                payload['version'] = record.version
                request_hash = digest(canonical_json({**payload, 'actor': actor}))
            connection.execute('INSERT INTO memory_events(idempotency_key,request_hash,memory_id,version,kind,actor,payload_json,created_at) '
                               "VALUES (?,?,?,?,'feedback',?,?,?)",
                               (idempotency_key, request_hash, memory_id, record.version, actor,
                                canonical_json(payload), now()))
            self._recompute_weight(connection, memory_id)
            return record

    def feedback(self, memory_id: str | None = None) -> list[dict]:
        with self._connection() as connection:
            sql = "SELECT * FROM memory_events WHERE kind='feedback'"
            params: tuple = ()
            if memory_id is not None:
                sql += ' AND memory_id=?'
                params = (memory_id,)
            result = []
            for row in connection.execute(sql + ' ORDER BY id', params):
                item = dict(row)
                item.update(json.loads(item.pop('payload_json')))
                result.append(item)
            return result

    @staticmethod
    def _weights(connection: sqlite3.Connection, ids: Sequence[str]) -> dict[str, float]:
        if not ids:
            return {}
        marks = ','.join('?' * len(ids))
        return {row['id']: row['weight'] for row in connection.execute(
            f'SELECT id,weight FROM memories WHERE id IN ({marks})', list(ids))}

    def put(self, memory_id: str, draft: MemoryDraft, *, expected_version: int,
            idempotency_key: str, actor: Actor = 'model', reason: str,
            relations: tuple[Relation, ...] = ()) -> MemoryRecord:
        embedding_model = ''
        embedding = None
        failure = None
        if self.embedder is not None:
            try:
                clean_text = sanitize_text(draft.summary + '\n' + draft.body)
                embedding = self.embedder.encode(clean_text)
                embedding_model = self.embedder.identity
            except Exception as error:
                # Embeddings are optional derived data; the authoritative write still proceeds.
                embedding = None
                failure = (str(error) or error.__class__.__name__)[:200]
        with self._connection(write=True) as connection:
            if failure is not None:
                self._record_embedding_failure(connection, self.embedder.identity, failure)
            return self._put(connection, memory_id, draft, expected_version=expected_version,
                             idempotency_key=idempotency_key, actor=actor, reason=reason,
                             relations=relations, embedding_model=embedding_model,
                             embedding=embedding)

    @staticmethod
    def _record_embedding_failure(connection: sqlite3.Connection, model: str, reason: str) -> None:
        """Count one embedding write failure per model and reason (design §5).

        A write must not fail because the provider did, so the failure is recorded rather
        than raised. The table is created lazily: a database written before this counter
        existed has no table, and there is nothing to count until the first failure. The
        reason is the exception text, truncated — an operator wants to know *why* the
        provider is failing, not just that it is.
        """
        connection.execute(
            'CREATE TABLE IF NOT EXISTS embedding_failures ('
            'model TEXT NOT NULL, reason TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0, '
            'last_at TEXT, PRIMARY KEY(model, reason))')
        connection.execute(
            'INSERT INTO embedding_failures(model,reason,count,last_at) VALUES (?,?,1,?) '
            'ON CONFLICT(model,reason) DO UPDATE SET count=embedding_failures.count+1, '
            'last_at=excluded.last_at', (model, reason, now()))

    def embedding_failures(self) -> dict[str, dict[str, dict]]:
        """Embedding write failures per model per reason — read by `doctor` (design §5).

        A missing table means no failure was ever recorded (old database, counter not yet
        created), which is a clean bill of health, not an error.
        """
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    'SELECT model,reason,count,last_at FROM embedding_failures '
                    'ORDER BY model,reason').fetchall()
        except MemoryStoreError:
            return {}
        out: dict[str, dict[str, dict]] = {}
        for row in rows:
            out.setdefault(row['model'], {})[row['reason']] = {
                'count': row['count'], 'last_at': row['last_at']}
        return out

    def _put(self, connection: sqlite3.Connection, memory_id: str, draft: MemoryDraft, *,
             expected_version: int, idempotency_key: str, actor: Actor, reason: str,
             relations: tuple[Relation, ...] = (), embedding_model: str = '',
             embedding: bytes | None = None) -> MemoryRecord:
        if not memory_id or not idempotency_key or not reason or expected_version < 0:
            raise ValueError('id, idempotency key, reason and nonnegative expected_version required')
        draft = MemoryDraft.model_validate(draft.model_dump())
        derived_risk = risk_of(draft)
        if draft.risk != derived_risk:
            draft = draft.model_copy(update={'risk': derived_risk})
        relations = tuple(Relation.model_validate(relation.model_dump()) for relation in relations)
        actor = _actor(actor)
        payload = {'draft': draft.model_dump(mode='json'), 'relations': [relation.model_dump(mode='json') for relation in relations],
                   'expected_version': expected_version, 'reason': reason}
        request_hash = digest(canonical_json({'memory_id': memory_id, 'actor': actor, **payload}))
        prior = connection.execute('SELECT * FROM memory_events WHERE idempotency_key=?', (idempotency_key,)).fetchone()
        if prior:
            if prior['kind'] != 'put' or prior['request_hash'] != request_hash:
                raise IdempotencyConflict('idempotency key was used for a different request')
            return self._record(connection, prior['memory_id'], prior['version'])
        previous = self._record(connection, memory_id)
        if (previous.version if previous else 0) != expected_version:
            raise VersionConflict(f'{memory_id}: expected version {expected_version}, found {previous.version if previous else 0}')
        if previous and previous.draft.kind != draft.kind:
            raise ValueError('memory kind cannot change')
        if previous and actor != 'human' and (previous.draft.suppressed or previous.draft.user_owned or previous.draft.status == 'disabled'):
            raise ProtectedMemory('human-owned or suppressed memory requires a human correction')
        if previous and actor != 'human' and access_broader(previous.draft, draft):
            raise ProtectedMemory('automatic writes may not widen access')
        version = expected_version + 1
        timestamp = now()
        for source in draft.sources:
            source_json = canonical_json(source.model_dump(mode='json'))
            stored = connection.execute('SELECT source_json FROM memory_sources WHERE identity=?', (source.identity,)).fetchone()
            if stored and stored['source_json'] != source_json:
                raise SourceConflict(f'source identity {source.identity!r} already has different content')
            connection.execute('INSERT OR IGNORE INTO memory_sources VALUES (?,?,?,?)',
                               (source.identity, digest(source.excerpt), source_json, timestamp))
        dimensions = (draft.provenance.host, draft.model or '', draft.prompt_version or '',
                      draft.evidence_level)
        if previous is None:
            connection.execute('INSERT INTO memories(id,kind,current_version,status,user_owned,suppressed,'
                               'host,model,prompt_version,evidence_level) VALUES (?,?,?,?,?,?,?,?,?,?)',
                               (memory_id, draft.kind, version, draft.status, draft.user_owned,
                                draft.suppressed, *dimensions))
        else:
            changed = connection.execute('UPDATE memories SET current_version=?,status=?,user_owned=?,suppressed=?, '
                                         'host=?,model=?,prompt_version=?,evidence_level=? '
                                         'WHERE id=? AND current_version=?',
                                         (version, draft.status, draft.user_owned, draft.suppressed,
                                          *dimensions, memory_id, expected_version))
            if changed.rowcount != 1:
                raise VersionConflict(memory_id)
        connection.execute(
            'INSERT INTO memory_versions '
            '(memory_id,version,snapshot_json,created_at,canonical_fingerprint,risk,embedding_model,embedding) '
            'VALUES (?,?,?,?,?,?,?,?)',
            (memory_id, version, canonical_json(draft.model_dump(mode='json')),
             timestamp, canonical_fingerprint(draft), draft.risk, embedding_model, embedding),
        )
        for source_id in sorted({source.identity for source in draft.sources}):
            connection.execute('INSERT INTO memory_version_sources VALUES (?,?,?)', (memory_id, version, source_id))
        _insert_projections(connection, memory_id, version, draft)
        event = connection.execute('INSERT INTO memory_events(idempotency_key,request_hash,memory_id,version,kind,actor,payload_json,created_at) '
                                   "VALUES (?,?,?,?,'put',?,?,?)",
                                   (idempotency_key, request_hash, memory_id, version, actor, canonical_json(payload), timestamp))
        self._add_relations(connection, memory_id, version, draft, relations, idempotency_key, event.lastrowid)
        self._index(connection, memory_id, draft)
        self._recompute_weight(connection, memory_id)
        return self._record(connection, memory_id, version)

    @staticmethod
    def _add_relations(connection: sqlite3.Connection, memory_id: str, version: int,
                       draft: MemoryDraft, relations: tuple[Relation, ...],
                       idempotency_key: str, event_id: int) -> None:
        source_ids = {source.identity for source in draft.sources}
        for ordinal, relation in enumerate(relations):
            evidence_ids = set(relation.evidence_ids)
            for condition in relation.applicability:
                evidence_ids.update(condition.evidence_ids)
            if not evidence_ids <= source_ids:
                raise ValueError('relation evidence must reference this version sources')
            relation_id = digest(canonical_json([idempotency_key, ordinal]))
            connection.execute('INSERT INTO memory_relations VALUES (?,?,?,?,?,?,?,?,NULL)',
                               (relation_id, memory_id, version, relation.target_id, relation.target_version, relation.kind,
                                canonical_json([condition.model_dump(mode='json') for condition in relation.applicability]), event_id))
            for source_id in sorted(evidence_ids):
                connection.execute('INSERT INTO memory_relation_sources VALUES (?,?)', (relation_id, source_id))

    @staticmethod
    def _index(connection: sqlite3.Connection, memory_id: str, draft: MemoryDraft) -> None:
        connection.execute('DELETE FROM memory_fts WHERE memory_id=?', (memory_id,))
        connection.execute('INSERT INTO memory_fts VALUES (?,?)',
                           (memory_id, ' '.join(tokenize(draft.summary + '\n' + draft.body))))

    def relations(self, memory_id: str, version: int | None = None, *, include_revoked: bool = False) -> list[dict]:
        with self._connection() as connection:
            record = self._record(connection, memory_id, version)
            if record is None:
                return []
            rows = connection.execute('SELECT * FROM memory_relations WHERE '
                                      '((memory_id=? AND version=?) OR (target_id=? AND target_version=?)) '
                                      'AND (? OR revoked_event IS NULL) ORDER BY id',
                                      (memory_id, record.version, memory_id, record.version, include_revoked))
            result = []
            for row in rows:
                relation = dict(row)
                relation['evidence_ids'] = [source['source_id'] for source in connection.execute(
                    'SELECT source_id FROM memory_relation_sources WHERE relation_id=? ORDER BY source_id', (row['id'],))]
                relation['applicability'] = json.loads(relation.pop('applicability_json'))
                result.append(relation)
            return result

    def revoke_relation(self, relation_id: str, *, expected_version: int,
                        idempotency_key: str, reason: str, actor: Actor = 'human') -> None:
        actor = _actor(actor)
        if not idempotency_key or not reason:
            raise ValueError('idempotency key and reason required')
        payload = {'relation_id': relation_id, 'expected_version': expected_version, 'reason': reason}
        request_hash = digest(canonical_json({**payload, 'actor': actor}))
        with self._connection(write=True) as connection:
            prior = connection.execute('SELECT kind,request_hash FROM memory_events WHERE idempotency_key=?', (idempotency_key,)).fetchone()
            if prior:
                if prior['kind'] != 'revoke_relation' or prior['request_hash'] != request_hash:
                    raise IdempotencyConflict(idempotency_key)
                return
            relation = connection.execute('SELECT * FROM memory_relations WHERE id=?', (relation_id,)).fetchone()
            if relation is None:
                raise KeyError(relation_id)
            current = self._record(connection, relation['memory_id'])
            if current.version != expected_version:
                raise VersionConflict(relation['memory_id'])
            if actor != 'human' and (
                current.draft.user_owned
                or current.draft.suppressed
                or current.draft.status == 'disabled'
            ):
                raise ProtectedMemory(relation['memory_id'])
            if relation['revoked_event'] is not None:
                raise VersionConflict('relation already revoked')
            event = connection.execute('INSERT INTO memory_events(idempotency_key,request_hash,memory_id,version,kind,actor,payload_json,created_at) '
                                       "VALUES (?,?,?,?,'revoke_relation',?,?,?)",
                                       (idempotency_key, request_hash, current.id, current.version, actor, canonical_json(payload), now()))
            connection.execute('UPDATE memory_relations SET revoked_event=? WHERE id=?', (event.lastrowid, relation_id))

    @staticmethod
    def _boundary_views(connection: sqlite3.Connection, ids: Sequence[str]) -> dict[str, BoundaryView]:
        if not ids:
            return {}
        marks = ','.join('?' * len(ids))
        rows = connection.execute(
            'SELECT a.memory_id,a.owner,a.privacy,a.visibility,a.allowed_projects_json,p.exclusions_json '
            'FROM memory_access_projection a '
            'JOIN memory_applicability_projection p ON p.memory_id=a.memory_id AND p.version=a.version '
            'JOIN memories m ON m.id=a.memory_id AND m.current_version=a.version '
            f'WHERE a.memory_id IN ({marks})', list(ids))
        views = {row['memory_id']: BoundaryView(
            row['owner'], row['privacy'], row['visibility'],
            tuple(json.loads(row['allowed_projects_json'])),
            tuple(json.loads(row['exclusions_json']))) for row in rows}
        missing = set(ids) - set(views)
        if missing:
            raise MemoryStoreError(
                f'boundary projection missing for {sorted(missing)}; run rebuild_projections()')
        return views

    @staticmethod
    def _applied_counts(connection: sqlite3.Connection, ids: Sequence[str]) -> dict[str, int]:
        if not ids:
            return {}
        marks = ','.join('?' * len(ids))
        return {f'{row["memory_id"]}@{row["version"]}': row['adoption']
                for row in connection.execute(
                    'SELECT memory_id,version,count(*) AS adoption FROM retrieval_events '
                    f"WHERE stage='applied' AND memory_id IN ({marks}) GROUP BY memory_id,version",
                    list(ids))}

    @staticmethod
    def _relation_links(connection: sqlite3.Connection, ids: Sequence[str]) -> dict[str, set[tuple[str, str]]]:
        if not ids:
            return {}
        marks = ','.join('?' * len(ids))
        links: dict[str, set[tuple[str, str]]] = {memory_id: set() for memory_id in ids}
        for row in connection.execute(
                'SELECT memory_id,target_id,kind FROM memory_relations WHERE revoked_event IS NULL AND '
                f'(memory_id IN ({marks}) OR target_id IN ({marks}))', [*ids, *ids]):
            if row['memory_id'] in links:
                links[row['memory_id']].add((row['kind'], row['target_id']))
            if row['target_id'] in links:
                links[row['target_id']].add((row['kind'], row['memory_id']))
        return links

    def expand_relations(self, seed_ids: Sequence[str], hops: int = 1) -> list[str]:
        """One hop along positive relations; explanation and conflict context only.

        Walks derived_from / supports / refines / overlaps in both directions.
        Conflict kinds are left to conflict presentation, and duplicate_of /
        supersedes are version bookkeeping rather than content affinity.
        Expanded ids never change seed ranking and never feed the feedback
        weight. Any `hops` other than 1 is refused: multi-hop spreading needs
        experimental evidence first.
        """
        if hops != 1:
            raise ValueError('associative expansion is one hop')
        seeds = tuple(dict.fromkeys(seed_ids))
        if not seeds:
            return []
        with self._connection() as connection:
            marks = ','.join('?' * len(seeds))
            kinds = ','.join('?' * len(EXPANSION_KINDS))
            rows = connection.execute(
                f'SELECT target_id AS id FROM memory_relations WHERE revoked_event IS NULL '
                f'AND kind IN ({kinds}) AND memory_id IN ({marks}) '
                f'UNION SELECT memory_id AS id FROM memory_relations WHERE revoked_event IS NULL '
                f'AND kind IN ({kinds}) AND target_id IN ({marks})',
                [*EXPANSION_KINDS, *seeds, *EXPANSION_KINDS, *seeds])
            found = sorted({row['id'] for row in rows} - set(seeds))
            if not found:
                return []
            alive = ','.join('?' * len(found))
            return sorted(row['id'] for row in connection.execute(
                f'SELECT id FROM memories WHERE id IN ({alive})', found))

    def search(self, query: str, *, project: str | None, changed_path: str | None = None,
               limit: int | None = 8, byte_budget: int = 65536,
               owner: str = DEFAULT_OWNER, technologies: frozenset[str] = frozenset(),
               domains: frozenset[str] = frozenset(), legacy_filter: bool = False) -> list[MemoryRecord]:
        return [record for record, _ in self.search_scored(
            query, project=project, changed_path=changed_path, limit=limit,
            byte_budget=byte_budget, owner=owner, technologies=technologies,
            domains=domains, legacy_filter=legacy_filter)]

    def search_candidates(self, query: str, *, project: str | None,
                          candidate_limit: int = 100, **kwargs) -> CandidatePool:
        if candidate_limit < 1 or candidate_limit > 100:
            raise ValueError('candidate_limit must be between 1 and 100')
        items = self.search_scored(query, project=project, limit=candidate_limit + 1,
                                   byte_budget=2**31 - 1, **kwargs)
        return CandidatePool(tuple(items[:candidate_limit]), len(items) > candidate_limit)

    def search_scored(self, query: str, *, project: str | None, changed_path: str | None = None,
                      limit: int | None = 8, byte_budget: int = 65536,
                      owner: str = DEFAULT_OWNER, technologies: frozenset[str] = frozenset(),
                      domains: frozenset[str] = frozenset(), legacy_filter: bool = False,
                      extra_risks: dict[str, Sequence[str]] | None = None
                      ) -> list[tuple[MemoryRecord, ScoreBreakdown]]:
        """Ranked recall with a five-dimension breakdown per record.

        `legacy_filter=True` is the rollback hatch: it reverts candidate-pool
        admission, five-dimension scoring and relevance top-K, leaving the
        BM25 order and the count/byte caps of the earlier binary filter. It
        does NOT revert the boundary trio, `project` no longer being a default
        filter, provenance retention, or the tri-state exclusion semantics \u2014
        those are correctness fixes, and undoing them would hide
        cross-project memories and silently drop records again.
        """
        if limit is not None and limit < 1 or byte_budget < 1:
            raise ValueError('positive limit and byte budget required')
        ctx = RetrievalContext(query=query, project=project, changed_path=changed_path,
                               technologies=frozenset(technologies), domains=frozenset(domains))
        tokens = list(dict.fromkeys(tokenize(query)))[:128]
        single_han = re.fullmatch(r'[\u3400-\u9fff\U00020000-\U0002fa1f]', query.strip()) is not None
        channel_notes: dict[str, str] = {}
        channel_labels: dict[str, str] = {}
        with self._connection() as connection:
            if single_han:
                rows = [(row['id'], 1.0) for row in connection.execute(
                    'SELECT m.id FROM memories m JOIN memory_versions v '
                    'ON v.memory_id=m.id AND v.version=m.current_version '
                    "WHERE m.status IN ('active','conflicted') AND m.suppressed=0 "
                    "AND instr(json_extract(v.snapshot_json,'$.body') || json_extract(v.snapshot_json,'$.summary'),?)>0 "
                    'ORDER BY m.id', (query.strip(),))]
            elif tokens:
                def search_with(terms: list[str], operator: str = ' AND ',
                                limit_rows: int | None = None) -> list[tuple[str, float]]:
                    sql = ('SELECT m.id,bm25(memory_fts) AS rank FROM memory_fts '
                           'JOIN memories m ON m.id=memory_fts.memory_id '
                           "WHERE memory_fts MATCH ? AND m.status IN ('active','conflicted') "
                           'AND m.suppressed=0 ORDER BY bm25(memory_fts),m.id')
                    if limit_rows:
                        sql += f' LIMIT {int(limit_rows)}'
                    return [(row['id'], float(row['rank'])) for row in connection.execute(
                        sql, (_fts_match(terms, operator),))]

                rows = search_with(tokens)
                wanted = min(limit or 100, 100)
                if len(rows) < wanted and len(tokens) > 1:
                    seen = {memory_id for memory_id, _ in rows}

                    def absorb(found: list[tuple[str, float]], reason: str) -> None:
                        added = [item for item in found if item[0] not in seen]
                        if not added:
                            return
                        rows.extend(added)
                        seen.update(memory_id for memory_id, _ in added)
                        for memory_id, _rank in added:
                            channel_notes[memory_id] = reason
                            channel_labels[memory_id] = 'widened'

                    # A term that appears in no document can never satisfy an AND, so giving it up
                    # costs nothing and buys everything. Measured: with a fixed drop budget, queries
                    # carrying four or more unseen terms recovered 0% until the budget exceeded
                    # their count.
                    impossible = {token for token in tokens
                                  if not _token_frequency(connection, token)}
                    if impossible:
                        keep = [token for token in tokens if token not in impossible]
                        if keep:
                            absorb(search_with(keep),
                                   f'去掉 {len(impossible)} 个匹配不上的词')
                    if len(rows) < wanted:
                        # Last resort: the union of every term, so nothing that matches has to be
                        # given up. Results are capped; the number of terms is not.
                        absorb(search_with(tokens, ' OR ', WIDEN_RESULT_CAP),
                               '词并集兜底：命中其中一个词即可')
                    rows.sort(key=lambda item: (item[1], item[0]))
            else:
                return []
            # Lexical text score: min-max over lexical ranks only, so folding the semantic
            # channel in below cannot distort it.
            lexical_scores = dict(zip(
                (memory_id for memory_id, _ in rows),
                text_scores([-rank for _, rank in rows])))
            semantic_scores: dict[str, float] = {}
            semantic_note = ''
            # Optional semantic candidates supplement lexical results. Invalid, drifted
            # (embedding_model mismatch — design §5) or unavailable vectors are ignored,
            # preserving lexical-only behavior.
            if self.embedder is not None and tokens:
                try:
                    query_blob = self.embedder.encode(query)
                    query_vector = unpack_vector(query_blob, dimensions=self.embedder.dimensions)
                    if not query_vector:
                        semantic_note = '语义通道未启用（向量维度不符）'
                    else:
                        import math
                        qnorm = math.sqrt(sum(value * value for value in query_vector)) or 1.0
                        for candidate in connection.execute(
                                "SELECT m.id,v.embedding,v.embedding_model FROM memories m "
                                "JOIN memory_versions v ON v.memory_id=m.id AND v.version=m.current_version "
                                "WHERE m.status IN ('active','conflicted') AND m.suppressed=0"):
                            # Identity filter: a vector from another model is not comparable.
                            if candidate['embedding_model'] != self.embedder.identity:
                                continue
                            vector = unpack_vector(candidate['embedding'], dimensions=self.embedder.dimensions)
                            if not vector:
                                continue
                            norm = math.sqrt(sum(value * value for value in vector)) or 1.0
                            semantic_scores[candidate['id']] = (
                                sum(a * b for a, b in zip(query_vector, vector)) / (qnorm * norm))
                        seen = {memory_id for memory_id, _ in rows}
                        for memory_id, score in sorted(
                                semantic_scores.items(), key=lambda item: (-item[1], item[0]))[:50]:
                            if memory_id not in seen:
                                rows.append((memory_id, -score))
                                channel_notes[memory_id] = f'语义相似 {score:.2f}'
                                channel_labels[memory_id] = 'semantic'
                except Exception as error:
                    # Query-time provider failure falls back to channels 1+2 (lexical only). The
                    # downgrade is recorded per result instead of swallowed, so a recalled set that
                    # silently lost its semantic channel is visible (design §5).
                    semantic_note = f'语义通道未启用（{type(error).__name__}）'
            effective_limit = min(limit or 100, 100) if single_han else limit
            ids = [memory_id for memory_id, _ in rows]
            views = self._boundary_views(connection, ids)
            applied = self._applied_counts(connection, ids)
            weights = self._weights(connection, ids)
            extras = extra_risks or {}
            candidates: list[tuple[MemoryRecord, float, int]] = []
            for memory_id, _rank in rows:
                # Text dimension is max(lexical, semantic); the semantic side is the raw
                # cosine (0..1), not a normalised value (design §7 defers calibration).
                text = max(lexical_scores.get(memory_id, 0.0), semantic_scores.get(memory_id, 0.0))
                if not boundary_visible(views[memory_id], owner=owner, ctx=ctx):
                    continue
                record = self._record(connection, memory_id)
                if record.draft.value == 'discard':
                    continue
                if not legacy_filter and not admitted(record, ctx):
                    continue
                candidates.append((record, text, applied.get(f'{memory_id}@{record.version}', 0)))
            if legacy_filter:
                return self._fit([(record, relevance(record, ctx, text=text, applied=count,
                                                     risks=extras.get(record.id, ())))
                                  for record, text, count in candidates], byte_budget)
            base = {record.id: (text, count) for record, text, count in candidates}
            ranked = sorted(
                ((record, relevance(record, ctx, text=text, applied=count,
                                    feedback=weights.get(record.id, DEFAULT_WEIGHT) - DEFAULT_WEIGHT,
                                    risks=extras.get(record.id, ())))
                 for record, text, count in candidates),
                key=lambda item: (-item[1].total, item[0].id))
            top = ranked if effective_limit is None else ranked[:effective_limit]
            links = self._relation_links(connection, [record.id for record, _ in top])
            order = [record.id for record, _ in top]
            kinds = {memory_id: {target: kind for kind, target in links[memory_id]
                                 if kind in CONFLICT_KINDS} for memory_id in order}
            notes: dict[str, list[str]] = {memory_id: [] for memory_id in order}
            conflicts: dict[str, int] = {memory_id: 0 for memory_id in order}
            for position, memory_id in enumerate(order):
                for other in order[:position]:
                    kind = kinds[memory_id].get(other) or kinds[other].get(memory_id)
                    if not kind:
                        continue
                    conflicts[memory_id] += 1
                    notes[memory_id].append(f'\u4e0e {other} \u5b58\u5728 {kind}, \u9700\u6309\u7248\u672c\u5206\u6d41')
                    notes[other].append(f'\u4e0e {memory_id} \u5b58\u5728 {kind}, \u9700\u6309\u7248\u672c\u5206\u6d41')
            rescored = []
            approved = _approved_pairs(connection, [(record.id, record.version)
                                                    for record, _ in top])
            for record, _score in top:
                if requires_confirmation(record.draft):
                    notes[record.id].append(
                        '高危指令：本版本已由人在终端批准'
                        if (record.id, record.version) in approved else
                        '高危指令：正文不随本次召回返回，需人在终端批准后换取')
                text, count = base[record.id]
                rescored.append((record, relevance(
                    record, ctx, text=text, applied=count, conflicts=conflicts[record.id],
                    feedback=weights.get(record.id, DEFAULT_WEIGHT) - DEFAULT_WEIGHT,
                    match_note=' '.join(part for part in (
                        channel_notes.get(record.id, ''), semantic_note) if part),
                    channel=channel_labels.get(record.id, 'bm25'),
                    lexical_score=lexical_scores.get(record.id),
                    semantic_score=semantic_scores.get(record.id),
                    risks=[*extras.get(record.id, ()), *notes[record.id]])))
            rescored.sort(key=lambda item: (-item[1].total, item[0].id))
            return self._fit(rescored if effective_limit is None else rescored[:effective_limit],
                             byte_budget)

    @staticmethod
    def _fit(ranked: list[tuple[MemoryRecord, ScoreBreakdown]],
             byte_budget: int) -> list[tuple[MemoryRecord, ScoreBreakdown]]:
        result = []
        used_bytes = 0
        for entry in ranked:
            size = len(entry[0].model_dump_json().encode('utf-8'))
            if used_bytes + size > byte_budget:
                continue
            result.append(entry)
            used_bytes += size
        return result

    def rebuild_projections(self) -> int:
        with self._connection(write=True) as connection:
            return self._rebuild_projections(connection)

    @staticmethod
    def _rebuild_projections(connection: sqlite3.Connection) -> int:
        connection.execute('DROP TABLE IF EXISTS memory_access_projection')
        connection.execute('DROP TABLE IF EXISTS memory_applicability_projection')
        _exec_script(connection, PROJECTIONS)
        rows = list(connection.execute('SELECT memory_id,version,snapshot_json FROM memory_versions '
                                       'ORDER BY memory_id,version'))
        for row in rows:
            draft = MemoryDraft.model_validate_json(row['snapshot_json'])
            _insert_projections(connection, row['memory_id'], row['version'], draft)
        return len(rows)

    def rebuild_fts(self) -> int:
        with self._connection(write=True) as connection:
            connection.execute('DROP TABLE IF EXISTS memory_fts')
            connection.execute('CREATE VIRTUAL TABLE memory_fts USING fts5(memory_id UNINDEXED, tokens)')
            rows = list(connection.execute('SELECT id FROM memories ORDER BY id'))
            for row in rows:
                self._index(connection, row['id'], self._record(connection, row['id']).draft)
            self._rebuild_projections(connection)
            return len(rows)

    def backup(self, target: Path) -> Path:
        target = Path(target).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb'):
            pass
        try:
            with self._connection() as source:
                destination = sqlite3.connect(target)
                try:
                    source.backup(destination)
                    if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise MemoryStoreError('backup integrity check failed')
                finally:
                    destination.close()
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return target

    @classmethod
    def restore(cls, backup: Path, target: Path) -> MemoryStore:
        cls._copy_database(Path(backup), Path(target))
        return cls.initialize(target)

    def export_markdown(self, target: Path) -> list[Path]:
        target = Path(target)
        if target.is_symlink():
            raise MemoryStoreError(f'export directory is a symlink: {target}')
        target = target.resolve()
        with self._connection() as connection:
            records = [self._record(connection, row['id']) for row in connection.execute('SELECT id FROM memories ORDER BY id')]
        target.mkdir(parents=True, exist_ok=True)
        exported = []
        for record in records:
            metadata = record.model_dump(mode='json')
            body = metadata['draft'].pop('body')
            content = '---\n' + json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2) + '\n---\n\n' + body.rstrip() + '\n'
            path = target / (digest(record.id) + '.md')
            if path.is_symlink():
                raise MemoryStoreError(f'export destination is a symlink: {path}')
            descriptor, temporary = tempfile.mkstemp(prefix='.memory-export-', dir=target)
            try:
                with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            finally:
                Path(temporary).unlink(missing_ok=True)
            exported.append(path)
        return exported

    def import_vault(self, root: Path, *, feedback_db: Path | None = None,
                     dry_run: bool = True, namespace: str | None = None,
                     snapshot_order: int = 0, owner: str = DEFAULT_OWNER) -> dict:
        from .memory_import import import_vault
        return import_vault(self, root, feedback_db=feedback_db, dry_run=dry_run,
                            namespace=namespace, snapshot_order=snapshot_order, owner=owner)
