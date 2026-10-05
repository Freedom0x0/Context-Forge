# 接上 inferred 终结：只看文件的 B+D 推断

## Goal

把从未执行过的 inferred 终结接上：**只用文件能证明的事实**推断"这段会话没人报丧"，
让它走和 explicit 完全相同的沉淀管道。

依据：`decisions.md` 的 **问题 8c**（含"误收尾只是分两段"的依据）与 **F1**（记账形状）。

## 改动

- `episodes.py`：`advance_inferred(now, *, new_activity=False, pending_after, grace)`
  —— **删掉 `process_alive` / `active_tools` 两个外部参数**（当初正因为要它们才没人接）。
  阈值 `PENDING_AFTER = GRACE = timedelta(minutes=30)` 写成模块常量并注明"不做成配置"的理由。
  新增 `_awaiting_tool_result(path)`：最后一行完整记录若是 `type=assistant` 且含 `tool_use` block，
  说明有工具仍在飞行（或宿主死在半路）→ 本轮不收。
- `watcher.scan_once()` 每轮末尾调用 `advance_inferred(new_activity=...)`；
  `new_activity` = 本轮出现过**此前没有 episode 的 transcript**（即"人在活动"，D 那条）。
- 可见性：`JobStore.metrics()` 增加 `episodes_inferred`，`forge status --json` 透出该计数。

## 与决策记录的一处偏离（有意，需要确认）

**执行节奏从"每日 step"改为"每轮 watcher pass"**。理由：30 分钟的阈值配每日轮，等于规则实际延迟可达一天；
而这段逻辑本身几乎不花钱（先比时间戳，只有进入疑似/待收状态才读文件末行），每 15 秒跑一次可以接受。
真正花钱的模型调用仍由既有的 `process_pending_finalizations` 在同一条 pass 里驱动，未新增通道。

顺带修正 8a.4 的措辞遗留：`--once` 现在也会推进 inferred（它不是删除类维护），但**仍不跑 retention**。

## Acceptance Criteria

- [x] 30 分钟静默 → `inferred_pending`；再 30 分钟 → 生成 `end_kind='inferred'` 凭据
- [x] 出现新 transcript 时，已静默满 30 分钟的 open episode **直接收尾**（跳过第二段等待）
- [x] 末行是未应答 `tool_use` 时**不推进**（open 不转 pending，pending 不收尾）
- [x] transcript 缺失/不可读时按"仍在飞行"处理：跳过本轮，不抛异常、不误收尾
- [x] `metrics()['episodes_inferred']` 单独可数，`forge status` 可见
- [x] 走的是同一条管道：`request_finalize(...)` → 既有闸门，无专用分支
- [x] 全量 `pytest`：**274 passed**
- [x] 变异验证三处：去掉 tool_use 抑制 → `2 failed`（顺带证明缺失文件那条也在它保护下）；
      去掉 new_activity 短路 → `1 failed`；scan_once 里不推进 → `1 failed`；恢复后全绿

## Out of Scope

- 多宿主 root 配置（8a 下迭代）
- 分桶报表层里"按 end_kind 细分反对率"（第 2 层，投票载体到位后做）
- 8b.1 的实例锁：与本条无关

## Notes

- 已知残余：误收尾会把一段对话分成两段沉淀（`collect()` 自动开新 segment），不丢数据；
  该代价靠 `episodes_inferred` 计数 + 后续报表观察。
- 决策依据以 `decisions.md` 的 **8c** 为准。
