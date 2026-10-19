# Context Forge

> 面向 Agent 的持续学习记忆系统：从经历中形成有依据的理解，在新任务中应用，并根据结果修正。

<p align="left">
  <img alt="license" src="https://img.shields.io/badge/license-MIT-0F1419?style=flat-square&labelColor=FAFAF9">
  <img alt="python" src="https://img.shields.io/badge/python-3.12%2B-3776AB?style=flat-square&logo=python&logoColor=white">
  <img alt="tests" src="https://img.shields.io/badge/tests-454%20passed-16A34A?style=flat-square">
</p>

Context Forge 是一个 local-first 的 Agent 记忆基础设施。它不修改大模型参数，也不把历史对话无差别塞进上下文，而是把经历、来源、适用条件、冲突和结果保存在可追溯的 SQLite 长期记忆中，在需要时检索，并允许人工纠正。

仓库历史名称为 `code-mentor`；包名、CLI 和 MCP 名称已统一为 `context-forge` / `forge`。

## 它能做什么

- **持续沉淀**：从现有 `SessionArtifacts` 生成零到多条候选记忆。
- **有依据的理解**：区分 observation、claim、hypothesis、preference、procedure，保存来源、证据等级、模型版本和提示词版本。
- **人工纠正旁路**：支持修订、抑制、恢复、作用域调整和替代；人工版本优先于后台更新。人工打成的 `conflicted`（待复核）对后台写入是**吸收态**——模型只能追加佐证，不能替人把它结算掉。
- **按需召回**：SQLite + FTS5 支持中文 bigram、ASCII、项目/路径过滤、稳定排序和字节预算。三条通道——BM25 严格 AND → 关键词放宽 → 可选语义 top-K——汇入同一条打分管线；每条结果带通道标签（`bm25`/`widened`/`semantic`），查询期语义降级会在解释里显式写出，不静默。
- **结果追踪**：区分 retrieved、tool_returned、presented、referenced、applied、validated；曝光不会自动提高可信度。
- **只读报表**：应用归因、异常候选、整理候选，外加各通道召回计数、人工判决计数与宿主收集健康——**一律给绝对数，不给百分比**。
- **保守整理**：完全重复且决策边界一致时才自动归档；相似内容只生成候选，不擅自判断同义、冲突或替代。
- **可恢复**：版本、来源、关系和事件事务一致；支持 FTS 重建、SQLite 在线备份/恢复和旧 vault 导入。
- **高危闸门**：确定性分类器判为 `risk=high` 的规则停在 `candidate`，人在终端批准后才可召回。**闸门也管已经发布的那一半**：召回遇到从未被批准过的高危规则时扣掉正文，只返回 `confirmation_required`、摘要和服务端句柄；人在终端批准（`forge approve`）后，宿主调 `context_forge_memory_confirm(token)` 才拿到可执行文本。两个后果：1) **召回响应形状变了**——`body` 可能是 `null`，连 `sources`/`applicability`/`scope`/`provenance`/`explanation` 一起不返回；2) **一个版本批一次**，改写即重新待批。待批请求是服务端随机句柄（不是内容的派生物，模型算不出第二个）：10 分钟有效、一次性（重复换取会被记进台账并由 `forge doctor` 打印）、记忆换版即作废。这条是**执行闸门，不是保密闸门**：它挡"没人读过就执行"，不挡"在本机读"；理由与边界见 `docs/adr/0001-withhold-unapproved-high-risk-rules.md`。
- **宿主接入**：读侧宿主无关——任何会说 MCP 的宿主读的都是同一个 `memory.db`，装一次 server 即可。写侧目前只认 Claude Code：T1（watcher 读 `~/.claude/projects` 的 transcript）与 T2（SessionEnd/PreCompact hook）两条**确定性**通路。因此共享是单向的：CC 沉淀的记忆别的宿主能搜到，别的宿主干的活不会自动入库。MCP 工具那条路（T3）只作人工补录的支线，任何宿主的默认沉淀都不得只依赖它。

### 明确不做

- 不进行模型参数训练，因此不宣称“数据库让模型自主训练”。
- 不引入**独立**向量数据库服务、图数据库或无界自主研究。向量本身允许（存在 `memory.db` 的 BLOB 列里，或 `sqlite-vec` 这类进程内扩展）；不允许的是再多起一个进程、多一套需要双向同步的存储。
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

