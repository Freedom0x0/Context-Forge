# 可证伪记忆系统 技术设计

状态：草案，待评审

日期：2026-09-22

前置文档：[technical-design-context-forge.md](technical-design-context-forge.md)

---

## 0. 摘要

Context Forge 的记忆系统从「记住更多」转向「让有来源、适用边界和不确定性说明的经验参与 AI 决策，并根据可归因结果持续修正」。
本设计确立四个必须保持的竞争优势，并给出对应的召回机制。

**定位一句话**：Context Forge 自动从经历中形成、应用并修正可追溯记忆；人工阅读、修改、抑制和删除是可选旁路，不是沉淀前置条件。

| 优势 | 一句话 | 对应章节 |
|------|--------|---------|
| 可证伪记忆 | 规则是假设，带证据、边界、反例 | §3 |
| 结果闭环 | 应用效果可归因，异常可审计 | §4.4 |
| 双向复利 | AI 变强，人也变强 | §6 |
| 记忆卫生 | 巩固、衰减、合并、冲突、保护是一等公民 | §5 |

**召回机制的三个核心转变**：

```
① 记忆分型（§3.1）
   从：knowledge / rules 两种产物
   到：情节 / 语义 / 程序 / 偏好 四种记忆
       —— 规则可跨项目使用，但始终保留来源 project；项目访问限制由 AccessPolicy 表达

② 边界三分（§3.3）
   从：Scope 一个概念同时当安全边界和适用条件
   到：安全边界(硬过滤) / 来源上下文(软加分) / 适用条件(软排序) 三者分离
       —— project 默认只作 provenance；显式访问策略仍可硬过滤

③ filter → score + 联想（§4）
   从：记忆 ∈ 项目        (container model, project 精确匹配)
   到：记忆 × 适用维度     (applicability model, 打分)
       + 一跳关系展开（联想召回）
```

---

## 1. 背景与问题

### 1.1 现状

系统有两代存储：

| 代 | 载体 | 检索 | 作用域 |
|----|------|------|--------|
| Gen1 Vault | `knowledge/accepted/*.md`、`rules/proposals/*.md` | `index.py` FTS + `RuleMatcher` | frontmatter `project` 字符串 |
| Gen2 MemoryStore | `memory.db`（SQLite + FTS5） | `MemoryStore.search()` | `Scope(kind: global\|project\|unknown)` |

注入全部是**拉取式**（MCP tool），不自动塞入上下文。唯一的自动注入是 SessionStart 状态行。

### 1.2 根因：脏记忆不是数据问题，是模型问题

2026-09-22 对 vault 的实测：

- 103 条 knowledge、103 条 rule
- `project` 分布：`platform-web-main` 78、`my-agent` 16、`code-mentor` 8、`cc-session-manager` 1
- **0 条属于 `context-forge` 自身**

表面看是「沉淀了别的项目的脏数据」。实际根因是**抽象层级放错了**：

| 条目 | 实际层级 | 被标成 |
|------|---------|--------|
| `antd Select 服务端搜索要设 filterOption=false` | 跨项目通用（L2 模式） | `project: platform-web-main` |
| `删代码别加注释解释它被删了` | 通用原则（L3） | `project: platform-web-main` |
| `platform-web .umirc.ts:55 配了 CAS 代理` | 项目实例（L1） | `project: platform-web-main` |

三条被压在同一个抽屉里。正确做法是**写入时就区分事实、归纳和程序**：归纳写成语义/程序并填适用条件，项目细节保留为情节实例。

### 1.3 当前检索的三个硬缺陷

1. **`RuleMatcher.match()` 无排序、无上限** — `match_rules("platform-web-main")` 返回 78 条规则全文，直接灌满上下文。
2. **`context_forge_search(query)` 不过滤 project** — 全文搜索命中所有项目，结果被稀释。
3. **`_visible()` 是二值黑名单** — 只有「可见/不可见」，没有相关度梯度，也没有反例、冲突的呈现。

### 1.4 竞品现状与本设计要补齐的能力

Mem0、Zep、Hindsight、Basic Memory、claude-mem、claude-memory-compiler、Devin Memories、Claude Code `CLAUDE.md` 各有侧重，公开能力也不一致；本设计不替竞品下绝对结论，而是明确 Context Forge 自己要补齐的五项契约：

1. **证据分级** — 事实主张必须携带可核对来源，模型自评不能升级证据等级
2. **矛盾显式** — 冲突、反例和例外以关系表达，而不是让两边同时进入上下文
3. **应用可归因** — 记忆被采用后，记录预期、实际结果和归因强度
4. **整理可逆** — 巩固、衰减、合并和归档都可追溯、可撤销
5. **数据可携带** — Markdown 可读、可编辑、可导出，资产不锁在供应商侧

本设计针对这五条，而非比拼向量检索、云端共享或 IDE 集成。

### 1.5 边界模型的三层混淆（必须纠正）

调研 [cross-project-memory-competitor-analysis.md](research/cross-project-memory-competitor-analysis.md) 指出市场上形成两类模式：

1. **编码助手**按 Git 仓库 / workspace 隔离自动记忆（Claude Code、Cursor）——简单、安全，跨项目迁移弱。
2. **通用 Agent 记忆系统**按 user / agent / memory bank 做最高层硬隔离，**项目只是 metadata、namespace 或图中的上下文，不构成召回边界**（Mem0、Hindsight、Graphiti、LangGraph Store）。

结论：要拆的不是「取消所有边界」，而是把当前单一的 `project scope` 拆成**三种不同职责的东西**：

| 层 | 职责 | 参与方式 |
|----|------|---------|
| **安全边界** | user / agent / memory_space / private bank | **硬过滤**，越界即不可见 |
| **来源上下文** | project / session / path | 只作溯源与**排序加分**，**不决定可见性** |
| **适用条件** | 技术栈 / 任务类型 / 约束 / 环境 / 反例 | 参与排序与采用判断 |

**本设计此前把 `projects` 放在 `Scope` 里当匹配维度，本质仍是过滤器**（只是从字符串精确匹配换成 glob）。正确行为是：**同项目加分，跨项目不删除**。这是 §3.3 的改造目标，也是「脏记忆不该删而该改标签」的结构性根据——跨项目联想正是个人长期学习的全部价值所在。

### 1.6 与调研文档的分歧点

| 调研建议 | 本设计取舍 | 理由 |
|---------|-----------|------|
| 方向 B：主体硬隔离 + 项目软排序 | **采纳** | 对应 §3.3 |
| 先用 SQLite，不引入向量库 / 图数据库 | **采纳** | 对应 §2.2 非目标 |
| 新增 `memory_space` / `owner` | **采纳** | 对应 §3.4 |
| 方向 C 时序知识图谱 | **本期不做，保留演进路径** | 个人规模未证明必要；见 §2.2 |

### 1.7 来源 project 不等于适用 project（产品定位的直接推论）

产品定位是**个人的本地记忆，电脑里的另一个你**（未来云端同步）。记忆空间 = 一个人，不是项目。

据此，`knowledge` / `rules` 的二分是**产物形态**，不是**记忆类型**。真正的分界是认知类型：

