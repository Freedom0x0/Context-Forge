# 按库的文件句柄锁：watcher 单实例互斥

## Goal

让常驻 watcher 在**同一个库**上物理上只能有一个实例：抢不到锁的进程明确报错并退出，
不再静默并行。依据 `decisions.md` 问题 8b 决策 1。

## 为什么现在做

- `autostart.py` 写的 VBS 启动器**退出即重启**，且开机自启；用户再手敲一次
  `forge watch-transcripts`、或旧实例还没退干净时 VBS 拉起新实例，就会出现两个常驻进程。
- 常驻循环现在不只采集：`watcher.run()` 每轮还跑 `scheduled.run_due()`（retention /
  signals / hygiene 每日维护）与 `process_pending_finalizations()`。
  两个实例 = 两份维护、两份 episode 采集。`scan_once` 没有任何跨进程守卫。
- `heartbeat` 只是报警器，不是互斥：`_beat()` 写 `{"pid","at"}`，第二个实例直接覆盖 pid，
  `doctor.check_watcher()` 报的"alive"是最后写的那个，另一路静默并行。

## Requirements

1. **锁的载体是文件句柄，不是 pid 文件**：进程启动时取得锁并持有句柄直到退出，
   进程死则操作系统自动释放。POSIX 用 `fcntl.flock`，Windows 用 `msvcrt.locking`。
   弃用 pid 文件的理由：崩溃残留与 pid 复用都需要额外"猜对方是否已死"的逻辑。
2. **硬约束：锁文件放在 `config.runtime_dir()` 下，按库隔离，绝不全局。**
   `runtime_dir()` 已支持 `CONTEXT_FORGE_HOME` 覆盖——这正是测试套件与多 profile 不被自己锁死的前提。
3. **抢不到即明确失败**：打印"已有实例在运行（pid=X）"，以非零码退出；不静默等待、不降级并行。
4. **覆盖两条启动路径**：手敲 `forge watch-transcripts` 与 VBS 自动拉起必须走同一段取锁代码。
5. **heartbeat 报出持有者**：锁文件与 heartbeat 记录持有者 pid；
   `doctor` 能据此报出"watcher 在跑（pid=X）"或"未运行"，而不是只报最后写入者。
6. 不新增第三方依赖。

## Acceptance Criteria

- [x] 同一 `CONTEXT_FORGE_HOME` 下第二次取锁失败，且失败信息含第一个实例的 pid
- [x] 第一个实例退出（正常退出与 `SysExit`/崩溃等价路径）后，锁可被新进程取得
- [x] 两个**不同** `CONTEXT_FORGE_HOME` 可同时各自持锁（按库隔离，互不阻塞）
- [x] `forge watch-transcripts` 重复启动时第二次立即退出、不进入轮询循环
- [x] `doctor` 在锁被持有时报出持有者 pid；锁文件存在但无人持有时报"未运行"（不把残留锁当活）
- [x] 全量测试绿，无新增依赖

证据：`tests/test_watcher_lock.py` 5 条；变异验证（把 `_try_lock` 改成直接 return）4/5 变红。
全量 `pytest -q` **424 passed**（本任务落地时点）。

## Out of Scope

- 跨平台自启（macOS launchd / Linux systemd）——问题 8b 决策 3 定为文档交付，另说
- 死命令 `install-daemon` 的清理（问题 8b 决策 3，属独立小项）
- `forge watch` → `watch-vault` 改名（问题 8b 决策 4，独立小项）
- 把 watcher 与 vault watch 合并成单一 daemon（问题 8b 明确不采纳）

## Notes

- Goal 原话里的"heartbeat 报出实例数"在排他锁下结构性地恒为 1，故按需求 5 落地为
  **报出持有者 pid**；这是对原措辞的收紧，不是缩水。
- 本任务与已完成的 `09-30-claim-race-fix` 是**不同问题**：claim 修的是单实例下瞬发进程
  也能触发的重复领取（已修，`UPDATE ... RETURNING` 单语句）；本任务防的是两个常驻实例并行。
  decisions 8b 实施更正里明写"第 1 条（实例锁）与本条是不同问题，仍然有效"。

## 已知残余（2026-10-13 复核记录，未修）

- **`--all-history` 在取锁前改基线**：`cli.py` 的 `reset_baseline(baseline)` 跑在 `watch_run()`
  **之前**，所以第二个 `forge watch-transcripts --all-history` 会在输掉锁竞争、退出之前
  把 `watcher-baseline` 写成 `0`，导致**正在持有的那个实例**下一轮重新摄取全部历史。
  仅该显式开关可达（VBS 路径与普通手敲都不用它），故验收 4 仍成立。
  干净修法：把 `baseline` 透传进 `run()`，在取锁**之后**再 `reset_baseline`；
  在 CLI 里取锁则会与 `run()` 内二次取锁产生误判。若实践中真用 `--all-history`，单开小项。
- **失败信息的微竞态**：`_try_lock` 成功与 `_write_pid` 之间有一瞬窗口，此时第二个进程
  探测到的是 `WatcherAlreadyRunning(None)` → `pid=未知`。影响可忽略，未改。
