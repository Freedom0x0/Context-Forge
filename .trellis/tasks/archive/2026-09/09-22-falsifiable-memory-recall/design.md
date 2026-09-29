# 设计：可证伪记忆系统与智能召回

完整技术设计（概念模型、打分公式、风险、分期）见
[docs/technical-design-falsifiable-memory.md](../../docs/technical-design-falsifiable-memory.md)。

本文只写**落地边界**：改哪些文件、契约定成什么样、数据怎么流、怎么回滚。

> **⚠ 对母文档的两条修订（以技术文档 §2.3 为准）**
> 母文档 `technical-design-context-forge.md` §3「设计原则」有两条已被取代，**母文档尚未回写**，冲突时以技术文档为准：
>
> | 母文档 §3 原则 | 修订后 |
> |---|---|
> | 「Markdown 是事实源」 | Gen2 后**`memory.db` 是长期权威数据源**；Markdown 是可读导出与显式纠正入口；SQLite/FTS/统计投影是可重建派生索引 |
> | 「人类确认是状态转换」 | 改为「**自动沉淀和有依据的修正是主流程**，人工阅读/修改/抑制/删除是可选旁路」。仅高风险动作禁止自动执行：扩大访问范围、覆盖人工版本、永久删除、无 tool 证据升 `verified` |
>
> 实现时权威写入走 `MemoryStore.put`；Markdown 只作导出与人工纠正回路。

---

## 1. 模块边界

| 模块 | 职责 | 本期改动 |
|------|------|---------|
| `context_forge/memory_models.py` | 领域模型 | `MemorySpace` / `AccessPolicy` / `Provenance` / `ApplicabilityScope` + `role` 分型；**不加 `generality` 列**（推导） |
| `context_forge/memory_schema.py` | SQL schema | `SCHEMA_VERSION` +1；新模型先进 `snapshot_json`，另建可重建投影表 |
| `context_forge/memory.py` | `MemoryStore` | `_visible` → `_score`；`search` 排序与 top-K |
| `context_forge/memory_recall.py` | `MemoryRecall` | `explanation` **四段式**（含迁移理由）；冲突检测 |
| `context_forge/memory_lifecycle.py` | 生命周期 | 自动巩固、衰减、合并候选（P4） |
| `context_forge/memory_merge.py` | 合并 | 相似度合并提议（P4） |
| `context_forge/rules.py` | `RuleMatcher` | **排序 + limit + project 过滤**（P0） |
| `context_forge/index.py` | FTS 全文检索 | **加 project 过滤参数**（P0） |
| `context_forge/context.py` | 上下文组装 | `Matched rules` 加上限与排序（P0） |
| `context_forge/mcp_server.py` | MCP 工具 | 新增/扩展工具签名 |
| `context_forge/tech_detect.py` | 技术栈检测 | **新建**（P1） |
| `context_forge/memory_effectiveness.py` | 三张报表 | **新建**（P3：应用效果 / 异常审计 / 整理候选） |

**不改**：`watcher.py`、`worker.py`、`queue.py`、`autostart.py`、`transcript.py`、`vault.py` 的写入路径。沉淀链路与召回链路解耦。

---

## 2. 契约

### 2.1 P0 — 止血（不改数据结构）

```python
# rules.py
class RuleMatcher:
    def match(self, project: str, changed_path: str | None = None,
              limit: int = 8, legacy_filter: bool = False) -> list[RuleMatch]:
        """按 relevance 排序取 top-K。
        legacy_filter=True 只撤销「排序 + 上限」（无排序、无上限），作为回滚逃生舱；
        P0.1b 的两个可见性 bug 修复不在逃生舱内，回滚不会让规则重新消失。
        limit <= 0 同样取消上限。
        """
```

```python
# index.py
def search(self, query: str, limit: int = 20,
           project: str | None = None) -> list[sqlite3.Row]:
    """project=None 表示不过滤（保留旧语义）；给定 project 则严格过滤。
    limit <= 0 取消上限。返回 list[sqlite3.Row]（含 path / snippet）。
    """
```

```python
# context.py
def build_context(vault_root, project, changed_path=None,
                  knowledge_limit=5, review_limit=5,
                  rule_limit=8) -> list[ContextSection]:
    """Matched rules 段落受 rule_limit 约束。"""
```

**排序键（P0 仅用 vault frontmatter + `changed_path` 可得的信号）**：

```python
rule_sort_key = (
    paths_hit,               # 0 = paths 命中 changed_path; 1 = 无 paths 的项目级规则
                             # （paths 未命中 changed_path 的规则不进候选集）
    -confidence_rank,        # high > medium > low; 未声明视同 medium
    -status_is_enabled,
    mtime_desc,              # 新的优先
    rule_id,
)
```

