# 可证伪记忆系统与智能召回

完整技术设计：[docs/technical-design-falsifiable-memory.md](../../docs/technical-design-falsifiable-memory.md)
（下称「设计文档」，本文只写需求、约束与验收标准，不写技术方案）

## Goal

把 Context Forge 的记忆系统改为自动从经历中形成、应用并修正有来源和适用边界的记忆，并把召回从 project 精确过滤改为可解释的多维排序。人工阅读、修改、抑制和删除是可选旁路，不是沉淀前置条件。

**定位一句话**：Context Forge 自动沉淀和修正可追溯记忆；人工可以干预，但系统运行不依赖人工审批。

## Background

2026-09-22 实测 vault：**105 条 knowledge + 105 条 rule**（数字持续增长，迁移前以实测为准），`project` 标签分布为 `platform-web-main` 78 / `my-agent` 16 / `code-mentor` 9 / `cc-session-manager` 1 / `Context Forge` 1，**0 条属于 `context-forge` 自身**。

根因不是「沉淀了脏数据」，而是**抽象层级放错了**：跨项目通用知识（如「antd Select 服务端搜索要设 `filterOption=false`」）被锁在项目抽屉里。同时 `RuleMatcher.match()` 无排序无上限。

**灌入量的正确口径**（2026-09-22 复核实测，勿再用 78 描述召回返回量）：

| 场景 | 真实返回 |
|------|---------|
| `match_rules("platform-web-main")`，`changed_path=None` | **0 条**（新旧皆然；全库规则都有非空 `paths`） |
| `changed_path` 命中多个 glob（如 `src/pages/platform/workspaces.tsx`） | **28 条** ← 真实的无上限峰值 |
| 构造 78 条同 project 规则 | 78 条（合成场景，用于复现灌入） |

`78` 是「该项目有多少条规则」，**不是**「召回返回多少条」。真实峰值 28 → P0 后 8。

**灌入 bug 的影响半径**（穷尽扫描：用全部 105 条规则的每个 `paths` 模式当探针）：

| project | 规则数 | 无上限峰值 | P0 后 |
|---------|-------|-----------|------|
| `platform-web-main` | 78 | **28** | 8 |
| `my-agent` | 16 | 6 | 6 |
| `code-mentor` | 9 | 2 | 2 |
| `cc-session-manager` | 1 | 1 | 1 |
| `Context Forge` | 1 | 1 | 1 |

**只有 `platform-web-main` 超过 `limit=8`** —— 其余四个项目的返回量不变，灌入 bug 只在这一个项目有实际影响。不要把 P0 的影响面说成「所有项目都在灌入」。

竞品（Mem0 / Zep / Hindsight / Basic Memory / claude-mem / claude-memory-compiler / Devin Memories / CLAUDE.md）各有侧重，公开能力也不一致。本需求不替竞品下绝对结论，而是明确 Context Forge 自己要补齐的五项契约：证据分级、矛盾显式、应用可归因、整理可逆、数据可携带。本需求不比拼向量检索、云端共享、IDE 集成。

## Requirements

### R1 可证伪记忆（必须保持）

- R1.1 每条记忆必须携带证据分级 `evidence_level`，其中 `verified` **强制**要求保留真实 tool observation 原文摘录（`Source.kind='tool'` 且 `excerpt` 非空）
- R1.2 模型自评的 confidence **永不**自动升级 `evidence_level`
- R1.3 适用边界必须是三态 `Applicability(condition, result: true|false|unknown)`；召回结果**必须**返回 `unknown` 条件，让模型知道自己不知道什么
- R1.4 矛盾、反例、例外必须以 `Relation` 显式表达（`conflicts_with` / `contradicts` / `exception_to` / `supersedes`），**不静默丢弃任何一条**
- R1.5 记忆是假设不是信条：召回时必须返回「适用边界 + 风险提示」

### R2 结果闭环 → 应用效果可归因（必须保持）

- R2.1 曝光链六阶段自动记录；只有具备可归因结果的 `applied` / `validated` 才能反向驱动记忆修正
- R2.2 **曝光 ≠ 强化**：只有 `applied` / `validated` / 负反馈校准排序；`presented` / `referenced` 只计数不加权，防止「高频出现但没用」的记忆自我强化
- R2.3 **负反馈必须进权重**：人工撤销 / 纠正、`outcome='harmful'` / `'irrelevant'` 产生负权；`harmful` 转 `conflicted` 待复核
- R2.4 证据按方向分派为**强化 / 削弱 / 推翻 / 扩展**，不静默覆盖（推翻建 `contradicts` 关系 + 新记忆，不删旧的）
- R2.5 产出应用效果、异常审计和整理候选报表；未使用不等于幻觉，无法归因时保持 unknown
- R2.6 曝光事件**永不**自动改写 `evidence_level` 到 `verified`（该线不破，否则 R1 塌）

