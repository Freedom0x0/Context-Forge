# 决策记录（逐题脑暴，一题一定）

平台说明：本文件记录 9 个已识别不足的脑暴结论。每题格式：**问题 → 决策 → 理由 → 待办边界**。
决策变更需在此追加，不回改历史条目（可标注"已被 X 取代"）。

---

## 问题 1：沉淀质量无评估闭环

**背景与事实**
- 结构门禁（finalization.py `_validate_output`）只保证候选的引用/脱敏/关系合法，不保证内容正确。
- 已知真实翻车案例：`.trellis/tasks/09-23-forge-precipitation-dedup`（同一会话 70 分钟沉淀 32 次、31 条同一教训的改写版）。
- 原候选方案"人工标注 golden set"**被否决**：用户（项目所有者）自述不具备"该沉淀什么"的专家判断，标注即凭空发明标准。

**决策：事后投票三层机制**

1. **能见度层（单条记忆）**
   - 强反对票（抑制）→ `suppressed=1`，SQL 硬过滤，立即不再召回（机制已有）。
   - 赞成/反对票 → `memories.weight`（`apply_feedback_weight`，memory.py:113；只认 applied/validated/负反馈，曝光不计）→ 召回排序 feedback 维度（机制已有）。
   - 长期未被使用 → `UNUSED_DECAY` 小幅降权（机制已有）。
   - **红线：票只影响排序与可见性，永不自动改写记忆内容**（人工版本优先原则）。
2. **分桶报表层（类别治理）**
   - 反对率按 ①沉淀来源通路（hook / watcher / import）②model+prompt_version ③evidence_level 分桶，进 `memory-report` / `hygiene` 报表。
   - 输出的是**给人看的迭代证据**（改提示词、回滚模型版本、调闸门严格度），不是自动执行。
3. **坏案例反哺层**
   - 高反对率典型案例逐个固化成 pytest 回放用例，形成"坏案例集"；改 prompt/换模型前先跑。

**票的定义（映射到现有动作）**
| 动作 | 票 | 强度 |
|---|---|---|
| suppress / harmful 反馈 | 反对 | 强 |
| revise（内容纠正）/ supersede | 弱反对（选题对、内容错） | 中 |
| validated / confirmed-applied 且未被否决 | 赞成 | 弱（隐式） |
| restore | 平反 | 中 |
- 可选月度轻问卷（前 20 条高频召回逐条"留/不留"）**默认关闭**，防问卷疲劳。

**已知短板（接受并记录）**
- 冷启动：前几周无数据，无法给"上线第一天"背书。
- 测不到"漏沉淀"：只能评已沉淀内容；漏沉淀靠坏案例集慢慢补。
- 沉默即赞成：不投票的垃圾记忆会累积中性偏正，靠分桶报表人工兜底。

**依赖**
- 隐式票（applied/validated）的采集入口 → **已并入本议题**（见下方"问题 2 处理结果"）。

**状态：已定（2026-09-29）**

---

## 问题 2：使用反馈无录入口 → 并入问题 1

**用户质疑**：录入口本来就是投票机制的一部分，为何单列？——成立。按目标切分而非按代码模块切分后，二者是同一件事。

**处理结果：撤销独立议题，作为问题 1 的实施范围**：
- 明票入口（suppress/revise/supersede）已存在，无需新建；
- 隐票入口需新建：MCP 写工具 `context_forge_memory_mark(memory_id, version, request_id, stage)`，宿主 Agent 在引用/采纳/验证后回报；
- 实施清单 = 收票入口 + 反对率分桶报表 + 坏案例反哺测试集。

**状态：已定（2026-09-29，并入问题 1，不再单独讨论）**

---

## 问题 3：整理只出报告，无执行端

**背景与事实**
- `MemoryMerge.apply_exact` / `apply_conflict` 已实现且有测试，但 CLI/daemon 零接线；归并提案（`ConsolidationProposal`）**只有提案结构，没有执行函数**。
- 已付学费：32 次洪泛事件（09-23 任务）——有执行端则完全重复部分可自动归一。
- 人工操作（suppress/revise/supersede）回流 memory.db 的通路**本来就有**，本议题真正缺的是"报告判决回流"。

**决策：按"可逆性 × 语义风险"分档，四件套全接**

自动档（daemon 每日低频轮，无人值守）：
- **G1 完全重复**（内容+边界全同）→ `apply_exact`：无语义风险、可逆，直接做。
- **G2 矛盾自动标注**→ `apply_conflict`：只写关系、不改内容、可撤；**判决谁对永留人工**。

人审档（只进队列，不自动执行）：
- **G3 高相似疑似重复** → 归并提案（`ConsolidationProposal` / `overlaps` 类候选）：相似≠相同，自动合并可能把例外吞进规律。**注意：执行函数尚不存在**，只有提案——人审判决后需要新写"应用归并"的实现。
- **G4 过时/衰减** → 报告 + 人审："没人用"≠"错"，低频关键知识会被误杀。

**四件套**
1. 一次性接全：`forge hygiene --apply` 按档执行（非单档）；
2. 过时/矛盾要处理：= G2/G3/G4 的档位划分；
3. 定时执行：daemon 每日轮仅跑 G1/G2；G3/G4 永停报告+队列；
4. 判决回流：`forge hygiene --review` 交互式逐条判决（保留/合并/归档/标替代），**库为权威**，文件卡片只是可选皮肤、不做骨架。

**安全底线**
- 每档操作写事件历史（put 已带版本+reason），G3/G4 需补通用"恢复状态"撤销口（`undo_exact` 只覆盖 G1）。
- 实现缺口备忘：G1/G2 执行函数已存在（待接线）；**G3 归并的执行函数不存在，需新写**；G4 本就是报告+队列。
- 自动档尺度由问题 1 投票数据调节：某自动规则常被人撤销 = 开太宽，收紧；反之放宽。反对率是自动化的油门和刹车。

**已知代价**
- daemon 无人值守改库（靠事件历史+撤销兜底）；
- 人审队列是新载体；
- 四件全接 ≈ 一次正经迭代，需拆子任务实施。

**状态：已定（2026-09-29）**

---

## 问题 4：模型产物直接 active，无发布前闸门

**决策：主链路全自动，人为支线；知识松、规则严；高危规则双确认**

1. **发布策略**：knowledge 与 rule 都自动入库生效，人不参与主链路。
   - knowledge（陈述性）：宽松，正常召回。
   - rule（指令性）：严不在发布环节，落在**使用时**——召回返回带四段解释（适用边界/风险提示），由宿主 Agent 权衡后采用。
2. **高危规则双确认（唯一的破例，只针对 `risk=high`）**【落地形式已被 **F4** 确定：召回侧不靠宿主自觉询问，改为"未批准则拿不到正文"】：
   - **沉淀时**：写入前需人确认一次（未确认=待审态、不可召回）；确认方式走旁路命令/人审队列，不打断日常。
   - **召回时**：规则原文随结果返回并标注"高危需确认"，宿主执行前再向人确认，**不得直接执行**。
   - 实现要点：高危判定需确定性分类器（正则/关键词识别删除、重置、迁移、生产环境等操作词），不能只靠模型自觉填写 risk 字段；待审超时策略（如 N 天未确认降级为 knowledge 或归档）实施时定。
3. **连带升级**：既然无发布前闸门，沉淀洪泛防护（09-23 任务的写入等价检查/频率闸门/幂等重试）从"治理优化"升格为**主链路安全依赖**，优先级提高；其 PRD 应引用本决策。

**理由**：与产品气质一致（local-first、不打断心流、人工纠正是旁路而非税）；代价是垃圾沉淀直达召回，由双通道兜底——写入门（09-23）防重复、投票机制（问题 1）防驻留。

**状态：已定（2026-09-29）**

---

## 问题 5：召回纯词法，换个问法就搜不到

**背景与事实**
- FTS 查询是全 token AND（memory.py:1206），中文按两字滑窗切词，长问句命中概率极低。
- 单纯"降级放宽"仍是在字面里找意思，解决不了语义近、字面远的漏召回（用户明确指出这是死穴）。

