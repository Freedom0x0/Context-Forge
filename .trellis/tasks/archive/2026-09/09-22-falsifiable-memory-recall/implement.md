# 执行计划：可证伪记忆系统与智能召回

按 P0→P4 分期。**每期独立可交付、独立可回滚**，一期做完走一次评审门再开下一期。
若某期实现中发现体量过大，拆子任务，不要在一期里堆。

上下文顺序：`implement.jsonl` → `prd.md` → `design.md` → 完整设计文档 `docs/technical-design-falsifiable-memory.md`。

---

## 阶段 0：开工前置（一次）

- [x] 0.1 与 `.trellis/tasks/09-21-forge-recall-rank-and-merge` 的关系 —— **决议（2026-09-23）**
  - design.md §7 的方案 A（并入后归档）**只采纳前半，暂不归档**。09-21 的 `implement.md` 全勾 [4/4]，其 PRD 的绝大多数验收已被本任务 P0–P3 覆盖（证据分级 / 曝光≠强化 / 归因闸门 / 三态 unknown / 冲突保留 / 迁移保护 / 幂等与备份恢复）。
  - **但有一条未被覆盖，故不得归档**：
    > 09-21 PRD：「**端到端证明宿主在需要时检索；只证明 MCP 可调用不算完成**」
    本任务至今只证明了 MCP 工具可调用、返回结构正确（`test_memory_recall.py` 的 `mcp_tools` 接线测试），**没有证明真实会话里 Claude Code 会在需要时去调它**。归档 09-21 会把这条丢掉。
  - **留待收尾处理**：要么把该验收折进本任务的收尾复核，要么原样留给 09-21。**不要**在没有 e2e 证据的情况下把它勾掉。
  - 09-21 另有一项独立待办：「真实数据库测试残留删除独立授权」——本任务不碰。
- [x] 0.2 基线已跑（2026-09-22）：`pytest` **98 passed**；P0 后 106 → P1 后 121 → P2 后 139 → P3 后 **155 passed**
- [x] 0.3 基线已记录：开工时实测 **104 knowledge / 103 rule**；P1 迁移前复点 **105 / 105 = 210**（文档里的 103/103 与 206 均已过期，以实测为准）

**回滚点 0**：无代码变更，可直接弃。

---

## 阶段 P0：止血（最小改动，立刻可交付）

> 目标：修掉「78 条规则全文灌入上下文」和「搜索跨项目稀释」两个真实 bug。不改数据结构。

### 实现清单

- [x] P0.1 `rules.py` — `RuleMatcher.match()` 加 `limit: int = 8`、`legacy_filter: bool = False`
  - 排序键（P0 返工后）：**`paths` 命中 `changed_path` → `confidence` → `status: enabled` → `mtime` 降序 → `rule_id`**
    - 首位是**相关度**（路径命中），第二位是**证据强度**（`confidence`）；`mtime` 只作同分打破，**不得冒充相关度**
    - 缺失 `confidence` 视同 `medium`（领域默认），**不低于**显式 `low`
  - `legacy_filter=True` 恢复旧行为（无排序、无上限）
- [x] P0.1b **两个根因 bug 一并修**（否则排序键是装饰）：
  - 旧 `match()` 在给定 `changed_path` 时把**无 `paths` 的项目级规则整条丢掉** → `paths_hit` 在结果集内恒为常量。改为：无 `paths` 的项目级规则恒进候选集，命中 `changed_path` 的排其前；`paths` 明确但未命中的仍硬排除
  - 旧 `_frontmatter` 把空 `paths: ` 变成 `[""]` → `glob_match` 恒 False → **自动规则完全不可见**（给不给 `changed_path` 都一样）。改为 `_path_patterns()` 归一化去空，空 `paths` 即项目级规则
- [x] P0.2 `index.py` — `search()` 加 `project: str | None = None` 过滤参数（`None` = 保留旧语义不过滤）
- [~] ~~P0.3 `context.py` — `build_context()` 加 `rule_limit: int = 8`，`Matched rules` 段落受约束~~  **作废：载体 `context.py` 已随 L8 删除**
- [x] P0.4 `mcp_server.py` — `context_forge_match_rules` / `context_forge_search` 暴露对应参数
- [x] P0.5 `cli.py` — 加 `--legacy-filter`、`--rule-limit` flag，默认走新行为