| 记忆类型 | 认知科学 | 例子 | 来源 project |
|---------|---------|------|-------------|
| **情节** episodic | 某时某地发生了什么 | 「platform-web 的 `.umirc.ts:55` 配了 CAS 代理」 | ✅ 保留，且参与准入 |
| **语义** semantic | 一般而言什么为真 | 「CAS `serviceValidate` 在浏览器端会撞 CORS」 | ✅ 保留，只作来源与弱排序 |
| **程序** procedural | 怎么做一件事 | 「配 CAS 时先在 dev proxy 转发 `/cas/*`」 | ✅ 保留，只作来源与弱排序 |
| **偏好** preference | 你喜欢怎样 | 「删代码不要加注释解释」 | ✅ 保留，只作来源与弱排序 |

**规则来源 project 不等于适用 project。** 现有 103 条 rule 全部带 `project` 标签（`platform-web-main` 78 / `my-agent` 16 / `code-mentor` 8 / `cc-session-manager` 1），问题是把来源直接当成默认召回边界，而不是 project 这个字段本身存在。

对应到现有 schema：`MemoryDraft.role` 已经是 `observation | claim | hypothesis | preference | procedure`，**四类记忆类型已齐备**。缺的是用 `role` 决定召回与整理策略，把 `project` 从默认过滤条件降为来源线索。

---

## 2. 设计目标与非目标

### 2.1 四个必须保持的竞争优势

#### 优势 A：可证伪记忆（Falsifiable Memory）

**别人**：`{"memory": "..."}` — 一条断言，信条式。
**我们**：`{断言, 证据, 成立条件, 失效条件, 反例, 冲突}` — 一条假设，科学式。

「verified」是**可验证的事实主张**，不是模型自评的置信度分数：强制要求保留真实 tool observation 原文摘录。

#### 优势 B：结果闭环 → 应用效果可归因

曝光链六阶段被记录后，结合可归因的应用结果，产出三张只读报表：

- **应用效果**：记录引用 / 采纳 / 验证 / 撤销次数，仅作观测，不单独构成因果结论（对应 `application_report`）
- **异常审计候选**：`verified` 但长期 0 次 `applied` → 先标记「未使用/待复核」，不判定为幻觉
- **整理候选**：长期停在 `presented` 从不到 `applied` → 提升整理优先级，转 `stale` 需有依据且可逆

#### 优势 C：双向复利（Double Compounding）

```
AI 侧：更准的规则 → 更少返工 → 更快交付
人 侧：结构化复盘 → 判断力沉淀 → 换工具也带走
```

Obsidian Markdown 不是存储格式，是产品另一半价值。这是价值观差异，竞品的商业模式不允许他们这么说。

#### 优势 D：记忆卫生是一等公民（Memory Hygiene）

巩固、衰减、合并、冲突、保护。只增不减的追加日志会逐渐退化成垃圾场。

### 2.2 非目标

明确不做，避免范围蔓延：

- **向量检索 / embedding** — 冷启动先用 FTS5 + 打分排序，验证模型有效后再考虑
- **云端、团队共享** — 本地优先是前提
- **原生 IDE 集成** — MCP 是唯一注入通道
- **多 agent 记忆编排** — 不在赛道
- **自动执行高风险动作** — 记忆自动沉淀，但不得自动执行危险操作

**保留演进路径**（不是永久否决，是本期不做）：

| 能力 | 何时值得做 | 触发信号 |
|------|-----------|---------|
| **`sqlite-vec` 向量扩展** | 语义召回成为主要瓶颈时 | FTS5 的决策帮助率明显低于语义基线 |
| **时序知识图谱 / 实体抽取**（调研方向 C） | 跨项目实体联想需求被实证后 | 多项目实体/技术/错误模式需沿图扩散召回 |
| **自我模型**（失败模式 / 判断习惯 / 思维盲点） | 产品差异化需要「另一个你」而不仅是「另一个 wiki」时 | 负反馈数据积累到可归纳 |
| team / 组织级 memory_space | 有真实团队使用者时 | 未来云端同步的第一阶段 |

**存储选型的分期判断**（§4.7 详述）：

| 档 | 手段 | 新依赖 | 解决 |
|----|------|--------|------|
| **本期** | FTS5 + `memory_relations` 一跳关系展开 | **0** | 关联、冲突、例外 |
| 中期 | `sqlite-vec`（向量存**同一个 .db**，不是独立向量库） | 1 个 SQLite 扩展 | 语义相似 |
| 后期 | Graphiti 式实体抽取 | 大 | 跨项目实体联想 |

**不做独立向量数据库**：它表达不了「矛盾」（两条相反的结论向量极接近），而显式冲突正是本产品的核心能力；且 embedding 模型要么占几百 MB 本地资源、要么走 API 违背本地优先。`sqlite-vec` 是 SQLite 扩展，仍是那个文件、仍可备份、仍是本地优先。

非目标的目的是**守住本地优先、低运维、证据可追溯**三条底线，不是把路堵死。

### 2.3 设计原则（继承母文档，并显式修订两条）

- 本地优先
- **memory.db 是长期记忆权威数据源**；Markdown 是用户可读导出与显式纠正入口；SQLite/FTS 派生索引可重建
- 自动沉淀和自动修正是主流程；人工纠正优先且不可被后台覆盖
- 知识与规则分离
- 每条规则都可解释
- 增量处理

**对本文件的母文档修订声明**：母文档 [technical-design-context-forge.md](technical-design-context-forge.md) §3「设计原则」中有两条已被本文件取代，冲突时以本文件为准：

| 母文档 §3 原则 | 本文件的修订 |
|---|---|
| 「Markdown 是事实源」 | 该原则针对 Gen1 Vault 知识资产。进入 Gen2 后，长期记忆（版本、来源、关系、检索事件）的权威数据是 `memory.db`；Markdown 只作可读视图与显式纠正入口。见本节第一条。 |
| 「人类确认是状态转换」 | 改为「自动沉淀和有依据的修正是主流程，人工阅读/修改/抑制/删除是可选旁路」。仅高风险动作（扩大访问范围、覆盖人工版本、永久删除、无 tool 证据升 `verified`）仍禁止自动执行。 |

母文档**尚未回写**这两条修订，属已知冲突，§11 的「不得违背」不适用于上表两行。

新增一条：

- **宁可少而真，不要多而假** — `verified` 门槛高、数据稀，是故意的

---

## 3. 记忆模型

### 3.1 记忆分型与三层抽象

#### 四种记忆类型（决定召回与整理策略，不决定是否保留来源）

| 类型 | `role`（现有枚举） | `Provenance.project` | 例子 |
|------|------------------|------------------------|------|
| **情节** episodic | `observation` | ✅ 保留，且参与准入判断 | 「platform-web 的 `.umirc.ts:55` 配了 CAS 代理」 |
| **语义** semantic | `claim` / `hypothesis` | ✅ 保留，只作来源与弱排序 | 「CAS `serviceValidate` 在浏览器端会撞 CORS」 |
| **程序** procedural | `procedure` | ✅ 保留，只作来源与弱排序 | 「配 CAS 时先在 dev proxy 转发 `/cas/*`」 |
| **偏好** preference | `preference` | ✅ 保留，只作来源与弱排序 | 「删代码不要加注释解释」 |

**规则 = 语义 + 程序，但仍保留来源 project。** 适用范围由 `ApplicabilityScope`（技术栈 / 问题域 / 约束）表达；来源项目用于解释和弱排序，不能替代适用条件或安全授权。

对应到产物形态：`kind: knowledge` 常承载情节，`kind: rule` 必然承载语义或程序。**`kind` 不决定记忆类型，`role` 才决定。**

