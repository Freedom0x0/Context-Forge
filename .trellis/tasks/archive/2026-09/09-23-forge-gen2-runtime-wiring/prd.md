# Gen2 记忆库接入运行时并证明宿主检索

本任务收口 09-21 PRD 的未覆盖验收：「**端到端证明宿主在需要时检索；只证明 MCP 可调用不算完成**」。

## Goal

让 Gen2 记忆库（`memory.db`）在生产运行时真正通电：存在、有内容、随 vault 更新且**过期可观测**；宿主（Claude Code）能通过真实 MCP 传输检索到它。并**划清可证与不可证的边界**，不用「MCP 可调用」冒充「宿主会检索」。

## Background

2026-09-23 实测生产运行时：

| 项 | 状态 |
|----|------|
| `~/.context-forge/` | `config.toml` / `index-*.db` ×2 / `queue.db` / `status.md` / `watcher-*` |
| `memory.db` | **不存在** |
| `~/.claude/settings.json` 的 `mcpServers` | **只有 `playwright`** —— Context Forge 未配置 |

后果：P2 召回、P3 归因闭环、P4 卫生**全部只有测试覆盖，生产上从未运行过**。`forge init` 会 `MemoryStore.initialize()`（`cli.py:79`），但从未重跑；且**没有任何命令把 vault 内容导进 memory.db**（`import_vault` 存在但无调用入口）。

## Requirements

### R1 创建并填充 `memory.db`
- R1.1 提供一条命令，把 vault 内容导入 `memory.db`（复用既有 `import_vault`，它已是增量的）
- R1.2 重复执行必须**幂等**（未变化的快照不重复导入）
- R1.3 导入必须**不修改 vault**（不改内容、不改 mtime）

### R2 同步与过期可观测
- R2.1 vault 变化后 `memory.db` 能跟上，无需人工记住去敲命令
- R2.2 **过期必须可观测**：`forge doctor` / `forge status` 能看出「vault 有 N 条，memory.db 只有 M 条」这类不一致，**不得静默**
- R2.3 同步失败不得静默吞掉

### R3 宿主接入
- R3.1 把 `context_forge` MCP server 配置进宿主，使工具**真的可达**
- R3.2 修改用户全局配置前**先备份**，且**只增不改**既有条目，可回滚
- R3.3 配置内容可被文档化复现（不能只存在于本机）

### R4 真实传输 e2e（核心交付）
- R4.1 以**子进程 + 真实 stdio JSON-RPC** 验证：`initialize` → `tools/list` → `tools/call` 全链路
- R4.2 `tools/call` 必须返回**来自真实 `memory.db` 的真实记忆**，不是桩数据
- R4.3 该验证必须是一条可重复运行的测试，不是一次性手工脚本

### R5 边界声明（不得含糊）
- R5.1 明确写出**已被证明**的：工具已注册、传输可用、返回真实数据
- R5.2 明确写出**未被证明**的：模型是否会在需要时主动调用（行为性，不可测）
- R5.3 **不得**在 R5.2 未证的情况下把 09-21 那条验收勾掉

## Constraints

- 不新增运行时依赖（`pyproject.toml` 的 `dependencies` 不得变）
- 不改变 Gen1 沉淀链路的行为（`worker.py` 写 vault 的逻辑不动）
- 修改 `~/.claude/settings.json` 属**用户全局配置**：先备份、只增不改、可回滚
- `memory.db` 是 Gen2 权威数据；vault 是 Gen1 事实源 —— 导入是**单向**（vault → memory.db），不得反向写

## Non-goals

- 不实现「模型是否选择调用」的自动化证明（不可证；只如实声明）
- 不做向量检索 / 知识图谱
- 不迁移 Gen1 沉淀链路到 Gen2（worker 继续写 vault）
- 不碰遗留 L3–L6

## Acceptance Criteria

- [ ] AC1 `memory.db` 存在且含 vault 的实际内容；重复导入幂等；导入不改 vault（内容与 mtime 均不变）
- [ ] AC2 vault 变化可被同步到 `memory.db`，且**不一致状态可被 `doctor`/`status` 观测**（有测试构造不一致并断言会被报出）
- [ ] AC3 MCP server 配进宿主；既有 `mcpServers` 条目**一个不少**；备份存在；移除后配置完全复原
- [ ] AC4 **真实 stdio 传输 e2e 测试**：子进程起 MCP server，`initialize` → `tools/list` → `tools/call` 成功，且返回体含真实记忆内容
- [ ] AC5 边界声明写进文档：已证 / 未证分开；09-21 那条验收**保持未勾**并注明原因
- [ ] AC6 无新增运行时依赖；`worker.py` 的 vault 写入逻辑零改动

## Notes

- 上游任务：`.trellis/tasks/archive/2026-09/09-22-falsifiable-memory-recall`（P0–P4，171 passed）
- 09-21 PRD 的原始措辞：「端到端证明宿主在需要时检索；只证明 MCP 可调用不算完成」
- AC4 是本任务的**核心**：它把「MCP 可调用」从**进程内函数调用**提升为**真实传输层**验证。但即便如此，它仍不构成 09-21 那条的完整证明 —— R5 要求如实区分。