**决策：混合召回 —— BM25 + SQLite BLOB 向量，不引入独立向量数据库服务**

1. **三路候选，一个管道**：BM25 全 AND（高精度）+ 关键词降级放宽（中间档）+ 语义向量 top-K，三路取并集后进入**现有的**边界过滤 → 五维打分 → 四段解释 → 字节预算。管道纪律不破。
2. **向量存现有 memory.db**：`memory_versions` 加 `embedding BLOB` + `embedding_model` 两列；`_put` 时算向量，未配置 embedding 通路则留 NULL。查询时 numpy 余弦相似度暴力扫描。
3. **可选件原则**：embedding 未配置 → 自动退回纯词法，功能完整、零新依赖（沿用 gateway 的 none/offline/remote 哲学）。保住 local-first 开箱即用。
4. **成本控制**（回答"全量加载代价"）：常驻进程缓存相似度矩阵，靠 `memory_versions` 版本号做失效判断（亚毫秒）+ 增量更新，**全量加载只发生在冷启动一次**（1 万条约几十毫秒）；可选 float16 存储砍半内存；超十万条的升级路是 `sqlite-vec` 进程内扩展（仍非独立服务）。
5. **模型漂移**：`embedding_model` 标记不一致时提示重建，照 `rebuild_fts` 先例；新增 `forge reindex-embeddings` 回填存量。
6. **解释扩展**：四段解释的"文本分"改为 `max(词法分, 语义分)`，并新增"语义相似 0.83"这类依据行，保持可解释。
7. **README 红线修订**（用户授权：红线按实际情况可变动）：由"不引入专用向量数据库"改为**"不引入独立向量数据库服务"**——向量本身允许，独立进程/双写同步不允许。

**规模依据**：个人记忆量级千~万条，1024 维 float32 约 4KB/条 → 1 万条 40MB、暴力扫描 1–5ms；专用向量库是为百万级/高并发准备的，此处引入只增加进程、双存储同步和备份复杂度，无收益。

> **【实测更正 2026-10-08，实施见 `10-08-hybrid-recall`】** "暴力扫描 1–5ms" 是**未实测的估计，错了约 300 倍**。
> 本机实测（1024 维 float32）：纯 Python 全扫 317 条 **38 ms** / 1000 条 **232 ms** / 10000 条 **1448 ms**；
> numpy（本机 2.5.3，**未声明为依赖**）10000 条 **11.9 ms**。真实库当前只有 314 条记忆 / 317 版本。
> 三条设计后果：① numpy 进 optional extra，向量通道属可选件；② **取消"纯 Python 语义扫描"这条降级路**
> （1000 条 232 ms 已吃光召回预算，是假降级），降级只有一档 = 纯词法；③ 本层收益是召回质量不是速度，
> 性能护栏为增长准备，不预先上 sqlite-vec。另外全仓 `embedding|vector` **零命中**，语义通路整条是新建。

> **【实施结果 2026-10-08，仅第 1 条的中间档】** 关键词放宽已落地，形状由实测决定，**不是**当初写的"逐轮丢弃最冷的字"：
> - **2a**：`df=0`（没有任何文档含它）的词无条件剔除，剔完仍走 AND。固定丢弃预算（3 轮）被证伪——
>   带 ≥4 个未知词的查询恢复率 **0%**，直到预算超过未知词数量才动，"轮数"这个规则本身就是错的。
> - **2b**：2a 之后仍不足 `wanted` 才走，**全部**词用 OR 并集、按 bm25 排名、`WIDEN_RESULT_CAP=100` 限**结果条数**。
>   匹配得上的词**一个都不丢**，所以不再有"丢词预算"（`WIDEN_ROUNDS` 已删）。
> - 试过并被测试证伪的三种排序：先丢高频词、先丢低频词、df=0 + 高频带上限。
> - 每条放宽命中在解释里标注来源段（`去掉 N 个匹配不上的词` / `词并集兜底：命中其中一个词即可`）；
>   只补新命中，已有高精度命中不被覆盖；边界过滤照旧在后面挡人（变异验证：让 2b 覆盖已有标签 → 6 例红，
>   含预注册的 `test_recall_eval.py` 门槛）。单 token 查询不放宽。额外 SQL 成本：400 条库上 df 计数 + 并集
>   共约 **1–3 ms**，单次 `search_scored` 全程 28–50 ms，**大头是逐条读记录，不是放宽**。
> - **一处行为变了**：`search('SQLite OR missing')` 以前返回空，现在返回那条 SQLite 记忆并带放宽标签。
>   旧断言"问了库里没有的词就什么都别给"与本轮目标直接冲突，改为"要给，但必须说清怎么来的"；
>   FTS 语法泄露那条（`'"* - : ()'` → 空）未动，仍然成立。
> - **没测出来的部分要讲明白**：这一档到底解决了多少"换个问法就搜不到"，**本机没有语料能量**。
>   `tests/recall_eval_cases.py` 是迁移正确性（`right`/`wrong`）标注，不含换问法这一维，
>   原 PRD 那条 AC 的前提不成立，已改。放宽的证据 = 自建语料 8 例 + 现有门槛不回归 + 三处变异验证。

**状态：已定（2026-09-29）**

---

## 问题 6：三处平方级/无界性能热点

**背景与事实**
- ①整理体检 `MemoryMerge.report/merge_candidates` 用 `combinations(records,2)` 两两比对（memory_merge.py:103,133）——10 万条 = 50 亿次，不可用。
- ②检索候选逐条反序列化整条记录（memory.py:1225 `_record`），多数候选最终被丢弃。
- ③`retrieval_events` 表只进不出，无保留策略、无聚合。
- 澄清：写入时去重（finalization `_plan` 的 `find_exact`）走指纹索引，本来就不是热点；召回时呈现归族只比候选池 ≤100 条，有硬上界，两处都不动。

**决策：三处全修 + 判重标准与召回对齐（用户原则："既然召回承认字面≠意思，判重也应同一标准"）**

1. **G1 完全重复 = 统一到指纹通路**：整理改为 `GROUP BY canonical_fingerprint HAVING count(*)>1`（已核实 `canonical_fingerprint` 与整理口径 `_normalized+_boundary` 覆盖字段一致，memory.py:52），删除 N² 里的完全重复比对；**【核实修正 2026-09-29 晚】`memory_versions(canonical_fingerprint)` 索引早已存在**（memory_schema.py:87 与 :144 两处都建），故**不要重复建索引**——`find_exact`（memory.py:708）慢的真实原因是查询写法：`JOIN memories` 后按 fingerprint 扫**所有版本**、缺"仅当前版本 / 仅 active"过滤。修法改查询与过滤，不是加索引。口径统一的隐性收益：将来调整"什么算完全重复"只改一个函数，写入门与整理门永不分叉。
2. **G3 疑似重复 = 双通道候选生成**：
   - 字面通道：前缀分块（token 按全局频次升序，取前 p=|A|-⌈θ|A|⌉+1 入倒排桶，只比前缀相交对，零漏检，数学保证）+ Jaccard `token_similarity`；
   - 向量通道：复用问题 5 的 embedding 缓存矩阵做近邻（判重是"同族内"任务，向量精度比召回更高）；
   - 两路候选取并集，`MergeCandidate.reason` 并排展示两个分数（`字面 0.41 / 语义 0.92`），人对照判决；
   - **边界冲突检查保持字面**（`_applicability_conflict` 是精确条件比对，本应字面判）；关键限定词差异（"生产/开发"）由字面通道兜住，语义改写型（09-23 洪泛 32 条全是此类）由向量通道兜住。
   - 传递性警示：A≈B、B≈C 但 A≉C 时禁止自动求连通分量合并——维持 G3"只提案不执行"纪律。