#### 三层抽象由 `role` + `ApplicabilityScope` 推导，**不落库**

```
┌───────────────────────────────────────────────────────────┐
│ L3 原则 (principle)     有资格进候选池, 几乎不衰减         │
│   判定: role ∈ {claim, hypothesis, procedure, preference}   │
│         且 ApplicabilityScope 全空                          │
│         （preference 无论 scope 是否为空都归 L3）           │
│   「信任边界必须做输入校验」                                │
│   「删代码不要加注释解释它被删了」                          │
├───────────────────────────────────────────────────────────┤
│ L2 模式 (pattern)       技术栈/问题域匹配时注入             │
│   判定: role ∈ {claim, hypothesis, procedure}               │
│         且 technologies / domains / constraints 非空        │
│   「antd Select 服务端搜索必须 filterOption=false + 防抖」  │
│   「SQLite 并发写用 WAL + busy_timeout」                   │
├───────────────────────────────────────────────────────────┤
│ L1 实例 (instance)      精确匹配 provenance 才注入          │
│   判定: role = observation（情节）                          │
│   「platform-web .umirc.ts:55 有 CAS 代理, 别重复配」       │
│   「workspaceinfo 有双重适配, 改 DTO 要改两处」             │
└───────────────────────────────────────────────────────────┘
```

**不落库的理由**：`generality` 可由 `role` + `ApplicabilityScope` 推导。来源 project 不属于 generality，必须保留在 provenance 中用于追溯。

**写入规则：模型提炼 transcript 时优先区分事实、归纳和程序，并为归纳填写适用条件与反例。** 文本是否出现项目名不能单独决定记忆类型；所有类型都保留来源，是否跨项目可用由 AccessPolicy 与 ApplicabilityScope 决定。

**用户偏好（`preference`）保留历史但允许变化**：使用 `last_confirmed_at` 与 `supersedes` 表达新旧偏好，不因长期未引用直接判定为错误。

### 3.2 可证伪结构

每条记忆由四组字段构成可证伪性：

```python
class MemoryDraft(MemoryModel):
    kind: Literal['knowledge', 'rule', 'review']     # 产物形态
    role: Literal['observation', 'claim', 'hypothesis', 'preference', 'procedure']
    #   ^ 记忆类型由 role 决定（§3.1）: observation=情节, claim/hypothesis=语义,
    #     procedure=程序, preference=偏好。三层抽象由 role + scope 推导, 不落库。

    # (1) 证据 — 现有, 使用强度要提高
    evidence_level: Literal['unknown', 'unverified', 'supported',
                            'verified', 'contradicted']
    sources: tuple[Source, ...]        # verified 强制要求 tool 摘录
    proof_count: int = 0               # 独立观测次数, §4.4

    # (2) 适用边界 — 现有, 三态条件是可证伪的核心
    applicability: tuple[Applicability, ...]   # condition + true|false|unknown

    # (3) 关系 — 现有, 冲突/反例/例外/取代
    #     经 put(relations=...) 写入 memory_relations

    # (4) 边界三分 — §3.3
    space: MemorySpace                 # 所属主体/记忆空间
    access: AccessPolicy               # 安全边界, 硬过滤
    provenance: Provenance             # 来源上下文, 所有类型都保留
    scope: ApplicabilityScope          # 适用条件, 软排序
```

#### 3.2.1 证据分级的语义约束

| level | 含义 | 硬约束 |
|-------|------|--------|
| `unknown` | 无证据 | — |
| `unverified` | 有来源但未交叉验证 | 至少 1 条 `Source` |
| `supported` | 有观测支持 | 至少 1 条 `Source` |
| `verified` | 已被真实 tool 观测确认 | **必须含 `kind='tool'` 且 `excerpt` 非空的 `Source`** |
| `contradicted` | 已被反例推翻 | 必须含反例 `Source`，并以 `contradicts` 关系指向被推翻者 |

`verified` 的门槛是这套证据纪律的关键。模型自评的 confidence **永不**自动升级 `evidence_level`。

#### 3.2.2 三态适用条件

```python
class Applicability(MemoryModel):
    condition: str          # 人类可读的条件描述
    result: Literal['true', 'false', 'unknown']
    evidence_ids: tuple[str, ...]
```

示例：

```yaml
applicability:
  - { condition: "antd@5.x + 服务端分页",        result: "true",    evidence: [e1, e2] }
  - { condition: "antd@6.x (Select 行为变更)",  result: "false",   evidence: [e3] }
  - { condition: "React 19",                    result: "unknown", evidence: [] }
```

召回时返回给 LLM 的**必须**包含 `unknown` 条件——让模型知道自己不知道什么。这是抑制幻觉沉淀的关键：只给断言而不给条件，模型会默认它到处成立。

#### 3.2.3 关系类型与语义

| kind | 语义 | 触发 |
|------|------|------|
| `derived_from` | 由某条知识派生 | 规则生成时 |
| `duplicate_of` | 完全重复，指向规范条目 | 合并时 |
| `overlaps` | 部分重叠 | 相似度检测 |
| `conflicts_with` | 结论相反，需分流 | 冲突检测 |
| `contradicts` | 有反例推翻 | 反例写入 |
| `exception_to` | 是某规则的例外 | 例外写入 |
| `supersedes` | 取代旧版本 | 人工修订 |
| `supports` / `refines` | 支持 / 细化 | 派生 |

**关系带版本**（`target_version`），新修订不静默继承旧关系——防止关系本身变成脏数据。

### 3.3 边界三分：安全边界 / 来源上下文 / 适用条件

现在只有 `Scope` 一个概念，把三种不同职责压在一起：

```python
class Scope(MemoryModel):
    kind: Literal['global', 'project', 'unknown']
    project: str | None
    paths: tuple[str, ...]
```

拆开后，`project` **不再是默认召回边界**（调研 §核心结论 / §建议 2）；项目级访问限制改由显式授权表达：

```python
class MemorySpace(BaseModel):
    """所属主体/记忆空间 — 不承担项目授权。"""
    owner: str                       # 'user:local' | 'agent:xxx' | 未来 'team:xxx'（由本地配置提供，不硬编码用户名）
    privacy: Literal['shared', 'private'] = 'shared'

class AccessPolicy(BaseModel):
    """显式授权，与来源和适用条件分离。"""
    visibility: Literal['owner', 'selected_projects', 'shared'] = 'shared'
    allowed_projects: tuple[str, ...] = ()

class Provenance(BaseModel):
    """来源上下文 — 只作溯源与排序加分，不决定可见性。"""
    projects: tuple[str, ...] = ()   # 来自哪些仓库
    sessions: tuple[str, ...] = ()   # 来自哪些会话
    paths: tuple[str, ...] = ()      # 证据所在文件 glob

class ApplicabilityScope(BaseModel):
    """适用条件 — 参与排序与采用判断。"""
    technologies: tuple[str, ...] = ()   # antd, umi, sqlite, cas, react...
    domains: tuple[str, ...] = ()        # proxy, auth, search, table, form...
    constraints: tuple[str, ...] = ()    # "monorepo", "offline", "node>=20"
    exclusions: tuple[str, ...] = ()     # 反例: "monorepo 根目录不适用"
    visibility: Literal['global', 'scoped'] = 'scoped'
```

**`Provenance` 按记忆类型填充**（§3.1）：

