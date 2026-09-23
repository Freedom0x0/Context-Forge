"""P2.e1 -- G1/G2/G3 deterministic corpus plus the human scoring table.

`label` is the human judgement that decides the metrics:

* ``right`` -- the conclusion still holds when transferred into this context
* ``wrong`` -- transferring it into this context is an error

`why_wrong` only matters for ``wrong`` and states what the system *could*
have detected, which is what T2 (warning coverage) checks:

* ``constraint-conflict`` -- a declared constraint the target context fails
* ``tech-mismatch``       -- a declared technology disjoint from the target
* ``conflict``            -- contradicts another recalled record
* ``none``                -- nothing in the tags can detect it; the only
  acceptable outcome is a *silent* wrong transfer, which T1 caps

Records not listed here are unlabelled noise. They exist only to give S1
(the project hard filter) something to flood with; they never enter a rate
denominator.

Context tags come from `context_forge.tech_detect.detect` or the caller's own
stack probe. A constraint is satisfied when its tag is declared in
`technologies` / `domains`, which is how `RetrievalContext` (design 2.3) can
decide it at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Task:
    group: str
    project: str
    technologies: tuple[str, ...]
    domains: tuple[str, ...]
    query: str


@dataclass(frozen=True)
class MemoryCase:
    memory_id: str
    label: str
    body: str
    why_wrong: str = 'none'
    role: str = 'claim'
    origin: tuple[str, ...] = ()
    sessions: tuple[str, ...] = ()
    technologies: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    evidence_level: str = 'supported'
    conflicts_with: str | None = None


@dataclass(frozen=True)
class Group:
    task: Task
    memories: tuple[MemoryCase, ...] = field(default_factory=tuple)
    noise: int = 10


GROUPS: tuple[Group, ...] = (
    Group(
        task=Task('G1 同技术同问题', 'admin-console', ('antd', 'react'), ('search',),
                  'select server search'),
        memories=(
            MemoryCase('g1-local-filter-option', 'right',
                       'antd select server search: set filterOption=false before querying',
                       origin=('admin-console',), domains=('search',)),
            MemoryCase('g1-local-option-cache', 'right',
                       'antd select server search: reuse the previous page of options in memory',
                       origin=('admin-console',), domains=('search',)),
            MemoryCase('g1-transfer-debounce', 'right',
                       'antd select server search: debounce the request by 300ms',
                       origin=('platform-web-main',), sessions=('c0f909b9',),
                       technologies=('antd',), domains=('search',)),
            MemoryCase('g1-transfer-virtual-list', 'right',
                       'antd select server search: virtualise option lists over 200 rows',
                       origin=('platform-web-main',), sessions=('c0f909b9',),
                       technologies=('antd',), domains=('search',)),
            MemoryCase('g1-transfer-empty-text', 'right',
                       'antd select server search: keep an empty search text from clearing the filter',
                       origin=('platform-web-main',), sessions=('c0f909b9',),
                       technologies=('antd',), domains=('search',)),
            MemoryCase('g1-wrong-local-storage', 'wrong',
                       'antd select server search: persist every option in localStorage',
                       why_wrong='constraint-conflict',
                       role='procedure', origin=('platform-web-main',), sessions=('c0f909b9',),
                       technologies=('antd',), domains=('search',),
                       constraints=('local-storage',), evidence_level='unverified'),
        ),
    ),
    Group(
        task=Task('G2 不同技术同模式', 'api-gateway', ('fastapi', 'httpx'), ('proxy',),
                  'proxy upstream 504'),
        memories=(
            MemoryCase('g2-local-status-page', 'right',
                       'proxy upstream 504: surface the upstream status on the gateway error page',
                       origin=('api-gateway',), domains=('proxy',)),
            MemoryCase('g2-local-request-id', 'right',
                       'proxy upstream 504: forward the request id so the upstream can be traced',
                       origin=('api-gateway',), domains=('proxy',)),
            MemoryCase('g2-transfer-explicit-timeout', 'right',
                       'proxy upstream 504: always set an explicit client timeout and a fallback',
                       origin=('web-portal',), sessions=('d1a20',),
                       domains=('proxy',)),
            MemoryCase('g2-transfer-idempotent-retry', 'right',
                       'proxy upstream 504: retry only idempotent requests',
                       origin=('web-portal',), sessions=('d1a20',),
                       domains=('proxy',)),
            MemoryCase('g2-wrong-umi-devserver', 'wrong',
                       'proxy upstream 504: raise the umi devServer proxy timeout in .umirc.ts',
                       why_wrong='tech-mismatch',
                       role='procedure', origin=('web-portal',), sessions=('d1a20',),
                       technologies=('umi',), domains=('proxy',),
                       constraints=('umi-devserver',), evidence_level='unverified'),
            MemoryCase('g2-wrong-retry-budget', 'wrong',
                       'proxy upstream 504: raise the upstream budget to 120s and retry twice',
                       why_wrong='none',
                       origin=('web-portal',), sessions=('d1a20',),
                       domains=('proxy',), evidence_level='unverified'),
        ),
    ),
    Group(
        task=Task('G3 表面相似但约束冲突', 'billing-api', ('python', 'fastapi'),
                  ('api', 'passthrough'), 'api payload whitespace'),
        memories=(
            MemoryCase('g3-local-audit-hash', 'right',
                       'api payload whitespace: record the raw payload hash for audit',
                       origin=('billing-api',), domains=('api',)),
            MemoryCase('g3-local-schema-guard', 'right',
                       'api payload whitespace: validate against the schema before any trimming',
                       origin=('billing-api',), domains=('api',)),
            MemoryCase('g3-transfer-idempotency-key', 'right',
                       'api payload whitespace: keep the idempotency key outside the body',
                       origin=('ledger-core',), sessions=('b77e1',),
                       domains=('api',)),
            MemoryCase('g3-transfer-length-guard', 'right',
                       'api payload whitespace: reject bodies larger than the declared limit',
                       origin=('ledger-core',), sessions=('b77e1',),
                       domains=('api',)),
            MemoryCase('g3-transfer-passthrough', 'right',
                       'api payload whitespace: pass the payload through unchanged',
                       origin=('ledger-core',), sessions=('b77e1',),
                       domains=('api',), constraints=('passthrough',)),
            MemoryCase('g3-wrong-strip-fields', 'wrong',
                       'api payload whitespace: strip surrounding whitespace from every string field',
                       why_wrong='conflict',
                       origin=('ledger-core',), sessions=('b77e1',),
                       domains=('api',), constraints=('strip',),
                       evidence_level='unverified', conflicts_with='g3-transfer-passthrough'),
        ),
    ),
)

WARNINGS = {
    'constraint-conflict': '约束未满足',
    'tech-mismatch': '技术栈未命中',
    'conflict': 'conflicts_with',
}
