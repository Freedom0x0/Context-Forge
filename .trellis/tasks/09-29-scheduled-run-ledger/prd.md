# 定时轮账本：scheduled_runs + watcher 挂载

## Goal

把 `decisions.md` 的 **F1** 落地成可用载体：定时步骤**各自记账、各自到期、互不阻塞**，
让第 1/2/4/5 层的定时内容有地方挂，且失败可见。

## 背景与事实（已 grep 核实）

- 常驻循环只有一个：`watcher.run()`（`watcher.py:171`），其 pass 内已调
  `process_pending_finalizations`（`watcher.py:205`）。定时步骤挂这里，**不新建 daemon**
  （8b 已定"不合并成单一 daemon"）。
- 记账形状有先例：`jobs` 表已带 `attempts` / `dead_letter` / `retry_job`
  （`queue.py:387-412`）；迁移形状有先例：`queue._migrate()` 的
  PRAGMA 查列 → `ALTER TABLE ADD COLUMN`（`queue.py:107-122`）。
- 现存的第一个可挂步骤：`forge retention --apply`（`cli.py:385`，默认 30 天，
  `store.retention_apply(days)`）——**今天纯手动，无自动触发**。
- 时间戳文件写法先例：`watcher.baseline_path()`（`watcher.py:37-40`）在 `runtime_dir()` 存 epoch。
  **本任务不用文件，改用表**（需要 `last_error` / `attempts` 这类多字段）。

## Requirements

1. **新表 `scheduled_runs`**：`(step TEXT PRIMARY KEY, last_success_at TEXT, last_error TEXT,
   attempts INTEGER NOT NULL DEFAULT 0)`，建表与迁移并入 `queue.py` 的既有模式。
2. **挂载点**：`watcher.run()` 每轮 pass 末尾调 `_maybe_scheduled(now)`；按 step 各自判断是否到期
   （周期默认 86400s，写死为常量，**不新增用户配置项**）。
3. **禁止整批事务**（F1 硬约束）：每个 step 各自一个事务，一个失败不回滚其它、不阻塞其它；
   下一个到期周期自动重试，连续失败按 jobs 的 attempts/dead_letter 语义累积。
4. **首批只接一步**：`retention`（把现有的手动 `forge retention --apply` 接成定时）。
   - 说明与调整：确认拆解时我曾把"8c 的 inferred 推进"列为第二步。**inferred 的实现属第 1 层
     （8c 任务）**，本任务只把它需要的 step 记账能力做出来；`inferred_advance` 由 8c 任务自己注册。
     这样两个任务不互相等。
5. **幂等键纪律做成测试**（F1 最易埋雷处）：定时轮写入使用的 `idempotency_key` 必须由
   **候选身份派生**（形如 `daily:g1:{canonical_id}:{dup_id}`），**绝不得含日期或轮次**；
   加一条测试——同一 step 连跑两次，关系/事件数量不得增加。
6. **失败可见**：`forge doctor` 增加一行，报出"哪个 step 超期未成功 / 最近一次错误"。
   这是 8a"沉默即报警"原则在定时轮上的落点。

## 范围陷阱（必须分开命名，勿混）

- `retention` 默认 **30 天**，作用对象是 **session / episode**。
- 问题 6.4 定的 **180 天**，作用对象是 **`retrieval_events` 原始事件**，且其删除边界必须由聚合
  水位派生：`purge_cutoff = min(保留期, aggregated_through)`。**该步属第 2 层**（等 `signal_counts`
  存在才能成立），本任务不做。

## Acceptance Criteria

- [ ] `scheduled_runs` 随库自动创建；老库打开即存在，无需手动迁移
- [ ] 到期时 retention 自动执行一次，未到期不执行；`last_success_at` 正确更新
- [ ] 人为让某 step 抛错 → 其它 step 照常执行；失败信息进 `last_error`，doctor 能报出来
- [ ] 同一 step 连续跑两次不产生重复数据（幂等键派生规则的测试通过）
- [ ] 不新建进程、不新增依赖、不新增用户配置项
- [ ] 全量测试通过

## Out of Scope

- 新建 daemon / 修改 8b 的锁与 autostart / 引入外部调度（cron、任务计划程序）
- `stage_counts` 聚合与 180 天事件清理（第 2 层）
- `inferred_advance` 的实现本体（第 1 层的 8c 任务）

## Dependencies

- 前置：无
- 被依赖：8c（注册 `inferred_advance`）、问题 3（G1/G2 每日自动档）、问题 6（聚合与清理）

## Notes

- 决策依据以 `decisions.md` 的 **F1** 为准（A② 的原始表述已被取代）
