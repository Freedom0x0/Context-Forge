# Context Forge 技术介绍

> 本文描述**当前代码**（`memory.db` schema v11）的实际形态，面向第一次读这个仓库的人。
> 设计意图、被否决的路线和保证的边界在 `docs/adr/` 与 `docs/technical-design-*.md`；本文不重复那段论证过程，只讲系统今天长什么样、为什么长这样。

---

## 1. 定位与设计红线

Context Forge 是一个 **local-first 的 Agent 长期记忆基础设施**。它不做两件流行的事：不训练模型参数，也不把历史对话无差别塞进上下文。它做的是把经历、来源、适用条件、冲突和结果存进一个可追溯的 SQLite 记忆库，在需要时召回，并允许人来纠正。

一句话目标：**从经历中形成有依据的理解，在新任务中应用，并根据结果修正。**

下面几条不是口号，是被写进契约、有测试和变异守着的红线。改代码时如果和它们冲突，先想清楚再动：

1. **观察和解释分开**。模型的归纳必须携带来源（`sources`）；没有证据时证据等级停在 `unknown`，不猜。证据等级为 `verified` 的草稿必须含一条保留的 **tool** 证据——模型自己的复述不算验证（`memory_models.py:141`）。
2. **未知不落成对错**。适用条件是三态 `true/false/unknown`；`unknown` 既不当作满足，也不当作违反，而是作为"风险"出现在解释里（`condition_result`）。
3. **检索不等于采用**。被召回、被返回、被呈现都**不**提高记忆的可信度，只有可归因的"应用/验证"才进权重（`freshness_score` 只吃 `applied` 计数）。
4. **写路径不得押在概率事件上**。嵌入失败不阻断写入（`embedding=NULL` 记一行失败计数）；宿主 liveness 只做**抑制**、绝不当前置条件——否则文件证据分支会在宿主未登记时结构上不可达。
5. **人工优先，且是不可逆的方向**。后台任务不得静默复活或覆盖人工的判定；人工的 `conflicted` 对非人写入是**吸收态**（见 §6）。

---

## 2. 总体架构与数据流

三块持久化，职责严格分开：

| 存储 | 角色 | 权威性 |
|---|---|---|
| `memory.db` | 长期记忆（版本、来源、关系、事件、投影） | **唯一权威** |
| `queue.db` | 运行队列：会话、情节、作业、判决、台账 | 运行状态，可重建 |
| Markdown `vault/` | 人类可读的视图与兼容层 | **导出视图，不是完整备份** |

```text
Claude Code hooks (SessionStart/PreToolUse/Stop/…)     transcript *.jsonl
              |                                                |
              v                                                v
        queue.db  ── liveness / active-tool 证据 ──>  watcher（常驻，按库加锁）
              |                                       ├─ 扫 transcript、按静默切情节
              |                                       ├─ 推断式收尾（仅抑制语义）
              v                                       └─ 每日维护（retention/signals/hygiene）
        worker（认领 job → 调模型）
              |
              v
   SessionArtifacts ──> Episode 收尾 ──> FinalizationService（模型调用在事务外）
              |
              v
      MemoryLifecycle.publish / human revise
              |
              v
        memory.db (SQLite + FTS5)  ──投影──>  vault/*.md
           |        |         |
           v        v         v
        Recall   Feedback   Merge / 报表
           |
           v
      MCP → 任意会说 MCP 的宿主按需读取
```

关键约束：

- **模型调用永远在数据库事务之外**（`_put` 不调 gateway；`finalization` 先取数再落库）。
- **确定性触发**。写记忆由"文件静默 N 秒 + 情节边界"触发，不由模型意愿触发；风险判定用固定正则，不用模型自评（`risk.py`）。
- **两个进程**：`watcher` 负责"发现与维护"，`worker` 负责"理解与写入"。都不需要人盯着。

---

## 3. 领域模型

一条记忆的当前快照是 `MemoryDraft`（`memory_models.py:95`），落库时随版本只追加。字段可按四组理解：

### 3.1 这是什么

- `kind`：`knowledge` / `rule` / `review`。**知识宽松，规则严格**——只有规则会被执行闸门看住（§7）。
- `role`：`observation`（情节实例）/ `claim` / `hypothesis` / `preference` / `procedure`。
- 三层抽象 `principle` / `pattern` / `instance` **不落库**，由 `role` + `ApplicabilityScope` 推导（`derive_level`）。
- `status`：`candidate` / `active` / `disabled` / `archived` / `conflicted` / `stale`。
- `evidence_level`：`unknown` / `unverified` / `supported` / `verified` / `contradicted`。