3. **检索批量解包**：候选池一次批量取 snapshot；完整 Pydantic 校验推迟到 top-K 之后（边界所需字段走投影表）。
4. **事件表治理**：月度聚合表 `stage_counts(memory_id, version, month, stage)`，daemon 定期滚动汇总；原始事件保留 180 天（`forge apply-retention` 同款参数化）。**红线：聚合表不得被删，否则使用统计失真**——`forge backup` 文档明示。

**代价（用户已知并接受）**：性能收益在当前几百条的库上感知不到，属于按极端情况设计的纪律性投入。

**状态：已定（2026-09-29）**

---

## 问题 7：单主体假设 + 默认 shared 偏激进

**讨论修正**：初判"默认 shared = 公告栏"不准确——本地场景下 shared 只对保险箱主人可见，**现状已经是"记忆像人记忆一样对本人全敞开"**，这正是想要的体验，无需收紧。真正需要管的是数据离开本地的瞬间。

**决策**

1. **语义定调**：文档写死一句话——"**shared 的含义是'对本保险箱全局敞开'，不是'对世界敞开'**。任何离开保险箱的路径（云同步、团队库、导出）必须过独立的出库闸门。"现有 `access.visibility` 三态保留，作为"主人对自己的项目说的话"（选择加入的例外，非默认）。
2. **云端多用户 = 一用户一库**（A 方案）：隔离放部署层，单主体语义原样上云，备份/FTS/向量缓存全复用；不引入行级 owner 过滤（散弹风险）。`space.owner` 概念保留（本地恒为 'local'，同步时置为 user_id），团队共享库等真实需求出现再立项（届时需要完整 ACL，非本决策范围）。
3. **技术债收拢（现在做）**：owner/privacy 判断点收拢成单一 `VaultPolicy` 出口，替代散落的 DEFAULT_OWNER 直查（memory.py:261/263 的 ponytail 债），保证"一用户一库"语义是**声明**而非巧合。
4. **出库闸门（设计已评审通过，挂起至云端立项时实施）**——四条规则备查：
   - 分级放行：excerpt 摘录永不自动出库（剥离或单独开关+再脱敏）；role=observation 逐条；claim/knowledge 首次批量、之后按模式放行；rule/preference 逐条；使用事件默认不出库，只出库聚合计数。
   - 审批以"模式"为单位（结构化字段匹配，**永不按正文内容自动放行**）；`sync_grants(memory_id, version, pattern)` 表，同步器只推有有效 grant 且版本未变的记录。
   - **版本变更 = 重新审批**（批准不延续，diff 呈现），防"先骗闸门后偷内容"。
   - 撤销全链路撤回（tombstone 通路扩展）+ 同步前二次脱敏（可维护红线清单）+ 出库审计日志。
   - 原则依据：出库是"支线上的支线"，审批可以慢、可以攒批，与"人不参与主链路"不冲突。

**状态：已定（2026-09-29；第 4 条设计挂起待云端立项）**

---

## 问题 8a：宿主耦合——"所有 agent 共享记忆"

**背景与事实（已 grep 核实）**
- **读侧本就宿主无关**：记忆通过 MCP server 暴露（`context_forge_memory_search` 等），任何会说 MCP 的宿主读的是同一个 `memory.db`；让多宿主读共享只需各家装一次 server。
- **写侧只认 Claude Code**，三处硬编码：`watcher.py:33 transcript_root()` 直接返回 `~/.claude/projects`；`context_forge/hooks/` 目录内只有 `claude_code.py`；`forge precipitate`（cli.py:574）注释即"读 `~/.claude/projects/**/*.jsonl`"。**共享因此是单向的**：CC 沉淀的记忆别的宿主能搜到，别的宿主干的活永不入库。
- **数据模型没锁死**：`domain.py:57`、`:163` 的 `source` 字段是自由字符串（现被写死 `"claude_code"`）；`EpisodeRecord`（`:171`）尚无 host 字段。

**触发方式三分法（本决策的核心判断）**
- **T1 被动扒文件**：watcher 读某家宿主自己的会话目录。不需要宿主配合、不需要装 hook、不需要模型调用。
- **T2 宿主框架事件**：宿主的 SessionEnd/PreCompact 钩子在会话结束时触发——由**宿主的程序流程**发起，与模型"想不想"无关；产出 **explicit** 终结，比 T1 靠时间猜（inferred）更准。CC 现在是 T1+T2 双路并行，多宿主即把这一对成熟模式复制 N 次，而非发明新机制。
- **T3 主动调用**：MCP 工具挂在库里，由模型或人决定调不调。

**决策**

1. **主链路 = T1 + T2**：两者都是确定性触发，符合问题 4"主链路全自动、人为支线"。每家宿主一个薄适配器（目录根 + transcript 解析器 + hook 脚本）。
2. **T3 只作支线**，定位给人手动补一条、或给既无 transcript 又无 hook 的宿主应急。**红线：任何宿主的沉淀默认不得只依赖 T3**；"支持某家宿主"的验收标准是"能自动产出 episode"，不是"有工具可调"。
3. **沉默即报警**：`memory-report` 增加"每 host 最近一次成功投料时间 + 解析失败次数"，`forge doctor` 检查各家目录存在性与样本行可解析性。理由：N 个适配器 = N 个会随宿主升级静默坏掉的东西，必须可观测。
4. **实施顺序**：本轮**不动多宿主代码**——README 写清现状（读：全宿主经 MCP 通用；写：仅 Claude Code）；**【核实修正 2026-09-29 晚】CC 的 T2 其实已经实现**（hooks/claude_code.py:62 已在调 `request_finalize(end_kind="explicit")`，`doctor.check_lifecycle_hooks` 也已存在），真正缺的是"装没装上"的验证与安装引导，以及 8c 那条从未接上的 inferred；第二家宿主单独立项做适配器。理由：每个适配器都需对着该家真实文件格式调试，攒在一起做反而更慢，且不阻塞任何已定决策。
5. **`ingest` 内部接口作为代码纪律保留**（管道只允许一个入口函数，防止长出第二条管道），但它是重构目标，**不是给宿主用的功能**，更不改变第 2 条对 T3 的定位。

**否决记录（用户提出的约束，后续设计必过此关）**：**"不能把写入从确定性改成概率事件。"** 初版推荐曾以"宿主主动调用投料口工具"为多宿主骨架，被用户指出那会让沉淀变成"看模型自觉"的概率事件，违反本条。任何引入"靠调用才发生"的新机制，须先证明有确定性的事实在兜底。

**状态：已定（2026-09-29）**

---

## 问题 8b：运行形态——进程没管住

**事实（已 grep 核实，含对初判的纠正）**
- **常驻循环只有一个**（初判"两个独立轮询循环"不准确）：`forge watch-transcripts` → `watcher.run()`（watcher.py:171）内部既采集 episode 也驱动终结（watcher.py:205 调 `process_pending_finalizations`）；worker 侧是 SessionEnd 瞬发一次的 `forge process-now`。真实并发风险 = 常驻 watcher + 瞬发 process-now 同时干活。
- **heartbeat 只是报警器**：`_beat()`（watcher.py:145）写 `{"pid","at"}`，`doctor.check_watcher()`（doctor.py:107）读它判活；启动路径**从不做互斥判断**。第二个实例照样能起，并覆盖 heartbeat 的 pid → doctor 报"alive"报的是最后写的那个，另一路静默并行。
- **`queue.claim()` 有真竞态**（queue.py:365-385）：先 SELECT 一条 queued job，再 `UPDATE ... WHERE id=? AND status='queued'`，随后**不检查 `rowcount`** 就把该行返回给调用方。两进程同时 SELECT 到同一条时，第二个的 UPDATE 影响 0 行却仍领取成功 → **同一 job 处理两遍 = 双份模型调用、双份候选**。此问题与进程锁无关，单实例下瞬发进程也能触发。`claim_extraction`（queue.py:265）的 owner lock 需按同一标准一并核查。
  - **【实测推翻 2026-09-30，任务 `09-30-claim-race-fix`】** 6 进程抢 400 条 queued job：`claimed_total=240 / unique=240 / double_claims=0`。**双重领取不可复现，上面这段的因果判断是错的**：`with self.db:` 块首条语句是租约回收的 UPDATE，SQLite 写锁因此把整段（含读）串行化。仍然成立的只剩"该保证无处声明"——回收语句一旦被改成条件执行或移到 SELECT 之后，保证静默消失且无测试变红。修复方向相应改变，见下面 8b 决策 2 的更正。
