# 执行计划：Gen2 接入运行时

需求 [prd.md](prd.md) · 设计 [design.md](design.md)。上下文顺序：`implement.jsonl` → `prd.md` → `design.md`。

**本任务只有一期**（不需要分期）——它的价值在于闭环，拆开每一块都不产生可用结果。

---

## 前置

- [x] 0.1 基线：`python -m pytest tests/ -q`（上游任务收尾时 **171 passed**）
- [x] 0.2 记录当前生产状态快照（接线前后对照用）：`ls ~/.context-forge/`（应无 `memory.db`）、`mcpServers` 现有键、vault 文档数

---

## 实现清单

### A. 同步器与命令
- [x] A1 `runtime_sync.py`（新建）— `SyncReport` + `sync(root, *, dry_run=True)`
  - `snapshot_order` 持久化在 `runtime_dir()/sync-order`，**只用持久化计数器，不用时间戳**（时钟回拨会破坏「变更的快照必须严格更大」）
  - 仅在实际发生变化时递增
  - 复用既有 `import_vault`，不重写导入逻辑
- [x] A2 `cli.py` — 新增 `forge sync [--dry-run]`，打印 `SyncReport`
- [x] A3 首次运行必须能**从零创建** `memory.db`（`MemoryStore.initialize` 后导入），因为生产上它不存在

### B. 同步触发与过期可观测
- [x] B1 `watcher.py` — 处理过 ≥1 个会话后触发一次 sync
  - **失败必须外显**（R2.3）：异常写进 `doctor` 可见的位置，不得 `except: pass`
  - sync 失败**不得**中断 watcher 主循环（下一次仍要尝试）
- [x] B2 `doctor.py` — 新增一致性检查：`memory.db` 缺失 / vault 文档数 > db 记录数 → 报告
- [x] B3 `status.py` — 暴露不一致计数

### C. 宿主接入
- [x] C1 新增 `forge mcp-install [--target PATH] [--uninstall]`（对齐既有 `install_hook` 的形状）
  - 改前备份 `settings.json` → `settings.json.bak-<ts>`
  - **只增不改**：`setdefault('mcpServers', {})['context_forge'] = {...}`，既有条目原样保留
  - 幂等（重复执行不产生第二个键）；`--uninstall` 只删自己的键
- [x] C2 `cwd` 用仓库绝对路径（否则 `memory.db` 会落到错误位置）
- [x] C3 `docs/mcp-transport.md` — 补 3.5 的**边界声明表**（已证 / 未证分开）

### D. 真实传输 e2e（**核心**）
- [x] D1 `tests/test_mcp_transport.py`（新建）— 子进程 + 真实 stdio JSON-RPC
  - `subprocess.Popen([sys.executable, "-m", "context_forge.mcp_server"], env={... CONTEXT_FORGE_HOME=tmp ...})`
  - **newline-delimited JSON-RPC**（MCP stdio 不是 `Content-Length` 帧）
  - `initialize` → `notifications/initialized` → `tools/list` → `tools/call`
  - `tools/call` **断言返回体含预先写入临时 db 的记忆 body 文本**（不是断言「返回了 JSON」）
  - `pytest.importorskip("mcp")`，**跳过时必须打印原因**（静默 skip = 假绿）
- [x] D2 `doctor.py` — 报告 `mcp` 包是否可用（让 optional 依赖的缺失可观测）

---

## 测试

- [x] T1 幂等：连跑两次 `sync`，第二次 `created=0 updated=0`
- [x] T2 单向：`sync` 前后 vault 文件内容与 **mtime 均不变**
- [x] T3 `snapshot_order` 单调：变更一次后递增；无变化时不递增
- [x] T4 **一致性可观测**：造「vault 3 条 / db 2 条」→ `doctor` 报出，`status` 计数反映
- [x] T5 **同步失败外显**：注入导入失败 → 不静默，`doctor` 可见
- [x] T6 配置只增不改：预置一个 `mcpServers.other` → 安装后仍在；`--uninstall` 后配置**完全复原**
- [x] T7 配置幂等：连装两次只有一个键
- [x] T8 **e2e 传输**（D1）
- [x] T9 `worker.py` 的 vault 写入逻辑零改动（`git diff` 断言）

---

## 验证命令

```bash
python -m pytest tests/ -q                      # 回归底线 171 passed
python -m pytest tests/test_mcp_transport.py -v  # e2e 单独看
python -m context_forge.cli sync --dry-run
python -m context_forge.cli doctor
python -m context_forge.cli status --json
```

---

## 评审门

- [x] **e2e 断言的是真实数据**（不是「返回了 JSON」）—— 这是本任务与「只证明 MCP 可调用」的分界
- [x] `importorskip` 的跳过路径会打印原因，不静默
- [x] 一致性检查能真的被构造出来（T4/T5 是**行为**测试不是源码钉）
- [x] 既有 `mcpServers` 条目一条不少
- [x] `worker.py` 零改动；无新增运行时依赖
- [x] 边界声明表已写进 `docs/mcp-transport.md`