**正文出境由 `model_provider` 决定**：`remote` 会将脱敏后的会话正文发往配置 endpoint；`none` / `offline` / `fake` 不发送正文，`local` 只访问本机 Ollama。语义向量使用独立的本地 `all-MiniLM-L6-v2` 模型，不调用远程 embedding 服务。`forge doctor` 会分别报告正文 provider 与本地语义模型状态。

安装可选本地模型依赖并缓存模型：

```bash
python -m pip install -e ".[semantic]"
python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"
forge reindex-embeddings
```

模型推理时只读本机缓存、不联网。模型未缓存时，记忆写入继续成功并回退到词法召回。

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

- `context_forge_memory_search`：按项目、路径和预算检索记忆（带通道标签）。
- `context_forge_memory_get`：读取当前或指定历史版本。
- `context_forge_memory_confirm`：用终端批准换来的句柄取回被扣留的高危规则正文。
- `context_forge_memory_mark` / `context_forge_memory_apply`：上报引用、应用与验证阶段。
- `context_forge_memory_report` / `context_forge_memory_effectiveness` / `context_forge_memory_hygiene`：只读整理、效果与卫生报表。

旧 vault 工具仍保留，便于渐进迁移。

## 数据和治理原则

1. **观察和解释分开**：模型归纳必须携带来源，缺少证据时保持 unknown。
2. **条件和作用域分开**：unknown 不等于 true，也不等于 global。
3. **新信息先分类**：补充、细化、反驳、例外和替代不能简单覆盖旧记忆。
4. **人工纠正优先**：后台任务使用版本比较，不能静默复活抑制内容。
5. **检索不是采用**：返回、呈现、引用、应用和验证分别记录。
6. **整理可逆**：自动整理保留历史、来源和关系，支持撤销。
7. **`shared` 是对内敞开，不是对外敞开**：它的含义是"对本保险箱全局可见"，不是"对世界可见"——本地场景下记忆就像人自己的记忆，对本人全部可见。任何离开保险箱的路径（云同步、团队库、导出）必须过一道独立的出库闸门。项目级授权（原 `access.visibility='selected_projects'`）已于 2026-10-19 移除：跨项目召回是产品核心，所有记忆对本人全部可见、可按 id 读取，只剩主体边界 `owner` 与 `privacy`。
8. **脱敏只挡"出境的那一次"，且规则永不完备**：唯一的真实外泄面是把文本发给远端模型 API 的那一次调用；按第 7 条，本地 `memory.db` 与 vault 属于保险箱内部，不算泄露。正则黑名单是追赶战（JSON 引号形态、裸密钥前缀、裸 Bearer 都曾漏过），不当强保证用。敏感路径与破坏性命令由确定性分类器判成 `risk=high`（不是模型自评），**新沉淀的高危规则停在 `candidate`：召回看不见它，直到有人在终端里批准；已经发布过的高危规则在召回时扣正文，宿主必须拿人去批换来的句柄才能取回文本**——用延迟换不打断。这条保证的边界写清楚：它挡的是"没被人读过就执行"，不挡"在本机读"——`memory.db` 与 vault 投影文件仍在本地可读，而有 shell 权限的宿主理论上也能自己跑 `forge approve`，所以批准必须回读挑战码、且批准记录进台账供事后审计。`model_provider` 配成 none/offline 时，出境面根本不存在。

## 测试与验收

```bash
python -m pytest tests/ -q
```

当前验证结果：`454 passed`（2026-10-14）。

测试覆盖存储事务和并发、证据与关系、中文/ASCII 检索、混合召回与通道标注、作用域隔离、生命周期、`conflicted` 吸收态、worker 端到端接入、MCP 工具、保守整理、高危规则的扣留与换取（含假句柄、过期、换版、重复换取）、备份恢复和旧 vault 导入。

持续学习质量仍需使用独立的多轮任务数据集评估，包括决策正确率、错误复发率、过度泛化、跨范围泄露、延迟和 token 成本；通过确定性测试不等于已经证明模型能力提升。

## 文档

- `docs/technical-introduction.md`：**先读这份**——按当前代码讲系统长什么样（架构、领域模型、写入通路、召回、闭环、治理边界）。
- `docs/context-forge-usage.md`：日常使用和 CLI。
- `docs/memory-store.md`：长期记忆 API、迁移和恢复。
- `docs/adr/`：判断理由（为什么不选另一条路、保证的边界在哪）；API 事实不放这里。
- `docs/technical-design-context-forge.md`、`docs/technical-design-falsifiable-memory.md`：技术设计论证。
- `docs/research/competitor-landscape-human-ai-learning.md`：竞品和研究背景。

## License

MIT