- **`forge install-daemon` 是死命令**：cli.py:413 `from .worker_service import install`，但 `context_forge/worker_service.py` **不存在**，按文档执行必 ImportError。实际开机自启由 `autostart.install()` 写 VBS 到 Windows Startup 目录，且由 `forge install-hook` 顺带安装——**常驻目前只有 Windows 有；macOS/Linux 无任何入口**（`_startup_dir()` autostart.py:23 是 Windows 硬路径）。
- 第三个无限循环 `forge watch`（cli.py:612）管 vault 变更→重建索引，与沉淀无关，但命名与 `watch-transcripts` 只差一词，易被误当成同一件事重复启动。

**决策（四条）**

1. **互斥用文件句柄锁，不用 pid 文件查存活**：进程启动时锁住锁文件并持有句柄直至退出，进程死则系统自动释放；POSIX 走 `fcntl.flock`，Windows 走 `msvcrt.locking`。抢不到即明确报错"已有实例在运行（pid=X），本次退出"。弃用 pid 文件的理由：崩溃后残留与 pid 复用都需要额外"猜对方是否已死"的逻辑。**硬约束：锁文件放在 `config.runtime_dir()` 下、按库加锁，绝不全局**——否则 `CONTEXT_FORGE_HOME` 隔离场景与测试套件会被自己锁死。
2. **`claim()` 竞态单独修，优先级高于第 1 条**：UPDATE 后检查 `cursor.rowcount`，为 0 则返回 None 让出；该段包进 `BEGIN IMMEDIATE`（SQLite 立即取写锁，消除"读完未写"交错窗口）。理由：第 1 条只防手滑起两个实例，第 2 条修的是**当下正在发生**的重复消耗。
   - **【实施更正 2026-09-30】** 前提被实测推翻（见上面 8b 事实节的更正），两条具体做法也随之作废：
     (a) **不引入 `BEGIN IMMEDIATE`**——当前事务形状已串行化，显式改锁要动 `isolation_level`，会连带影响同文件所有
     `with self.db:` 块的语义，风险大于收益；(b) 优先级从"最高"降为"纪律性加固"，因为**没有正在发生的重复消耗**。
     实际落地：选中与占用合并成**一条** `UPDATE ... WHERE id=(SELECT ...) RETURNING id`，形状对齐同文件已有的
     `claim_extraction`（`queue.py:316-327` 的单语句 + rowcount）；并发守卫固化成 `tests/test_queue_concurrency.py`
     （把 claim 改成只读不占用时该测试会失败，证明守卫有牙齿）。第 1 条（实例锁）与本条是不同问题，仍然有效。
3. **跨平台常驻本轮只止血**：处理掉死命令 `install-daemon`（删除，或把职责并入 `install-hook`——它现在承诺的能力代码里不存在）；macOS/Linux 自启以**文档 + 可直接抄的 launchd plist / systemd user unit 示例**交付。不新建服务管理器的理由：产品是 local-first CLI，使用者即开发者，会自己挂服务；等 8a 的多宿主适配器立项、真出现"必须后台常驻"的产品要求时再立项写三家 `install-daemon` 实现。此处 CLI/文档作为开发者基础设施保留，不属于"用户写数据走脚本"的范畴。
4. **命名去歧义**：`forge watch` → `forge watch-vault`，与 `watch-transcripts` 对称，成本极低。

**明确不采纳**：把 watcher 与 vault watch 合并成单一 `forge daemon` 多任务进程。少一个锁是唯一的干净处，代价是把两个毫无关系的循环绑成一个故障域，且没有需求在推它。

**状态：已定（2026-09-29）**

---

## 问题 8c：`advance_inferred` 零调用者（inferred 终结从未执行）

**事实（已 grep 核实）**
- `EpisodeRecord.state` 五态（domain.py:177）`open → inferred_pending → finalizing → finalized` + `reopened`；`end_kind` 三值（domain.py:180）explicit / manual / inferred。
- 三个扳机里只有两个在跑：explicit（hooks/claude_code.py:62、cli.py:80）、manual（cli.py:91）。**inferred 唯一实现 `advance_inferred`（episodes.py:47-66）在生产代码中零调用者**，仅测试调它（test_episodes.py:176/181）。
- 该函数签名要求调用方提供 `process_alive`、`active_tools`，而 watcher 只看得见文件 mtime，无从获知 → 接口留了六年没人接。
- 后果：会话若非正常收尾（关终端、CC 崩溃、hook 未装/未触发——`doctor` 自己承认 hook 可能死），episode 永停在 `open`，内容进不了库；`active_episodes()`（queue.py:184 查 `state IN ('open','inferred_pending')`）会持续把它算作活跃项。

**改变判断的发现（本决策的依据）**：**提前收尾的代价远小于这段代码的假设。** `collect()`（episodes.py:31-38）里，若 current 已 `finalized` 且有新行，自动 `segment += 1` 并从上次收止行往后继续提炼，状态机另留 `reopened`。所以误收尾的后果是"一段对话分两次沉淀"，**不是丢数据**；而不收尾的后果才是真丢数据。用一个"危害小且可自修"的场景去要求"昂贵且跨平台拿不到"的证据，正是它没人接上的根因。

**决策：B + D —— 推断只看文件，删掉两个外部参数**

1. **B（纯文件推断）**：watcher 每轮顺手扫 active episodes；`last_activity_at` 静默 ≥30 分钟 → `inferred_pending`；再 ≥30 分钟 → `end_kind="inferred"` 收尾。额外抑制信号改用**文件内可判**的事实：最后一行若是未配对的 `tool_use` block（`transcript.py:86` 已在解析此类 block），说明有工具卡着或崩在半路，本轮不收。`advance_inferred` 的 `process_alive`/`active_tools` 两个参数**删除**（不做默认值兼容垫片）。
2. **D（用新活动代替长静默）**：watcher 扫到**新出现的 transcript**，即证明用户在活动，对更老的 open episode 立即收尾，不必等满 60 分钟。单独用不够（可能一天只开一个新对话），作为 B 的第二条腿成本极低。
3. **阈值 30/30 写死，不新增配置项**——先能跑、能观测，再谈调参。
4. **`end_kind="inferred"` 必须在报表中单列**（接问题 1 的分桶报表层）：若推断收尾造成大量割裂（同 session 出现许多 segment），报表可见，届时再决定是否回头引入 A 那类重证据。
5. **inferred 与 explicit 走完全相同的管道**（同一 `request_finalize` → 同一闸门 → 同一指纹），不设二等公民通道，唯一区别只有 `end_kind` 标记。

**明确不采纳**
- **A 补齐外部信息源接原设计**：进程存活需枚举进程（Windows `tasklist` / POSIX `psutil`），新依赖 + 平台分支；且"有 claude 进程活着"推不出"本 session 正在活动"；按 8a 多宿主方向，每接一家都要重新回答"它的进程叫什么"。昂贵、耦合、且回答不准。
- **C 删掉 inferred 只留 explicit/manual**：等于把沉淀押在宿主 hook 配合上（崩溃/断电/kill/hook 死都不保证），直接违反 8a 刚定的红线"不得把写入从确定性改成概率事件"。

**状态：已定（2026-09-29）**

---

## 问题 9：正则脱敏偏弱