`confidence` 是证据强度（这条规则多可信），**不是**相关度。P0 可得的唯一相关度信号是
`paths` 命中 `changed_path`，因此它排在首位；mtime 只作同分打破，不得冒充相关度。
未声明 `confidence` 的存量/手写规则视同 `medium`（与领域模型默认一致），不得低于显式 `low`。

P0 不做五维打分——那是 P2。P0 只求「不再灌 78 条」。

### 2.2 P1 — 模型（边界三分 + 三层抽象）

```python
# memory_models.py — 边界三分（设计文档 §3.3）
class MemorySpace(MemoryModel):
    """所属主体/记忆空间 — 不承担项目授权。"""
    owner: str = 'local'          # 由本地配置提供，不硬编码用户名
    privacy: Literal['shared', 'private'] = 'shared'   # 主体级隐私

class AccessPolicy(MemoryModel):
    """项目级显式授权 — 与来源、适用条件分离。"""
    visibility: Literal['owner', 'selected_projects', 'shared'] = 'shared'
    allowed_projects: tuple[str, ...] = ()

class Provenance(MemoryModel):
    """来源上下文 — 只作溯源与排序加分，不决定可见性。"""
    projects: tuple[str, ...] = ()
    sessions: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()

class ApplicabilityScope(MemoryModel):
    """适用条件 — 参与排序与采用判断。"""
    technologies: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()
    visibility: Literal['global', 'scoped'] = 'scoped'

class MemoryDraft(MemoryModel):
    kind: Literal['knowledge', 'rule', 'review']   # 产物形态
    role: Literal['observation', 'claim', 'hypothesis', 'preference', 'procedure']
    #   ^ 记忆类型由 role 决定: observation=情节, claim/hypothesis=语义,
    #     procedure=程序, preference=偏好。三层抽象由 role + scope 推导, 不落库。
    space: MemorySpace = MemorySpace()
    access: AccessPolicy = AccessPolicy()
    provenance: Provenance = Provenance()
    scope: ApplicabilityScope = ApplicabilityScope()
    proof_count: int = 0
    last_confirmed_at: str | None = None   # preference 的时效判据（§设计文档 5.2）
    # 其余字段不变（evidence_level / applicability / sources / relations）

def derive_level(draft: MemoryDraft) -> Literal['principle', 'pattern', 'instance']:
    """纯函数推导三层抽象，不落库。覆盖全部 role，无遗漏分支。"""
    if draft.role == 'observation':
        return 'instance'
    if draft.role == 'preference':
        return 'principle'            # 偏好永远是 L3，写了 technologies 也仍是偏好
    s = draft.scope                   # claim / hypothesis / procedure
    if s.technologies or s.domains or s.constraints:
        return 'pattern'
    return 'principle'
```

> **命名分工（易混，实现时务必分清）**：`MemorySpace.privacy` 是**主体级**隐私（`shared`/`private`）；
> `AccessPolicy.visibility` 是**项目级**授权（`owner`/`selected_projects`/`shared`）。
> 两者**不是同一个字段**，不存在 `access.visibility='private'`。

**`Provenance` 按 `role` 填充**（R5.2；对齐技术文档 §3.3）：

| `role` | `Provenance` | 参与匹配？ |
|--------|-------------|-----------|
| `observation`（情节） | **必须**填 `projects` / `sessions`，通常填 `paths` | ✅ 候选池**准入**判断 + 软加分 |
| `claim` / `hypothesis` / `procedure` / `preference` | **同样保留** `projects` / `sessions` / `sources` | ❌ 只作来源展示与软排序；**不参与准入、不参与淘汰** |

来源永不删除，只从默认过滤路径移出；适用范围由 `ApplicabilityScope` 表达，项目专属访问由 `AccessPolicy.allowed_projects` 表达。

**淘汰路径只有四条**（其余一律进打分）：

```
① space.owner 不匹配                                → 淘汰
② space.privacy='private' 且非本 owner              → 淘汰（主体级隐私）
③ access.visibility='selected_projects' 且当前项目不在 access.allowed_projects
                                                   → 淘汰（项目级显式授权）
④ scope.exclusions 命中                             → 淘汰
```

**存量映射（确定性，不可协商；对齐技术文档 §7.2）**：

| 存量 | 映射到 |
|------|--------|
| `Scope(kind='global')` | `AccessPolicy(visibility='shared')` + `ApplicabilityScope(visibility='global')` |
| `Scope(kind='project', project=X, paths=P)` | 一律 → `Provenance(projects=(X,), paths=P)`（**来源保留**）+ `AccessPolicy(visibility='shared')` |
| `Scope(kind='unknown')` | `ApplicabilityScope()` + 在 `applicability` 追加 `{condition: '未声明适用范围', result: 'unknown'}` |
| **全部存量 → `MemorySpace`** | `owner` ← **本地配置**（不写入硬编码用户名）；`privacy` ← `'shared'`（默认）。需要主体级私有的才显式设 `privacy='private'`，由迁移报告列出 |