### 3.2 边界四分，各管一件事

这是全库最容易搞混的地方——**四个对象故意分开**，因为它们回答的是不同问题：

| 对象 | 回答的问题 | 参与 |
|---|---|---|
| `MemorySpace`（`owner`, `privacy`） | 属于谁的空间 | 边界消元 |
| `AccessPolicy`（`visibility`） | 主体级可见性（`owner`/`shared`） | 边界消元 |
| `Provenance`（`projects`, `sessions`, `paths`, `host`） | 从哪来 | **只加排序分，绝不参与可见性** |
| `ApplicabilityScope`（`technologies`, `domains`, `constraints`, `exclusions`） | 何时适用 | 排序 + 准入 |

`privacy` 和 `visibility` 是两个不同枚举，不可互换。`scope.visibility`（`global/scoped`）是**适用条件**，`access.visibility`（`owner/shared`）是**可见性**，名字像但语义无关。项目级授权（原 `selected_projects` + `allowed_projects`）已于 2026-10-19 移除。

`Applicability` 是三元组 `(condition, result, evidence_ids)`，是一条条可判定的条件及其结论。`exclusions` 是反例：只有被上下文**判定成立**时才排除，判不定的记为风险、不静默过滤（`exclusion_outcome`）。

### 3.3 证据与来源

`Source.identity` 标识**不可变的证据**：同一 identity 配不同内容直接报 `SourceConflict`；重复引用同一 identity 不算独立证据。`evidence_level` 与来源强绑定（§1 红线 1）。

### 3.4 关系

9 种，分三类（`memory_models.py:172`）：

- 扩展类 `derived_from` / `supports` / `refines` / `overlaps`（`EXPANSION_KINDS`，召回时做一跳关联）
- 冲突类 `conflicts_with` / `contradicts` / `exception_to`（`CONFLICT_KINDS`，进排序惩罚与分流提示）
- 治理类 `duplicate_of` / `supersedes`

`supersedes` 只把旧理解置 `stale` 并记一条版本化的关系，**不删历史**。

---

## 4. 写入通路：从经历到长期记忆

### 4.1 触发与切分

`watcher.scan_once` 只处理"已静默超过 `DEFAULT_SETTLE` 且基线之后有新内容"的 transcript；基线机制保证**新装不会回放历史**。每个 session 按行偏移切成分段（Episode）；`SESSION_END` 或推断式收尾把情节"闭环"，交给 `FinalizationService`。

`advance_inferred` 的语义是：**只有在明确观察到"进程已退且无活跃工具"时才抑制收尾**；未知一律落回"静默 + 文件证据"判定。liveness 是抑制器，不是前置条件。

### 4.2 收尾 = 可恢复的三段式

`FinalizationService.process`（`finalization.py:30`）刻意做成崩溃可重入：

1. **extract**：取样例 → 调模型 → 校验输出（来源、关系目标、脱敏）。
2. **freeze**：把每个候选及其 `request_hash` 冻结成 operation，先落库。
3. **commit**：逐条落库，`request_hash` 对不上就抛；幂等键 = operation key，重跑不会重复发布。

### 4.3 模型只能"提案"，不能"结算"

模型写入的 `actor='model'`；它提案的草稿默认是 `candidate`，`publish` 会把**新**记忆升成 `active`（无人值守自动沉淀的前提），但**碰不到被保护的记忆**。

存储层只有一个收口——`MemoryStore._put`（`memory.py:1469`）——所有写入（public `put`、`record_feedback`、import、merge/finalization/publish）都过它。它守着三件事：

- **保护集**：`suppressed` / `user_owned` / `disabled` 的非人写入 → `ProtectedMemory`。
- **`conflicted` 吸收态**：非人写入**不得把记录移出** `conflicted`（"移出"而非"触及"——追加证据这种状态不变的写入照样放行）。
- **访问只收窄**：非人写入不得放宽 `access`（`access_broader`）——扩大可见性是人才能做的动作。

因为只有一个收口，`publish` 和 `finalization` 两条入口一次覆盖，不留第二条缝。

### 4.4 无人值守的批处理一律"跳过"，不"抛异常"

`worker.publish_session` 在 `set_lines_processed` **之前**跑，且批次是生成器。批里抛异常会导致同一片重试、并中断后面所有候选（历史上就是洪泛的放大器）。所以 `publish` / `MemoryMerge.apply_exact` / `Finalization` 遇到不能动的记录时**返回原记录或标记结束**，绝不抛。