| `role` | `Provenance` | 理由 |
|--------|-------------|------|
| `observation`（情节） | **必须**填 `projects` / `sessions`，通常填 `paths` | 它本来就是「某时某地发生了什么」；来源同时参与准入判断 |
| `claim` / `hypothesis` / `procedure` / `preference` | **同样保留** `projects` / `sessions` / `sources` | 来源永不删除，只从默认过滤路径移出；适用范围由 `ApplicabilityScope` 表达 |

**校验约束**：所有记忆都保留 `Provenance.projects`；来源 project 只用于 `explanation` 溯源展示与弱排序加分，不参与默认硬过滤。项目专属访问由 `AccessPolicy.allowed_projects` 表达。

**三层职责对照**（调研原文：「bank 决定安全隔离，metadata 决定局部过滤」）：

| 层 | 硬/软 | 作用 |
|----|-------|------|
| `MemorySpace` + `AccessPolicy` | **硬过滤** | `space.owner` 不匹配、`space.privacy='private'` 跨主体、`access.visibility='selected_projects'` 未授权 → 不可见 |
| `Provenance` | **软排序** | 同项目加分；跨项目**不删除**。同时是「迁移理由」的证据来源 |
| `ApplicabilityScope` | **软排序 + 采用判断** | 技术栈/领域命中 → 加分；`exclusions` 命中 → 直接淘汰 |

**匹配逻辑**（两个阶段不要混用）：

- **淘汰路径（4 条）**：命中即完全不可见，发生在打分前。
- **候选池准入**：不淘汰，只决定是否进入打分；`instance` 依赖 provenance 是准入条件，不是淘汰条件。

```
space.owner 不匹配                              → 淘汰（主体边界）
space.privacy='private' 且非本 owner            → 淘汰（主体级隐私）
access.visibility='selected_projects' 且当前项目不在 access.allowed_projects → 淘汰（项目级显式授权）
scope.exclusions 命中                           → 淘汰
────────────────────────────────────────────────────────
visibility == 'global'                         → 适用分 1.0
technologies 命中当前技术栈                    → 适用分 0.8
domains 命中当前领域                           → 适用分 0.7
constraints 不满足                             → 强惩罚（不淘汰，但降权并进「风险提示」）
────────────────────────────────────────────────────────
provenance.project == 当前项目                 → 来源加分 +0.2（不是准入条件）
provenance.paths 命中 changed_path             → 来源加分 +0.15
无任何适用维度声明                             → 适用分 0.3（未解析）
```

**关键行为差异**：旧逻辑下 platform-web 的记忆在 context-forge 里**看不见**；新逻辑下它**看得见但排得靠后，且必须说明为什么适用**。这正是「脏记忆不该删、该改标签」的结构性根据——跨项目联想是个人长期学习的全部价值。

**`AccessPolicy` 是显式授权**，不是默认。`visibility='selected_projects'` 承接「确实不能跨项目使用」的内容（如含内部 URL、客户名、环境变量的条目），并独立于来源项目保存 `allowed_projects`。

**当前项目技术栈自动检测**，不需要人工标注：

```
package.json / pyproject.toml / requirements.txt / go.mod
  + import/require 扫描
  + lockfile 提取
  → ApplicabilityScope.technologies / domains
```

### 3.4 `MemorySpace` 与既有保护机制的关系

单用户本地部署下 `MemorySpace` 看似多余，但它给两条既有约束提供了**锚点概念**：

| 既有机制 | 现状 | `MemorySpace` 提供 |
|---------|------|-------------------|
| `user_owned` / `ProtectedMemory` | 字段级保护 | 「谁拥有」有了实体：`owner` |
| 「Markdown 是用户可读视图 / `memory.db` 是权威数据」 | 设计原则 | 「用户的」有了定义域：`owner='user:*'` |
| 未来 team 共享（§2.2 演进路径） | 无处安放 | `owner='team:*'` 天然扩展 |

因此 `MemorySpace` 本期就建表，即使只有一个 owner。**不建等于把隐私边界永久绑在 project 上**，那正是本次要拆掉的东西。

---

## 4. 召回机制

### 4.1 核心转变：filter → score

现在 `_visible()` 返回 `bool`（黑名单），改为返回 `float`（相关度），取 top-K 而非「全过 filter 的都返回」。

这是 §1.3 三个硬缺陷的直接对策——`match_rules` 灌 78 条全文进上下文的 bug，根因就是没有排序和上限。

### 4.2 五维打分

```python
def relevance(memory: MemoryRecord, ctx: RetrievalContext) -> float:
    return (
        w1 * match_score(memory, ctx)                  # 适用条件 + 来源加分
      + w2 * evidence_score(memory.evidence_level)     # 证据强度
      + w3 * freshness_score(memory, now)              # 时效权重
      + w4 * feedback_score(memory)                    # 采用反馈（含负反馈）
      + w5 * text_score(memory, ctx.query)             # 文本相关度
      - conflict_penalty(memory, activated)            # 冲突惩罚
    )
```

| 维度 | 取值 | 说明 |
|------|------|------|
| **匹配** `w1` | `applicability` + `provenance` 两段相加，见下 | **全部是加分项，无准入门槛**（硬过滤在 §3.3 已完成） |
| **证据** `w2` | verified=1.0, supported=0.7, unverified=0.3, unknown=0.1, contradicted=−0.5 | 优势 A 的落地 |
| **时效** `w3` | `e^(-λ·天数) × (1 + 0.3·复用次数)` | `复用次数` = 可归因的 `applied` 次数（**不是** `proof_count`，也**不含** `presented`/`referenced`） |
| **采用反馈** `w4` | 见 §4.4 权重表；**曝光不算强化** | 优势 B 的落地 |
| **文本** `w5` | BM25（FTS5）归一化 | 现有能力 |
| **冲突惩罚** | 与已激活记忆 `conflicts_with`/`contradicts` → 降权并附解释 | 优势 D 的落地 |

#### `match_score` 的两段构成（§3.3 三层模型的落地）

```python
def match_score(memory, ctx) -> float:
    scope = memory.scope                         # ApplicabilityScope
    applicability = 0.0
    applicability += 1.0 if scope.visibility == 'global' else 0.0
    applicability += 0.8 if set(scope.technologies) & set(ctx.technologies) else 0.0
    applicability += 0.7 if set(scope.domains) & set(ctx.domains) else 0.0
    if not (scope.technologies or scope.domains or scope.constraints or scope.visibility == 'global'):
        applicability = 0.3                      # 未解析，基础分
    applicability -= 0.4 if violated(scope.constraints, ctx) else 0.0

    provenance = 0.0                             # 只加分，永不淘汰
    provenance += 0.20 if ctx.project in memory.provenance.projects else 0.0
    provenance += 0.15 if any(p.match(ctx.changed_path) for p in memory.provenance.paths) else 0.0

    return applicability + provenance
```

**注意**：`provenance` 是**加分项**，跨项目条目得 0 分但仍可凭 `applicability` + 文本相关度进入 top-K。这正是「同项目加分，跨项目不删除」的实现。

初始权重 `w1..w5` 用可调常量（默认 `1.0 / 1.2 / 0.6 / 0.8 / 1.0`），**不引入 ML**。权重是校准旋钮（见 §8）。

三层抽象（§3.1 推导结果）决定**候选池准入**，不进打分公式：