**必须有测试守卫（三条）**：
1. `search()` 的默认过滤路径中**不得出现 `project` 字面量**（访问过滤只走 `AccessPolicy`）
2. 所有 `role` 的 `Provenance.projects` 都完整保留，不被迁移清空
3. `derive_level()` 对 `role` + `scope` 的推导结果稳定（纯函数）

Vault frontmatter 新增可选键：`role`、`technologies`、`domains`、`access_visibility`、`allowed_projects`、`privacy`、`exclusions`（均逗号分隔或单值）。缺省时脚本给出建议值并写入迁移报告。**不新增 `generality` 键**。

> **两个补充键的依据**（实现期补入，非随意扩展）：
> - `privacy`（`shared|private`）是 P1.8「含凭据/内部 URL 标 `space.privacy='private'`」的**唯一载体**；`memory_import._privacy()` 读它。缺了它 P1.8 的契约在 vault 里无处落地。
> - `exclusions` 承接 R5.4 的**反例**，落 `ApplicabilityScope.exclusions`（§3.3 明确 `exclusions: 反例`）；`memory_import` 读它。缺了它 `counterexamples` 写入 vault 后会在回读时丢失。

```python
# tech_detect.py  (新建)
def detect(root: Path) -> TechProfile: ...
# TechProfile: technologies: frozenset[str], domains: frozenset[str], raw_evidence: dict
```

### 2.3 P2 — 召回

```python
@dataclass(frozen=True)
class RetrievalContext:
    query: str
    project: str | None
    changed_path: str | None
    technologies: frozenset[str]
    domains: frozenset[str]

def relevance(record: MemoryRecord, ctx: RetrievalContext,
              activated: Sequence[MemoryRecord],
              weights: ScoreWeights = DEFAULT_WEIGHTS) -> ScoreBreakdown: ...

@dataclass(frozen=True)
class ScoreBreakdown:
    total: float
    match: float              # = applicability + provenance_bonus
    applicability: float
    provenance_bonus: float   # 只加分，永不淘汰
    evidence: float; freshness: float
    feedback: float; text: float; conflict_penalty: float
    admitted_by: Literal['principle', 'pattern', 'instance']  # = derive_level(record)
    explanation: str          # 四段式
```

```python
def match_score(memory, ctx) -> float:
    """§3.3 边界三分的落地。provenance 是加分项不是准入门槛。
    与技术文档 §4.2 逐行一致，不另立判定 helper。"""
    scope = memory.scope                         # ApplicabilityScope
    applicability = 0.0
    applicability += 1.0 if scope.visibility == 'global' else 0.0
    applicability += 0.8 if set(scope.technologies) & set(ctx.technologies) else 0.0
    applicability += 0.7 if set(scope.domains) & set(ctx.domains) else 0.0
    if not (scope.technologies or scope.domains or scope.constraints
            or scope.visibility == 'global'):
        applicability = 0.3                      # 未解析，基础分
    applicability -= 0.4 if violated(scope.constraints, ctx) else 0.0

    provenance_bonus = 0.0                       # 永不淘汰
    provenance_bonus += 0.20 if ctx.project in memory.provenance.projects else 0.0
    provenance_bonus += 0.15 if paths_hit(memory.provenance.paths, ctx.changed_path) else 0.0
    return applicability + provenance_bonus
```

**五维权重语义**（`ScoreWeights`，可调常量，默认 `1.0 / 1.2 / 0.6 / 0.8 / 1.0`，不引入 ML）：

| 维度 | 公式 / 取值 | 易错点 |
|------|------------|--------|
| `w1` 匹配 | `match_score()` = 适用 + 来源加分 | 全加分项，**无准入门槛** |
| `w2` 证据 | verified=1.0, supported=0.7, unverified=0.3, unknown=0.1, contradicted=−0.5 | — |
| `w3` 时效 | `e^(-λ·天数) × (1 + 0.3·复用次数)` | **`复用次数` = 可归因的 `applied` 次数**（**不是** `proof_count`，**不含** `presented`/`referenced`） |
| `w4` 采用反馈 | 见 §2.4 权重表 | 只聚合非零项，**且仅可归因时生效** |
| `w5` 文本 | BM25（FTS5）归一化 | 现有能力 |
| 冲突惩罚 | 与已激活记忆 `conflicts_with`/`contradicts` → 降权并附解释 | 不删除任何一方 |