### R3 双向复利（必须保持）

- R3.1 AI 侧：会话 → 证据 → 三层抽象 → 打分召回 → 曝光 → 权重 → 卫生
- R3.2 人侧：Markdown 是用户可读、可编辑、可导出的视图；`memory.db` 保持长期记忆权威数据
- R3.3 任何为 LLM 注入做的压缩，**不得**污染 Markdown 导出内容

### R4 记忆卫生是一等公民（必须保持）

- R4.1 晋升 = **情节 → 语义/程序**（巩固）：多个独立经历可自动生成高层记忆，保留全部 `Provenance.projects` 与 `derived_from`；自动操作有依据、可回滚，人工确认不是前置条件
- R4.2 衰减：长期无 `applied` 且无来源更新 → 提升整理优先级；反复 `applied` + 可归因 `validated` → 强化；转 `stale` 需有依据且可逆；`role='preference'` 保留历史、允许 `supersedes`、用 `last_confirmed_at` 判断时效
- R4.3 合并：精确重复自动归档；相似度 >0.9 且边界兼容生成合并候选（低风险可自动归档）；相似但 applicability 冲突**不合并**，改标 `conflicts_with`
- R4.4 保护：`user_owned` / `suppressed` / `disabled` 永不被模型改动，只有 `actor='human'` 可恢复

### R5 记忆分型与召回机制改造

- R5.1 **记忆分型由 `role` 决定**：`observation`=情节 / `claim`·`hypothesis`=语义 / `procedure`=程序 / `preference`=偏好。**`kind` 只表产物形态，不表记忆类型**
- R5.2 **规则保留来源 project**：所有条目都保留 `Provenance.projects`；`project` 不参与默认过滤，只作 `explanation` 溯源展示与弱排序加分。适用范围由 `ApplicabilityScope` 表达，访问限制由 `AccessPolicy` 表达
- R5.3 三层抽象（原则 / 模式 / 实例）**由 `role` + `ApplicabilityScope` 推导，不落库**
- R5.4 **写入规则：模型提炼时区分事实、归纳和程序，并为归纳填写适用条件与反例**。文本是否出现项目名不单独决定记忆类型
- R5.5 **边界三分（4 类 3 职责）**：`MemorySpace`（**owner + privacy**，主体级，硬过滤）/ `AccessPolicy`（visibility + allowed_projects，项目级，硬过滤）/ `Provenance`（来源 + 软加分）/ `ApplicabilityScope`（软排序）。`project` 移出默认过滤路径但保留来源
  - ⚠ `MemorySpace.privacy`（`shared|private`）与 `AccessPolicy.visibility`（`owner|selected_projects|shared`）是**不同字段**，不存在 `access.visibility='private'`
- R5.6 保留显式 `AccessPolicy.visibility='selected_projects'` 例外，并独立保存 `allowed_projects`，处理确实不能跨项目的内容（含内部 URL、客户名、凭据的条目）
- R5.7 当前项目技术栈自动检测（`package.json` / `pyproject.toml` / lockfile / import 扫描），不需人工标注
- R5.8 召回从二值 filter 改为五维加权打分（匹配 / 证据 / 时效 / 采用反馈 / 文本）+ 冲突惩罚，取 top-K。**同项目加分，跨项目不删除**
- R5.9 **联想召回**（零新依赖）：沿 `memory_relations` 的正向关系展开 1 跳（冲突类关系不展开）；**不使用 `tool_returned` 共现自动建边**
- R5.10 后续只有「共同 applied + 关联同一 validated 结果 + 多个独立任务重复出现」才能生成待复核的关联候选；候选不自动生效
- R5.11 召回结果的 `explanation` 必须回答四件事：为什么命中、**来源与迁移理由**、适用边界、风险提示
- R5.12 跨项目条目必须显示来源项目/会话，并说明「为什么在当前上下文也成立」；仅凭文本相关时显式降级为「谨慎采用」
- R5.13 三态 `unknown` 条件必须出现在召回结果里（让模型知道自己不知道什么）
- R5.14 召回结果内部冲突必须显式呈现并按 `applicability` 分流；需要复核时指向人工纠正入口（可选旁路，非阻塞）