```
L3 原则   (role ∈ {claim,hypothesis,procedure,preference} 且 scope 全空
           或 role = preference)                            → 恒准入
L2 模式   (role ∈ {claim,hypothesis,procedure} 且 technologies/domains 非空)
                                                         → 适用维度命中才进
L1 实例   (role = observation)                           → provenance 命中才进
```

**L1 的准入依赖 provenance 是有意的**：具体实例（「.umirc.ts:55 配了 CAS 代理」）在别的项目里没有意义。这是**候选池准入**，不是淘汰——L1 未命中 provenance 时只是不进候选池，不涉及 §3.3 的淘汰语义。真正跨项目能用的内容应该在写入时就写成语义或程序（R5.2）。

**规则（`role != observation`）的 `Provenance.projects` 不参与准入**——这是 §1.7 的落地。规则只受 `ApplicabilityScope` 约束。

### 4.3 检索流程

```
1. tokenize(query)           中文 bigram + ASCII lowercase (现有)
2. FTS5 MATCH → 候选集        BM25 初排
3. 硬过滤                    space.owner / space.privacy / access.visibility / exclusions / status
4. 候选池准入                 按推导的三层分层过滤 (§4.2)
5. relevance() 打分          五维加权
6. 关系展开                   沿 relations 走 1 跳，作为解释与冲突上下文 (§4.7)
7. 冲突检测                  对 top-K 内部两两比对 relations
8. byte_budget + limit 截断   默认 64KB / 8 条 (现有约束保留)
9. 组装 RecallResult          全文 + applicability + sources + relations + explanation
```

**`explanation` 必须回答四件事**（现有 `MemoryRecall._result()` 已有雏形，扩展它）：

```
1. 为什么命中   — 哪个维度匹配, 命中原因
2. 来源与迁移   — 来自哪个项目/会话; 若跨项目, 说明「为什么在当前上下文也成立」
3. 适用边界     — true / false / unknown 三态条件（unknown 必须出现）
4. 风险提示     — 未解析条件、已知反例、约束违反、冲突或例外需评估
```

**第 2 段是本次改造的核心收益的可见化**。调研要求「跨项目结果必须显示来源和迁移理由」——一条 platform-web 的记忆出现在 context-forge 里，用户必须能看到**为什么它在这里也成立**，而不是靠猜。迁移理由由匹配命中的 `ApplicabilityScope` 维度生成：

```
来源: platform-web-main / session c0f909b9
迁移: 适用技术 [antd, umi] 与当前项目技术栈重合; 问题域 [proxy] 命中
```

无任何适用维度命中、仅凭文本相关度进 top-K 时，迁移理由必须显式降级为：

```
迁移: 仅文本相关, 未匹配任何适用维度 — 谨慎采用
```

### 4.4 曝光链与权重更新（优势 B 的落地）

`MemoryStore.record_retrieval()` 已实现六阶段事件采集，schema 完备：

```
retrieved → tool_returned → presented → referenced → applied → validated
```

**当前缺口：数据在收集，但没有反向更新记忆权重。** 补上这一环。

**硬原则（调研 §建议 7）：曝光 ≠ 强化。** 只有 `applied` / `validated` / 负反馈校准排序；单纯被展示、被阅读不产生正向权重——否则「高频出现但没用」的记忆会被自我强化，正是我们要防的脏数据机制。

| 信号类型 | 事件 | 权重影响 | 产出报表 |
|---------|------|---------|---------|
| **采用** | `referenced` → `applied` | **+0.3** | 真正被采用 |
| **有效** | `applied` → `validated` | **+0.5**；可升 `evidence_level`（仅 `unverified`→`supported`） | 真正有效 |
| **曝光** | `presented` / `tool_returned` | **0**（只计数，不加权） | 曝光率 vs 采用率的比值 |
| **弱信号** | `presented` → `referenced` | **0**（被读进上下文 ≠ 被采纳） | 「读了没用」是整理候选，不是强化信号 |
| **负反馈** | `applied` 后被人工撤销 / 纠正 | **−0.8**；记录撤销原因，**不自动转 `contradicted`** | 人工纠正 |
| **负反馈** | `context_forge_record_feedback(outcome='harmful')` | **−1.0**，转 `conflicted` 待复核 | 异常审计 |
| **负反馈** | `outcome='irrelevant'` | **−0.2** | 弱负信号 |
| **衰减** | 长期无 `applied` 且无来源更新 | 累积负权 → 提升整理优先级；转 `stale` 需有依据且可逆 | 整理候选 |

`feedback_score` 只聚合上表的非零项，且只在**可归因**时生效；`presented`/`referenced` 的计数另存于报表，**不进 `w4`**。

**关键约束**：这些事件是**观察性的**，永不自动改写 `evidence_level` 到 `verified`。`verified` 只能由真实 tool 观测写入。`applied` / `validated` 只有在记录应用结果（预期、实际结果、验证来源、归因）且归因可确认时，才能修改记忆状态；无法归因时只记录。

**证据的强化 / 削弱 / 扩展，而不是静默覆盖**（借鉴 Hindsight 的 observation 语义）：新证据到来时按方向分派——

| 新证据方向 | 动作 |
|-----------|------|
| 支持现有结论 | 追加 `Source`，`proof_count` +1；`evidence_level` **最多升到 `supported`**，无新 tool 摘录不得升 `verified` |
| 削弱但不推翻 | 追加 `Source`，标 `applicability.result='false'` 的新条件（缩小适用面） |
| 推翻 | 建 `contradicts` 关系 + 新记忆，**不删旧的** |
| 扩展适用面 | 建 `refines` / `exception_to` 关系 |

`proof_count` 是同一 `Source.identity` 被独立观测的次数（沿用 `memory_sources` 的 identity 语义：内容重复不算独立证据）。它进 `evidence_score`，但**不越过** `verified` 的硬门槛。

#### 三张报表

```python
class MemoryEffectiveness:
    def application_report(rule_id) -> {referenced, applied, validated, revoked}  # 应用效果
    def anomaly_candidates() -> list[MemoryRecord]          # verified 但 0 applied：未使用/待复核，不判定为幻觉
    def tidy_candidates(days=90) -> list[MemoryRecord]      # 长期无 applied：整理候选
```

#### 应用结果与归因 `MemoryApplication`（P3 核心契约）

曝光计数不构成因果。一次实际应用必须单独记录预期、结果和归因强度；只有可归因的结果才允许修改记忆状态。

```python
class MemoryApplication(MemoryModel):
    application_id: str                 # 幂等键
    task_id: str                        # 哪一次任务
    memory_id: str
    memory_version: int                 # 版本固定，避免归因漂移
    action: str                         # 该记忆触发的动作/被采用的方式
    expected_outcome: str
    actual_outcome: str | None          # 未知则 None，不虚构
    validation_source_ids: tuple[str, ...] = ()   # 指向本版本 sources
    attribution: Literal['confirmed', 'plausible', 'unknown'] = 'unknown'
```

**判定规则**（硬约束）：

| attribution | 何时成立 | 允许的动作 |
|-------------|---------|-----------|
| `confirmed` | 有验证来源把「该动作」与「实际结果」直接连接 | 计入成功统计、触发 `+0.3/+0.5` 反馈权、允许 `unverified`→`supported`、允许负反馈 |
| `plausible` | 结果与动作相关，但无独立验证来源 | 只记录；不进 `w4`，不改 `evidence_level` |
| `unknown` | 无法归因 | 只记录；不进 `w4`，不改状态 |