### 4.5 幂等与去重

- 幂等键按**候选身份**推导（`hygiene:exact:{a}:{b}@{version}`、`{request_id}:{stage}:{id}:{version}`），不按日期或运行序号，否则同一对会被每天重复处理。
- 同 id 换内容被拒；待批句柄、证据 identity 的冲突都有各自的错误类型。

---

## 5. 召回机制

召回是"三通道 → 一条统一管线"，没有第二条打分路径（`memory.py:1714`）。

### 5.1 三通道

1. **BM25 严格 AND**（`bm25`）：全部词都要命中。单汉字查询走 `instr()` 子串路。
2. **关键词放宽**（`widened`）：AND 命中不足时，先把"库里根本不存在的词"免费丢掉（丢它不损失任何命中），仍不足再降级为**词并集**（命中任一词即可，结果数封顶 `WIDEN_RESULT_CAP=100`）。放宽由**允许的算子**界定，不由能丢几个词界定。
3. **语义 top-K**（`semantic`，可选）：本地 `all-MiniLM-L6-v2` 向量，原始余弦（0–1）。`embedding_model` 与当前 embedder 身份不符的行被跳过（模型漂移不自动重算）。

### 5.2 统一管线

```
并集 → 三边界消元 → 候选池准入 → 五维打分 + 冲突惩罚 → 解释 → 字节预算
```

- **三边界消元**（`boundary_visible`）：`owner` 不匹配、`private` 跨主体、`exclusions` 判定成立——各自独立，只做**排除**。
- **准入**（`admitted`）：按推导层级放行（principle 恒放；pattern 需命中技术/领域/约束；instance 需来源项目或路径命中）。这是"是否进候选池"，**不是排除**，两阶段不合并。
- **五维打分**（`relevance`）：适用条件 + 来源加分、证据等级、新鲜度、反馈权重、文本分；再减冲突惩罚。权重见 `ScoreWeights`（evidence 1.2 最重）。
- **`text` = `max(词法, 语义)`**：词法分只在**词法排名列**上做 min-max（把语义行并进去会污染它），语义侧是原始余弦。没有 embedder 时合并列表就等于词法列表，行为不变。

### 5.3 两个刻意的"可见性"

- **语义降级外显**：配了 embedder 但查询期编码失败时，仍返回通道 1+2，**每条结果的解释里带 `语义通道未启用（…）`**，不静默吞掉。没配 embedder 时不留标记（那是正常词法模式）。
- **通道是结构化事实**：`ScoreBreakdown.channel` ∈ {`bm25`,`widened`,`semantic`}，被写进 `retrieval_events.explanation_json`，由 `MemoryStore.channel_mix()` 计数、`EffectivenessReport.channels` 暴露。**只给绝对计数，不给比率**。

### 5.4 排序稳定

排序键是 `(-total, id)`，快照/查询/参数相同则顺序稳定。`legacy_filter=True` 是回退闸门，只回退准入与五维打分，**不回退**边界修正与三态反例——那些是正确性修复。

---

## 6. 闭环：从被召回到被修正

### 6.1 六个阶段分开记

被召回的记忆会经历六个阶段，**分阶段落事件**：`retrieved` / `tool_returned` / `presented` / `referenced` / `applied` / `validated`。前三个由召回路径自己写；宿主回话只能报它**做了什么**（`referenced`/`applied`/`validated`）——`MARKABLE_STAGES` 之外的上报被拒。

曝光事件**不更新**证据、价值或版本。这是"检索≠采用"的机械保证。

### 6.2 反馈权重

可分阶段的信号折算成权重（`WEIGHT_TABLE`）：`applied +0.3`、`validated +0.5`、`irrelevant −0.2`、`revoked −0.8`、`harmful −1.0`；曝光类**没有条目**（故意的）。长期不用会轻微衰减（90 天 `−0.1`）。

### 6.3 人工否决是吸收态

人工给一条活跃记忆打 `harmful`，它翻成 `conflicted`（待复核）。**从这一刻起，非人写入不能再把它搬出去**——模型的 `publish`、合并、收尾都只能跳过或保持。这条以前是漏洞：保护集漏了 `conflicted`，模型的下一次提案会静默抹掉人的反对。

### 6.4 效果报表（只读）

