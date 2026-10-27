# signal_counts 聚合表 + 维度列提到 memories

## Goal

让报表所依赖的**桶维度**与**月度聚合**成为可重算的派生数据，并让"删原始事件"这件事
只能在聚合已经覆盖之后发生。

依据：`decisions.md` 的 **F2**（撤七维交叉、改单条粒度 + 查询时 join、覆盖式重算、样本分母纪律）、
**F1**（`purge_cutoff` 由聚合水位派生）、**问题 6.4**（180 天保留 + 聚合表不得被删）。

## 落地形状

**维度列**
- `Provenance.host`（`memory_models.py`）—— 权威值存在 `snapshot_json` 里，理由：episode 侧的 host
  在 queue.db，而 episode **30 天就被 retention 删掉**，桶不能跟着一起消失。
  已核实 `canonical_fingerprint` 不含 provenance → 加这个字段不改变记忆身份判定。
- `memories(host, model, prompt_version)` 三列是**派生缓存**（spec 允许的那一类），
  写路径在 `_put` 里从 draft 取值；`INSERT`/`UPDATE` 两条分支都写，避免新行有值、改行没值。
- 真实来源接线：finalization `_plan` 收 `episode.host`；worker 从 `store.latest_episode(session_id).host`
  取并传给 `MemoryLifecycle(host=...)`（与 `model` / `prompt_version` 同一类"这次运行是谁"的状态）。

**聚合表**
- `signal_counts(month, memory_id, version, signal, events)`，主键即前四列。
  `signal` 是**单一枚举**：`stage:<stage>` 与 `vote:<outcome>`，避免 `stage × outcome` 的笛卡尔稀疏。
  **2026-10-27 后续变更**：反对票带上出票人，枚举扩为 `vote:<outcome>:<actor>`（schema v13）；
  迁移前写的行没有出票人，重写为 `vote:<outcome>:unknown`。
- `MemoryStore.aggregate_signals(month)` = **先 DELETE 该月再写入**（覆盖式），因此重跑、崩溃后补跑都幂等；
  每日步重算**当月 + 上月**，跨月自动补齐上月尾部的迟到事件。
- `aggregated_through()` 返回 `MAX(month)`，是剪枝唯一允许的边界来源。
- `prune_retrieval_events(retention_days=180)`：只删 `month <= aggregated_through` **且** `month < (now - 180天)` 的事件；
  聚合表为空时**一行都不删**。这是 F1 那条"清理不得跑在聚合前面"不变式的可实现形式。

**消费者（不留休眠表）**
- `MemoryEffectiveness.report()` 新增 `signals` 段：`aggregated_through` / `window_months` / `totals`，
  随 `forge memory-report` 输出。

**账本语义**
- `scheduled.Context(queue, memory)`：步骤第一次需要两个库，所以给步骤传上下文而不是裸 JobStore。
- `MEMORY_STEPS = {'signals'}`：没有 memory.db 时**跳过该步而不记成功** —— 把"没做"记成"做过了"
  正是这套账本要防的那种谎。

## Acceptance Criteria

- [x] `memories` 三列在写路径被填（host=codex 的记忆查得到 codex）
- [x] 聚合覆盖式重算：跑两次 totals 不变（不是翻倍）
- [x] `vote:harmful` 这类反馈进入同一张表
- [x] 无聚合时剪枝删 0 行；聚合覆盖后只删被覆盖且过保留期的月份，当月保留
- [x] v5 库迁移到 v6：加三列 + 建 `signal_counts` + 从快照回填维度；`dry_run` 默认不改字节
- [x] 定时步在无 memory.db 时跳过 `signals` 且不写成功账
- [x] `memory-report` 输出 `signals` 段（形状被测试钉住）

## 我犯的错（已修）

1. `INSERT INTO memories(...)` 我列了 9 个列名却写了 10 个占位符 —— `10 values for 9 columns`，
   由新测试第一时间抓到。
2. 两处旧断言随 schema 版本前进而失效：`memory-report` 的键集合断言、risk 迁移测试里的
   `report['to'] == 5`（现为 6）。都属于"我把数字写死在测试里"的必然代价，已更新。

## Out of Scope

- 按 host / model+prompt_version / evidence_level **分桶算反对率**与分母纪律（样本 <20 不出百分比）
  → 下一任务 `10-05-vote-report-buckets`
- 类别级物化表（十万条以上才需要，F2 的"留升级路"）

## Dependencies

- 前置：`10-05-memory-mark-tool`、`10-05-memory-apply-tool`、`09-29-host-dimension`（episode 的 host）
- 被依赖：`10-05-vote-report-buckets`（分桶就建在这张表 + 这三列上）