- `validation_source_ids` 必须 ⊆ 该版本 `sources`，否则拒绝写入（沿用现有 evidence 引用校验）。
- `attribution != 'confirmed'` 时**不得**提升 `evidence_level`、不得降权、不得标记 `contradicted`。
- `MemoryApplication` 是本文件对 P3 的权威定义；`design.md` 只引用，不另立字段。

### 4.5 冲突呈现

召回结果内部两两比对 `conflicts_with` / `contradicts` / `exception_to`，命中则在 `explanation` 中显式标注：

```
[冲突] rule-a (结论 X) 与 rule-b (结论非 X) 在 condition C 上重叠
       → 按 applicability 分流: C 成立用 a, C 不成立用 b
       → 需要复核时: 联系 [[memory-curation]]（可选旁路，不阻塞召回）
```

**不静默丢弃任何一条**。与其让模型自己猜，不如把矛盾摊到台面上。

### 4.6 效果评估与验证实验

打分权重是拍脑袋的初值，**必须用实验校准**，否则「五维打分」只是换个方式拍脑袋（调研 §需要验证的实验）。

#### 对照组

构造至少三组跨项目任务，比较三种召回策略：

| 组 | 任务构造 | 检验什么 |
|----|---------|---------|
| **G1 同技术同问题** | 两个项目都用 antd + 都遇 Select 服务端搜索 | 跨项目迁移应当生效 |
| **G2 不同技术同模式** | 一个 umi + 一个 FastAPI，都要处理代理 504 | `pattern`/`principle` 层能否抽象命中 |
| **G3 表面相似但约束冲突** | 同样的 API 设计问题，一个要求 `strip` 一个要求 `passthrough` | 冲突检测与「为什么可能不适用」 |

三种策略对照：

```
S1  当前 project 硬过滤                    （基线，现状）
S2  全库 FTS + 同项目加权                  （P0-P2 打分召回）
S3  全库 FTS + 上下文重排                  （P2+ 冲突/迁移理由完整版）
```

#### 指标

**不看召回条数**，看决策质量（调研 §需要验证的实验）：

| 指标 | 定义 | 期望 |
|------|------|------|
| **决策帮助率** | 有记忆可参考的任务中，记忆被 `applied` 且结论正确的比例 | S3 > S2 > S1 |
| **错误迁移率** | 跨项目记忆被 `applied` 后被撤销/纠正的比例 | S3 < S2（迁移理由降低误用） |
| **首次成功率** | 不返工完成任务的比例 | S3 ≥ S2 ≥ S1 |
| **额外 token / 延迟** | 召回结果占用的上下文与耗时 | S3 ≈ S2 ≪ S1（top-K 生效） |

G3 的**错误迁移率**是本次改造的风险指标，优先级最高——跨项目联想开得越宽，它越重要。

#### 通过判据

P2 上线前必须至少跑完 G1–G3 的 S1 vs S2 对照；S3 在 P3/P4 后补测。判据是**绝对阈值**：S2 的错误迁移率不得超过**实验前写明的阈值**（与 §9 / AC-P2 同一口径）。超阈值说明 `ApplicabilityScope` 标注不足，回到 P1 补标签，**不要靠调权重硬压**。

### 4.7 联想召回：关系展开与后续关联学习

人脑不用数据库，它用**线索**（编码特异性，Tulving）。关键词检索只走一条线索；联想召回走的是**关联网络**。本期只上线**一跳显式关系展开**，不引入新依赖，也不把系统自身输出当作学习信号。

#### 4.7.1 关系展开（spreading activation）—— 走 `memory_relations`

概念是网络节点，激活一个会沿连接扩散到相关节点。本期落地为**关系图上的 1 跳展开**：

```
FTS 命中种子集 S₀
  → 沿 memory_relations 展开 1 跳: derived_from / supports / refines / overlaps
    → 得 S₁, 作为解释与冲突上下文
  → 与 S₀ 合并去重
  → top-K（关系展开不改变种子集排序）
```

**只沿「正向」关系展开**：`conflicts_with` / `contradicts` / `exception_to` / `duplicate_of` **不展开**，它们在 §4.5 冲突呈现里单独处理——展开它们等于把冲突噪声放大。

**约束**：`byte_budget` 是硬约束。关系展开进来的条目只作解释与冲突上下文，不抢占种子集排序。两跳扩散与衰减系数属于后续演进项，需先有实验证据。

#### 4.7.2 关联学习：本期不做自动共现强化

**删除 `tool_returned` 共现建边。** 同一个请求里一起返回，只说明检索器把它们放在了一起，不能证明它们相关；用它建边会让检索器反复强化自己的输出，形成回音室。

后续只有在满足全部条件时，才能生成**待复核的关联候选**：

```
① 两条记忆在同一次任务中都被 applied
② 都关联到同一个 validated 结果
③ 在多个独立任务中重复出现
```

候选关联是**建议**，不自动生效、不进入召回排序，除非有独立证据支持。关联统计只读、可缓存、可随时重建，**不进权威数据**。

#### 4.7.3 为什么不是向量或图数据库

| 方案 | 解决什么 | 致命盲点 | 判断 |
|------|---------|---------|------|
| 独立向量库 | 语义相似 | **表达不了矛盾**（两条相反结论向量极接近）；需要 embedding 模型（本地几百 MB 或走 API 违背本地优先） | 不做 |
| 知识图谱（Graphiti 式） | 实体关系 + 时间 | 抽取 + 消歧成本高，脏图比没图更糟 | 后期，见 §2.2 |
| **FTS + relations 一跳关系展开** | 关联、冲突、例外 | 无语义相似 | **本期**，零依赖 |

向量补的是「措辞不同但意思相近」；图补的是「实体间的关系」。两者都不解决**矛盾**，而矛盾显式化是本产品的核心能力（优势 A）。关联学习必须基于可归因结果，不能基于检索器自身的输出。

中期若语义召回确成瓶颈，上 `sqlite-vec`（SQLite 扩展，向量存同一个 `.db`，仍是本地优先、可备份）。它**补充**而非替代 relations 图。

---

## 5. 记忆卫生（优势 D 的落地）

### 5.1 巩固：情节 → 语义/程序

记忆随时间**抽象化、泛化**，这是人脑的方向，也是 §1.2 / §1.7 根因的长期解。认知科学上对应**巩固**（complementary learning systems）：海马快存情节，皮层慢积语义，两者必须交错——太快会灾难性遗忘，太慢学不到东西。**巩固速率就是这个 interleave 速率。**

```
L1 情节 (role=observation)  多个独立来源出现相似表述
   ↓  自动巩固  → 新建 role=claim / procedure 的高层记忆
L2 模式 (scope.technologies/domains 非空)   多个项目 applied + validated
   ↓  自动巩固  → 收敛 ApplicabilityScope
L3 原则 (scope 全空)
```

**巩固动作的三要素**：
1. **新建高层记忆并改 `role`**（observation → claim/procedure）—— 这才是实质：从「发生了什么」变成「一般而言什么为真」
2. **保留全部 `Provenance.projects` / `sessions` / `sources`** —— 来源永不删除，只从默认过滤路径移出
3. **收敛 `ApplicabilityScope`** —— L2→L3 时收窄适用维度

**自动执行，不依赖人工确认**——沉淀是主流程；人工阅读、修改、抑制和删除是可选旁路。巩固必须有独立来源支撑、保留完整 provenance、通过 `derived_from` 可追溯，并且可撤销；出现反例时自动收窄适用范围。