`MemoryEffectiveness` 产出三张报表：应用归因、异常候选（`verified` 却从未 `applied`——"待复核"，绝不等于"幻觉"）、整理候选（长期未用）。另有 `signals`（聚合覆盖度）、`buckets`（分桶）、`verdicts`（人工判决计数）、`host_health`（宿主收集健康）、`channels`（各通道召回计数）。**全部计数，不给百分比**——小样本不得被折成比率。

---

## 7. 治理与安全闸门

### 7.1 风险判定是确定性的

`risk.py` 用固定正则判 `high`（破坏性命令、生产环境词 + 破坏性动词），**只对 `rule` 生效**——知识是信息，误标它会延迟错误的东西。模型自报的 `risk` 字段不采信（`MemoryDraft` 有校验器把它降级为 `normal`）。

### 7.2 执行闸门，不是保密闸门

高危规则要两级：

- **沉淀闸门**：高危规则停在 `candidate`，召回本就看不见它；同时为**这一版**发一个待批句柄。
- **召回闸门**：已经发布过的高危规则被召回时**扣掉正文**——`body` 为 `null`，`applicability`/`sources`/解释一起不返回（半条破坏性命令也是线索）。

批准必须回读挑战码、在终端里敲（管道喂进来的答案一律拒），句柄是**服务端随机**的（模型算不出第二个）、10 分钟有效、一次性、换版即作废，批准进台账供 `forge doctor` 审计。这条挡的是"没被人读过就执行"，**不挡"在本机读"**——理由与边界见 `docs/adr/0001-withhold-unapproved-high-risk-rules.md`。

### 7.3 出境面

正文是否离开本机由 `model_provider` 决定：`remote` 才把**脱敏后**的正文发给配置 endpoint，`none`/`offline`/`fake` 不发，`local` 只连本机 Ollama。语义向量用独立的本地模型，不调远程 embedding。正则脱敏是追赶战（曾漏过 JSON 引号、裸 key、裸 Bearer），**不当强保证用**。

### 7.4 宿主接入

**读侧宿主无关**：任何会说 MCP 的宿主读的是同一个 `memory.db`。**写侧目前只认 Claude Code**（watcher 读 transcript + hook），所以共享是单向的。

MCP 注册**委托宿主自己的 `claude mcp add/remove`**，不手写宿主配置文件（曾手写 `settings.json` 导致 server 根本没注册、而 `doctor` 仍报 OK）。存在性与意图判定由我们读配置文件得出，**不解析宿主输出**。MCP 共 14 个工具：8 个 `context_forge_memory_*`（search/get/confirm/mark/apply/report/effectiveness/hygiene）+ 6 个旧 vault 工具。

---

## 8. 已知边界与后续方向

诚实列出来，避免把"没做"读成"已保证"：

**数据面**
- 语义通道**未校准**：`text` 用的是原始余弦，没有归一化器。校准等评测数据。
- 嵌入是**派生缓存**：`snapshot_json` 才是权威；模型漂移只报告 + 提示重算，不自动重算。
- `check_mcp` 只验"名字已注册"，**不验条目是否正确/可连接**；仓库移动后到下次 `mcp-install` 之间，漂移条目仍会报 OK（`host-integration.md` 记为开放项）。

**路线图缺口（尚未排期）**
- 无 persona / 身份 / 关系存储。
- 无用户模型：`DEFAULT_OWNER = 'local'` 是常量，owner 这一维判定钉死在它上面。
- 纯被动：没有推送/通知通路，只有宿主来调。
- "根据结果修正"未自动打标：记忆反馈只有 MCP 工具在写，钩子不碰。

**交付状态**
- 宿主端到端检索验收（证明"宿主真在需要时检索"，而非只证明 MCP 可调用）与模型质量收益评估：**明确延期**，不以 plumbing 测试冒充收益。
- 通用宿主适配器（`hooks/generic.py`）：保留在库，下个迭代再认领。

**质量门槛**
- 自动整理的两档（归档完全重复、标注矛盾）可无人值守；**近似重复与"很久没用"永远停在报告里等人判**——相似不等于相同，没人用不等于错。
- 每个守卫都要有**能变红**的变异验证："一个你无法让它失败的闸门，就不是闸门。"

---

## 延伸阅读

- `README.md` — 是什么、快速开始。
- `docs/context-forge-usage.md` — 日常命令与 Obsidian 工作流。
- `docs/memory-store.md` — 存储 API、迁移与恢复。
- `docs/technical-design-context-forge.md`、`docs/technical-design-falsifiable-memory.md` — 设计论证。
- `docs/adr/` — 一条条"为什么不选另一条路"。
- `.trellis/spec/backend/` — 各层的实现契约（改代码前必读）。