### 测试（每个非平凡改动留一条最小可跑校验）

- [x] P0.t1 `test_rules_limit` — 造 78 条同 project 规则，`match()` 默认返回 ≤8 且有序
- [x] P0.t2 `test_rules_legacy_filter` — `legacy_filter=True` 返回全部 78 条（逃生舱可用）
- [x] P0.t3 `test_search_project_filter` — 混入两个 project 的同关键词条目，过滤后只留目标 project
- [~] ~~P0.t4 `test_context_rule_limit` — `build_context` 的 Matched rules 段落不超过 `rule_limit`~~  **作废：载体 `context.py` 已随 L8 删除**

### 验证命令

```bash
python -m pytest tests/ -q
python -c "from context_forge.mcp_server import create_server; \
    print(sorted(create_server()._tool_manager._tools))"
python -m context_forge.cli status --json
```

### 评审门 P0

- [x] 78 条全文灌入的场景已消失（构造数据复现一次并确认）
- [x] 逃生舱 `--legacy-filter` 验证可用
- [x] AC-P0 四条全勾

**回滚点 P0**：`--legacy-filter` / `--rule-limit 0` 恢复旧行为；或 revert 单个 PR。

---

## 阶段 P1：模型（记忆分型 + 边界三分）

> 目标：记忆能表达「类型」（情节/语义/程序/偏好），并把「安全边界 / 来源上下文 / 适用条件」拆开。存量数据改标签，不删除。

### P1.0 前置（来自 P0 放行条件，**必须先做**）

- [x] P1.0a **补 `confidence` 标签是 P0 排序收益的兑现前置**。真实 vault 105 条规则全部无 `confidence` 字段（全回退 `medium`），且 105/105 都有非空 `paths`、无项目级规则 → 排序键的前两元在生产上都是常量，**实际次序是 `mtime desc`**。P1 改标签时**必须**：
  1. 给存量规则补 `confidence`（依据证据来源强度，不是拍脑袋）
  2. 把部分条目**收成项目级规则**（清空 `paths`）以拉开 `paths_hit` 分层
  3. **禁止**通过调键序或加权重绕过——那是掩盖数据缺口
- [x] P1.0b **统一 `gateways.py` 的 `confidence` 语义**：三个 `compile_rule` stub 用字段默认 `medium`（非模型来源），而 `OfflineGateway` 给 knowledge 标 `confidence="low"`、其派生 rule 却得 `medium`（**子比父更可信的倒挂**）。P1 一并处理。
- [x] P1.0c **数量以实测为准**：2026-09-22 穷尽扫描为 **105 knowledge + 105 rule = 210**，且持续增长（watcher 在工作）。迁移前重新点数，勿沿用文档里的 206。

### 实现清单

- [x] P1.1 `memory_models.py` — **不加 `generality` 字段**；新增纯函数 `derive_level(draft) -> 'principle'|'pattern'|'instance'`，由 `role` + `ApplicabilityScope` 推导
- [x] P1.2 `memory_models.py` — **边界拆分（4 类 3 职责）**：`MemorySpace`（**owner + privacy**，主体级）/ `AccessPolicy`（visibility + allowed_projects，项目级）/ `Provenance`（projects/sessions/paths，来源）/ `ApplicabilityScope`（technologies/domains/constraints/exclusions/visibility，适用）
  - **`space.privacy` 与 `access.visibility` 是不同字段**，枚举也不同（`shared|private` vs `owner|selected_projects|shared`），不存在 `access.visibility='private'`
