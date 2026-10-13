# 接上 inferred 终结：文件证据为准，liveness 仅作抑制

## Goal

把从未执行过的 inferred 终结接上：**只用文件能证明的事实**推断"这段会话没人报丧"，
让它走和 explicit 完全相同的沉淀管道。宿主侧上报的进程/工具 liveness **只作为额外的抑制信号**，
不是放行前提。

依据：`decisions.md` 的 **问题 8c**（含"误收尾只是分两段"的依据）与 **F1**（记账形状）。

## 改动

- `episodes.py`：`advance_inferred(now, *, new_activity=False, process_alive=None,
  active_tools=None, liveness_by_session=None, pending_after, grace)`。
  阈值 `PENDING_AFTER = GRACE = timedelta(minutes=30)` 写成模块常量并注明"不做成配置"的理由。
  新增 `_awaiting_tool_result(path)`：最后一行完整记录若是 `type=assistant` 且含 `tool_use` block，
  说明有工具仍在飞行（或宿主死在半路）→ 本轮不收。
- `watcher.scan_once()` 每轮末尾调用 `advance_inferred(new_activity=..., <本轮探到的 liveness>)`；
  `new_activity` = 本轮出现过**此前没有 episode 的 transcript**（即"人在活动"，D 那条）。
- 可见性：`JobStore.metrics()` 增加 `episodes_inferred`，`forge status --json` 透出该计数。

## 红线修正（2026-10-13，状态归一时发现并回改）

原 PRD 写的是"**删掉** `process_alive` / `active_tools`"。但提交 `d4d5f81` 落地的是相反形状：
宿主 hook（`hooks/claude_code.py` 的 SessionStart/UserPromptSubmit/Stop，靠可选依赖 `psutil`）
登记本会话进程身份，`watcher` 探测后传入；闸门写成 **必须有确证 `process_alive is False`
才放行**。两者各写各的，且后者的形状踩红线的**同时**引入了 8c 本该消灭的 bug：

- 宿主**没登记**（老宿主、hook 未装/死掉、`psutil` 未装）时探测值是 `None`，
  `None is not False` 为真 → `continue`。文件证据分支（30/30 静默、`new_activity`、`_awaiting_tool_result`）
  **结构上不可达**，episode 永远停在 `open`——正是 8c 要修的"内容进不了库"（`decisions.md:628`）。
- 这等于让写入重新押在宿主 hook 是否配合上，即 `decisions.md:642` 明令不许的概率事件。

**回改（Option 2）**：闸门反转为**只在确证活着时抑制**——
`if session_process is True or session_tools is True: continue`。
未知（`None`）落回纯文件证据路径，与本文档 Goal 一致；确证活着或确有工具在飞仍抑制。
代价：保留 `hooks/liveness.py` / `queue.py` 的 `session_*` 表 / hook 登记 / `psutil` 可选依赖，
但它们降级为**增强项而非前提**——没有它照样能收尾，有它多一层准确性。

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
- [x] **红线修正**：宿主未登记 liveness（探测值 `None`）时，30/30 静默仍能推断收尾——
      即 inferred 不依赖 hook / `psutil` 存在（对应 `decisions.md:642` 红线）
- [x] 确证活着（`process_alive is True`）或确有工具在飞（`active_tools is True`）时仍抑制收尾
      —— 2026-10-13：闸门反转为 `if session_process is True or session_tools is True: continue`；
      `tests/test_episodes.py::test_inferred_end_falls_back_to_file_evidence_when_liveness_is_unknown`
      钉住两个方向（变异回 `is not False` → RED）。全量 439 passed。

## Out of Scope

- 多宿主 root 配置（8a 下迭代）
- 分桶报表层里"按 end_kind 细分反对率"（第 2 层，投票载体到位后做）
- 8b.1 的实例锁：与本条无关

## Notes

- 已知残余：误收尾会把一段对话分成两段沉淀（`collect()` 自动开新 segment），不丢数据；
  该代价靠 `episodes_inferred` 计数 + 后续报表观察。
- 决策依据以 `decisions.md` 的 **8c** 为准。