### R6 存量兼容

- R6.1 现有 103 条 knowledge + 103 条 rule **不删除、改标签、保留来源**；迁移产出报告，先备份、可回滚，不依赖人工审批
- R6.2 MemoryStore schema 变更必须显式迁移（沿用 `initialize` 既有约束），普通打开不自动迁移；迁移前在线备份、支持 dry-run、可回滚
- R6.3 每期可独立回滚；保留老的二值过滤作为逃生舱

## Constraints

- 本地优先，不上传完整会话
- **`memory.db` 是长期权威数据源**；Markdown 是用户可读导出与显式纠正入口；FTS 与统计投影可重建
- **自动沉淀、召回和有依据的修正是主流程**；人工阅读、修改、抑制和删除是可选旁路。后台不得覆盖人工版本、不得扩大访问范围、不得伪造证据
- 增量处理，不重复编译整个知识库
- **宁可少而真，不要多而假**：`verified` 门槛高、数据稀是故意的，README 需明示这是特性不是缺陷
- 不引入新的运行时依赖；打分权重是可调常量，**不引入 ML**

## Non-goals

明确不做，避免范围蔓延：

- **独立向量数据库** — 它表达不了「矛盾」（两条相反结论向量极接近），而显式冲突是本产品核心能力；且 embedding 模型要么占几百 MB 本地资源、要么走 API 违背本地优先。中期若语义召回确成瓶颈，上 `sqlite-vec`（SQLite 扩展，向量存同一个 `.db`）
- **知识图谱 / 实体抽取**（Graphiti 式） — 抽取 + 消歧成本高，脏图比没图更糟。个人规模未证明必要
- **自我模型**（失败模式 / 判断习惯 / 思维盲点） — **延后**。它是「另一个你」与「另一个 wiki」的分水岭，但依赖负反馈数据积累，本期不做。见设计文档 §2.2 演进路径
- 云端、团队共享 — 本地优先是前提（未来上云是**同步**不是换供应商库）
- 原生 IDE 集成 — MCP 是唯一注入通道
- 多 agent 记忆编排 — 不在赛道
- 自动执行危险操作 — 记忆可以自动沉淀和修正，但不能自动执行高风险动作

## Acceptance Criteria

### AC-P0 止血（独立可交付）

- [ ] `context_forge_match_rules` 不再无上限返回：默认 top-K 且按相关度排序，无上限灌入（真实峰值 28 条 / 构造场景 78 条）不再出现
- [ ] `context_forge_search` 支持 project 过滤，搜索结果不跨项目稀释
- [ ] 逃生舱：`--legacy-filter` 可恢复旧的二值过滤行为
- [ ] 回归测试覆盖上述三条

> **⚠ AC-P0#1 的兑现前置（2026-09-22 复核实测）**
> 排序键 `paths_hit → confidence → status → mtime → rule_id` 的**代码侧已达成**，且守卫经 4 组变异测试验证有效。
> 但在**真实 vault** 上两个前键都是常量：`confidence` 0/105 条有该字段（全部回退 `medium`）；`paths` 105/105 非空、无项目级规则，故结果集内 `paths_hit` 恒为 True。
> **净效果：生产数据上 5 元键退化为 `mtime desc`。**「按相关度排序」目前**不可观测**。
> 这是**数据缺口不是代码缺陷**。兑现前置是 **P1 的「存量 206 条改标签」必须补 `confidence`**（并把部分条目收成项目级规则以拉开 `paths_hit`）。**不得**通过调键序或加权重绕过。

### AC-P1 模型（独立可交付）

- [ ] **记忆分型落地**：`role` 决定情节/语义/程序/偏好；三层抽象由 `role` + `ApplicabilityScope` **推导可得且未落库**
- [ ] **边界三分落地**：`MemorySpace`（owner）/ `AccessPolicy`（visibility + allowed_projects）/ `Provenance`（projects/sessions/paths）/ `ApplicabilityScope`（technologies/domains/constraints/exclusions/visibility）
- [ ] **规则保留来源**：所有条目的 `Provenance.projects` 都保留；`project` 不参与默认过滤，只作来源展示与弱排序（有测试守卫）
- [ ] **`project` 已移出默认过滤路径**：迁移后只影响来源展示与排序加分；显式访问限制由 `AccessPolicy` 决定（有测试守卫）
- [ ] `access.visibility='selected_projects'` 与 `space.privacy='private'` 硬过滤均生效（**两个不同字段**，有测试守卫）
- [ ] `SCHEMA_VERSION` 递增，显式迁移，拒绝隐式迁移；**无 `generality` 列**
- [ ] 存量 206 条完成改标签（103 条 rule 保留 project 来源、移出默认过滤路径），迁移报告产出且可回滚
- [ ] 模型提炼 prompt 的判据：区分事实/归纳/程序并填写适用条件与反例，不依赖是否出现项目名（R5.4）

