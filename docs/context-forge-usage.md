# Context Forge

本地优先的"开发会话 → 可复用知识 → AI 规则"编译器。基于
`docs/technical-design-context-forge.md` 的设计。

## 安装

```bash
git clone <repo>
cd Context-Forge
python -m pip install -e .

# 可选：装 MCP server 依赖（让 Claude Code / Desktop 直接读 vault）
python -m pip install -e ".[mcp]"
```

## 配置

默认 `~/.context-forge/config.toml`：

```toml
vault_path = "C:/Users/you/Documents/Obsidian"
knowledge_dir = "context-forge"
processing_mode = "redacted_model"
model_provider = "offline"      # offline | fake | local | remote
poll_interval_seconds = 2
max_attempts = 3
retention_days = 30
excluded_globs = ["**/.env*", "**/.ssh/**", "**/*secret*"]

# 真实 provider 配置（model_provider = "remote" 时使用）
remote_api_url = ""             # 留空走 ANTHROPIC_BASE_URL
remote_api_key = ""             # 留空走 ANTHROPIC_AUTH_TOKEN
remote_model = ""               # 留空走 ANTHROPIC_DEFAULT_SONNET_MODEL 等

# 本地 provider 配置（model_provider = "local" 时使用）
local_api_url = "http://localhost:11434"
local_model = "llama3"
```

未配置 provider 时 capture / vault / search 仍然可用。

## Claude Code hook 接入

```bash
forge install-hook
# 撤销：forge install-hook --uninstall
```

会装 SessionEnd / PreCompact / SessionStart 三条钩子进
`~/.claude/settings.json`。SessionStart 跑 `forge status`，输出注入
到你 context 顶部 —— 每次开 Claude Code 都能立刻看到状态。

## 日常使用

**装好之后基本不用命令**。worker daemon 自动处理队列，SessionStart
钩子自动让你看到状态。所有动作在 Obsidian 里。

### 6 个日常命令

```bash
forge init <vault-path>          # 首次：写 config + 创建 vault 目录
forge install-hook              # 首次：装 3 条钩子
forge worker run [--once]       # 手动 / 调试；daemon 自动轮询
forge status [--json]           # 一行：sessions / rules / dead-letter 数
forge doctor                    # 健康检查
forge retention --apply         # 手动清理过期 session
```

### 5 个 escape hatch（debug 用）

```bash
forge provider-check            # ping 模型 provider
forge import-transcript <path>   # 历史 transcript 导入
forge session-info <id>         # 单 session 完整 JSON
forge jobs [--status dead_letter]
forge jobs-retry <id>            # 死信救回
```

### 用户在 Obsidian 里做的事（无命令）

| 想做什么 | 怎么操作 |
|---|---|
| 看知识 | 打开 `vault/knowledge/accepted/` |
| 改知识 | 直接编辑文件（worker 看到 `user_owned: true` 不覆盖） |
| 加知识 | 新建 `vault/knowledge/accepted/{id}.md`，frontmatter 加 `user_owned: true` |
| 删知识 | 删文件 |
| 启用规则 | 改 frontmatter `status: proposed` → `enabled` |
| 禁用规则 | 改 frontmatter `status: enabled` → `disabled` 或删文件 |
| 看规则候选 | 看 `vault/rules/proposals/` 里 `status: proposed` 文件 |

### Worker 自动循环（每次 SessionEnd 触发）

```
SessionEnd hook → ~/.context-forge/queue.db
worker daemon picks job → 调模型
  ├─ vault/_drafts/{session_id}.md               模型原文归档
  ├─ vault/knowledge/accepted/{k_id}.md         知识自动发布
  └─ vault/rules/proposals/rule-{k_id}.md       候选规则 status=proposed
下次开 Claude Code SessionStart 钩子显示：
[context-forge] 9 sessions, 1 rule proposed
```

### MCP（给 AI 用）

5 个工具，让 Claude Code 直接读 vault：

```bash
python -m pip install -e ".[mcp]"
python -m context_forge.mcp_server
```

工具：`context_forge_search` / `context_forge_get` / `context_forge_match_rules` / `context_forge_recent_knowledge` / `context_forge_recent_rules` / `context_forge_record_feedback`。
配置见 `docs/mcp-transport.md`。

### 跑过几次后

```bash
forge metrics      # §11 五项指标
forge validate     # vault 文件健康
forge doctor       # 5 项只读诊断
```

## 数据契约

- Markdown 是事实源，SQLite 是可重建索引。
- SessionEvent 必须经过 Pydantic 校验。
- 事实型 claim 必须带 `evidence_ids`，否则 worker 拒收。
- 所有写入带 expected hash 冲突检测，hash 不匹配生成 `.conflict-<ts>` 副本。
- 凭证脱敏在 worker 调模型前完成，不写入 vault / 错误日志。

## 测试

```bash
python -m pytest tests/test_context_forge.py -v
```

21 条测试覆盖：auto-loop 三文件写、`user_owned` 跳过、daemon 循环、gateway 异常不死循环、dead_letter / retry、事实拒收、select_gateway 凭据校验、status / metrics 刷新。
