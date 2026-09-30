# Context Forge

> 面向 Agent 的持续学习记忆系统：从经历中形成有依据的理解，在新任务中应用，并根据结果修正。

<p align="left">
  <a href="README.md"><img alt="version" src="https://img.shields.io/badge/version-v1.0.0-0F1419?style=flat-square&labelColor=FAFAF9"></a>
  <img alt="license" src="https://img.shields.io/badge/license-MIT-0F1419?style=flat-square&labelColor=FAFAF9">
  <img alt="python" src="https://img.shields.io/badge/python-3.12%2B-3776AB?style=flat-square&logo=python&logoColor=white">
  <img alt="tests" src="https://img.shields.io/badge/tests-98%20passed-16A34A?style=flat-square">
</p>

Context Forge 是一个 local-first 的 Agent 记忆基础设施。它不修改大模型参数，也不把历史对话无差别塞进上下文，而是把经历、来源、适用条件、冲突和结果保存在可追溯的 SQLite 长期记忆中，在需要时检索，并允许人工纠正。

仓库历史名称为 `code-mentor`；包名、CLI 和 MCP 名称已统一为 `context-forge` / `forge`。

## v1.0 做到了什么

- **持续沉淀**：从现有 `SessionArtifacts` 生成零到多条候选记忆。
- **有依据的理解**：区分 observation、claim、hypothesis、preference、procedure，保存来源、证据等级、模型版本和提示词版本。
- **人工纠正旁路**：支持修订、抑制、恢复、作用域调整和替代；人工版本优先于后台更新。
- **按需召回**：SQLite + FTS5 支持中文 bigram、ASCII、项目/路径过滤、稳定排序和字节预算。
- **结果追踪**：区分 retrieved、tool_returned、presented、referenced、applied、validated；曝光不会自动提高可信度。
- **保守整理**：完全重复且决策边界一致时才自动归档；相似内容只生成候选，不擅自判断同义、冲突或替代。
- **可恢复**：版本、来源、关系和事件事务一致；支持 FTS 重建、SQLite 在线备份/恢复和旧 vault 导入。
- **宿主接入**：worker 会把会话产物写入 `memory.db`，MCP 提供长期记忆检索、详情和只读质量报告。

### 明确不做

- 不进行模型参数训练，因此不宣称“数据库让模型自主训练”。
- 不引入专用向量数据库、图数据库或无界自主研究。
- 不把记忆当作高权限指令自动执行。
- 不以记忆数量、模型自评或曝光次数冒充学习效果。

## 架构

```text
Claude Code hooks / transcript
            |
            v
     queue.db + worker
            |
            v
   SessionArtifacts / gateway
            |
            v
      MemoryLifecycle
            |
            v
 memory.db (SQLite + FTS5)
   |          |          |
   v          v          v
 Recall     Feedback   Merge report
   |
   v
 MCP / Agent 按需读取
```

权威数据是 `memory.db`。`queue.db` 只负责运行队列，Markdown 是导出或兼容视图，不是长期记忆的完整备份。

## 快速开始

### 安装

需要 Python 3.12+：

```bash
git clone https://github.com/Freedom0x0/Context-Forge.git
cd Context-Forge
python -m pip install -e ".[mcp]"
```

### 初始化

```bash
forge init /absolute/path/to/your/obsidian-vault
```

初始化会创建运行目录、配置、vault 视图和 `memory.db`。默认运行目录是 `~/.context-forge`，测试或多配置环境可用 `CONTEXT_FORGE_HOME` 隔离。

### 配置模型

编辑 `~/.context-forge/config.toml`：

```toml
vault_path = "/absolute/path/to/your/obsidian-vault"
model_provider = "remote" # none | offline | fake | local | remote
remote_model = "deepseek-v4-flash"
```

API key 应通过本地安全配置或环境变量提供，不要提交到 Git。

**出境面只有一条，由这条配置决定**：`model_provider = "remote"` 时，脱敏后的会话正文会发往该 endpoint（将来若启用 embedding，也走同一条约束）；`none` / `offline` / `fake` 不产生任何出境，`local` 只到本机的 Ollama。`forge doctor` 的 `egress` 行打印正文实际去向，`forge status --json` 的 `egress` 字段同值。也就是说：风险等级取决于你选了哪个 provider，而不是取决于脱敏规则有多强。

### 运行 worker

```bash
forge worker run --once   # 处理一个队列任务
forge worker run          # 持续运行
forge status              # 查看队列、记忆和运行状态
forge doctor              # 健康检查
```

### 启用 MCP

```bash
forge-mcp
```

长期记忆工具包括：

- `context_forge_memory_search`：按项目、路径和预算检索记忆。
- `context_forge_memory_get`：读取当前或指定历史版本。
- `context_forge_memory_report`：生成只读整理和质量报告。

旧 vault 工具仍保留，便于渐进迁移。

## 数据和治理原则

1. **观察和解释分开**：模型归纳必须携带来源，缺少证据时保持 unknown。
2. **条件和作用域分开**：unknown 不等于 true，也不等于 global。
3. **新信息先分类**：补充、细化、反驳、例外和替代不能简单覆盖旧记忆。
4. **人工纠正优先**：后台任务使用版本比较，不能静默复活抑制内容。
5. **检索不是采用**：返回、呈现、引用、应用和验证分别记录。
6. **整理可逆**：自动整理保留历史、来源和关系，支持撤销。

## 测试与验收

```bash
python -m pytest tests/ -q
```

当前验证结果：`98 passed`。

测试覆盖存储事务和并发、证据与关系、中文/ASCII 检索、作用域隔离、生命周期、worker 端到端接入、MCP 工具、保守整理、备份恢复和旧 vault 导入。

持续学习质量仍需使用独立的多轮任务数据集评估，包括决策正确率、错误复发率、过度泛化、跨范围泄露、延迟和 token 成本；通过确定性测试不等于已经证明模型能力提升。

## 文档

- `docs/context-forge-usage.md`：日常使用和 CLI。
- `docs/memory-store.md`：长期记忆 API、迁移和恢复。
- `docs/technical-design-context-forge.md`：整体技术设计。
- `docs/research/competitor-landscape-human-ai-learning.md`：竞品和研究背景。

## 版本状态

`v1.0.0` 表示本地长期记忆闭环的首个可验收版本：存储、生命周期、按需召回、曝光追踪、保守整理和宿主接入均已实现。模型质量收益、跨用户服务化和参数训练属于后续版本，不作为 v1.0 的隐含承诺。

## License

MIT