**事实（已 grep 核实）**
- `transcript.py:18-22` 仅四条模式（authorization bearer / `api[_-]?key|token|password|secret` + `[:=]` / PEM 私钥块 / postgres|mysql|mongodb 连接串），命中即替换为 `[REDACTED]`。
- 调用点两类：**出境前**（finalization.py:33、gateways.py:45、provider_http.py:201、worker.py:103、episodes.py:73 的 checkpoint 快照）与**产出闸门**（finalization.py:84-86：候选正文或证据摘录若仍会被 sanitize 改动，整条拒收，报 `candidate contains material requiring redaction`）。
- 从正则本身可确证三类漏检：**JSON 引号形态**（`"access_token": "..."` 中 `token` 后是引号而非 `:`/`=`，第二条要求紧跟 `[:=]` → 不匹配）；**裸密钥**（`sk-ant-`、`ghp_`、`AKIA`、JWT `eyJ` 等无标签前缀 → 四条全不沾）；**裸 Bearer**（第一条要求 `authorization:` 打头）。另：单行 base64 私钥逃过第三条，`redis://`/`amqp://` 逃过第四条，PII（手机号/身份证/姓名/住址）完全无覆盖。

**威胁模型（先定这个，它决定后面所有取舍）**
按问题 7 已定方向（保险箱对内敞开 + 禁止出库），本地 `memory.db` 与 vault 存了什么只有主人看得见，**不构成泄露**。唯一真实外泄面是**把文本发给远端模型 API 的那一次调用**。推论两条：①脱敏目标收窄为"出境的那份文本"，本地存储不需要全套 PII 治理；②若 `model_provider` 配成 none/offline（沿用问题 5 的可选件哲学），出境不存在，风险等级由 provider 配置决定，而非由正则强弱决定。

**决策（四条）**

1. **① 规则扩充——现在做，并在文档写死"规则永不完备"**。补已知前缀（`sk-`/`sk-ant-`/`ghp_`/`github_pat_`/`AIza`/`xox`/`AKIA`/JWT `eyJ`/`ssh-rsa`）、补 JSON 引号形态（关键词与 `:`/`=` 之间允许引号）、补 `redis://`、`amqp://` 等协议串。文档必须明确：这是黑名单追赶战，不得把它当作强保证。
2. **④ 敏感路径启发式——现在做，性价比最高**。transcript 中出现 `.env`、`id_rsa`、`.aws/credentials`、`.npmrc`、keychain 导出等路径名即命中；不读内容、只看"刚才在碰什么文件"，误伤面小、实现量小。**命中结果接问题 4 的既有机制**：该 episode 产出的候选走 `risk=high` 双确认通道，不新发明闸门。依据：密钥绝大多数不是用户想沉淀的知识，而是工具读回来的文件内容。
3. **② 高熵检测——只做检测，不做替换**。按形状（长度 ≥20、字符集受限、两侧为空白或引号）识别"像密钥的孤立串"，**命中只记一条事件进问题 1 的报表层**，让"这段有可疑串"可见。不自动抹除的理由：哈希 / commit sha / base64 片段 / UUID / 代码长标识符都会误报，误报的代价是吃掉真知识；等报表证明误报率可接受，再决定是否转成替换。
4. **③ 缩小暴露面（tool_result 正文不再整段出境）——本轮不做，留给用户显式决定**。它是唯一能真正封顶风险的招，但代价直接打在沉淀质量上（报错栈、diff、实际输出正是最有价值的证据），不该由工程侧顺手实施。若日后要，应作为独立立项并让使用者在"少学一点"与"文件内容出境"之间明确选边。

**明确不采纳**：引入 NER / 本地小模型做 PII 全量识别——违背 local-first 开箱即用，且按上述威胁模型，它要防的问题（隐私内容留在本地库）目前不存在。

**状态：已定（2026-09-29；第 4 条挂起待用户立项）**

---

## 跨题关联与实施分层（2026-09-29 晚，整体审阅追加）

审阅九题后发现的结构性问题：**不改变任何已定方向**，但补 5 条约束、纠正 2 处措辞（就地标注在上述条目）、并给出不等于题号的实施分层。

### A. 未认领的公共基础件（不补则已批准决策空转）

- **① `risk` 无落库字段。** 问题 4 的双确认、问题 9 的"敏感路径走 risk=high"都写在 `risk=high` 上，但**没有任何 SQL 可过滤的列承载它**。**【核实更正 2026-09-29 晚】`MemoryDraft.risk` 其实已定义**（memory_models.py:102，类型 `str | None` 自由文本），但全项目仅此一处出现、**无人写也无人读**——是个休眠字段；`model` / `prompt_version` 同处定义且**确实被填**（worker.py:131 → memory_lifecycle.py:177/193，落在 `snapshot_json` 里）。问题 4 本身也要求"不能只靠模型自觉填 risk，需确定性分类器"。→ 前置任务四步：(a) 枚举化 `risk: Literal['low','normal','high'] = 'normal'`；(b) finalization 阶段用**确定性词表分类器**赋值，形状照 `canonical_fingerprint`（单一纯函数、可单测，模型自报仅参考）；(c) 落 `memories.risk TEXT NOT NULL DEFAULT 'normal'` 列——**先例即同表的 `weight REAL`（memory_schema.py:10），运营缓存列这个形状可直接照抄**；(d) 高危待审**不需要新状态值**：`status` 的 CHECK 已含 `candidate`（memory_schema.py:7），召回 SQL 只认 `active/conflicted`，留在 candidate 天然不可召回、零迁移。代价：词表必漏判，由 9 的路径启发式 + 问题 1 反对率 + 坏案例测试集补。
- **② 定时载体必须指定。**【失败与原子性语义已被 **F1** 取代】 问题 3 要 daemon 每日跑 G1/G2、问题 6 要 daemon 滚动汇总 `stage_counts`、8c 要 watcher 推进 inferred，而 8b 明确"不合并成单一 daemon、跨平台自启只给文档"→ 三题的"定时"悬空。**定：复用 watcher 常驻循环作为唯一定时执行体**（已常驻、已有 heartbeat，8b 的锁正好保护它），内部按"每日一次"节流；不新建 daemon 进程。附带收益：8b.1 的锁从"防手滑"升级成有实质职责。
- **③ `host` 维度尚未落地。** 8a 已指出 `EpisodeRecord`（domain.py:171）无 host 字段，而问题 1 要按"沉淀来源通路"分桶、8a.3 要按 host 报"最近成功投料时间"。**定：host 字段进 EpisodeRecord 与投料事件属于第 0 层前置件**，先有维度，报表层才有东西可切。**落地路径（全部有现成先例，代价极低）**：`session_episodes`（queue.py:71-79）加 `host TEXT NOT NULL DEFAULT 'claude_code'`，走 `queue._migrate()` 既有的"PRAGMA table_info 查列 → `ALTER TABLE ADD COLUMN`"模板（queue.py:107-122）；数据来源也是现成的——`SessionEvent.source` / `SessionLifecycleEvent.source`（domain.py:57、:163）本就是自由字符串、现被写死为 `"claude_code"`，透传进 `collect()` 即可。老数据落默认值 `'claude_code'` 语义正确（本来就全是它）。

### B. 两个"各自合理"决策的正面冲突

- **④ 聚合表会饿死投票报表。**【聚合表形状与基数已被 **F2** 取代】 6.4 的 `stage_counts(memory_id, version, month, stage)` 不含 host / model / prompt_version / evidence_level，也没有反对与反馈维度，而原始事件 180 天后删除 → 半年后问题 1 的分桶报表查无数据，**性能决策删掉了投票机制的数据载体**。**定：聚合键扩为 `(month, host, model, prompt_version, evidence_level, stage/outcome) → count`。** 纯设计修正，必须现在定，否则按 6.4 字面做完就要回头补。
- **⑤ embedding 是第二条出境路径。** 9 的威胁模型称"唯一外泄面是发给远端模型 API 那一次"，而 5.2 在 `_put` 时算 embedding；若 embedding 走 remote provider，就是新增出境且送的是原文。**定：embedding 输入必须是 sanitize 之后的文本，embedding provider 与提取模型共用同一套出境脱敏约束**；9 的表述相应改写为"任何 remote 通路（含 embedding）"。

### C. 两条口径纪律