**回滚**：删 `memory.db` / 删 `mcpServers.context_forge` / 删 watcher 里的 sync 调用点 / 删 `sync-order`（见 design.md §4）。

---

## 我（主会话）亲自做的部分

`~/.claude/settings.json` 是**用户全局配置**。实现方只交付 `forge mcp-install` 命令与其测试；**实际对真实宿主配置的写入由主会话在评审门后单独执行**，并当场验证既有条目未损、备份存在。理由：改动全局配置要一次可控、可回滚的动作，不适合混在批量代码改动里。

---

## 边界（R5，不得含糊）

本任务**能**证明：工具已注册、真实 stdio 传输可用、能从真实 `memory.db` 取回真实记忆、宿主配置已写入且既有条目未损。

本任务**不能**证明：模型会在需要时主动调用它（行为性、不可由测试证伪）。

**因此 09-21 那条验收在本任务后仍不得勾掉**，只能更新为「宿主已接入且传输已验证；模型调用行为未证」。

---

## 接线期间发现的运行时缺陷（全部实测，非理论）

接线动作本身暴露了 5 个只有「让系统真的跑起来」才能发现的问题：

| # | 缺陷 | 根因 | 处置 |
|---|------|------|------|
| **F1** | **watcher 常驻进程跑了 21 小时的旧代码** | VBS 只在进程**退出**时重启，Python 进程内存里是启动时的模块。P1 的 `yaml_scalar` 修复落地于 09-23 00:06，而 watcher 启动于 09-22 14:25 —— 它从没加载过该修复 | 杀掉 pythonw，VBS 30s 内以新代码重启（StartTime 14:25 → 11:22:44）。**这是一个通用运维缺口**：docs 应写明「代码更新后需重启 daemon」 |
| **F2** | **`.retag-backup/` 被索引成 vault 内容** | 8 处遍历各有各的「什么是 vault 文档」约定：`index`/`vault`/`doctor`/`cli` 只排 `index.md`，而 `memory_import`/`retag`/`runtime_sync` 用顶层白名单。备份目录就在 vault 根下 → 索引 480（240 正式 + 240 备份），检索返回重复 | 建共享遍历器 `index.iter_vault_documents()`（顶层白名单），收敛 5 处。生产 scan 480 → **240**。3 个变异全 RED |
| **F3** | **1 个坏文件挡住全部 240 条导入** | F1 期间落下的 `mcp-runtime-wiring-checklist.md` 的 `sources` 值含 `: `（YAML 裸标量不允许），旧的旧代码没加引号。`import_vault` 首个坏文件即抛 | `forge retag-vault --apply` 修复（`quoted_scalars: 1`），顺带补 30 份未打标签的新文档。同步随即成功 240/240 |
| **F4** | 大量 `feedback N: missing or ambiguous legacy id rule-k1` | 旧队列 feedback 行引用无法解析的 legacy id | **残留**：这是诊断输出不是失败（`import_vault` 如实报告而非静默丢弃）。不阻塞 |
| **F5** | `doctor.py` 有未使用的 `json` / `heartbeat_path` | 预存在（属遗留 L6 同类） | 未修，记录 |

**F1 + F3 的教训**：`forge sync` 的失败是**可见的**（R2.3 生效，报错 + `status` 报 `memory_unsynced: 240`）—— 这正是「不得静默」的价值。若当时静默跳过，坏文件会被无声吞掉，而 Gen2 永远 0 条。

---

## 验收（最终）

| AC | 状态 | 证据 |
|----|------|------|
| AC1 `memory.db` 有内容 / 幂等 / 不改 vault | ✅ | **生产实测** 240/240，`memory_unsynced: 0`；二次运行 `created=updated=0`；vault 字节与 `mtime_ns` 不变 |
| AC2 同步可用且**过期可观测** | ✅ | 构造「vault 3 / db 2」→ `doctor` 与 `status` 均报；变异 RED |
| AC3 MCP 配置进宿主，既有条目未损，可回滚 | ✅ | **主会话已写入真实 `~/.claude/settings.json`**：8 个顶层键齐全、`playwright` 逐字节未动、备份 `settings.json.bak-20260923113031199631` |
| AC4 **真实传输 e2e** | ✅ | 测试（临时 db）+ **主会话用配置里的确切命令对真实 `memory.db` 复验**，取回真实记忆含五维解释 |
| AC5 边界声明 | ✅ | `docs/mcp-transport.md` 已证/未证分开；09-21 那条**保持未勾** |
| AC6 无新依赖 / `worker.py` 零改动 | ✅ | `pyproject.toml` 未变（`mcp` 是已声明的 optional 依赖，本次仅安装）；`worker.py` diff 为空 |

**生产状态**：`doctor` **9/9 全绿**（`index: fts=240 markdown=240`、`memory-sync: agree`、`mcp: available`）。测试 **179 passed**。

**仍未证**：模型是否会在需要时**主动**调用（行为性，不可由测试证伪）。故 09-21 那条验收**仍不得勾掉**。