**候选池准入**（先于打分，`derive_level()` 推导的三层决定；淘汰路径仅 §2.2 那四条）：

```
principle (role ∈ {claim,hypothesis,procedure,preference} 且 scope 全空，或 role=preference)
                                                      → 恒准入
pattern   (role ∈ {claim,hypothesis,procedure} 且 technologies/domains/constraints 非空)
            → scope.technologies ∩ ctx.technologies ≠ ∅  或  scope.domains ∩ ctx.domains ≠ ∅
instance  (role = observation)
            → provenance.projects ∩ {ctx.project} ≠ ∅   或  provenance.paths 命中 changed_path
```

**`Provenance.projects` 不参与默认过滤**（R5.2）：它只影响来源展示与排序加分。项目专属访问由 `AccessPolicy.allowed_projects` 决定——这是本次改造的第二条核心断言。

`instance` 的准入依赖 provenance **是有意的**：具体实例在别的项目里没意义。真正跨项目能用的内容应在写入时落到 `claim` / `procedure`（R5.4：区分事实、归纳和程序）。

**`explanation` 四段式**（必含 `unknown` 条件；第 2 段是跨项目联想的可信度来源）：

```
为什么命中: applicability=technology:antd(0.8)  text=BM25 0.42
来源与迁移: 来自 platform-web-main / session c0f909b9;
            迁移理由: 适用技术 [antd, umi] 与当前技术栈重合, 问题域 [proxy] 命中
适用边界:   true="antd@5.x + 服务端分页"; false="antd@6.x"; unknown="React 19"
风险提示:   未解析 1 项; 与 rule-xxx 存在 conflicts_with, 需按版本分流
```

**迁移理由的降级规则**（R5.8）：无任何适用维度命中、仅凭文本相关度进 top-K 时，必须显式输出：

```
来源与迁移: 来自 platform-web-main; 迁移理由: 仅文本相关, 未匹配任何适用维度 — 谨慎采用
```

**冲突检测**：对 top-K 内部两两查 `memory_relations` 中 `conflicts_with` / `contradicts` / `exception_to`，命中写入双方 `explanation` 的「风险提示」段。**不删除任何一方。**

`MemoryRecall.search()` 签名扩展：

```python
def search(self, query, *, project, changed_path=None, limit=8,
           byte_budget=65536, request_id=None, session_id=None,
           stage='retrieved', technologies=frozenset(), domains=frozenset(),
           legacy_filter=False) -> tuple[RecallResult, ...]
```

`legacy_filter=True` 恢复 `_visible` 二值过滤（回滚逃生舱）。

#### 联想召回契约（R5.9 / R5.10，**零新依赖**）

```python
# memory.py — 一跳关系展开
def expand_relations(self, seed_ids: Sequence[str], hops: int = 1) -> list[str]:
    """沿 memory_relations 正向关系展开 1 跳，结果只作解释与冲突上下文。
    只走这 4 种（与技术文档 §4.7.1 一致）：
        derived_from / supports / refines / overlaps
    不展开：conflicts_with / contradicts / exception_to / duplicate_of / supersedes
    （冲突类在 §2.5 冲突呈现里单独处理，展开等于放大噪声；
      supersedes 是版本取代关系，不表内容相关性）。
    展开结果不改变种子集排序，也不参与打分。
    `hops != 1` 直接 ValueError：多跳扩散需先有实验证据，不做静默降级。
    """
```

**不使用 `tool_returned` 共现自动建边。** 同一请求里一起返回，只说明检索器把它们放在了一起，不能证明它们相关；用它建边会让检索器强化自己的输出，形成回音室。

后续只有满足全部条件时，才生成**待复核的关联候选**（不进权威数据、不进排序）：

```
① 两条记忆在同一次任务中都被 applied
② 都关联到同一个 validated 结果
③ 在多个独立任务中重复出现
```

#### P2 实现期落地契约（与上文冲突时以本节为准）

**1. `_exclusion_hit` 的最终语义：三态 `condition_result`，与 `Applicability` 同一套词表**

`scope.exclusions` 承载**反例**。原实现拿它当查询子串匹配（`item in query`），设计自带的例子
「monorepo 根目录不适用」永远匹配不到任何东西，且「查询里出现这个词」与「当前上下文不适用」
是两件不同的事。改为与 `Applicability(condition, result, evidence_ids)` **同一套三态求值**：

```python
def condition_result(condition: str, ctx: RetrievalContext) -> 'true' | 'false' | 'unknown':
    """可判定形态只有两种：
       * 路径 glob（含 * ? / \\）→ 用 ctx.changed_path 判定（无 changed_path 即 unknown）
       * 裸名（无空白、无 glob/运算符字符）→ 与 ctx.bag() 求交
    其余一律 unknown：不可判定的条件进风险提示，绝不当静默硬过滤。"""
```