- [x] P1.3 `memory.py` — **`Provenance` 全类型保留**：`observation` 必须填 `projects`/`sessions`（参与**准入**）；`claim`/`hypothesis`/`procedure`/`preference` 同样保留来源但**不参与准入、不参与淘汰**，只作展示与弱排序
- [x] P1.4 `memory_schema.py` — `SCHEMA_VERSION` +1；新模型进入 `snapshot_json`，另建可重建投影表（`memory_access_projection` / `memory_applicability_projection`）；**无 `generality` 列**，**不为 `scope` 另立权威 JSON 列**（字段名就叫 `scope`，不是 `applicability_scope`）
- [x] P1.5 `memory.py` — `initialize`/`migrate` 显式迁移；存量三种 `Scope.kind` **+ 全量 `MemorySpace`** 映射见 design.md §2.2（逐条写测试）
- [x] P1.6 `memory.py` — **硬过滤只剩四条**：`space.owner` 不匹配 / `space.privacy='private'` 且非本 owner / `access.visibility='selected_projects'` 且当前项目不在 `access.allowed_projects` / `scope.exclusions` 命中。`project` 从默认过滤路径移除
- [x] P1.7 `tech_detect.py` — 新建，从 `package.json`/`pyproject.toml`/`requirements.txt`/`go.mod`/lockfile/import 扫描产出 `TechProfile`
- [x] P1.8 vault 迁移脚本 — **103 条 rule 保留 `project` 来源**并从默认过滤路径移出，按内容改 `role`；knowledge 按内容改 `role`；补 `technologies`/`domains`/`access_visibility`/`allowed_projects`；**`space.privacy` 与 `access.visibility` 分工**：含内部 URL/客户名/凭据的条目按需标 `space.privacy='private'`（不可跨主体）**或** `access.visibility='selected_projects'` + `allowed_projects`（限定项目）；产出**迁移报告**；先备份、支持 dry-run、可自动执行并回滚
- [x] P1.9 worker 提炼 prompt — 判据「区分事实、归纳和程序，并为归纳填写适用条件与反例」；不依赖文本是否出现项目名；所有类型都保留来源

### 测试

- [x] P1.t1 存量三种 `Scope.kind` 映射逐条断言；**全量 `MemorySpace` 映射**断言（`owner` ← 本地配置、`privacy` ← `'shared'` 默认、显式 `private` 仅由迁移报告列出）
- [x] P1.t2 `derive_level()` **覆盖全部 5 个 role、无遗漏分支**（有测试守卫）：
  - `observation` → `instance`
  - `claim` / `hypothesis` / `procedure` + scope 非空 → `pattern`
  - `claim` / `hypothesis` / `procedure` + scope 全空 → `principle`
  - `preference` **无论 scope 空否** → `principle`
- [x] P1.t3 拒绝隐式迁移：老库直接打开报 `MemoryStoreError`，只有 `initialize`/`migrate` 能改 schema
- [x] P1.t4 `tech_detect` 对 `pyproject.toml` + `package.json` 双料仓库产出正确集合
- [x] P1.t5 迁移脚本 `dry_run=True` 时不写盘
- [x] P1.t6 **核心断言 1**：`search()` 默认过滤路径中不含 `project` 字面量（访问过滤只走 `AccessPolicy`）；跨项目条目**不被过滤掉**，只降权
- [x] P1.t7 **核心断言 2**：所有 `role` 的 `Provenance.projects` 完整保留、迁移不清空；`project` 不参与默认过滤
- [x] P1.t8 `access.visibility='selected_projects'` 硬过滤生效；`space.privacy='private'` 跨 owner 不可见（两者是**不同字段**，不得混用）

### 验证命令

```bash
python -m pytest tests/ -q
python -m context_forge.cli import-transcript --dry-run   # 或等价的迁移 dry-run 入口
python -c "from context_forge.tech_detect import detect; from pathlib import Path; print(detect(Path('.')))"
python -c "from context_forge.memory_models import derive_level; ..."  # 推导纯函数抽查
```

### 评审门 P1

- [x] 迁移报告产出；迁移可自动执行、可回滚；来源 `project` 未被清空
- [x] 老库可读、新库可写、拒绝隐式迁移
- [x] **P1.t6 / P1.t7 绿**——这两条是本次改造的意义所在，不许跳过
- [x] AC-P1 八条全勾

**回滚点 P1**：新列有默认值 → 不迁移新库即回到旧行为；vault frontmatter 新键可选 → 忽略即可。

---

## 阶段 P2：召回（五维打分 + 迁移理由 + 冲突显式）

> 目标：filter → score，召回结果可解释、可复现、冲突可见、**跨项目来源与迁移理由可见**。

### 实现清单

