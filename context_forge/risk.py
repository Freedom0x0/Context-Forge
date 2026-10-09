"""Deterministic high-risk classification for memory drafts.

Risk must be decided by a fixed, testable rule rather than by the model that
produced the draft: a self-reported `risk` field is a claim about the content,
not a check on it. The word list below is deliberately incomplete — it is a
first gate, and misses are expected to surface through the effectiveness report.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .memory_models import MemoryDraft

RiskLevel = Literal['normal', 'high']

# Shell/SQL/VCS forms that destroy or rewrite state. Matched anywhere in the text.
_COMMAND_PATTERNS = (
    re.compile(r'rm\s+-[a-z]*r[a-z]*f|rm\s+-[a-z]*f[a-z]*r', re.IGNORECASE),
    re.compile(r'\bdrop\s+(table|database|schema)\b', re.IGNORECASE),
    re.compile(r'\btruncate\s+table\b', re.IGNORECASE),
    re.compile(r'\bdelete\s+from\b', re.IGNORECASE),
    re.compile(r'push\s+(-f\b|--force)', re.IGNORECASE),
    re.compile(r'reset\s+--hard', re.IGNORECASE),
    re.compile(r'clean\s+-[a-z]*f', re.IGNORECASE),
    re.compile(r'\bterraform\s+destroy\b', re.IGNORECASE),
    re.compile(r'\bkubectl\s+delete\b', re.IGNORECASE),
    re.compile(r'\b(migrate|rollback|revert)\b.{0,40}\b(production|prod)\b', re.IGNORECASE),
)

# Instruction wording that only counts as high risk next to a destructive verb.
_DESTRUCTIVE_WORDS = ('删除', '清空', '重置', '回滚', '覆盖', '强推', '硬重置', '销毁')
_PRODUCTION_WORDS = ('生产环境', '线上环境', '生产库', 'prod', '线上')


def classify_risk(text: str) -> RiskLevel:
    """Return 'high' when the text instructs a destructive or production-bound action."""
    if any(pattern.search(text) for pattern in _COMMAND_PATTERNS):
        return 'high'
    if not any(word in text for word in _DESTRUCTIVE_WORDS):
        return 'normal'
    # A bare destructive verb is routine in instructions; require a production cue
    # or an unqualified bulk action to escalate.
    if any(word in text for word in _PRODUCTION_WORDS):
        return 'high'
    return 'high' if any(marker in text for marker in ('所有', '全部', '批量', '整个')) else 'normal'


def risk_of(draft: MemoryDraft) -> RiskLevel:
    """Single authoritative derivation used by every write path.

    Only rules are gated: they are the ones a host may act on. Knowledge is
    information, and marking it high-risk would delay the wrong thing.
    """
    if getattr(draft, 'kind', 'knowledge') != 'rule':
        return 'normal'
    return classify_risk(draft.body)


def requires_confirmation(draft: MemoryDraft) -> bool:
    """One definition of "a human has to read this text before it is usable".

    The deposit gate parks such a draft and the recall gate withholds its body. If the two
    predicates drifted apart, a rule could reach a host that nobody ever showed a human.
    """
    return getattr(draft, 'kind', 'knowledge') == 'rule' and risk_of(draft) == 'high'
