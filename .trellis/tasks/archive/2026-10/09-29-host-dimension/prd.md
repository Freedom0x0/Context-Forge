# host 维度打通：EpisodeRecord 与 session_episodes 加列

## Goal

让"这段记忆来自哪家宿主 agent"成为**可统计的维度**。它是问题 1 的分桶报表和 8a 的
"沉默即报警"的共同前置——今天库里根本没有这一列，报表无从按宿主切。

依据：`decisions.md` 的 **A③**（含落地先例补充）、**问题 8a 决策 3**。

## 背景与事实（已 grep 核实）

- `SessionEvent.source`（`domain.py:57`）与 `SessionLifecycleEvent.source`（`domain.py:163`）
  都是自由字符串 `Field(min_length=1)`，**现被写死为 `"claude_code"`**（`hooks/claude_code.py:51`）。
- `EpisodeRecord`（`domain.py:171`）**没有 host 字段**。
- `session_episodes` 表定义在 `queue.py:71-79`，列集合里同样没有 host。
- 加列先例：`queue._migrate()`（`queue.py:107-122`）—— `PRAGMA table_info(...)` 查列 →
  `ALTER TABLE ... ADD COLUMN`。

## Requirements

1. `EpisodeRecord` 增加 `host: str = 'claude_code'`。
2. `session_episodes` 增加 `host TEXT NOT NULL DEFAULT 'claude_code'`，迁移并入 `queue._migrate()`
   的既有模板（老库打开即自动加列）。
3. `EpisodeCollector.collect()` 接收 host 参数并写入；调用方（`watcher.py:135`、
   `hooks/claude_code.py`）把已有的 `source` 透传进来，**不再写死**。
4. `memory-report` 增加按 host 的最小计数（哪怕当前只有一个取值，先证明管道通）。

## Acceptance Criteria

- [x] 老库打开即自动加列；既有 episode 落 `'claude_code'`，无需回填脚本
- [x] 新采集的 episode 记录 host；`collect(session_id, path, project, host=...)` 有测试覆盖
- [ ] `forge memory-report` 输出含 host 维度计数
      —— **未按字面达成**：计数落在 `forge status`（`status.py` 的 `episodes_by_host`），
      `memory_effectiveness.py` 内无 host 代码。意图（host 成为可统计维度）达成，落地面不同。
      2026-10-13 状态归一审核记录。
- [x] 接入第二家宿主时**只需在建事件/采集时传 host**，不改管道逻辑（用一个假 host 值的测试证明）
- [x] 全量测试通过，无新增依赖

## Out of Scope

- 第二家宿主的适配器（8a 已定：下个迭代单独立项）
- watcher 多 root 配置化（`transcript_root()` 硬编码的改造属多宿主立项）
- host 的枚举白名单（`source` 保持自由字符串，按 8a 的事实描述）
- "每 host 最近成功投料时间"的完整报警报表（第 2 层，依赖本任务的列 + 报表层）

## Dependencies

- 前置：无
- 被依赖：问题 1 分桶报表（①沉淀来源通路维度）、8a 沉默即报警

## Notes

- `model` / `prompt_version` 目前只活在 `snapshot_json` 里（`memory_versions` 仅 5 列，
  `memory_schema.py:14-16`）。把它们提到 `memories` 列的动作属 **F2** 的范畴（为聚合表 join 得起），
  不在本任务内，但两者应协调列命名与迁移写法，避免同一张表改两次。

## 实现记录（2026-09-29）

改动面：`domain.py`（`EpisodeRecord.host` 必填）、`queue.py`（CREATE 末列、`_migrate()` 加列、
`save_episode` 位置式 INSERT 末尾追加 `:host` 与 `host=excluded.host`、新增 `host_counts()`）、
`episodes.py`（`collect()` 增加必填 `host`）、`watcher.py`（`HOST = "claude_code"` 常量 + 传入）、
`hooks/claude_code.py` 与 `cli.py`（`event ingest`）透传 `event.source`、`status.py`（`episodes_by_host`）。

测试：新增 4 条（host 记录、host_counts 分桶、旧库迁移后 host 成为末列且老行落默认值、
`forge status` 报出分桶），并把 12 处 `collect(...)` 三参调用改为四参。

证据：全量 `pytest` **234 passed**（改动前 218 passed + 12 failed 的中间态是必填参数在测试里暴露的位置）。
变异验证：把 `host` 挪到 `project` 之后（破坏"最后一列"约束）→ `tests/test_episodes.py` **14 failed / 2 passed**，
失败形式为 `NOT NULL constraint failed: session_episodes.last_activity_at`，即位置式插入立刻错位报错，
守卫真实有效；随后已恢复正确写法并复跑全量绿。

未提交：工作区另有 947 行此前在做的未提交改动（episode/finalization 那套），本次改动叠加在其上，**未执行任何 git commit**。