- **⑧ `prompt_version` 只有一个取值。** 字段已落库（memory_models.py:104），但 worker.py:131 全局硬编码 `"session-artifacts-v1"`。**纪律：改提示词必须 bump 该字符串**，否则问题 1 的"按 prompt_version 分桶"永远只有一个桶，等于没有。
- **⑨ 双分数需要字段而非文本。** `MergeCandidate.reason` 是单个字符串（memory_merge.py:11），而 6.2 要"字面 0.41 / 语义 0.92 并排"供人判决。**定：加 `score_literal` / `score_semantic` 两字段**，因为 `--review` 与报表要按分数排序。

### D. 与 8a 红线的张力（已核对，不需要新决策；本段 confirm_token 方案已由 F3 细化定稿）

- 问题 2 的 `context_forge_memory_mark` 属 T3（宿主主动回报），隐式票本质是概率的。**不违反红线**（红线只管沉淀写入），但要写明：隐式票缺失时投票层退化为"明票 + 分桶报表"，已被问题 1 的"沉默即赞成"短板覆盖。
- 问题 4 的"召回时执行前再向人确认"同样依赖宿主模型自觉，且方向危险（该确认没确认就执行）。**建议补一条硬保证：高危规则在 MCP 返回里不给可执行原文，只给"需确认"状态 + 摘要**，把"靠模型自觉"换成"靠返回内容缺失"——这是 8a 红线原则在召回侧的第二次应用。

### E. 实施分层（依赖决定顺序，不是题号）

硬约束链：`5 embedding → 6.2 判重向量通道 → 3 的 G3 归并执行端`；`① risk → 4 双确认 → 9 路径启发式`；`③ host → 1 分桶报表 + 8a 沉默报警`。按题号 1→9 做，会在问题 1 撞上没有 host 维度、在问题 6 撞上还没有 embedding。

| 层 | 内容 | 依据 |
|---|---|---|
| 0 基础件 | ①risk 字段+分类器、②定时载体=watcher、③host 维度、⑤embedding 出境纪律、⑥⑦措辞纠正 | 被四题依赖且体量小 |
| 1 正确性 | 8b.2 `claim` 竞态、8b.1 按库锁、8c 推断接上 | 竞态是当下正在发生的双份消耗，最该早做 |
| 2 观测与投票 | 1 三层机制、2 `memory_mark`（`retrieval_events.request_id` 已存在，不改表）、④聚合表扩维 | 先有数据，自动档才有刹车 |
| 3 召回 | 5 混合召回；`reindex-embeddings` **必须是独立命令**，不得挂进 `rebuild_fts`（memory.py:1297 会 `DROP TABLE memory_fts` 全量重建并顺带 `_rebuild_projections`，挂进去等于每次重建 FTS 都烧一遍远端 embedding） | 4 的"使用时严"依赖解释里的语义分 |
| 4 性能 | 6 三热点（按⑥⑨修正后口径） | 向量通道需 3 完成 |
| 5 执行端 | 3 的 G1/G2 接线 + G3 归并执行函数（需 4 的双通道判重） | 依赖最多，理应最后 |
| 6 文档 | README/docs 一次改四处：5.7 红线措辞、7.1 shared 语义、8a.4 读写现状、9.1 规则永不完备 | 攒起来避免同段改四遍 |

**状态：已定（2026-09-29 晚追加；①—⑤ 为约束、⑧⑨ 为纪律、D 段高危返回形态待用户拍板）**

### F. 审阅二次修正（②/④/D 从"方向"改为"设计"）

外部审阅指出 A②、B④、D 三处只给了方向、没给设计，雷在失败语义、桶基数与 token 保证上。以下为定稿，**取代前述对应表述**。

**F1（取代 A②）定时轮：不做"一批"，改为每步独立记账 + 一条派生不变式**
- **取消全局时间戳**，改为 `scheduled_runs(step, last_success_at, last_error, attempts)`；记账语义直接复用 `jobs` 表现成的 `attempts` / `dead_letter` / `retry_job`（queue.py:387-412），不新造机制。副产品满足"沉默即报警"：doctor 能报出"G1 归并已 3 天未成功"，而非笼统报 daemon 存活。
- **明确不做整批回滚**：四步分属不同数据面（改库内容 / 写聚合 / 删原始事件），整批事务会把已成功的 G1 归并一起回滚，而 G1 是真改库内容的操作，方向更危险。规则：**跑到哪算哪，每步独立记账，任一步失败不阻塞其它步，下次单独补。**
- **硬约束（最易埋雷处）**：`apply_exact` / `apply_conflict` 均收 `idempotency_key`（memory_merge.py:153、:170），关系 id = `digest(idempotency_key, ordinal)`（memory.py:1017），故重跑无害——**前提是 key 由候选身份派生**（如 `daily:g1:{canonical_id}:{dup_id}`）。**key 内绝不得多含日期或轮次**，否则同一重复对每天新写一份关系，表面正常、实际每日加倍。
- **"清理成功但聚合失败"用结构消除而非约定**：180 天清理不是一步，而是不变式 `purge_cutoff = min(now - 180天, aggregated_through)`——删除边界由聚合进度推导，聚合未推进则物理删不到未聚合数据。`DELETE WHERE created_at < cutoff` 幂等；聚合以"月"为事务单位，**重算即覆盖写，不做累加**，崩溃重跑无害。

**F2（取代 B④）撤掉七维交叉表，改为单条粒度汇总 + 查询时 join**
- **基数账（审阅要求补算）**：七维全交叉理论基数 ≈ month(在存 7) × host(≤5) × model(≤10) × prompt_version(≤20) × evidence_level(6) × stage(6) × outcome(≈5) = **百万级桶**，而个人库真实事件量为万级 → 主要后果不是表变大，而是**多数桶为空、非空桶近半仅 1–2 条，比率失去意义**。故分母问题是本条主问题（原表述完全未提，已纠正）。
- **改法**：`signal_counts(month, memory_id, version, signal, events)`，主键 `(month, memory_id, version, signal)`。`signal` 为**单一枚举**，把 stage 与反馈结果吸收进去（`stage:applied` / `vote:harmful` / `vote:helpful`…），专为消除 `stage × outcome` 笛卡尔稀疏。基数降为 记忆数 × 月 × 少量 signal = 万级，可全表扫。
- **类别级分桶不物化**：host / model / prompt_version / evidence_level 在报表查询时 join `memories` 的列计算。这让 A①/B④ 里"把三列提到 `memories`"的理由更硬——这三个维度今天只埋在 `snapshot_json` 内（memory_versions 仅 5 列，memory_schema.py:14-16），不提列则 join 需反序列化整库。
- **写放大**：事件写入路径**不** upsert；每日轮重算当月（覆盖写，约几千行），历史月仅跨月首日重算一次。与 F1 的"按月重算"是同一纪律。
- **分母纪律**：报表演示一律给绝对数+比率（`3/12 反对`）；**桶内样本 < 20 时只标"样本不足"、不出百分比**。同时治理问题 1"沉默即赞成"在小桶上的噪声。
- 十万条以上再物化类别级表，姿势同问题 5 的 `sqlite-vec`：**留升级路，不预先施工**。

**F3（细化 D）confirm_token 必须是服务端状态，否则保证等于零**
- **签发方 = Context Forge**：召回遇 `risk=high` 返回 `confirmation_required` + 摘要 + `token`；token 为 **uuid4 随机句柄、不由内容派生**（模型算不出第二个）。**不采用 JWT/自签串**——本地无密钥体系，可计算即等可伪造。
- **存储 = 库内新表**：`pending_confirmations(token PRIMARY KEY, memory_id, version, request_id, issued_at, expires_at, used_at, issued_to_session)`，随 `memory.db` 持久，进程重启不丢。
- **校验 = 服务端**：`context_forge_memory_confirm(token)` 查四项——存在、未过期（TTL 建议 10 分钟）、`used_at IS NULL`、换取的 `(memory_id, version)` 与签发一致。**版本变更即失效**，与问题 7.4"版本变更 = 重新审批"同源。
- **防重放**：一次性，用后置 `used_at`；同 token 二次调用返回"已使用"，并**记异常事件进报表**（重复确认尝试本身即信号，提示宿主在自循环自动确认）。
- **残余风险（写明，不掩盖）**：本设计保证"**不调 confirm 就拿不到可执行正文**"，**不保证"人真的被问过"**——confirm 亦是一次工具调用，模型可自行按下。要再兜一层只有两条路：(1) 报表侧抓异常模式（高危秒确认且上下文无人类输入迹象），可观测但**只能事后**；(2) 确认由本地 `forge` 侧发起（TTY 提示/系统通知），模型只能触发不能代答——唯一硬保证，代价是与问题 4"不打断心流"直接对冲。**默认只做 (1)，(2) 留给用户拍板。**