巩固是**新建高层记忆 + `derived_from` 关系指向所有来源实例**，不是原地改写。历史可追溯。

### 5.2 衰减

```
强化: applied + 可归因 validated             → weight ↑ (§4.4)
整理: 长期无 applied 且无来源更新             → 提升整理优先级 (§2.1 / §4.4 整理候选)
衰减: 有依据时可转 stale, weight ↓           → 必须可逆、可追溯 (§4.2 w3)
偏好: role = 'preference'                    → 保留历史，允许 supersedes，用 last_confirmed_at 判断时效
保护: user_owned / suppressed / disabled      → 后台不得覆盖，只有人工可恢复
```

### 5.3 合并

`MemoryMerge` 已实现精确重复与 token 相似度检测，扩展为：

| 情形 | 动作 |
|------|------|
| 规范化内容完全相同 + 决策边界完全相同 | `apply_exact` → `duplicate_of` 归档 |
| 相似度 > 0.9 且 scope 兼容 | 生成合并候选；低风险可自动归档，高风险留人工复核 |
| 相似度 > 0.9 但 applicability 冲突 | **不合并**，改标 `conflicts_with` |
| 相似但 evidence 来源不同 | 合并 sources，保留全部证据链 |

### 5.4 保护

现有 `ProtectedMemory` 异常已实现，规则不变：

- `user_owned: true` / `suppressed: true` / `status: 'disabled'` → 模型提案必须失败
- 只有人类 `actor='human'` 的修订能恢复
- `VersionConflict` 保证并发安全
- `IdempotencyConflict` 保证重试安全

---

## 6. 双向复利（优势 C 的落地）

### 6.1 AI 侧闭环

```
会话 transcript
  → 证据提取 (含 tool 摘录)
  → 三层抽象写入 (§3.1)
  → 召回打分 (§4)
  → 注入上下文
  → 曝光追踪 (§4.4)
  → 权重更新 + 应用效果报表
  → 巩固 / 衰减 / 合并 / 冲突 (§5)
```

### 6.2 人侧复盘资产

Obsidian Vault 是**用户拥有的数据**，不是供应商格式：

- 每条记忆是独立 Markdown，可读、可编辑、可导出
- frontmatter 记录证据、边界、关系、来源会话
- 双向链接 `[[name]]` 形成个人知识网络
- 换工具、换模型，资产随人走

**产品要保证**：复盘的可读性不因检索优化而退化。任何为 LLM 注入做的压缩，都不能污染 Markdown 导出内容；`memory.db` 仍是长期记忆权威数据。

---

## 7. 兼容性与迁移

### 7.1 Vault（Gen1）存量数据

2026-09-22 实测 103 条 knowledge + 103 条 rule，`project` 标签覆盖全部 206 条。**不删除，改标签**：

| 现状 | 迁移动作 |
|------|---------|
| `kind: rule`（103 条） | **保留 `project` 作来源**，但从默认过滤路径移出；按内容改 `role` 为 `claim`/`procedure`/`preference`；把技术/领域词迁到 `technologies`/`domains` |
| `kind: knowledge` 且是具体项目细节 | `role: observation`，**保留** `project`/`paths` 作 `Provenance` |
| `kind: knowledge` 但结论跨项目通用 | 升为 `role: claim`/`procedure`，**保留 `project` 作来源**，补 `technologies`/`domains`（这是一次离线巩固） |
| 内容含内部 URL / 客户名 / 凭据 | 标 `space.privacy: private`（主体级不可跨主体），或 `access.visibility: selected_projects` + `allowed_projects`（限定项目） |

**判据**：看这条记忆描述的是「某时某地发生了什么」还是「一般而言什么为真」。文本是否出现项目名不能单独决定类型；所有类型都保留来源。

迁移脚本产出**迁移报告**（哪些改了、改成什么、依据是什么）；先做在线备份、支持 dry-run，可自动执行并随时回滚，不依赖人工审批。

### 7.2 MemoryStore（Gen2）schema 变更

```sql
-- 权威表示仍是 memory_versions.snapshot_json（MemoryDraft 全量快照）。
-- 新模型字段先进入 snapshot_json，不额外增加权威 JSON 列：
--   MemoryDraft.access               : AccessPolicy
--   MemoryDraft.provenance           : Provenance
--   MemoryDraft.scope                : ApplicabilityScope (角色=适用条件)
--   MemoryDraft.proof_count          : int

-- 需要高效过滤/排序时，建立可重建投影（非权威，可从 snapshot_json 重建）
CREATE TABLE memory_access_projection (
  memory_id TEXT NOT NULL, version INTEGER NOT NULL,
  owner TEXT NOT NULL,
  visibility TEXT NOT NULL CHECK(visibility IN ('owner','selected_projects','shared')),
  allowed_projects_json TEXT NOT NULL CHECK(json_valid(allowed_projects_json)),
  PRIMARY KEY(memory_id, version)
);
CREATE TABLE memory_applicability_projection (
  memory_id TEXT NOT NULL, version INTEGER NOT NULL,
  technologies_json TEXT NOT NULL CHECK(json_valid(technologies_json)),
  domains_json TEXT NOT NULL CHECK(json_valid(domains_json)),
  exclusions_json TEXT NOT NULL CHECK(json_valid(exclusions_json)),
  PRIMARY KEY(memory_id, version)
);

-- 存量 Scope(kind, project, paths) 一分为三（§3.3）
-- Scope(kind='global')                      → AccessPolicy(visibility='shared') + ApplicabilityScope(visibility='global')
-- Scope(kind='project', project=X, paths=P) → Provenance(projects=(X,), paths=P)   [来源保留]
--                                             + AccessPolicy(visibility='shared')
-- Scope(kind='unknown')                     → ApplicabilityScope() + applicability 未解析条件
-- MemorySpace 全部存量：owner ← 本地配置（不写入硬编码用户名）；privacy ← 'shared'（默认）
-- 需要主体级私有的存量条目，才显式设 privacy='private'（由迁移报告列出，可自动执行、可回滚）
```

**关键**：`project` 迁移到 `Provenance`（来源 + 弱排序），**不再留在默认过滤路径上**。存量 103 条规则原本靠 `project` 精确匹配才能命中——迁移后它们会开始出现在其他项目的召回结果里，靠 `ApplicabilityScope`、访问策略和迁移理由决定采用与否。这是有意的行为变更，必须配合 §4.6 的 G3 错误迁移率验证。

`memory_schema.SCHEMA_VERSION` 递增。**迁移必须显式**（继承 `MemoryStore.initialize` 的既有约束），普通打开不自动迁移。迁移前做在线备份，支持 dry-run，自动执行后可回滚；投影表可从 `snapshot_json` 随时重建。

### 7.3 灰度

```
阶段 1: 新字段可选, 老数据用默认值, 检索行为不变
阶段 2: 打分排序上线, 老的二值过滤保留为 --legacy-filter 逃生舱
阶段 3: 曝光反馈驱动权重, 三张报表上线
阶段 4: 巩固 / 衰减 / 合并自动化, 人工干预作为可选旁路保留
```

每个阶段可独立回滚：schema 向后兼容（新列有默认值），打分可用开关关掉。

---

## 8. 风险与边界