- `exclusions` 条目：`true` → **淘汰**（§2.2 第 ④ 条）；`false` → 放行；`unknown` → **不淘汰**，进「风险提示 / 适用边界的 unknown 槽」
- `constraints` 条目：`false` → `violated()` 成立 → 打分 −0.4 并进风险提示；`unknown` → 不惩罚，进风险提示
- `ctx.bag()` = `technologies ∪ domains ∪ project ∪ changed_path 的路径段`（全部 casefold）

一致性论证：`Applicability.result` 的三态语义是「该条件下这条记忆成立 / 不成立 / 未知」。
`exclusions` 是它的**结构化反例简写**（可机械判定、可进硬过滤），`applicability` 是**人读三态全文**
（含证据、可叙述）。写成语句的反例（如「monorepo 根目录不适用」）**不可机械判定**，
按 R1.3「让模型知道自己不知道什么」必须落进 `unknown`，而不是伪装成硬过滤。
因此**不需要扩展 `ApplicabilityScope.exclusions` 的形状**——形状没缺，缺的是求值语义。
写成语句的反例应写成 `Applicability(condition='monorepo 根目录不适用', result='false', evidence_ids=(...))`，
它已经在「适用边界 / 已知反例」里呈现。

**2. Vault 新键 `evidence_paths`（P1.0a 遗留 #6：`paths` 一词两义）**

P1.0a 把「声明的 glob 其实是证据出处」的 `paths` **直接删除**，54 条规则因此丢失了证据出处，
与「来源永不删除」冲突。改为**搬运不删除**：

| Vault 键 | 语义 | 迁移后 |
|---|---|---|
| `paths` | Gen1 `RuleMatcher` 的**适用**路径 glob（规则作用于哪些文件） | 保留不动；`memory_import` 读入 `Provenance.paths` |
| `evidence_paths`（**新增**） | **证据出处**文件 glob | `vault_retag` 在清空 `paths` 时把原值写入此键；`memory_import` 与 `paths` 合并进 `Provenance.paths` |

理由：Gen2 的 `ApplicabilityScope` **没有**路径字段（适用面由 technologies/domains/constraints 表达），
所以 Gen1 的 `paths` 进入 Gen2 后只可能是来源证据——`Provenance.paths` 的定义就是「证据所在文件 glob」，
并作为 `match_score` 的 +0.15 来源加分。删除它等于删来源。
`paths` 与 `evidence_paths` 在导入时**取并集**写入 `Provenance.paths`，两边都不丢。
列表读取统一走 `memory_import._strings` / `vault_retag._list`（不再新增第三套解析）。

**3. 投影表是硬过滤的读取路径（P1 遗留：只写不读）**

`memory_access_projection` / `memory_applicability_projection` 现在被 `search_scored` **真正查询**：
FTS 命中的 id 先用一条 join 取出 `BoundaryView`，淘汰发生在解析 `snapshot_json` 之前。
`_insert_projections` 的唯一转换函数是 `boundary_view(draft)`，投影行就是它的镜像
（`tests/test_memory_boundary.py::test_projections_are_rebuildable_from_snapshots` +
`tests/test_memory_recall_score.py::test_boundary_projection_mirrors_the_draft_and_is_required` 钉住）。
投影缺行**报错**（`MemoryStoreError: boundary projection missing; run rebuild_projections()`），
不做静默放行或静默丢弃。

**不补 `scope.constraints` / `scope.visibility` 两列**：打分与准入需要它们，但两者读的是权威
`snapshot_json` 里的 `draft.scope`（记录本来就要载入），没有任何**载入前**的查询需要它们。
补进去就是给 `snapshot_json` 再造一份无人读取的副本——正是 `database-guidelines.md`
「Never add a second authoritative column」要防的双真相。要补的触发条件是出现**打分前**的
SQL 侧约束/可见度过滤需求。

**4. `match_score` 落成两段（§4.2「两段构成」的直译）**

`applicability_score(record, ctx)` + `provenance_bonus(record, ctx)`；`match_score` = 两者之和。
未解析判定**内联**在 `applicability_score` 里，写成
`not (scope.technologies or scope.domains or scope.constraints or scope.visibility == 'global')`，
不抽 helper。`scope` 是 `memory.draft.scope` 的局部变量（`MemoryRecord` 的字段路径与伪码不同，语义一致）。

**5. `relevance` 的签名偏差（两处，均为关系表不在记录内的必然结果）**

