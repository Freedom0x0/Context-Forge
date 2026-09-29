# 设计：Gen2 接入运行时

需求见 [prd.md](prd.md)。本文只写落地边界：改哪些文件、契约定成什么样、怎么回滚。

---

## 1. 架构定位

```
Gen1（已生效）                          Gen2（本任务通电）
vault/*.md  ←── watcher ← transcripts       memory.db
    │                                          ▲
    │  import_vault（单向，既有实现）           │
    └──────────────────────────────────────────┘
    │                                          │
    └──→ RuleMatcher（P0，已生效）              └──→ MCP → 宿主
```

**单向**：vault → `memory.db`。`memory.db` 是 Gen2 权威数据，vault 是 Gen1 事实源；绝不反向写。

---

## 2. 模块边界

| 模块 | 职责 | 本任务改动 |
|------|------|-----------|
| `context_forge/cli.py` | 命令面 | **新增 `forge sync`** |
| `context_forge/runtime_sync.py` | 同步器 | **新建**：计算 `snapshot_order`、调 `import_vault`、产出报告 |
| `context_forge/watcher.py` | 转录监听 | 处理过会话后触发一次 sync；**失败必须外显** |
| `context_forge/doctor.py` | 健康检查 | **新增 vault 与 `memory.db` 的一致性检查** |
| `context_forge/status.py` | 状态行 | 暴露不一致计数 |
| `~/.claude/settings.json` | 宿主 MCP 配置 | **只增 `mcpServers.context_forge`**，先备份 |
| `docs/mcp-transport.md` | 文档 | 补「已接入本机」与可复现步骤 |
| `tests/test_mcp_transport.py` | e2e | **新建**：子进程 + 真实 stdio JSON-RPC |

**不改**：`worker.py` 的 vault 写入逻辑、`vault.py`、`queue.py`、`autostart.py`、`transcript.py`。

---

## 3. 契约

### 3.1 `forge sync`

```python
@app.command("sync")
def sync(dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    """把 vault 内容导入 memory.db（增量、幂等、单向）。"""
```

```python
# runtime_sync.py
def sync(root: Path, *, dry_run: bool = True) -> SyncReport: ...

@dataclass(frozen=True)
class SyncReport:
    created: int; updated: int; unchanged: int; protected: int
    vault_documents: int; db_records: int
    snapshot_order: int
    diagnostics: tuple[str, ...]
```

**`snapshot_order` 契约**（沿用 `memory_import` 既有约束：同一逻辑源必须用单调非减的显式修订号，变更的快照必须严格更大）：
- 持久化在 `runtime_dir()/sync-order`（一个整数）
- 每次**实际发生变化**的同步后 +1
- 无变化时不递增（保持幂等）
- **不用时间戳**：时钟回拨会让「变更的快照必须严格更大」失败

### 3.2 一致性检查（R2.2）

```python
# doctor.py
def check_memory_sync(vault: Vault | None) -> list[str]:
    """vault 文档数 vs memory.db 记录数；不一致则返回告警行。"""
```

| 状态 | 输出 |
|------|------|
| `memory.db` 不存在 | `memory.db missing; run forge sync` |
| 有文档未导入 | `vault has N documents, memory.db has M (N-M not imported); run forge sync` |
| 一致 | 无输出 |

**必须可被构造出来**：测试造一个「vault 有 3 条、db 只有 2 条」的状态，断言 `doctor` 报出，**且 `status` 的计数也反映**。

### 3.3 宿主 MCP 配置

`~/.claude/settings.json` 增加：

```json
"context_forge": {
  "command": "python",
  "args": ["-m", "context_forge.mcp_server"],
  "cwd": "<repo 绝对路径>"
}
```

- **改前**：复制 `settings.json` → `settings.json.bak-<ts>`（同目录）
- **只增不改**：读 → `setdefault('mcpServers', {})['context_forge'] = {...}` → 写回；既有 `playwright` 原样保留
- **可回滚**：删掉该键即复原；备份文件可比对
- 幂等：重复执行不产生第二个键