- [x] P2.1 `RetrievalContext` / `ScoreWeights` / `ScoreBreakdown`（design.md §2.3）
- [x] P2.2 `match_score` = `applicability` + `provenance_bonus`（**来源只加分不淘汰**）；再加证据/时效/反馈/文本 + 冲突惩罚
- [x] P2.3 候选池准入按 `derive_level()` 三层分层（`instance` 依赖 provenance，`pattern` 依赖 technologies/domains；**`projects` 不参与默认过滤**）
- [x] P2.4 `MemoryStore.search` 接入打分排序 + top-K；`legacy_filter=True` 保留二值过滤
- [x] P2.5 `MemoryStore.expand_relations()` — **一跳关系展开**：只走 `derived_from` / `supports` / `refines` / `overlaps` **这 4 种**；结果只作解释与冲突上下文，不改变种子集排序、不参与打分
  - **不展开**：`conflicts_with` / `contradicts` / `exception_to` / `duplicate_of`（冲突类在冲突呈现里单独处理）+ `supersedes`（版本取代，不表内容相关性）
- [x] P2.6 **不使用 `tool_returned` 共现自动建边**（有测试守卫）；后续关联候选仅来自「共同 applied + 同一 validated 结果 + 多个独立任务重复出现」，且不自动生效
- [x] P2.7 关系展开结果只作解释与冲突上下文，不进 `w4`、不改权重
- [x] P2.12 **淘汰路径与候选池准入不得混用**：淘汰仅 4 条（space.owner / space.privacy / access.visibility=selected_projects / scope.exclusions）；`instance` 的 provenance 命中是准入条件，不是淘汰条件
- [x] P2.8 `explanation` **四段式**：为什么命中 / **来源与迁移理由** / 适用边界 / 风险提示；**必含 `unknown` 条件**
- [x] P2.9 迁移理由降级规则：仅文本相关时输出「未匹配任何适用维度 — 谨慎采用」
- [x] P2.10 冲突检测：top-K 内部两两查 `conflicts_with`/`contradicts`/`exception_to`，写入双方 `explanation`，**不删任何一方**
- [x] P2.11 `mcp_server.py` — `context_forge_memory_search` 透出 `technologies`/`domains`/`legacy_filter`

### 测试

- [x] P2.t1 同一 query 两次调用 top-K 一致（可复现）
- [x] P2.t2 `derive_level()` 准入：`pattern` 条目在技术栈不命中时不得进候选池
- [x] P2.t3 `exclusions` 命中直接淘汰，优先于准入
- [x] P2.t4 `explanation` 含四段，`unknown` 条件存在时必须出现
- [x] P2.t5 跨项目条目**出现在结果中**，且第 2 段含来源项目 + 迁移理由
- [x] P2.t6 仅文本相关时迁移理由降级为「谨慎采用」
- [x] P2.t7 构造一对 `conflicts_with` 记忆，断言两条都返回且互相标注
- [x] P2.t8 `legacy_filter=True` 行为与 P0 前一致（回滚逃生舱）
- [x] P2.t9 **一跳关系展开**：`derived_from`/`supports`/`refines`/`overlaps` 一跳可达；展开结果不改变种子集顺序、不进 `w4`；`conflicts_with`/`contradicts`/`exception_to`/`duplicate_of`/`supersedes` 邻居**均不出现在展开结果**中
- [x] P2.t10 **无自动共现强化**：任意 `tool_returned` / `presented` 事件组合都不得改变召回排序或生成关联边（有测试守卫）
- [x] P2.t11 `derive_level()` 覆盖全部 5 个 role，无 ValueError/无缺失返回值；`hypothesis` 与 `claim` 同规则；`preference` 恒为 principle（有测试守卫）
- [x] P2.t12 `MemoryApplication`：`validation_source_ids ⊄ 该版本 sources` 时拒绝写入；`attribution != 'confirmed'` 时不改 `evidence_level`、不加权、不降权
- [x] P2.t13 **`w3` 复用次数口径守卫**：`freshness_score` 的复用次数取**可归因 `applied` 次数**；喂入等量 `proof_count` / `presented` / `referenced` 时**不得**产生强化（易错点：误用 `proof_count`）

### 效果评估实验（design.md §2.6，**上线闸门**）

> **⚠ 阈值已在此写死（2026-09-23，P2 实现开始前）。实现与实验完成后不得修改本节数值。**
> 口径同步 §4.6：**错误迁移率**的可测代理是「跨项目记忆被 `applied` 后被撤销/纠正的比例」——
> 在确定性构造数据里没有 applied/撤销事件，因此用**沉默错误迁移率**代替：
> top-K 中被标注为「迁移到该上下文即错」**且召回解释里没有任何适用性警示**（未匹配适用维度 / 技术栈未命中 /
> 问题域未命中 / 约束未满足 / 已知反例 / 冲突例外）的槽位占比。有警示的错误迁移是**特性**（模型据此谨慎采用），
> 无警示的才是风险。