- 去掉 `activated: Sequence[MemoryRecord]`：`Relation` 存在 `memory_relations`，不在 `MemoryRecord.draft` 里，
  `relevance` 看不到它。改为入参 `conflicts: int`，由 `search_scored` 用 `_relation_links` 算出后传入。
  `conflict_penalty(conflicts)` 仍是一等函数，`ScoreBreakdown.conflict_penalty` 仍是字段。
- `feedback: float = 0.0` 是 **w4 校准旋钮**（P3 的 `apply_feedback_weight` 接进来的地方）。
  P2 没有可归因应用数据，恒为 0.0；**曝光事件永不进 w4**（R2.2）。

**6. 冲突惩罚的可复现次序**

`search_scored` 两遍：先按 `(total desc, id asc)` 取 top-K，再对 top-K 内部两两查
`conflicts_with`/`contradicts`/`exception_to`。命中写入**双方**的风险提示（技术文档 §4.5），
惩罚只加在 base 序靠后的一方（「与已激活记忆冲突 → 降权」），最后按 `(total desc, id asc)` 重排。
成员集合不变（不删任何一方），排序与两次调用的一致性由固定 base 序保证。

**7. `legacy_filter=True` 撤什么、不撤什么**

撤：候选池准入、五维打分、按相关度 top-K（回退 BM25 序 + 原有条数/字节上限）。
**不撤**：四条边界淘汰、`project` 不作默认过滤、来源保留、三态 exclusion 语义
（后者是 P2 的正确性修复，撤了会重新出现「解析器吞字段 → 条目完全不可见」的静默全失）。


### 2.4 P3 — 闭环

```python
# memory_effectiveness.py  (新建) — 签名与技术文档 §4.4 一致
class MemoryEffectiveness:
    def application_report(self, rule_id: str
                           ) -> dict[str, int]:     # {referenced, applied, validated, revoked}
    def anomaly_candidates(self) -> list[MemoryRecord]              # verified 但 0 applied：未使用/待复核，**不判定为幻觉**
    def tidy_candidates(self, days: int = 90) -> list[MemoryRecord] # 整理候选：长期无 applied
```

权重更新落地为 `MemoryStore` 上的纯函数 `apply_feedback_weight(record, events) -> float`，**只算分数，不写库**。持久化的权重列在 P3 加：

```sql
-- weight 是可重建的操作性排序缓存，不是权威事实；权威内容仍在 snapshot_json。
-- proof_count 已随 snapshot_json 保存，不在此重复建列（避免双权威）。
ALTER TABLE memories ADD COLUMN weight REAL NOT NULL DEFAULT 1.0;
```

**硬原则：曝光 ≠ 强化**（调研 §建议 7）。权重表：

| 信号 | 事件 | 权重 |
|------|------|------|
| 采用 | `referenced` → `applied` | **+0.3**（记录；不单独证明因果） |
| 有效 | `applied` + 可归因 `validated` | **+0.5**；可升 `evidence_level`（仅 `unverified`→`supported`） |
| 曝光 | `presented` / `tool_returned` | **0**（只计数进报表，不进 `w4`） |
| 弱信号 | `presented` → `referenced` | **0**（被读进上下文 ≠ 被采纳） |
| 负反馈 | `applied` 后被撤销 / 纠正 | **−0.8**；记录原因，**不自动转 `contradicted`** |
| 负反馈 | `record_feedback(outcome='harmful')` | **−1.0**，转 `conflicted` 待复核 |
| 负反馈 | `outcome='irrelevant'` | **−0.2** |
| 衰减 | 长期无 `applied` 且无来源更新 | 累积负权 → 提升整理优先级；转 `stale` 需有依据且可逆 |

`feedback_score`（`w4`）只聚合上表非零项，且只在**可归因**时生效；`presented`/`referenced` 计数另存报表。

**应用结果与归因 `MemoryApplication`** —— **模型定义以技术文档 §4.4 为权威**（`design.md` 只引用，不另立字段）。这里只落**存储边界**：

```sql
-- 可归因应用记录。非曝光计数：曝光在 retrieval_events，归因在此表。
CREATE TABLE memory_applications (
  application_id TEXT PRIMARY KEY,          -- 幂等键
  task_id TEXT NOT NULL,
  memory_id TEXT NOT NULL,
  memory_version INTEGER NOT NULL,
  action TEXT NOT NULL,
  expected_outcome TEXT NOT NULL,
  actual_outcome TEXT,                      -- 未知则 NULL，不虚构
  validation_source_ids_json TEXT NOT NULL CHECK(json_valid(validation_source_ids_json)),
  attribution TEXT NOT NULL CHECK(attribution IN ('confirmed','plausible','unknown')),
  created_at TEXT NOT NULL
);
CREATE INDEX idx_memory_applications_memory ON memory_applications(memory_id, memory_version);
```

