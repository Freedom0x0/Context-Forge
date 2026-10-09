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
- **`forge hygiene-undo` 不存在**：`undo_exact`/`undo_consolidation` 是库函数，没有命令入口。
  `--apply` 的提示语如实说"还没有撤销命令"，不编命令名。
- 判决回流到问题 1 的反对率报表：账已在 queue.db，但报表从 memory.db 组装 ⇒ **本轮不做跨库比率**。

## 验收

- [x] 自动档只碰 G1/G2，且重跑不新增版本（幂等）
- [x] 每日轮记账：无库=跳过，失败=记录错误，不静默成功
- [x] 人审 merge 保住跨来源项目的召回（实测 beta 项目仍能命中锚点）
- [x] 非终端 `--review` 被拒；认不得的答复不记账
- [x] 每步都有变异验证（第 1 步 5 处、第 2 步 3 处、第 3 步 5 处，全被抓到）
- [ ] 撤销命令（`hygiene-undo`）与 G4 的人审交互
- [ ] 判决计入反对率报表（跨库口径待定）

## 全量测试

第 3 步完成时 **358 passed**。