**F4（用户选定 B）高危确认走"异步旁路批准"，并更正 F3(2) 的技术前提**
- **技术前提更正**：F3 里写的"TTY 提示"在召回那一刻**做不到**——MCP server 是宿主以子进程拉起的 stdio 服务（mcp_server.py:177 `run(transport="stdio")`），身后没有终端。同步弹窗只剩"系统通知 + 另起进程"一条路（跨平台代码量大且属真打断），故不采用。
- **定稿流程**：召回遇 `risk=high` → 返回"待批准 + 摘要 + token"，**规则此刻不可执行**；人在自己的终端跑 `forge approve`（列出待批 → 选择 → 批准）→ token 生效 → 宿主再调 confirm 换取正文。形状沿用仓库既有习惯（所有需人点头的操作都是 dry-run + `--apply`：cli.py:386/435/494，`retag-vault --apply` 还自带备份与 rollback）。
- **代答防护三件**（宿主 Agent 多有 shell 权限，`forge approve` 理论上可被代跑，故这是**抬高成本 + 可审计**，不是绝对屏障）：
  1. `approve` 在 `sys.stdin.isatty()` 为假时**直接拒绝**（工具管道通常非真终端，此条可挡掉绝大多数代跑）；
  2. 批准时显示随机**挑战码并要求回读输入**；
  3. 批准事件记录 tty 状态、时间、来源，进问题 1 的报表层，**事后可审**。
- **明确接受的代价**：高危规则**不能立即生效，需等人批一次**——用延迟换不打断。
- **明确不采纳**：C 方案同步桌面通知确认（最硬但真打断，且需新写跨平台通知代码）。
- **对问题 4 的改写影响（追加，不回改）**：问题 4 第 2 条"召回时由宿主向人确认、不得直接执行"的**落地形式确定为 F4**——不是"宿主自觉询问"，而是"拿不到正文即无法执行"。这是 8a 红线（不靠自觉）在召回侧的第二次应用。

**状态：已定（2026-09-29 晚，F1/F2 取代 A②/B④ 的表述；F3+F4 定稿高危确认形态，D 段"待拍板"事项已闭合）**

### F5（2026-09-30 补）隐式票缺半个入口：定 A 方案

**实测**（临时库、一条 active 记忆）：`3 次 applied + 2 次 validated` → `retrieval_events` 5 行，
`weight 1.0 → 1.0`，`memory_applications` **0 行**。原因：`_weight_events` 只在该 memory/version 存在
`attribution='confirmed'` 的 application 记录时才计入 applied/validated（当初为止住"自报采纳就抬权重"设的闸），
而 `record_application` 在生产代码里**零调用者**（只有测试在调）。所以 `context_forge_memory_mark` 单独上线后，
隐式票是"记得到、看不见效果"的空转，问题 1 的"投票数据当自动档油门刹车"这条依赖悬空。

**定：A —— 补 `context_forge_memory_apply` 写 application 记录。**

- `confirmed` **不允许宿主自封**：`attribution='confirmed'` 必须带验证来源（schema 已有的
  `validation_source_ids_json` / `actual_outcome`），即"结果真的好"要有证据撑着；只凭一句"我用了且没问题"
  最多落成 `plausible`/`unknown`，不参与权重。
- 明确不采纳 **B**（放宽 `_weight_events` 让 applied/validated 自身计票）：等于把权重变成宿主可自刷的指标，
  正是当初否掉的方向。
- 明确不采纳 **C**（mark 只当观测、权重靠明票）：会让"隐式票"这条设计整条作废，而它正是"不打断心流"下
  唯一能持续收到信号的路；明票只作强信号补充的现状不需要靠废除隐票来维持。

**状态：已定（2026-09-30），任务 `10-05-memory-apply-tool`**

### G. 第 0 层任务拆分（2026-09-29 晚，用户确认拆解后建档）

| 任务目录 | 内容 | 归属调整 |
|---|---|---|
| `09-29-risk-field-classifier` | A① 四步（枚举化 / `context_forge/risk.py` 纯函数分类器 / `memories.risk` 列 / 待审留 `candidate`） | **审阅⑧ 并入本任务**：`PROMPT_VERSION` 常量与 prompt 文本同模块 + pytest 钉住 prompt 哈希，改 prompt 不 bump 即失败 |
| `09-29-scheduled-run-ledger` | F1 账本：`scheduled_runs(step, last_success_at, last_error, attempts)`、每步独立事务、幂等键派生做成测试、doctor 报超期 | 首批**只接 `retention`**（现纯手动，cli.py:385）；`inferred_advance` 由 8c 任务自行注册，两任务不互等 |
| `09-29-host-dimension` | A③：`EpisodeRecord` + `session_episodes` 加 host 列，走 `queue._migrate()` 模板，透传已存在的 `source` | 与 F2 的 `model/prompt_version` 提列协调命名，避免同表改两次 |
| `09-29-egress-surface-visible` | `doctor` 增加 `egress-surface` 项，remote 时明示发往哪个 provider、none/offline 报"无出境" | **T0.4b 转约束条款**：embedding 落地时输入必须取 `sanitize_text` 后文本、复用同一三态与出境明示（现无代码可改，约束记于本行，第 3 层任务引用） |

范围纪律：四个任务共同的不做项——不新建 daemon、不新增依赖、不新增用户配置项、不动 8b 的锁与
autostart、不做第二家宿主适配器。30 天（session/episode 保留）与 180 天（`retrieval_events` 保留）
是**两套语义**，后者属第 2 层且删除边界必须按 F1 由聚合水位推导。

**状态：已建（4 个任务，均 `--no-start`，未影响当前激活任务）**

---

## 进度看板

