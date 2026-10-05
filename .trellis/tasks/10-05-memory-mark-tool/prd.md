# 隐式票入口：MCP 写回工具 memory_mark

## Goal

给宿主一个**只能报"我对这条记忆做了什么"**的写回通道，补上问题 1/2 里"隐式票没有录入口"的缺口，
同时不把"被展示"算成票。

依据：`decisions.md` 的 **问题 1（三层机制 · 能见度层）**、**问题 2**、**8a 红线**。

## 改动

- `memory.py`：新增两个模块常量，把阶段词汇集中到一处
  - `RETRIEVAL_STAGES`（全部六档，供 `record_retrieval` 校验，取代原来内联的字面集合）
  - `MARKABLE_STAGES = ('referenced', 'applied', 'validated')` —— **曝光三档（retrieved / tool_returned /
    presented）不允许由宿主上报**
- `mcp_server.py`：新工具 `context_forge_memory_mark(memory_id, request_id, stage, version=0,
  session_id="", note="")`
  - `version=0` 表示"当前版本"，宿主不必自己猜版本号
  - 幂等键沿用召回路径已有的形状 `f'{request_id}:{stage}:{memory_id}:{version}'`，重发不会叠票
  - 返回值带**当前 weight**，让调用方能看见这次标记有没有改变什么
  - 非法 stage / 缺 request_id → 抛错（FastMCP 会转成 `isError` 文本回给宿主）
- `tests/test_mcp_transport.py`：抽出 `_start_server` / `_stop_server` / `_tool` 三个助手（原来那 20 行
  进程脚手架在测试里是内联的），新增一条 mark 的真 stdio e2e。

## Acceptance Criteria

- [x] 工具出现在 `tools/list`
- [x] `stage="applied"` 写入 `retrieval_events`，且**重发同一 (request_id, stage, memory, version) 只留一行**
- [x] `stage="presented"` 被拒：`isError=True`，错误文本点名允许的三档
- [x] 变异验证：把 `presented` 放进 `MARKABLE_STAGES` → e2e **1 failed**；恢复后 2 passed
- [x] 全量 `pytest`：**275 passed**

## 实测发现：这个入口今天只补齐了一半（已定 A 方案）

新工具能把票记进 `retrieval_events`，但**记了不改权重**。实测（临时库，一条 active 记忆）：

```
3 次 applied + 2 次 validated  →  retrieval_rows = 5
weight: 1.0 → 1.0（applied 后 1.0，validated 后仍 1.0）
memory_applications rows = 0
```

原因在既有的权重纪律里：`_weight_events`（`memory.py:810` 区段）**只在该 memory/version 存在
`attribution='confirmed'` 的 application 记录时**，才把 applied/validated 计入权重——这是当初为防止
"自报采纳就抬权重"设的闸。而 `record_application` 在生产代码里**没有任何调用者**（只有测试在调）。

也就是说：按现在的决定继续做下去，隐式票会一直是"记得到、看不见效果"的空转。

**定：A（用户 2026-09-30 选定）** —— 补 `context_forge_memory_apply` 写 application 记录，且**不允许宿主自封
`confirmed`**：`confirmed` 必须由服务端从验证来源（`validation_source_ids_json` / `actual_outcome`）派生，
不接受调用方声明。详见 `decisions.md` 的 **F5**；B（放宽 `_weight_events`，等于让宿主自刷权重）与
C（隐式票退出权重通道、只靠明票）均不采纳。

## Out of Scope

- application 写入口 → 独立任务 `10-05-memory-apply-tool`
- 报表分桶与 `signal_counts`（`10-05-signal-aggregates`）

## Notes

- 决策依据见 `decisions.md` 问题 1 / 问题 2。
