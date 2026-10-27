# 反对率分桶报表 + 沉默即报警

## Goal

让"哪一类沉淀在被反对"变成看得见的数字，并让"某家宿主停止投料"变成会失败的检查。

依据：`decisions.md` 的 **问题 1 分桶报表层**（三轴 + 分母纪律）、**F2**（桶维度必须先成列）、
**8a.3**（沉默即报警）。

## 落地形状

**第四个桶维度**
- `memories.evidence_level` 与 host/model/prompt_version 同族（派生缓存），`_put` 两条分支都填，
  `migrate()` 加列并从快照回填。`SCHEMA_VERSION` 6 → 7。
  放在这里而不是查询时解 `snapshot_json`：分桶要 GROUP BY，反序列化整库正是问题 6 要消灭的开销。

**分桶报表**
- `MemoryStore.bucket_report(months=6, min_sample=20)`，三轴：`host`、`model+prompt_version`、
  `evidence_level`。每桶给 `memories / against_memories / against_events / support_events`。
  - 反对 = `signal LIKE 'vote:%'`（harmful / irrelevant / revoked）
    **2026-10-27 后续变更**：反对票带出票人（`vote:<outcome>:<actor>`，schema v13），平反票 `restored` 不计入反对；报表新增 `against_events_by_actor`。
  - 赞成 = `signal IN ('stage:applied','stage:validated')`
- **分母纪律**：样本 < `min_sample` 时 `against_rate` 给 `None` + `note='样本不足…只给绝对数'`。
  "2 条里 1 条反对"不配拥有一个看起来能据此行动的百分比。
- **分母故意包含 suppressed / archived 记忆**：若只数活着的，删掉几条坏记忆就能让比率变好看——
  那等于让报表可被"清理"美化。
- 聚合表为空时返回 `[]`，不凭空造桶。
- 消费者：`forge memory-report` 的 `buckets` 段（`EffectivenessReport.buckets`）。

**沉默即报警（时间维度）**
- `JobStore.host_activity()`：每 host 的 episode 数与**最近一次采集时间**。
- `forge status` 新增 `episodes_last_by_host`（`codex=2026-09-30(4)` 形式）。
- `doctor.check_host_silence(days=7)`：某 host 超期没有新 episode → **该行失败**并点名是哪一家；
  健康的 host 不被牵连；从未采集过时判 OK（无内容可沉默）。已挂进 `run_all`，并有测试钉住在册。

## 实测到的缺口（记为后续任务，本任务不做）

8a.3 原文还要求"**解析失败次数**"。核实结果：`watcher.scan_once` 对读不出 meta 或 stat 失败的文件
是**静默 `continue`**，仓库里没有任何采集点，所以"失败计数"今天无数据可报。
补它要新增一条失败记录路径（改 watcher 控制流），与分桶报表不是一件事 → 单独立务，
此处如实标注：**当前的"沉默即报警"只有时间维度，没有失败计数。**

## Acceptance Criteria

- [x] 三轴桶齐全，`memories`/反对/赞成计数为绝对数
- [x] 样本 25 的桶给出比率，样本 1 的桶 `against_rate is None` 且 note 说明原因，绝对数仍在
- [x] 聚合表为空 → `bucket_report()` 返回 `[]`
- [x] `memory-report` 输出含 `buckets` 段，三轴都在
- [x] host 1 天前有活动 → 检查 OK；某 host 30 天静默 → 检查失败且只点名它
- [x] `host-silence` 确实在 `doctor.run_all()` 里（防止"写了但没人调"）

## Out of Scope

- 解析失败计数（见上）
- 月度趋势曲线、告警阈值配置项
- 自动档据此收紧/放宽的执行端（第 5 层）

## Dependencies

- 前置：`10-05-signal-aggregates`（表与维度列）、`09-29-host-dimension`（episode 的 host）
- 被依赖：第 5 层 G3 自动档的"刹车"数据

## 实现记录与证据（2026-09-30）

- 新增 `tests/test_bucket_report.py` 7 例：三轴桶与绝对数、跨门槛的双向断言（25 样本出比率 /
  1 样本只出绝对数 + note）、空聚合返回 `[]`、`memory-report` 的 `buckets` 段三轴齐全、
  host 活动新鲜时 OK、某 host 30 天静默时**只点名它**、从未采集时判 OK。
- 连带更新：`SCHEMA_VERSION` 到 7 后，`test_risk.py` 与 `test_memory_signals.py` 里写死的
  `report['to']` 随之前进；`memory-report` 的键集合断言加了 `buckets`。
- 全量 `pytest`：**304 passed**。
- 变异验证四处，全部被抓：去掉样本门槛 → `1 failed`；分母改成只数 `active`（即"可被清理美化"）→
  `1 failed`；让沉默检查永不失败 → `1 failed`；把 `host-silence` 从 `run_all` 摘掉 → `1 failed`；
  恢复后 `18 passed`。