| 编号 | 指标 | 阈值 | 性质 |
|------|------|------|------|
| **T1** | S2 **沉默错误迁移率**（G1–G3 合计） | **≤ 0.10** | **硬闸门** |
| **T2** | S2 **可检出错误的警示覆盖率**（`why_wrong` 声明了不匹配的条目，解释中必须有对应信号） | **= 1.00** | **硬闸门** |
| **T3** | S2 **原始错误迁移率**（top-K 中错误条目占比，含已警示的） | **≤ 0.30** | **硬闸门** |
| T4 | S2 决策帮助率 ≥ S1 决策帮助率 | — | 记录项 |
| T5 | S2 返回字节 ≤ 1.5 × S1 | — | 记录项 |

**通过判据 = T1 ∧ T2 ∧ T3**。超阈值 → 回 P1 补 `ApplicabilityScope` 标签，**禁止调权重硬压**（design.md §2.6）。

**已知口径限制（必须写进实验报告）**：T1/T3 的分子分母来自人工评分表（合成数据的作者判断），
不是线上 `applied → 撤销` 事件。真实的错误迁移率要等 P3 的 `MemoryApplication`（`attribution='confirmed'`）
落库后才可测；本期 T1/T3 度量的是**解释层的警示覆盖是否完备**，不是统计意义上的误用率。

**闸门判别力复核（2026-09-23，独立变异测试）**：三道硬闸门里只有两道真的有判别力。

| 闸门 | 判别力 | 证据 |
|------|--------|------|
| **T1** | ✅ **有** | 抹掉全部风险警示 → `test_recall_eval.py` RED。但**余量极薄**：地板是 1/18=0.0556（`g2-wrong-retry-budget` 结构上无法警示），阈值 0.10 ⇒ 只容忍 **0 条**额外沉默错误（2/18=0.111 即超） |
| **T2** | ✅ **有** | 需为 3 条可检出错误各发出对应警示。但样本仅 3 条，且 `why_wrong`→期望警示串的映射由实现方自己写 |
| **T3** | ❌ **无（空转）** | **T3 ≡ 0.2222 是本语料的常量**：每组标签 6 条 ≤ `limit=8`，noise 不被录取 ⇒ 18 条标签条目**必然全部返回**，其中 4 条错。分子分母都与实现无关，任何实现都得不到别的值 |

**T3 的正确读法**：它不是「错误迁移闸门」，而是「**别丢正确条目**」的代理——它只会在实现少返回正确条目时上升（丢 2 条正确 → 4/16=0.25，丢 4 条 → 4/14=0.286，丢 6 条 → 0.333 才超阈值）。**不得**把「T3 通过」读作「错误迁移率 22.2% 已被验证合格」。

**T3 要变成真闸门需要**：语料里出现**可被系统抑制**的错误条目（带 `exclusions` 或访问限制），或标签条目数 > `limit`（迫使排名决定谁被返回）。当前两者都不成立。

- [x] P2.e1 构造 G1（同技术同问题）/ G2（不同技术同模式）/ G3（表面相似约束冲突）三组跨项目任务数据
- [x] P2.e2 跑 S1（project 硬过滤）vs S2（打分召回）对照，按预设规则盲评填表
- [x] P2.e3 记录四项指标：决策帮助率 / **错误迁移率** / 首次成功率 / 额外 token·延迟
- [x] P2.e4 **判据**：S2 错误迁移率不得超过实验前写明的阈值。超阈值则回 P1 补 `ApplicabilityScope` 标签，**禁止调权重硬压**
- [x] P2.e5（**P3 接手**）把 T1/T3 换成真指标：`MemoryApplication.attribution='confirmed'` 下的 `applied → 撤销/纠正` 比例；届时 T3 需重建为有判别力的形式（见上）

### 验证命令

```bash
python -m pytest tests/ -q
python -m pytest tests/test_recall_eval.py -q   # P2.e1 的确定性构造数据
python -m context_forge.cli status --json
# 手工抽查：同一 query 的 top-8 explanation 是否四段齐、是否含 unknown、跨项目条目是否写了迁移理由
```

### 评审门 P2

- [x] top-K 可解释、可复现
- [x] 冲突场景手工跑通一次
- [x] **P2.e4 判据通过**（错误迁移率是本次改造的最大风险，阈值已事前写明）
- [x] AC-P2 十一条全勾