| 风险 | 对策 |
|------|------|
| **幻觉沉淀** | `verified` 强制 tool 摘录；模型 confidence 永不升 `evidence_level`；§4.4 异常审计候选 |
| **错误迁移**（本次改造**新增的最大风险**） | 跨项目召回开宽后，别的项目的结论被误用到当前项目。对策：§3.3 的 `AccessPolicy.visibility='selected_projects'` 显式授权、§4.3 的迁移理由强制展示、「仅文本相关」时显式降级警告、§4.6 的 G3 错误迁移率作为**优先级最高的**验证指标 |
| **知识污染** | 三态 applicability + 关系显式 + 一键撤销；`ProtectedMemory` 保护人工条目 |
| **曝光自我强化** | §4.4 硬原则「曝光 ≠ 强化」；`presented`/`referenced` 只计数不加权 |
| **打分权重拍脑袋** | 权重是**校准旋钮**不是模型参数——真实使用会偏离初始假设，留 `forge doctor` 的权重诊断入口；**必须过 §4.6 的实验**才上线；不预设 ML |
| **打扰成本** | 沉淀过程不打扰用户；合并/冲突只生成可选复核项，批量呈现 |
| **隐私与凭据** | transcript 解析前本地脱敏，默认不上传；`space.privacy='private'` 承接不可跨主体内容，`access.visibility='selected_projects'` 承接限定项目内容 |
| **冷启动检索质量差** | FTS5 打不过向量检索是已知边界。靠证据层和卫生能力补，不硬碰硬；§2.2 明确向量检索不在本期 |
| **自动归因错误** | 只有 `attribution='confirmed'` 才能修改记忆状态；无法归因时只记录，不提升证据、不降权 |
| **Obsidian 生态碎片化** | Markdown 是最低共同点，不依赖特定插件；链接/目录约定可配置 |

---

## 9. 分期交付

| 期 | 内容 | 交付判据 |
|----|------|---------|
| **P0 止血** | `RuleMatcher` 加排序 + limit；`context_forge_search` 加 project 过滤 | 不再出现 78 条全文灌入；搜索不跨项目稀释 |
| **P1 模型** | **记忆分型落地**（`role` 决定类型）；**边界三分**（`MemorySpace` / `AccessPolicy` / `Provenance` / `ApplicabilityScope`）；`project` 移出默认过滤路径但保留来源；schema 迁移；存量改标签 + 迁移报告 | 三层抽象可由 `role`+`scope` 推导；`project` 已移出默认过滤路径且来源完整保留；迁移可自动执行、可回滚 |
| **P2 召回** | 五维打分（`match_score` = 适用 + 来源加分）；**一跳关系展开**；`explanation` **四段式**含迁移理由；冲突检测 | top-K 可解释可复现；**过 §4.6 的 G1–G3 之 S1 vs S2 对照** |
| **P3 闭环** | 曝光链 + **应用结果归因**；只有可归因结果驱动权重（**曝光≠强化**）；证据强化/削弱/扩展；三张报表 | 应用效果 / 异常审计 / 整理候选三张报表可导出；S3 对照补测 |
| **P4 卫生** | 自动巩固；衰减；合并；冲突分流 | 自动执行且有依据、可回滚；高风险动作仍需人工 |

P0 是唯一必须立刻做的——它修的是当前真实存在的上下文污染 bug，不依赖任何新模型。

**P2 的实验是上线闸门，不是上线后的补作业**：错误迁移率超过预定阈值时回到 P1 补 `ApplicabilityScope` 标签，不许调权重硬压。阈值必须在实验前写明。

---

## 10. 与既有代码的对应

| 设计概念 | 现有实现 | 状态 | 主要依据 |
|---------|---------|------|---------|
| 证据分级 + tool 摘录强制 | `memory_models.MemoryDraft.evidence_level` / `Source` | **已实现**，使用强度不足 | Hindsight 的 observation/proof count |
| 三态适用条件 | `memory_models.Applicability` | **已实现**，召回时未强制返回 | — |
| 冲突/反例/例外关系 | `memory_models.Relation` 九种 kind | **已实现**，无检测与呈现 | Graphiti 的事实有效期思想 |
| 强化/削弱/扩展（非静默覆盖） | `put` 全量快照替换 | **待改造** | Hindsight |
| 曝光链六阶段 | `MemoryStore.record_retrieval` | **已实现**，无反向权重更新 | — |
| 负反馈（撤销/纠正/harmful） | `JobStore.add_rule_feedback` | **已实现**，未进权重 | 调研 §建议 7 |
| 合并 / 精确重复 | `MemoryMerge` | **已实现**，缺相似度合并提议 | — |
| 人工保护 / 并发 / 幂等 | `ProtectedMemory` / `VersionConflict` / `IdempotencyConflict` | **已实现** | — |
| 记忆分型 `role`（情节/语义/程序/偏好） | `memory_models.MemoryDraft.role` 五值枚举 | **已定义**，召回时未用于决策 | 认知科学四分类 |
| 三层抽象 | — | **由 `role` + `ApplicabilityScope` 推导，不落库** | 巩固 / CLS 理论 |
| **安全边界 `MemorySpace`** | — | **待建** | Mem0 的 `user_id`；Hindsight 的 bank |
| **来源上下文 `Provenance`** | `Scope.project` / `paths`（当过滤器用） | **待拆出** | 调研 §建议 2：project 是 metadata 不是边界 |
| 适用条件 `ApplicabilityScope` | `Scope` 混在过滤里 | **待拆出** | 调研 §核心结论 |
| 五维打分召回 | `_visible()` 二值过滤 | **待改造** | Mem0/Hindsight 的混合召回 + RRF 思想 |
| **关系展开**（关系图 1 跳） | `memory_relations` 九种 kind | **表已建**，无遍历召回 | 扩散激活（本期仅 1 跳） |
| **应用结果归因** `MemoryApplication` | `retrieval_events`（request_id + memory_id + stage） | **表已建**，缺应用结果与归因对象 | — |
| 迁移理由（`explanation` 第 2 段） | — | **待建** | 调研 §建议 6 |
| 巩固 / 衰减 | — | **待建** | 巩固（海马→皮层）；continuous-learning-v2 的 promote |
| 三张报表 | — | **待建** | — |
| 效果评估实验（G1–G3） | — | **待建** | 调研 §需要验证的实验 |

**结论：已有版本、来源、关系、检索事件四类基础表，可以复用。** 仍缺的是访问策略、可执行适用模型、应用结果归因、迁移协议和评估基线——这些是风险最高、也尚未实现的部分，不能因为表已存在就视为完成。

`memory_relations` 提供了显式关系的基础，`retrieval_events` 提供了应用过程的原始记录。两者都需要在访问策略和归因模型落地之后，才能安全地参与召回。

---

## 11. 参考

- [cross-project-memory-competitor-analysis.md](research/cross-project-memory-competitor-analysis.md) — **边界三分模型、project 软排序、曝光≠强化、评估指标**的主要来源
- [competitor-landscape-human-ai-learning.md](research/competitor-landscape-human-ai-learning.md) — 八家竞品的结构性盲区、双向复利论据
- [technical-design-context-forge.md](technical-design-context-forge.md) — 母设计。本文件遵循其定位与治理原则；其中 §2.3 已**显式修订**母文档 §3「Markdown 是事实源」一条（见 §2.3），该处冲突以本文件为准
- [memory-store.md](memory-store.md) — MemoryStore 既有契约基线