### AC-P2 召回（独立可交付）

- [ ] 五维打分召回实现；三层抽象决定候选池准入
- [ ] `match_score` = 适用条件 + 来源加分，**来源是加分项不是准入门槛**
- [ ] **一跳关系展开**：沿 `memory_relations` 正向关系展开 1 跳；`conflicts_with`/`contradicts`/`exception_to` **不展开**
- [ ] 关系展开结果只作解释与冲突上下文，不改变种子集排序
- [ ] **不使用 `tool_returned` 共现自动建边**（有测试守卫）；后续关联候选仅来自共同 `applied` + 同一 `validated` 结果
- [ ] 同一 query 的 top-K 结果可解释、可复现
- [ ] `explanation` 返回「为什么命中 / **来源与迁移理由** / 适用边界 / 风险提示」四段，**必含 `unknown` 条件**
- [ ] 跨项目条目显示来源与迁移理由；仅文本相关时显式降级警告
- [ ] 冲突检测与分流：`conflicts_with` / `contradicts` 命中时显式标注，不静默丢弃
- [ ] 技术栈自动检测产出 `technologies` / `domains`
- [ ] **§4.6 实验闸门**：跑完 G1–G3 的 S1（project 硬过滤）vs S2（打分召回）对照；**错误迁移率不得超过实验前写明的阈值**

### AC-P3 闭环（独立可交付）

- [ ] 曝光链六阶段自动记录；只有可归因结果才反向更新权重
- [ ] **曝光 ≠ 强化**：`presented`/`referenced` 只计数不进权重（有测试守卫）
- [ ] **负反馈进权重**：撤销/纠正/`harmful`/`irrelevant` 产生负权；撤销不自动转 `contradicted`
- [ ] 证据强化 / 削弱 / 推翻 / 扩展四种方向分派正确，推翻不删旧记忆
- [ ] 三张报表（应用效果 / 异常审计 / 整理候选）可导出
- [ ] 断言：曝光事件不改写 `evidence_level` 到 `verified`（有测试守卫）

### AC-P4 卫生（独立可交付）

- [ ] 自动巩固（新建高层记忆、保留全部来源、`derived_from` 指向全部来源实例、可撤销）
- [ ] 衰减与强化生效
- [ ] 合并候选与 `conflicts_with` 分流
- [ ] 保护约束测试覆盖（`user_owned` / `suppressed` / `disabled` 模型不可改）

### AC-横切

- [ ] 每期可独立回滚
- [ ] 不新增运行时依赖
- [ ] Markdown 导出可读性不退化（人工抽查）；`memory.db` 权威数据不因导出改变

## Notes

- 完整技术方案见设计文档，本 PRD 不重复其内容
- **本 PRD 的边界模型（R5.5/R5.6）与反馈校准（R2.2/R2.3）来自调研** [docs/research/cross-project-memory-competitor-analysis.md](../../docs/research/cross-project-memory-competitor-analysis.md)，不是拍脑袋
- **新增最大风险：错误迁移**。`project` 移出默认过滤路径后，别的项目的结论会被召回进来。对策见 R5.11/R5.12（迁移理由 + 降级警告）、R5.6（`AccessPolicy` 显式授权）、AC-P2 的实验闸门。**这是上线前必须验的，不是上线后补的**
- **对母文档有两条修订**（「Markdown 是事实源」→「memory.db 权威」；「人类确认是状态转换」→「自动为主、人工旁路」），见技术文档 §2.3 的修订声明。母文档尚未回写，属已知冲突，冲突时以技术文档为准。
- P0–P4 每期独立可交付；若实现期发现某期过大，可拆为子任务
- 当前与 `.trellis/tasks/09-21-forge-recall-rank-and-merge` 有重叠（召回排序部分）；后者可视为 P0 的子集，实现时应合并处理或在完成本任务 P0 后归档