- ✅ 问题 1 沉淀质量评估 → 事后投票三层机制
- ✅ 问题 2 反馈录入口 → 并入问题 1 实施范围
- ✅ 问题 3 整理执行端 → G1-G4 分档四件套
- ✅ 问题 4 发布闸门 → 全自动主链路 + 高危双确认
- ✅ 问题 5 召回漏检 → 混合召回（BM25+放宽+SQLite BLOB 语义，可选件）
- ✅ 问题 6 性能热点 → 三处全修；判重与召回同标准（指纹统一 G1、字面+向量双通道 G3）
- ✅ 问题 7 权限模型 → 保险箱对内敞开（现状即对）+ 一用户一库 + VaultPolicy 收拢；出库闸门设计存档、挂起至云端立项
- ✅ 问题 8a 宿主耦合 → 读侧已通用；写侧 T1+T2 为确定性主链路、T3 只作支线（红线：不得把写入变成概率事件）+ 沉默即报警；本轮仅 README 写清现状，多宿主适配器下个迭代
- ✅ 问题 8b 进程形态 → 按库的文件句柄锁 + `claim` rowcount/BEGIN IMMEDIATE 竞态修复（优先级更高）+ 止血死命令 `install-daemon` + `watch` 改名 `watch-vault`；不合并成单一 daemon
- ✅ 问题 8c inferred 终结从未执行 → 只看文件的 B+D 推断（删外部参数），阈值写死、报表单列、与 explicit 同管道；否决 A（枚举进程）与 C（只靠 hook）
- ✅ 问题 9 脱敏偏弱 → 威胁模型先定（唯一外泄面 = 远端模型 API 调用）；规则扩充 + 敏感路径启发式接 risk=high 双确认，现在做；高熵检测只报警不替换；"tool_result 整段不出境"挂起待用户立项；否决 NER/PII 全量识别
- **挂起项**：问题 7 出库闸门（待云端立项）、问题 9 第 4 条（待用户选边）、多宿主适配器与跨平台 `install-daemon`（下个迭代/另立项）。
- ✅ 整体审阅 → 追加"跨题关联与实施分层"一节：3 个未认领基础件（risk 字段、定时载体、host 维度）、1 个决策互撞（聚合表饿死投票报表）、1 条新出境路径（embedding）、2 处措辞纠正（fingerprint 索引已存在、CC 的 T2 已实现）、7 层实施顺序（**不等于题号**）
- ✅ 审阅二次修正（F1/F2/F3/F4）→ 定时轮改"每步独立记账 + `purge_cutoff` 派生不变式"（并写死 `idempotency_key` 不得含日期）；聚合表撤七维交叉、改单条粒度 `signal_counts` + 查询时 join + 样本 <20 不出比率；`confirm_token` 定为服务端随机句柄 + 库内一次性表；**F4 选定异步旁路批准**（`forge approve` + isatty 拒绝非终端 + 挑战码 + 审计；代价=高危规则延迟生效；同步桌面通知方案不采纳）。另更正 A①：`MemoryDraft.risk` 已存在但是休眠字段
- ✅ 第 0 层已建 4 个 Trellis 任务（见 G 节）：`09-29-risk-field-classifier`（含审阅⑧的 prompt 哈希测试）、`09-29-scheduled-run-ledger`（首批只接 retention）、`09-29-host-dimension`、`09-29-egress-surface-visible`；T0.4b（embedding 出境纪律）转为约束条款
- **下一步**：第 1 层任务已建（`09-30-claim-race-fix`、`09-30-per-db-instance-lock`、`09-30-inferred-finalization-wiring`）；
      其中 **8b.2 的前提被 `09-30-claim-race-fix` 的实测推翻**（6 进程抢 400 job，`double_claims=0`）：
      原代码没有可复现的双重领取，真实缺陷是"保证无处声明"。已改为单语句领取（对齐 `claim_extraction` 的形状）
      + `tests/test_queue_concurrency.py` 并发守卫，作废 `BEGIN IMMEDIATE` 与"最高优先级"两条；全量 270 passed
- ✅ 8c 已实施（`09-30-inferred-finalization-wiring`）：`advance_inferred` 删掉 `process_alive`/`active_tools`，
      改为纯文件判据（时间戳 + 末行未应答 `tool_use` 抑制 + "出现新 transcript 即视为人在活动"短路 grace），
      缺失/不可读 transcript 按"仍在飞行"跳过。`metrics()['episodes_inferred']` 单独可数。全量 **274 passed**，
      三处变异验证都变红。
      **有意的节奏偏离**：执行点从"每日 scheduled step"改为"**每轮 watcher pass**"——30 分钟阈值配每日轮会让规则延迟到一天，
      而这段逻辑先比时间戳、几乎不花钱；真正花钱的模型调用仍由同一条 pass 里既有的 `process_pending_finalizations` 驱动。
      附带修正：`--once` 现在会推进 inferred（非删除类维护），但**仍不跑 retention**。
- **剩余第 1 层**：`09-30-per-db-instance-lock`（因 8b.2 实测更正，已从"正确性问题"降级为"卫生问题"）
- ✅ `10-05-signal-aggregates` 完成（全量 297 passed，4 次变异验证）：`memories(host/model/prompt_version)` 派生缓存
      + `Provenance.host`（默认 `'unknown'`，因为导入/手写记忆没有宿主）+ `signal_counts` 覆盖式月重算
      + 剪枝边界由 `aggregated_through()` 派生、无聚合则不删 + 无 memory.db 时 `signals` 步**跳过而非记成功**。
- ✅ `10-05-vote-report-buckets` 完成（全量 **304 passed**，4 次变异验证）：第四维度 `evidence_level`（schema 7）、
      `bucket_report()` 三轴（host / model+prompt_version / evidence_level）、样本 <20 只给绝对数、
      **分母含 suppressed/archived**（防"清理坏记忆来美化比率"）、`memory-report` 增 `buckets` 段；
      `doctor.check_host_silence` + `status.episodes_last_by_host` 落地时间维度的沉默报警。**第 2 层四件全部完成。**
- ⚠️ 实测缺口记务：**8a.3 的"解析失败次数"无采集点** —— `watcher.scan_once` 对 stat 失败 / 读不出 meta /
      无完整行的文件是静默 `continue`。故当前沉默报警**只有时间维度**。已建 `10-08-scan-failure-counter`（P3）承接。
- ✅ 第 2 层已建三件：`10-05-memory-mark-tool`、`10-05-signal-aggregates`、`10-05-vote-report-buckets`
- ⚠️ `10-05-memory-mark-tool` 已完成（全量 **275 passed**），但**实测暴露隐式票只做了一半**：
      3 次 applied + 2 次 validated 都进了 `retrieval_events`，`weight` 仍是 `1.0 → 1.0`，因为
      `_weight_events` 要求同 memory/version 存在 `attribution='confirmed'` 的 application 记录才计票，
      而 `record_application` 在生产代码里**零调用者**。三条出路（A 补 `memory_apply` 且禁止宿主自封 confirmed /
      B 放宽计票门槛（等于让宿主自刷权重，原设计已否）/ C 承认 mark 只是观测信号、权重靠明票）**待用户选定**。
      选定前，第 2 层的"投票驱动自动档"这条依赖是悬空的。
- ✅ F5 的 A 方案已实施（`10-05-memory-apply-tool`，全量 **288 passed**）：`record_host_application` 服务端派生
      attribution（`confirmed` 需"有结果 + 指得出该版本的证据来源 + 同 request_id 上已有 validated 回报"三件齐备），
      宿主不能自封；隐式票现在**真的会移动权重**（实测断言 `weight != 1.0`）。
      实施中发现并修掉一个设计缺陷：`application_id` 原方案只由 `(task_id, memory_id, version)` 派生，
      会让"先报 plausible、补证据后再报 confirmed"的正常流程撞 `IdempotencyConflict`（e2e 复现）；
      改为把 `attribution` 纳入 id 种子，弱记录留档、强记录另成一行，权重只看是否存在 confirmed 行故不叠算。
- ✅ 第 0 层实施进度：`09-29-host-dimension` 完成（全量 234 passed，列序守卫变异验证 14 failed）；
      `09-29-risk-field-classifier` 完成（全量 251 passed，kind 门与 migrate 回填各做过变异验证）；
      `09-29-scheduled-run-ledger` 完成（全量 258 passed，含新增 7 例）。**已提交** `9b66941`（代码）、
      `2c27dcd` + `590255f`（`.trellis/tasks`、`.trellis/spec` 进版本控制）、`9028628`（ledger）。
- ✅ `09-29-egress-surface-visible` 完成（全量 269 passed，三处变异验证）：`gateways.egress_surface` 单点派生、
      `doctor` 的 `egress` 行在 provider 拼错时**会失败**、`forge status --json` 带 `egress` 字段、
      README 写清"出境面由 model_provider 决定"。**第 0 层四件事全部完成**（尚未提交的只有 egress 这一件）。
- ⚠️ risk 落地时对原方案有三处主动偏离（详见该任务 PRD 的实现记录）：枚举去掉 `low`（无人赋值的档级就是下一个休眠字段）；
      模型自报冲突**改为降级 + 报表看分布**（记录自报值需要新增原始 payload 留存结构，不属第 0 层）；
      **未改发布行为**——risk=high 今天仍自动 publish，让高危停在 candidate 必须与 F4 批准通道同时上，
      否则是把它永久关进黑屋。第 0 层交付的是"可判定、可落库、召回解释里可见"。