### 3.4 真实 stdio 传输 e2e（**核心交付**）

`tests/test_mcp_transport.py` 必须**真的起子进程**并走 stdio，不是进程内调用：

```python
proc = subprocess.Popen([sys.executable, "-m", "context_forge.mcp_server"],
                        stdin=PIPE, stdout=PIPE, stderr=PIPE,
                        env={**os.environ, "CONTEXT_FORGE_HOME": str(tmp_runtime)})
```

帧格式：MCP 的 stdio 传输是 **newline-delimited JSON-RPC 2.0**（不是 `Content-Length` 头）。

三次往返，每次都断言：

| 步骤 | 请求 | 断言 |
|------|------|------|
| 1 | `initialize` (protocolVersion / capabilities / clientInfo) | 返回 `serverInfo.name == "context-forge"`、`capabilities.tools` 存在 |
| 2 | `notifications/initialized` | 无响应（通知） |
| 3 | `tools/list` | 含 `context_forge_memory_search`、`context_forge_memory_get` 等 |
| 4 | `tools/call` `context_forge_memory_search` | `content` 里含**预先写入该临时 `memory.db` 的记忆 body 文本** |

**关键**：断言的是**真实数据**。测试先用 `MemoryStore.initialize` 往临时 db 写一条可识别的记忆，再让子进程检索它。若只断言「返回了 JSON」则退化成本任务要防的「只证明 MCP 可调用」。

**可选依赖**：`mcp` 包是 optional（`pyproject.toml` 的 `[project.optional-dependencies]`）。测试必须 `pytest.importorskip("mcp")` 而非硬失败 —— 并在跳过时**明确输出原因**，不得静默跳过（静默 skip 会让「e2e 通过」变成假绿）。

### 3.5 边界声明（R5）

写进 `docs/mcp-transport.md`：

| 已证明 | 依据 |
|--------|------|
| 工具已注册且可枚举 | e2e `tools/list` |
| 真实 stdio 传输可用 | e2e 子进程往返 |
| 能从真实 `memory.db` 取回真实记忆 | e2e `tools/call` 断言内容 |
| 配置已写入宿主且既有条目未损 | 配置测试 |

| **未证明** | 原因 |
|------------|------|
| 模型会在需要时**主动**调用 | 行为性，取决于模型与提示，不可由测试证伪 |
| 召回质量在真实任务上更好 | 需线上样本；P2 的实验是确定性语料上的代理 |

**不得**把上表第二行读成「09-21 那条验收已完成」。

---

## 4. 回滚

| 变更 | 回滚 |
|------|------|
| `forge sync` 建出的 `memory.db` | 删除该文件即回到当前状态（vault 未被动过） |
| `~/.claude/settings.json` | 删 `mcpServers.context_forge`；或还原 `settings.json.bak-<ts>` |
| watcher 内的 sync 调用 | 删除调用点即回到纯 Gen1 |
| `sync-order` 计数 | 删除文件；下次从 0 重新计（`import_vault` 对未变快照幂等，安全） |

---

## 5. 风险

| 风险 | 对策 |
|------|------|
| **同步失败被静默吞掉** | R2.3：watcher 内 sync 的异常必须写进 `doctor` 可见的位置；有测试构造失败并断言外显 |
| **`memory.db` 悄悄过期** | R2.2 的一致性检查 + 测试构造不一致状态 |
| **改坏用户全局配置** | 先备份 + 只增不改 + 幂等 + 测试断言既有条目保留 |
| **e2e 退化成「只证明可调用」** | 3.4 强制断言真实内容，不是「返回了 JSON」 |
| `mcp` 包未装导致静默跳过 | `importorskip` 必须打印原因；并在 `doctor` 里报告 `mcp` 是否可用 |
| 时钟回拨破坏 `snapshot_order` | 用持久化计数器，不用时间戳 |