`validation_source_ids` **必须 ⊆ 该版本 `sources`**，否则拒绝写入（沿用现有 evidence 引用校验）。

**归因判定（技术文档 §4.4 的硬约束，实现时逐条落测试）**：

| attribution | 允许的动作 |
|-------------|-----------|
| `confirmed` | 计入成功统计、触发 `+0.3/+0.5` 反馈权、允许 `unverified`→`supported`、允许负反馈 |
| `plausible` | **只记录**；不进 `w4`，不改 `evidence_level` |
| `unknown` | **只记录**；不进 `w4`，不改状态 |

`attribution != 'confirmed'` 时**不得**提升 `evidence_level`、不得降权、不得标记 `contradicted`。

**证据的方向分派**（不静默覆盖，借 Hindsight 语义）：

| 新证据方向 | 动作 |
|-----------|------|
| 支持 | 追加 `Source`，`proof_count` +1；`evidence_level` **最多升到 `supported`**，无新 tool 摘录**不得**升 `verified` |
| 削弱不推翻 | 追加 `Source`，新增 `applicability.result='false'` 条件（缩小适用面） |
| 推翻 | 建 `contradicts` + 新记忆，**不删旧的** |
| 扩展适用面 | 建 `refines` / `exception_to` |

`proof_count` 进 `evidence_score`，但**不越过** `verified` 的硬门槛（需真实 tool 摘录）。

**硬约束（需测试守卫，四条）**：
1. 曝光事件处理路径中不得出现写 `evidence_level` 到 `verified` 的分支
2. `presented` / `referenced` 不得产生正权重
3. `attribution != 'confirmed'` 不得改权重、不得升级证据、不得产生负反馈
4. 撤销 / 纠正**不得**自动把 `evidence_level` 置为 `contradicted`（只记原因与负权）

### 2.5 P4 — 卫生

```python
# memory_lifecycle.py 扩展
def consolidation_candidates(self, min_sources: int = 3) -> list[ConsolidationProposal]
def decay_candidates(self, days: int = 90) -> list[MemoryRecord]
def merge_candidates(self, threshold: float = 0.9) -> list[MergeProposal]
```

巩固**自动执行**：新建高层记忆 + `derived_from` 关系指向全部来源实例，**不原地改写、不删除来源**，保留全部 provenance，并且可撤销。低风险的降权、收窄范围、归档精确重复可自动执行；扩大访问范围、覆盖 `user_owned` 版本、永久删除仍禁止自动执行。

**衰减与偏好时效**：

| 类型 | 时效判据 | 动作 |
|------|---------|------|
| 通用 | 长期无 `applied` 且无来源更新 | 提升整理优先级；转 `stale` 需有依据且可逆 |
| `role='preference'` | `last_confirmed_at`（不看引用次数） | 偏好会变化 → 用 `supersedes` 表达新旧，**不因长期未引用判定为错误** |

`last_confirmed_at` 落在 `MemoryDraft`（随 `snapshot_json` 存，不另建权威列）。**只在人工 `revise`（`actor='human'`）时刷新** —— 表示「人最后一次确认」。

**不从 `record_application(attribution='confirmed')` 自动刷新**（2026-09-23 决议）：采纳即写新版本会制造版本抖动，且与 R2.2「曝光与应用事件是观察性的、不改写内容」冲突。代价是 `last_confirmed_at` 目前**无消费者**（`preference` 不进衰减、`mark_stale` 拒 `preference`），偏离是潜伏的。若产品需要「采纳也算确认」，须先明确「confirmed 应用产生新版本」这一契约。

### 2.6 P2 上线闸门 — 效果评估实验

打分权重是拍脑袋的初值，**必须用实验校准才准上线**（设计文档 §4.6）。三组跨项目任务：

| 组 | 构造 | 检验 |
|----|------|------|
| **G1 同技术同问题** | 两项目都用 antd + 都遇 Select 服务端搜索 | 跨项目迁移应生效 |
| **G2 不同技术同模式** | umi + FastAPI，都要处理代理 504 | `pattern`/`principle` 能否抽象命中 |
| **G3 表面相似但约束冲突** | 同类 API 设计问题，一个要求 `strip` 一个要求 `passthrough` | 冲突检测 + 「为什么可能不适用」 |

对照：`S1` project 硬过滤（现状基线） / `S2` 打分召回（P2） / `S3` + 迁移理由完整版（P3 后补测）。

指标（**不看召回条数**）：决策帮助率、**错误迁移率**、首次成功率、额外 token/延迟。

**通过判据**：S2 的错误迁移率不得超过实验前写明的阈值。若超阈值 → 回 P1 补 `ApplicabilityScope` 标签，**不许调权重硬压**。