**回滚点 P2**：`legacy_filter=True` 回退二值过滤；或 revert PR。

---

## 阶段 P3：闭环（采用反馈 → 权重 → 三张报表）

> 目标：让应用效果可归因，异常可审计，整理有依据。**曝光不算强化。**

### 实现清单

- [x] P3.1 `memory_effectiveness.py` — `application_report` / `anomaly_candidates` / `tidy_candidates`（与 §2.1 命名一致）
- [x] P3.2 `apply_feedback_weight(record, events) -> float` — **纯函数，只算不写**；仅聚合**可归因**项（design.md §2.4）
- [x] P3.3 `memories.weight` 列（可重建的排序缓存，非权威）；`proof_count` 随 `snapshot_json` 保存，不重复建列（避免双权威）
- [x] P3.4 **负反馈进权重**：人工撤销/纠正、`outcome='harmful'`/`'irrelevant'` 产生负权（`harmful` 转 `conflicted` 待复核；撤销不自动转 `contradicted`）
- [x] P3.5 **证据方向分派**：支持→`proof_count`+1 / 削弱→缩适用面 / 推翻→`contradicts`+新记忆（不删旧）/ 扩展→`refines`·`exception_to`
- [x] P3.5b **应用结果归因 `MemoryApplication`**（模型以技术文档 §4.4 为权威，**不另立字段**）
  - 存储：新建 `memory_applications` 表，DDL 见 design.md §2.4
  - 写入校验：`validation_source_ids ⊆ 该版本 sources`，否则**拒绝写入**
  - 归因闸门：只有 `attribution='confirmed'` 才能改权重 / 升级证据 / 触发负反馈；`plausible` / `unknown` **只记录**
- [x] P3.6 `relevance()` 接入 `weight`（合并进 `w4`）
- [x] P3.7 CLI / MCP 导出三张报表（含曝光率 vs 采用率比值）

### 测试

- [x] P3.t1 权重表逐条断言：`applied` +0.3、`validated` +0.5、撤销 −0.8、`harmful` −1.0、`irrelevant` −0.2；**且撤销后 `evidence_level` 仍非 `contradicted`**
- [x] P3.t2 **硬约束 1 守卫**：曝光处理路径中不存在写 `evidence_level` 到 `verified` 的分支
- [x] P3.t3 **硬约束 2 守卫**：`presented` / `referenced` 不产生正权重（曝光≠强化）
- [x] P3.t4 证据方向分派四条路径正确；推翻场景旧记忆仍可查
- [x] P3.t5 应用效果报表数值正确（构造已知事件序列）
- [x] P3.t5b **硬约束 3 守卫**：`attribution != 'confirmed'` 时不改权重、不升 `evidence_level`、不触发负反馈、不标 `contradicted`
- [x] P3.t5c **硬约束 4 守卫**：撤销 / 纠正路径**不得**自动把 `evidence_level` 置为 `contradicted`（只记原因与负权）
- [x] P3.t5d `MemoryApplication` 写入校验：`validation_source_ids ⊄ 该版本 sources` 时拒绝写入
- [x] P3.t6 证据升级上限：无新 tool 摘录时 `evidence_level` 最高只到 `supported`，不得出现 `verified`（有测试守卫）
- [x] P3.t7 整理候选：长期无 `applied` 的记录被列出；未使用不被判定为幻觉

### 验证命令

```bash
python -m pytest tests/ -q
python -m context_forge.cli doctor        # 期望暴露权重诊断入口
```

### 评审门 P3

- [x] 三张报表（`application_report` / `anomaly_candidates` / `tidy_candidates`）可导出且数值可信
- [x] **P3.t2 / P3.t3 / P3.t5b / P3.t5c 四条硬约束守卫全绿**（不许灰——它们守的是产品定位 R1/R2：`verified` 必须有真实 tool 证据、曝光不能自我强化、无归因不改状态、撤销不自动判死）
- [x] AC-P3 六条全勾

**回滚点 P3**：`UPDATE memories SET weight = 1.0`（`weight` 是 `DEFAULT 1.0 + 有符号增量`，中性值是 1.0 不是 0）即回到默认权重；报表是只读的，无副作用。

---

## 阶段 P4：卫生（晋升 / 衰减 / 合并 / 冲突分流）

