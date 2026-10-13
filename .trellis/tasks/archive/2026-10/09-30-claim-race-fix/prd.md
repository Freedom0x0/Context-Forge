# 修复 claim 竞态：单语句领取 + 与 claim_extraction 同形

## Goal

让"一个 job 只交给一个进程"成为**代码声明的性质**，而不是搭在事务时序上的巧合。

依据：`decisions.md` 的 **8b.2**——但那条的前提描述被本次实测推翻，已就地更正。

## 实测（2026-09-30，先量再改）

用 6 个进程对同一个 `queue.db` 抢 400 条 queued job，每进程最多领 40 条：

```
claimed_total = 240   unique = 240   double_claims = 0   errors = []
```

**结论：原代码没有可复现的双重领取。** 我原先断言的"`claim()` 有真竞态、正在造成双份模型调用"**是错的**，
原因当时没算到：`with self.db:` 块里第一条语句是租约回收的 **UPDATE**，SQLite 的写锁因此把整段
（读 + 写）串行化了，第二个进程会阻塞到前者提交。

仍然值得改的那一半：这个保证**没有任何地方声明**。只要有人把回收改成条件执行（例如"没有过期租约就跳过"）
或把 SELECT 提到写语句之前，保证就静默消失，而且没有测试会为此变红——正是"把正确性压在一个会变的前提上"。

## Requirements

1. **领取改成单语句**：`UPDATE ... WHERE id = (SELECT id FROM jobs WHERE status='queued' ...
   ORDER BY created_at LIMIT 1) AND status='queued' RETURNING id`——选中与占用在同一语句里完成。
2. **形状对齐同文件既有先例**：`claim_extraction`（`queue.py:316-327`）已经是"单条 UPDATE + 看 rowcount"。
   本改动是向仓内既有做法靠拢，不是新发明。
3. 行为保持不变：仍然先回收过期租约，仍返回 `sqlite3.Row`，空队列返回 `None`。

## Acceptance Criteria

- [x] 新增并发测试：N 个进程、需求恰好等于供给，断言"每个 job 只被领一次"且 `running` 计数等于领取数
      （`tests/test_queue_concurrency.py`，2 个进程×15 与 3×45 的规模都跑过，约 2s）
- [x] 测试有牙齿：把 claim 改成"只读不占用"后该测试**失败**（`1 failed`），恢复后 `1 passed`
- [x] 既有队列测试不回归（`tests/test_context_forge.py` 全绿）
- [x] 改动前 / 改动后的 6 进程探测都报 `double_claims = 0`

## Out of Scope

- **不**引入 `BEGIN IMMEDIATE`：实测表明当前事务形状已经串行化；显式加锁要改 `isolation_level`，
  会连带影响同文件所有 `with self.db:` 块的语义，风险大于收益。8b.2 原文里那句"BEGIN IMMEDIATE"因此作废。
- **不**改 `lease_seconds` 语义、**不**动 `finish`/dead-letter 逻辑。
- `AND status='queued'` 这层保险在本测试下**未被证明必要**（去掉它测试仍绿），保留为防御性代码，
  但不宣称它有实测效果。

## Dependencies

- 前置：无
- 被依赖：8b.1 的实例锁是**另一个问题**（防手滑起两个 watcher），本任务不解决它

## Notes

- 探测脚本是一次性的，已随本次改动删除；固化下来的守卫是 `tests/test_queue_concurrency.py`。
- Windows 下 `multiprocessing` 用 spawn 重新导入模块，因此数据库路径必须走环境变量传递——
  第一版探测脚本就是因为把临时目录放在模块顶层，导致子进程各建各的库、测出 `claimed_total = 0` 的假绿。