产物形态：`tests/test_recall_eval.py` 的确定性构造数据 + 一份手工评分表（决策是否正确需人判）。

---

## 3. 数据流

### 召回（P2 生效后）

```
MCP context_forge_memory_search(query, project, changed_path)
  → TechProfile.detect(cwd)                      # 或调用方显式传入
  → RetrievalContext(query, project, path, techs, domains)
  → MemoryStore.search: tokenize → FTS5 → 候选集
  → 硬过滤: space.owner / space.privacy / access.visibility / exclusions / status
                                                    # 唯一会「删除」的路径 (§2.2)
  → derive_level() 候选池准入                      # 准入 ≠ 淘汰, 见 §2.3
  → match_score(适用 + 来源加分) + 其余四维打分
  → top-K 两两冲突检测
  → byte_budget / limit 截断
  → RecallResult(memory, relations, explanation)  # 四段式, 含迁移理由
  → record_retrieval(stage='tool_returned', request_id=...)
```

### 反馈（P3 生效后）

```
调用方继续上报 stage: presented → referenced → applied → validated
  以及 负反馈: record_feedback('harmful'/'irrelevant') / 人工撤销
  → record_retrieval(...)                        # 现有
  → MemoryApplication 记录预期/实际/归因        # 新增
  → MemoryEffectiveness 汇总                     # 新增
  → apply_feedback_weight 算分                   # 新增, 只读; 仅可归因项生效
  → 三张报表导出
```

权重**不自动回写** `evidence_level` 到 `verified`（R2.6）；证据的方向分派见 §2.4。

---

## 4. 权衡

| 决策 | 取 | 舍 | 理由 |
|------|----|----|------|
| 打分权重用常量 | 可解释、可调、零依赖 | 不如 ML 自适应 | 数据量不足以训模型；权重是校准旋钮，用 `forge doctor` 暴露诊断入口 |
| FTS5 不上向量 | 零依赖、可重建 | 语义召回弱 | 冷启动阶段；打分层补质量，数据够了再评估 |
| 巩固新建而非改写 | 历史可追溯 | 多占存储 | 自动执行 + 可撤销 + 来源完整保留，比省存储重要 |
| `generality` 推导不落库 | 少一列、少一处标错、少一份不一致状态 | 推导函数要维护 | `role` + `scope` 已唯一确定层级，存了就冗余 |
| `Provenance` 只加分不淘汰 | 跨项目联想是产品价值所在 | 误迁移风险升 | 由迁移理由 + G3 实验闸门兜住 |
| `legacy_filter` 逃生舱 | 可回滚 | 多一条分支 | P0–P2 都是行为变更，必须可退 |

---

## 5. 兼容性

- **schema**：`SCHEMA_VERSION` +1。新模型先进入 `snapshot_json`，另建可重建投影表 → 老库可读。迁移只在 `initialize` / 显式 `migrate` 中发生（普通打开不自动迁移）；迁移前在线备份、支持 dry-run、可回滚。
- **Vault frontmatter**：新键 `role`、`technologies`、`domains`、`access_visibility`、`allowed_projects`、`privacy`、`exclusions` 均可选 → 老文件可解析。存量 `project` 键保留作来源，只是不再参与默认过滤。
- **MCP 工具**：`context_forge_memory_search` 新参数全部有默认值 → 老调用不破坏。
- **CLI**：新增 `--legacy-filter`、`--rule-limit` 等 flag，默认走新行为。

---

## 6. 发布与回滚

按 PRD 的 P0→P4 分期，每期独立可回滚：

| 期 | 上线开关 | 回滚动作 |
|----|---------|---------|
| P0 | 无（行为变更，但有 flag） | `--legacy-filter` / `--rule-limit 0` |
| P1 | schema 默认值保证老库可读 | 不迁移新库即可 |
| P2 | `legacy_filter=True` | 传 flag 回退二值过滤 |
| P3 | 权重写入独立列，不改 `evidence_level` | `UPDATE memories SET weight = 1.0`（中性值是 1.0，不是 0） |
| P4 | 自动执行且带依据；高风险动作仍需人工 | 撤销对应版本即回滚 |

**每个 PR 合并前必须**：`pytest` 全绿 + 对应 AC 勾选 + 回滚路径验证过一次。

---

## 7. 与其他任务的关系

`.trellis/tasks/09-21-forge-recall-rank-and-merge` 的「召回排序」部分被本任务 P0/P2 覆盖。处理方式二选一，在开工前定：

- **A**：把 09-21 的已完成部分并入本任务，归档 09-21
- **B**：09-21 收窄为 P0 的实现载体，本任务 P1 起另起

默认取 **A**（避免两处维护同一段召回代码）。需用户确认。
