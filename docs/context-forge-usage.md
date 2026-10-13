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

# 本地语义向量（只读本地缓存，不联网下载）
embedding_model = "all-MiniLM-L6-v2"
embedding_dimensions = 384
```

未配置 provider 时 capture / vault / search 仍然可用。
语义模型未安装或未缓存时自动回退到词法检索；安装 `.[semantic]` 并在本地缓存模型后，运行
`forge reindex-embeddings` 为已有记忆生成向量。

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

### 整理与判决（每天自动 + 偶尔人工）

```bash
forge hygiene                   # 只读：完全重复 / 近似候选 / 矛盾 / 衰减 / 已判过的计数
forge hygiene --apply           # 只跑可无人值守的两档：归档完全重复、标注矛盾（每档一轮上限 100）
forge hygiene --review          # 逐条判决，必须在终端里敲（管道喂进来的答案一律拒绝）
forge hygiene-undo <id>         # 撤销一次自动改动；先打印计划，加 --apply 才写
forge approve                   # 列出等你批的高危规则（只读）
forge approve --token <handle>           # 显示将要放行的全文，不写库
forge approve --token <handle> --apply   # 终端里回读挑战码才放行；管道进来的直接拒绝
```

定时执行不需要命令：watcher 常驻循环每天跑一次 `--apply` 的那两档，每一步单独记账（`forge doctor` 的 `scheduled` 行会列出哪个步骤上次失败、错在哪）。近似重复和"很久没用"永远停在报告里等人判——**相似不等于相同，没人用不等于错**。自动档宽窄由你的判决记录回看：`forge memory-report` 的 `verdicts` 段给绝对数，不给百分比。

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

11 个工具，让 Claude Code 直接读 Gen2 记忆库：

```bash
python -m pip install -e ".[mcp]"     # 可选依赖，不装则 server 起不来
forge sync                            # 建/更新 memory.db（单向：vault → memory.db）
forge mcp-install                     # 交给 claude CLI 注册（user scope，写入 ~/.claude.json）
```

工具：召回类 `context_forge_memory_search` / `_memory_get` / `_memory_confirm` /
`_memory_report` / `_memory_effectiveness` / `_memory_hygiene`；Gen1 vault 类 `context_forge_search` /
`_get` / `_match_rules` / `_recent_knowledge` / `_recent_rules` / `_record_feedback`。

高危规则的两步：召回遇到没人批过的 `risk=high` 规则时**不返回正文**，只给 `confirmation_required` +
摘要 + 句柄，`body` 是 `null`；你在终端批完之后，宿主用 `context_forge_memory_confirm(token)` 换取正文，
一个句柄只用一次。同一条规则改一个字就是新版本，重新待批；批过的版本后续召回正常返回。
这条闸门挡"没人读过就执行"，不挡"在本机读"——本地库和 vault 文件仍是明文，理由与边界见
`docs/adr/0001-withhold-unapproved-high-risk-rules.md`。

撤销：`forge mcp-install --uninstall`。配置细节与**已证/未证边界**见 `docs/mcp-transport.md`。

> **改了代码要重启 daemon。** `forge watch-transcripts` 是常驻进程，Python 内存里是
> **启动时**的模块 —— VBS 只在它**退出**时重启。2026-09-23 实测：watcher 跑了 21 小时的
> 旧代码，期间落下的文档仍带已修复的 YAML 引号缺陷。改完 `context_forge/` 后：
> `taskkill /F /IM pythonw.exe`，VBS 30 秒内以新代码重启。
>
> 同理，`memory.db` 的同步只在该进程处理过会话后触发；**手工编辑 vault 后要跑 `forge sync`**。

### 跑过几次后

```bash
forge sync             # 同步 vault → memory.db
forge memory-report    # 应用效果 / 异常审计 / 整理候选 三张报表
forge hygiene          # 巩固 / 衰减 / 合并候选
forge doctor           # 9 项只读诊断（含 vault↔memory.db 一致性、mcp 可用性）
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
