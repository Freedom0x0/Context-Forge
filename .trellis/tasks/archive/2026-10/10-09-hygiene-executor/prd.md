# 整理执行端：自动档接线 + 人审判决入口

问题 3 的实施。决策原文与全部实测在 `.trellis/tasks/09-29-memory-improvement-brainstorm/decisions.md`
（问题 3 段 + "第 5 层第 1/2 步实施结果" + 更正条目）。本文件只记进度与剩余项。

## 分档（已定，不再讨论）

| 档 | 内容 | 谁执行 |
|---|---|---|
| G1 | 完全重复（指纹全同 + 来源相同） | 自动：`apply_exact` |
| G2 | 矛盾标注（只写关系，不改内容） | 自动：`apply_conflict` |
| G3 | 近似疑似重复、跨来源的同一条 | **只给人**：`adjudicate` |
| G4 | 过时/衰减 | 只给人（报告 + 判决） |

## 已落地

- **第 1 步** `MemoryLifecycle.apply_auto_tier()` + `forge hygiene --apply`（`8313a5e`）。
  三条必须的守卫，各有测试：三成员组锚点离场即跳、冲突幂等键含左端版本号、每档上限 100 并记 `deferred`。
- **第 2 步** 每日轮新增 `hygiene` 步（`e621a36`）。没库时**跳过而非记成功**；上限被咬住时打印一行。
- **第 3 步** 人审判决（本次提交）：
  - `MemoryMerge.merge_pair()` —— 人推翻自动档的执行函数。合并**先把输家的 projects/sessions/paths
    与 sources 并到锚点**，再把输家归档并挂 `duplicate_of → 锚点新版本`；重跑幂等（不再多写版本）。
  - `queue.db` 新表 `hygiene_verdicts(candidate_key PK, action, ids_json, score, note, created_at, updated_at)`，
    `record_verdict` 按候选身份**覆盖式**记账（改主意不重复计数）。
  - `memory_lifecycle.adjudicate(candidate, action, store=, queue=, target_id=)`：动作 `keep/merge/archive/suppress`；
    `keep` 不改库但记账（反对率需要分母）；目标不在候选对内 / 动作名未知 → **先报错、不记账**。
  - `forge hygiene --review`：逐条问，`sys.stdin.isatty()` 为假 → 打印拒绝并 `Exit(2)`；
    认不得的答复"跳过且不记账"。默认只读报告多一个 `adjudicated` 段（判决计数）。

## 明确不做 / 未接

- **"标替代"（supersede）不接**：`supersede` 要求人**另写一条替代正文**（它 publish 一个新 Candidate），
  那是编辑动作，不是对一对候选的判决。留给 revise/supersede 自己的入口。
  这是对原决策四动作中的一项主动收窄，理由记在此处。
- **`forge hygiene-undo <id>`（第 4 步）**：读库决定撤销哪一种——带 `duplicate_of` 出边的走 `undo_exact`
  （归档的输家回 active，且**那条关系被撤销**），带 `derived_from` 出边的走 `undo_consolidation`。
  默认 dry-run 只打印计划，`--apply` 才写。幂等键 `undo:{id}@{version}`（身份派生，不含日期）。
  认不出的记忆报 BadParameter，而不是随便撤销一个。
- **第 6 步：判决已并入报表（A 方案）**。`MemoryEffectiveness(store, queue)` 多一个 `verdicts` 段
  = `{counts, total}`，**只给绝对数不给比率**（沿用"样本 < 20 不出百分比"那条纪律）。
  没有队列句柄的调用方（如 `doctor`）拿到空段而不是假的 0。CLI `memory-report` 与 MCP
  `context_forge_memory_effectiveness` 两处都接，且 MCP 那处有测试（否则"只有 CLI 看得见"）。
- **比率本身仍未定义**：`keep` 与 `merge/archive/suppress/stale` 之间怎么算"自动档开太宽/太窄"，
  是问题 1 的口径题，不在本任务里发明。

## 验收

- [x] 自动档只碰 G1/G2，且重跑不新增版本（幂等）
- [x] 每日轮记账：无库=跳过，失败=记录错误，不静默成功
- [x] 人审 merge 保住跨来源项目的召回（实测 beta 项目仍能命中锚点）
- [x] 非终端 `--review` 被拒；认不得的答复不记账
- [x] 每步都有变异验证（第 1 步 5 处、第 2 步 3 处、第 3 步 5 处，全被抓到）
- [x] 撤销命令 `hygiene-undo`：默认 dry-run、`--apply` 才写、被撤销的关系有断言
- [x] G4 人审：`adjudicate_decay(keep|stale)` + review 循环第二段（先 G3 后 G4；`q` 中止后续阶段）
- [x] 判决计入报表：`verdicts` 段跨库读入（CLI + MCP 两处都有测试覆盖）
- [ ] 反对率的**比率口径**（keep vs 动手）仍未定义，留给问题 1

## 全量测试

第 3 步 358 passed；第 4、5 步（`hygiene-undo` 与 G4 人审）363；第 6 步（报表跨库读判决）**366 passed**。
变异验证累计：第 1 步 5 处、第 2 步 3 处、第 3 步 5 处、第 4/5 步 7 处、第 6 步 4 处
（有队列也不读、CLI 不给句柄、出口漏段、MCP 不给句柄），全被抓到。
第 6 步的 MCP 那条**第一轮抓不到**，补了 `test_the_mcp_effectiveness_tool_sees_verdicts_too` 才红。

- **第 4 步** `forge hygiene-undo`（默认 dry-run）。
- **第 5 步** G4：`adjudicate_decay(memory_id, keep|stale)`，review 循环加第二段；
  `mark_stale` 对 `role='preference'` 抛错 ⇒ 该判决被拒且**不记账**（有测试）。
- **顺带修掉一条既有缺陷**（我的命令把它变成人可达）：`undo_exact` 原先不带出边重写版本，
  而 `put` 是**替换**该版本的关系集合 ⇒ 撤销重复边会顺手删掉输家身上别的人工关系（如 `refines`）。
  现在撤销时把除被撤那条之外的出边全部带到新版本上。测试：`kinds == ['refines']`；
  变异两处（不带任何边 / 把要撤的边也带回去）各 1 红。