> 目标：记忆自动抽象化（情节→规则）、收敛范围、合并并显式分流。自动执行且有依据、可回滚；人工干预是可选旁路。

### 实现清单

- [x] P4.1 自动巩固 — **情节 → 语义/程序**：新建高层记忆 + 改 `role` + **保留全部 `Provenance.projects`** + 收敛 `ApplicabilityScope`
- [x] P4.2 `decay_candidates(days=90)` + 强化路径；长期无 `applied` 且无来源更新 → 提升整理优先级；转 `stale` 需有依据且可逆；`role='preference'` 保留历史、允许 `supersedes`、用 `last_confirmed_at` 判断时效
- [x] P4.3 `merge_candidates(threshold=0.9)` — 边界兼容生成合并候选（低风险可自动归档）；applicability 冲突改标 `conflicts_with` 不合并
- [x] P4.4 巩固落地：**新建**高层记忆 + `derived_from` 指向全部来源实例，不原地改写、不删除来源，可撤销
- [x] P4.5 复核项（合并/冲突/低置信巩固）批量呈现，不逐条打扰

### 测试

- [x] P4.t1 多个独立来源的相似情节 → 自动新建高层记忆（`role` 变为 `claim`/`procedure`），**`projects` 完整保留**，原记录未被改写
- [x] P4.t2 整理：长期无 `applied` → 提升整理优先级；`role='preference'` 不因未引用被判定过期；`user_owned` 不受影响
- [x] P4.t3 整理依据：长期无 `applied` 且无来源更新的条目进入整理候选（不以共现为准）
- [x] P4.t4 合并分流：边界兼容 → MergeProposal；applicability 冲突 → 标 `conflicts_with`
- [x] P4.t5 保护约束：`user_owned` / `suppressed` / `disabled` 被后台自动改动时抛 `ProtectedMemory`；扩大访问范围、永久删除禁止自动执行

### 验证命令

```bash
python -m pytest tests/ -q
python -m context_forge.cli status --json
```

### 评审门 P4

- [x] 自动执行项均有依据、保留来源、可撤销；高风险动作仍禁止自动执行
- [x] 保护约束测试全绿
- [x] AC-P4 四条全勾

**回滚点 P4**：撤销对应版本即回滚；`undo_exact` 已支持撤销合并。

---

## 横切验收（最后一轮全量检查，对应 step 2.2 的全量范围检查）

- [x] `python -m pytest tests/ -q` 全绿（170 passed）
- [x] 每期的回滚路径各验证过一次
- [x] 无新增运行时依赖（对照 `pyproject.toml` 的 `dependencies`）
- [x] Markdown 导出的可读性抽查（打开 vault 若干 `.md` 确认 frontmatter 与正文可读）；`memory.db` 权威数据未被导出改变
- [x] PRD 的 AC-横切 三条全勾

---

## 风险提示（执行中注意）

1. **P0 是唯一必须立刻做的**——它修的是当前真实存在的上下文污染 bug，不依赖任何新模型。如果时间紧，先只做 P0。
2. **错误迁移是本次改造的最大风险**（新增）。`project` 移出默认过滤路径后，别的项目的结论会被召回进来。P2.e4 的错误迁移率判据是**上线闸门**，阈值必须事前写明，不是上线后补作业。超阈值就回 P1 补标签，**不许调权重硬压**。
3. **P1.t6 / P1.t7 是核心断言**（默认过滤路径不含 `project`；所有 `role` 的来源 `projects` 完整保留）。破了等于白改，实现时先写这两条测试再改代码。
4. **P1 迁移必须先备份、支持 dry-run、可回滚**。可以自动执行，但不得清空来源、不得覆盖 `user_owned` 版本。存量 206 条是用户资产。
5. **P3.t2 / P3.t3 是硬约束测试**，不许因为「先跑通」而跳过。它们守的是产品定位（R1/R2）：`verified` 必须有真实 tool 证据；曝光不能自我强化。
6. **打分权重是校准旋钮**，不要在实现里假装它精确。留 `forge doctor` 的诊断入口，用 P2 的实验数据调，不预设 ML。
7. **P2.t10 是硬约束测试**：禁止用 `tool_returned` 共现自动建边或改变排序。它守的是「系统不能用自己的输出当学习证据」。
8. 每期做完走评审门再开下一期，不要连做。
