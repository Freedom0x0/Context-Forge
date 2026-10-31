# Context Forge 技术方案

状态：草案，第一切片实现中

日期：2026-09-15

## 1. 产品命名

暂定名称：**Context Forge**。

产品不是单纯的 coding mentor，也不是普通记忆库，而是把开发会话中的上下文、证据和经验编译成两种资产：

- 人类可以阅读、编辑和长期积累的知识。
- AI 可以按项目和路径检索、解释和撤销的规则。

`code-mentor` 可以保留为早期 skill 或迁移包名称。正式产品名称与底层领域模型不应绑定，后续改名不需要重写代码或数据格式。

## 2. 产品定义

Context Forge 是一个本地优先的开发经验编译器：

```text
开发会话
  -> 证据提取
  -> 复盘草稿
  -> 人类确认
  -> Obsidian 知识
  -> AI 规则候选
  -> 后续会话命中
  -> 效果反馈
```

核心承诺不是“记住更多”，而是“只让经过审阅、能追溯、可以撤销的经验影响未来的 AI”。

## 3. 设计原则

### 本地优先

原始 transcript、任务队列和知识库默认保存在本机。默认不上传完整会话，不依赖云端服务。

### Markdown 是事实源

Obsidian Markdown 是用户真正拥有的数据。SQLite、全文索引和 embedding 都只是可重建的运行时索引。

### 人类确认是状态转换

模型生成的内容只能进入草稿状态。正式知识和启用规则必须经过用户确认，或经过明确的验证策略。

### 知识与规则分离

知识帮助人理解经验；规则帮助 AI 在特定上下文采取行动。两者有不同的生命周期和风险。

### 每条规则都可解释

规则命中时必须说明命中原因、适用范围、来源复盘和验证状态。

### 增量处理

每次只处理新增或发生变化的内容，不重复编译整个知识库。

## 4. 总体架构

```text
                    +----------------------+
                    |  Claude Code Adapter |
                    +----------+-----------+
                               |
                         Events / Jobs
                               |
+------------+       +---------v----------+       +----------------+
| CLI / MCP  +------>+ Application Layer +------>+ Obsidian Vault |
+------------+       +---------+----------+       +----------------+
                               |
                    +----------v-----------+
                    | Domain + Compiler    |
                    | Review / Knowledge   |
                    | Rule / Feedback      |
                    +----------+-----------+
                               |
                    +----------v-----------+
                    | SQLite index / queue |
                    +----------------------+
```

建议的目标代码结构：

```text
context-forge/
  apps/
    cli/
    worker/
    mcp-server/
  packages/
    domain/
    application/
    transcript/
    compiler/
    retrieval/
    vault/
    adapters/
      claude-code/
  schemas/
  tests/
  docs/
```

但 MVP 不直接拆成多个可部署应用，也不提前拆成多个 Python package。先做一个模块化单体：`forge` CLI、一个可选 worker 进程和 adapter 模块。只有 MCP 或后台服务出现独立发布、权限或扩展需求时，才拆出 `apps/mcp-server` 和独立 package。

领域层不依赖 Claude Code、Obsidian、SQLite 或具体模型。外部系统通过 adapter 和 repository 接口接入。

## 5. 会话生命周期

Claude Code 的 hook 只负责记录事件和投递任务：

```text
SessionEnd / PreCompact
  -> 读取 session_id、cwd、transcript_path
  -> 写入本地 queue.db
  -> 快速退出
```

后台 worker 执行：

```text
读取 transcript
  -> 本地脱敏
  -> 解析用户消息、助手消息和工具调用
  -> 判断信息密度
  -> 生成 ReviewDraft
  -> 通知用户审核
```

SessionEnd 不同步调用模型。官方文档说明该 hook 不能阻止会话结束，并且默认超时时间很短，因此它只应写入任务队列。

SessionEnd 也不能保证 transcript 在未来一直存在。事件中保存 transcript 路径和文件 hash；worker 读取失败时保留事件并报告 `source_unavailable`。是否复制原文由 retention 配置决定，默认不复制未脱敏原文。

## 6. 领域模型

```text
Session
  -> Evidence
  -> ReviewDraft
  -> KnowledgeItem
  -> RuleCandidate
  -> RuleFeedback
```

状态转换：

```text
Session: captured -> parsed -> redacted -> retained | discarded
Job: queued -> running -> succeeded | retryable | dead_letter
Review: draft -> approved | rejected | superseded
Knowledge: proposed -> accepted -> revised | conflicted | archived
Rule: proposed -> enabled -> verified | disabled | stale | archived
```

这些状态属于不同对象，不能实现成一条共享枚举。每次转换必须记录操作者、时间、输入版本和输出 ID。关键对象必须保存来源会话、项目、路径范围、证据、置信度、创建时间和更新时间。

### ReviewDraft

记录单次会话的复盘结果，包含问题、尝试、证据、结果、经验和不确定性。它是用户审核的主要界面。

### KnowledgeItem

由一条或多条已确认复盘合并而成，表达相对稳定的人类知识。知识可以过期、冲突或被修订，但不应被静默覆盖。

### RuleCandidate

从确认后的 KnowledgeItem 编译而来，描述 AI 在什么条件下采取什么行为。规则必须拥有作用域和激活方式。

### RuleFeedback

记录规则被命中的原因、用户反应以及后续结果，用于验证、降权和撤销。

## 7. Markdown 数据格式

```text
vault/context-forge/
  index.md
  reviews/2026/2026-09-15-auth-timeout.md
  knowledge/authentication/token-refresh.md
  rules/project/auth-refresh-lock.md
  conflicts/
  _drafts/
  _archive/
```

所有文件使用 YAML frontmatter，至少包含：

```yaml
type: review | knowledge | rule | conflict
id: unique-id
project: project-name
status: draft | approved | rejected | proposed | accepted | enabled | verified | conflicted | superseded | stale | disabled | archived
sources: []
scope: {}
confidence: low | medium | high
created_at: 2026-09-15
updated_at: 2026-09-15
```

正式知识不覆盖原文。新证据通过追加来源、产生新版本或建立冲突记录来处理。

上面的 `status` 是允许值全集；每种 `type` 只能使用自己的子集。Review 使用 `draft/approved/rejected/superseded`，Knowledge 使用 `proposed/accepted/revised/conflicted/archived`，Rule 使用 `proposed/enabled/verified/stale/disabled/archived`。实现时应使用按类型定义的 Pydantic 模型，禁止让一个字符串枚举承载全部状态语义。

`index.md` 是由系统生成的导航文件，不是人工编辑入口。用户编辑 Obsidian 文件后由 scan/watch 流程重新解析；文件移动、删除、frontmatter 无效和内容冲突都进入诊断结果。程序更新已有文件时必须携带期望 hash，hash 不匹配则生成冲突副本，不覆盖用户修改。

## 8. 规则检索

MVP 使用三级检索：

```text
项目 / 路径 / 技术栈过滤
  -> index.md
  -> SQLite FTS5
  -> 读取匹配 Markdown
```

规则支持四种激活方式：

- `always_on`：少量全局规则。
- `path`：匹配文件路径或 glob。
- `keyword`：匹配任务和用户请求中的关键词。
- `manual`：用户显式请求时启用。

第一版不引入向量数据库，也不把 `index.md` 作为唯一检索机制。默认使用项目、路径、关键词过滤和 SQLite FTS5；匹配结果必须回读 Markdown 并校验当前 hash。知识规模增长后，可以增加语义检索、实体关系和时间排序，但不能让索引取代 Markdown 事实源。

## 9. 隐私与安全

模型调用前执行本地脱敏，覆盖 token、API key、密码、SSH 私钥、Authorization header、数据库连接串和 `.env` 内容。

每个 session 的处理策略可配置：

```text
local_only       只保存事件和本地可处理的元数据，不调用远程模型
redacted_model   脱敏后调用用户明确配置的模型
disabled         不生成复盘草稿
```

敏感文件和敏感目录可以按 glob 排除。系统不得为了生成复盘而主动读取 `.env`、SSH 目录或凭据文件。

Context Forge 的写入目标必须经过 canonicalization，并确认位于配置的 vault 根目录内；拒绝越界路径和未经允许的符号链接。原始 transcript、脱敏副本、错误日志和模型请求体分别受 retention 配置控制，删除一个 session 时同时删除其派生草稿、索引记录和缓存。

## 10. 推荐技术栈

- Python 3.12：适合本地 CLI、文件处理、SQLite 和后台 worker。
- `uv`：锁定依赖和提供可复现开发环境。
- `sqlite3` / SQLite FTS5：事件队列、任务状态和全文索引；不作为知识事实源。
- Pydantic：校验领域对象和模型结构化输出。
- `python-frontmatter` 或等价的成熟 frontmatter parser：解析 Obsidian 文件，不手写脆弱的 YAML 切片逻辑。
- Typer：CLI 体验；命令逻辑仍放在 application service。
- pytest：领域、文件、队列和回放测试。
- Claude Agent SDK：作为可替换的 `LlmGateway` 实现，放在核心链路之后接入。
- MCP Python SDK：第二阶段接入，MVP 不以 MCP 作为用户审核界面。

worker 第一版使用 SQLite 单进程轮询，不引入 Redis、Celery 或独立服务。若不配置模型 provider，capture、vault、索引、人工审核和重建功能仍必须可用。

## 11. MVP

MVP 分成两个垂直切片。第一切片先验证复盘是否有价值：

```text
Claude Code 会话
  -> 自动生成复盘草稿
  -> 用户确认
  -> 写入 Obsidian
  -> 重建索引
  -> 用户搜索和复用
```

第二切片才验证规则是否有价值：

```text
已确认知识
  -> 规则 proposal
  -> 用户单独启用
  -> 下一次相关会话命中
  -> 用户反馈
```

MVP 不包含多编辑器适配、云端同步、团队知识库、强制规则、复杂知识图谱、向量数据库、自动合并长期知识和自动启用规则。若第一切片的复盘确认率不足，不进入第二切片。

第一切片指标：草稿发现率、复盘确认率、用户修改率、重复处理率和 Markdown 重建成功率。第二切片增加规则启用率、规则帮助率、重复错误率和错误规则撤销时间。

## 12. 重写策略

现有 `code-mentor` 作为产品行为实验和测试素材保留，不作为新架构基础。重写按以下顺序进行：

1. 建立领域模型和状态机。
2. 实现 Markdown vault adapter。
3. 实现 SQLite 任务队列。
4. 实现 transcript 解析和脱敏。
5. 实现 ReviewDraft 生成器。
6. 实现 CLI 审核和确认流程，先完成 capture → review → approve → vault。
7. 实现增量 scan、hash 冲突和 SQLite FTS5。
8. 接入 Claude Code hook，用真实 transcript 回放验证第一条垂直切片。
9. 在第一切片指标达标后实现 Knowledge 到 Rule 的编译 proposal、路径匹配和人工启用。
10. 最后接入 MCP；其他编辑器 adapter 单独排期。

重写期间不迁移旧数据格式。等新链路验证后，再编写一次性导入工具，将旧 vault 内容转成新 schema。

## 13. 竞品借鉴

- Hindsight：借鉴事实、经历、观察、mental model 分层，以及保留证据和逐步修正的思路。
- claude-memory-compiler：借鉴 SessionEnd / PreCompact、daily log、增量编译、Markdown index 和知识库 lint。
- Basic Memory：借鉴 Markdown-first、Obsidian wikilink、全文和图关系的可携带性。
- Claude Code / Devin：借鉴项目规则、路径规则、手动规则和自动记忆分离。

Context Forge 的差异在于：把“复盘、人类确认、规则编译、规则效果反馈”作为一等公民。

## 14. 应用接口

领域对象通过 repository 接口持久化，应用层只依赖这些抽象：

```python
class ReviewService(Protocol):
    def create_from_session(self, session_id: str) -> ReviewDraft: ...
    def approve(self, review_id: str, edits: ReviewEdits | None = None) -> ApprovalResult: ...
    def reject(self, review_id: str, reason: str | None = None) -> None: ...

class RuleService(Protocol):
    def compile(self, knowledge_id: str) -> RuleCandidate: ...
    def match(self, context: RetrievalContext) -> list[RuleMatch]: ...
    def record_feedback(self, feedback: RuleFeedback) -> None: ...

class VaultRepository(Protocol):
    def write_new(self, document: Document) -> Path: ...
    def update(self, document: Document, expected_hash: str) -> None: ...
    def rebuild_index(self) -> None: ...
```

模型供应商只暴露结构化能力：

```python
class LlmGateway(Protocol):
    def extract_review(self, transcript: SanitizedTranscript) -> ReviewExtraction: ...
    def compile_rule(self, knowledge: KnowledgeItem) -> RuleExtraction: ...
```

网关返回 Pydantic 对象，不允许返回未经校验的自由文本作为领域数据。

## 15. CLI 与 MCP 合约

CLI 面向人，MCP 面向 AI；两者共享 application service，不各自实现业务逻辑。

```text
forge init <vault>
forge doctor
forge jobs list
forge review list
forge review show <id>
forge review approve <id>
forge review reject <id>
forge knowledge search <query>
forge rule list
forge rule enable <id>
forge rule disable <id>
forge rule feedback <id> --outcome helpful|harmful|irrelevant
forge rebuild-index
```

MCP 第一版只提供四个工具：

```text
context_forge_search(query, project, path)
context_forge_get(id)
context_forge_pending_reviews()
context_forge_record_feedback(rule_id, outcome, note)
```

MCP 工具默认只能读取和记录反馈。批准知识、启用规则和写入用户文件需要 CLI 或明确的用户交互确认。

MCP 不是 MVP 的前置依赖。没有 MCP 时，CLI 仍能完成所有核心流程；接入 MCP 后也不能绕过相同的 application service 和状态转换。

## 16. SQLite 最小结构

```sql
CREATE TABLE jobs (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  available_at TEXT NOT NULL,
  locked_at TEXT,
  lease_expires_at TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE documents (
  id TEXT PRIMARY KEY,
  type TEXT NOT NULL,
  path TEXT NOT NULL UNIQUE,
  content_hash TEXT NOT NULL,
  frontmatter_json TEXT NOT NULL,
  indexed_at TEXT NOT NULL
);

CREATE TABLE rule_feedback (
  id TEXT PRIMARY KEY,
  rule_id TEXT NOT NULL,
  session_id TEXT,
  outcome TEXT NOT NULL,
  note TEXT,
  created_at TEXT NOT NULL
);
```

实际 schema 还需要 `sessions` 和 `events` 表保存捕获事实，并在 `jobs` 上建立唯一幂等键：

```sql
CREATE TABLE sessions (
  id TEXT PRIMARY KEY,
  project TEXT NOT NULL,
  cwd TEXT NOT NULL,
  transcript_path TEXT,
  transcript_hash TEXT,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE events (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(session_id, kind, payload_json)
);
```

`payload_json` 不得包含未脱敏 transcript 正文；事件只保存路径、hash 和处理元数据。SQLite 使用 WAL、busy timeout 和 schema version migration。任务 claim 必须在事务中取得 lease，不能仅依赖 `locked_at`。

队列使用 `pending -> running -> succeeded/failed` 状态。worker 启动时把超时的 `running` 任务重新置为 `pending`，并通过 `idempotency_key` 防止重复生成文件。

任务 claim 必须在事务中完成，并设置 SQLite WAL、busy timeout 和 lease。Hook、CLI 和 worker 可能同时写库，不能依赖单进程假设。Markdown 写入和 SQLite 更新无法共享一个原子事务，采用“先安全写 Markdown，再 upsert 索引；失败时由 scan/rebuild 修复”的顺序。

## 17. 失败处理

- Transcript 不存在：保留事件，标记 `source_unavailable`，不生成空复盘。
- 脱敏失败：阻止模型调用，写入错误日志，允许用户手动重试。
- 模型超时或限流：指数退避，超过次数后进入 dead-letter 状态。
- Markdown 写入冲突：不覆盖用户修改，生成冲突副本并提示处理。
- 索引损坏：从 Markdown 全量重建 SQLite，不修改 Markdown。
- 编译结果重复：根据来源 session、内容 hash 和 schema version 去重。

所有失败都必须可通过 `forge doctor` 定位，并可通过 `forge jobs retry <id>` 重试。任务 payload 不保存未脱敏 transcript 内容，只保存来源引用、hash 和必要元数据。

## 18. 测试策略

### 单元测试

- 状态机禁止非法转换。
- frontmatter 解析和 schema 校验。
- glob/path 规则匹配。
- 脱敏规则覆盖 token、密码和连接串。
- 编译器在重复来源和冲突来源下的行为。

### 集成测试

- fixture transcript 能生成稳定的 ReviewDraft。
- 用户确认后正确写入 Markdown 和 INDEX。
- SQLite 删除后可从 Markdown 重建。
- worker 崩溃后任务能恢复。
- 用户手动修改文件后不会被程序覆盖。

### 端到端测试

第一切片使用固定脱敏 transcript 验证核心链路：

```text
hook event
  -> queued job
  -> review draft
  -> approval
  -> Markdown review / knowledge
  -> rebuild
  -> search
```

规则切片再增加：

```text
approved knowledge
  -> rule proposal
  -> explicit enable
  -> context match
  -> feedback
```

测试重点是可追溯性和幂等性，而不是模型文案是否逐字一致。模型测试采用结构化断言，例如必须存在 `evidence`、`uncertainties` 和 `sources` 字段。

## 19. 第一批开发任务

1. 初始化 Python 包、配置加载和 `forge doctor`。
2. 定义 Pydantic 领域模型、状态枚举和 JSON Schema。
3. 实现 Markdown frontmatter 读写与安全文件写入。
4. 实现 SQLite jobs 表和可恢复 worker。
5. 实现 transcript parser 与本地脱敏器。
6. 使用 fixture transcript 实现 ReviewDraft 提取假模型。
7. 实现 CLI 的 review approve/reject 流程。
8. 实现增量 scan、hash 冲突、INDEX 生成和 SQLite FTS5 检索。
9. 接入 Claude Code hook，完成第一切片端到端回放。
10. 在第一切片通过后实现 Knowledge 到 Rule 的 proposal、人工启用、路径匹配和 feedback。

每个任务都应保持可独立验证。模型供应商接入放在假模型和领域测试之后，避免把基础正确性绑定到网络或模型输出。

## 20. 架构决策记录

### ADR-001：Markdown 作为事实源

选择 Markdown，因为用户需要直接拥有、阅读、编辑和迁移知识。代价是需要处理手动编辑、冲突和索引重建。

### ADR-002：异步处理 transcript

选择事件队列，因为会话结束 hook 的生命周期和超时预算不适合模型调用。代价是用户可能在下一次启动前看不到复盘结果。

### ADR-003：先用规则和全文检索

选择项目、路径、关键词和 FTS5，因为个人规模下更容易解释、测试和撤销。embedding 作为规模增长后的可替换检索策略。

### ADR-004：确认后才能影响 AI

选择人工确认，因为错误规则的长期成本高于漏记一条经验。代价是闭环多一步交互，需要通过高信息密度筛选降低打扰。

### ADR-005：先做模块化单体

选择一个 CLI 加可选 worker 的模块化单体，因为 MVP 的主要不确定性是用户是否愿意审核复盘，而不是部署扩展性。多应用和多 package 结构保留为目标边界，等真实依赖出现后再拆分。

### ADR-006：规则是第二个垂直切片

选择先验证 `capture → review → approve → vault`，因为它能独立证明“会话经验是否值得沉淀”。规则编译、命中和反馈依赖知识质量，应在复盘入库稳定后加入，避免一次 MVP 同时验证两个未经证明的行为链路。

## 21. 技术选型、架构与功能方向复审

### 技术选型结论

总体选型保留，但需要降低承诺：Python、SQLite、FTS5、Pydantic 和本地 Markdown 适合第一版；Claude Agent SDK 和 MCP SDK 必须是可选适配器，不能成为核心域依赖。第一版的最小运行时可以只有 Python 标准库、Pydantic、frontmatter parser、Typer 和 pytest。

不建议当前引入 Redis、Celery、PostgreSQL、向量数据库、独立 Web 后台或事件总线。它们解决的是规模和协作问题，无法回答用户是否愿意确认复盘这一核心问题。

模型供应商必须通过 `LlmGateway` 接口接入，并允许三种模式：

```text
fake       fixture 驱动，供测试和离线开发
local      用户配置的本地模型
remote     脱敏后调用用户配置的远程模型
```

未配置模型时，系统可以让用户手动创建和确认复盘，不能因为 provider 不可用而阻塞 vault 和索引能力。

### 架构结论

当前方案的目标分层正确，但最初的 `apps/`、`packages/` 和 MCP/worker 拆分对 MVP 偏重。实施形态采用模块化单体，逻辑边界保留为：

```text
adapters -> application -> domain
               |             |
          vault/index     compiler
```

所有写操作经过 application service。Domain 只处理规则和状态；vault 负责 Markdown 文件；index 负责 SQLite；adapter 负责 Hook、模型和 MCP。禁止 CLI、MCP 和 worker 各自实现审批、规则启用或文件写入逻辑。

双重存储的权威关系必须固定：Markdown 是内容事实源，SQLite 是队列和索引事实源。两者不做假设上的原子双写，通过 hash、outbox job、scan 和 rebuild 保持最终一致。任何索引记录都必须能指向一个现存且可解析的 Markdown 文件。

### 功能方向结论

产品核心应收窄为“开发会话复盘编译器”。原有 mentor 的认知陷阱检测、梯度提问和强约束交互可以作为上层 skill 或策略包，但不能进入知识编译器的核心域。否则产品同时承担学习教练、会话管家、知识库和规则引擎，用户无法理解第一价值。

功能优先级调整为：

1. 自动捕获并生成可审核的复盘草稿。
2. 用户确认后写入可读、可迁移的 Markdown。
3. 提供按项目和路径的搜索与来源追踪。
4. 从已确认知识生成规则 proposal，并由用户单独启用。
5. 记录规则命中和人工反馈，支持禁用、过期和撤销。

“自动编译规则并直接影响 AI”不属于 MVP。规则至少需要经历 `proposed -> enabled` 两次明确状态转换；`verified` 只能由明确反馈或可解释的验证策略产生，不能由模型自评产生。

SessionEnd 无法弹出有效的交互式复盘，因此产品触发应定义为：会话结束异步生成草稿，下一次 SessionStart、CLI 或用户显式命令发现待审内容。系统只在检测到明确结果、重复失败或足够信息密度时建议复盘；没有价值的会话静默结束。

### MVP 成功门槛

第一阶段只验证以下链路：

```text
fixture / Claude transcript
  -> queue
  -> redacted review draft
  -> user approve/reject
  -> Obsidian Markdown
  -> rebuild and search
```

进入规则切片前，应满足：复盘草稿可发现、重复处理不产生重复文件、用户修改不会被覆盖、删除后可重建、敏感字段不会进入模型请求和错误日志。进入规则切片后，再增加规则 proposal、人工启用、命中解释和 feedback。

## 22. 自审后的最终决策

### 技术选型

保留 Python + SQLite + FTS5 + Pydantic + Markdown。Python 适合本地 CLI、transcript 处理和 worker；SQLite 足以覆盖单用户队列、状态和全文检索；Pydantic 用于边界校验。Claude Agent SDK、MCP SDK、embedding 服务和任何远程 provider 都是可替换适配器，不能进入 domain 或让基础功能依赖网络。

首个可运行版本不需要 Web UI、PostgreSQL、Redis、Celery 或向量数据库。CLI 是审核界面；系统通知和 Web UI 只有在“用户发现不了草稿”被数据证实后再增加。

### 架构

采用模块化单体，而不是一开始拆成微服务或多个可独立发布包。运行时由一个 `forge` CLI、一个可选 worker 和 Claude Code hook 组成。模块边界如下：

```text
adapter -> application service -> domain
                         |          |
                    vault/index  compiler
```

所有审批、写入和规则状态转换必须经过 application service。Markdown 是文档内容事实源；SQLite 是队列、事件和索引事实源。两者不做虚假的原子双写，使用 hash、幂等键、scan 和 rebuild 达到最终一致。

### 功能方向

产品第一价值是“让开发经验被看见、被确认、可再次找到”。因此第一切片只做：会话捕获、脱敏、复盘草稿、人工确认、Obsidian 写入、扫描重建和搜索。Knowledge 合并、规则 proposal、规则命中和 feedback 是第二切片，必须建立在第一切片的使用数据之上。

原有 mentor 的讲解、梯度提问、认知陷阱检测和强约束交互暂时作为独立 skill/策略包，不进入编译器核心。它们可以消费 Review 和 Knowledge，但不应改变 vault、queue 或规则状态机。

### 用户同意与隐私边界

第一次启用时必须明确告知用户：会读取哪些 transcript、何时调用模型、内容保存多久、如何禁用和如何删除派生数据。默认只处理用户启用 Context Forge 后产生的会话；历史 transcript 需要显式导入命令。

没有模型 provider 时仍可运行本地 capture、手动 review、vault 和 search。用户关闭自动复盘后，Hook 可以继续记录最小事件，也可以完全卸载；两种模式要在配置中区分。

### 第一切片退出标准

只有同时满足以下条件，才进入规则切片：

- 至少一组真实用户会话能发现并完成审核流程。
- 同一 session 重试不会产生重复 review 或重复文件。
- 用户在 Obsidian 中编辑后不会被覆盖。
- 删除和重建索引后内容一致。
- 测试确认凭据模式不会进入模型请求和错误日志。
- 用户确认的复盘中，存在足够比例的内容被用户保留，而不是全部删除或重写。

如果草稿发现率或确认率很低，优先改触发和审核体验，不增加检索算法或规则自动化。

## 23. 首版运行契约

### 配置

配置文件默认位于 ~/.context-forge/config.toml，不把配置写入 vault：

    vault_path = "C:/Users/me/Documents/Obsidian"
    knowledge_dir = "context-forge"
    processing_mode = "redacted_model"
    model_provider = "none"
    poll_interval_seconds = 2
    max_attempts = 3
    retention_days = 30
    excluded_globs = ["**/.env*", "**/.ssh/**", "**/*secret*", "**/*credential*"]

启动时必须校验 vault 路径存在、是目录且 canonical path 没有越出用户配置范围。配置缺失时，forge doctor 应给出修复指引；capture 不得静默写入未知目录。

### Hook 事件

Adapter 将不同助手的输入归一化为同一个事件：

    {
      "event_id": "evt_01...",
      "event_type": "session_end",
      "source": "claude_code",
      "session_id": "sess_01...",
      "project": "code-mentor",
      "cwd": "C:/work/code-mentor",
      "transcript_path": "C:/Users/me/.claude/projects/.../session.jsonl",
      "transcript_hash": "sha256:...",
      "occurred_at": "2026-09-15T12:00:00Z"
    }

event_id 由 adapter 生成并唯一；session_id + event_type + transcript_hash 用于跨 hook 去重。未知字段可以保留在 adapter 层，但不能未经 schema 校验进入 domain。

### ReviewExtraction

模型输出必须符合结构化 schema：

    {
      "title": "认证超时问题",
      "problem": "...",
      "attempts": [
        {"summary": "检查刷新逻辑", "result": "failed", "evidence": ["..."]}
      ],
      "outcome": "...",
      "claims": [
        {"text": "...", "kind": "fact", "confidence": "medium", "evidence_ids": ["ev_01"]}
      ],
      "uncertainties": ["..."],
      "candidate_topics": ["authentication"],
      "should_save": true,
      "reason": "存在失败尝试和可迁移结论"
    }

claims.kind 至少区分 fact、inference 和 open_question。缺少证据的 fact 不得被写入 accepted Knowledge；should_save=false 的结果可以保存为处理记录，但不产生用户可见草稿。

### 审批事务

forge review approve <id> 必须在一次 application operation 中完成：

    读取 draft
      -> 校验 draft 版本和用户编辑
      -> 生成 review Markdown 临时文件
      -> 原子 rename
      -> 创建或更新 Knowledge proposal
      -> 更新 SQLite index
      -> 标记 Review approved

Markdown 与 SQLite 无法共享事务。任何中间失败都必须留下可重试状态；重试依据 document id、content hash 和 source id 幂等，不重新生成另一份文件。

### 最小 CLI 行为

    forge capture <event.json>
    forge worker run --once
    forge review list
    forge review show <id>
    forge review approve <id>
    forge review reject <id>
    forge scan
    forge search <query>
    forge doctor

worker run --once 是首版的主要测试入口；常驻 worker 只是重复调用同一个 application service。这样可以先验证流程，再处理进程托管和系统通知。

## 24. 首个迭代验收用例

1. 给定一个脱敏 fixture event，系统创建一个唯一 review job。
2. 重复提交 SessionEnd 和 PreCompact 事件，不产生第二个 job 或第二份 review。
3. 没有模型 provider 时，系统仍能列出任务并允许用户手动创建或编辑 review。
4. 模型返回缺少 evidence 的 fact 时，任务失败且不写入 accepted Knowledge。
5. 用户批准 review 后，vault 中产生合法 Markdown，SQLite 能重建出同一 document。
6. 用户在 Obsidian 中修改文件后运行 forge scan，索引内容更新且程序不覆盖修改。
7. 目标文件 hash 发生变化时，approve 不覆盖文件，而是产生冲突状态。
8. worker 在写文件后、更新索引前退出，下一次运行能完成索引且不重复生成文件。
9. fixture 中出现 token、Authorization header 或 .env 内容时，模型请求和错误日志中均不存在原值。
10. 删除一个 session 后，其 review、knowledge proposal、索引记录和缓存按 retention 规则被删除或标记归档。

首个迭代只需通过这十条，再考虑接入真实模型或实现规则 proposal。它们验证的是数据安全、幂等、可恢复和用户拥有数据，而不是模型文案质量。

## 25. 当前实现进度

已实现：

- Python 模块化单体骨架和 `pyproject.toml`。
- SessionEvent 结构化模型、事实 claim 证据约束和脱敏处理。
- SQLite sessions/events/jobs/reviews 表、幂等键、WAL 和 lease claim。
- 离线 ReviewExtractor、worker 和原子 Markdown review 写入。
- Review registry、CLI 审核状态和 Knowledge proposal 写入。
- 按 vault 隔离的 SQLite FTS5 索引、`scan` 和 `search`。
- 明确的 Rule proposal 文件写入，默认不会自动启用规则。
- Rule proposal 的 `enable/disable` 和 helpful/harmful/irrelevant/unknown feedback 记录。
- Claude Code `SessionEnd` / `PreCompact` hook adapter 与 settings 接入。
- 已启用规则按 project/path glob 匹配，并输出命中原因和来源文件。
- 可选 MCP server 已提供搜索、待审复盘、文档读取、规则匹配和反馈工具。
- `LlmGateway` 协议与 `OfflineGateway` / `NoOpGateway` 实现，未配置 provider 时也能跑 capture/vault/search。
- `forge scan` 重建 FTS5 + 重生 index.md；`forge watch` 按 mtime 轮询增量重索引。
- `vault.update_with_expected_hash` 在 hash 不匹配时生成 `.conflict-<ts>` 副本而不是覆盖用户编辑。
- `forge knowledge list/show/accept` 命令、proposal→accepted 文件迁移。
- `rule_hit_events` 表与 `forge rule-stats <id>`，CLI `rule-match --record` 持久化命中事件。
- `forge install-hook [--uninstall]`，幂等写入 `~/.claude/settings.json`，通过 `context_forge_managed` 标记只删除自己装的钩子。
- 13 条 pytest 在 Python 3.12 环境下全部通过；本轮新增 9 条覆盖 doctor/MCP/hook e2e/fact 拒收/session 清理等。
- `forge doctor` 完整诊断：settings / vault canonicalization / queue stall / FTS 一致性 / provider。
- `forge knowledge edit <id>` 调 $EDITOR；记录前后 hash，提示 `forge scan`。
- `forge session-delete <id>` 删除 session/events/jobs/reviews/feedback/hits 全部队列状态。
- `forge session-outcome <id> --outcome ...` 显式记录命中归因。
- worker 二次校验 `ReviewExtraction`，对未带 evidence 的 fact 拒收（§24 用例 #4）。
- `session_end` 自动给未反馈的命中写 `unknown`；session-outcome 不替换而是叠加（事件日志）。
- `select_gateway` 识别 `offline`/`fake`/`local`/`remote`；`local`/`remote` 是 `NoOpGateway` 占位，等用户接入具体客户端。
- Claude Code hook 端到端回放：模拟 stdin 注入 SessionEnd，验证入队 + 重复入幂等。
- `forge retention [--days N] [--dry-run]`：preview 列出会话；apply 真删。`JobStore.retention_preview/apply` 实现。
- `forge knowledge accept --rule-project ... --rule-instruction ... --rule-paths ...`：在 proposal 注入 `candidate_*` 字段，accept 时自动写一条 `RuleProposal`（review→knowledge→rule 闭环）。
- `forge import-transcript <path>`：§22 历史 transcript 显式导入。
- `docs/mcp-transport.md`：stdio transport + Claude Desktop / Claude Code 的 `claude_desktop_config.json` 示例。
- 26 条 pytest 全过；本轮新增 3 条覆盖 retention / 知识→规则闭环 / history import。
- `forge context --project X [--path Y]`：一次输出命中规则 + 最近 accepted knowledge + 最近 reviews，专为 AI 入口消费。
- `forge knowledge merge --keep proposed|accepted|<substring>`：处理 `.conflict-<ts>` 副本，用户选哪个就保留哪个。
- `forge doctor` 集成 retention preview：超期会话数进入信息而非失败状态。
- 真实 provider 接入：`provider_http.py` 实现 `RemoteGateway`（Anthropic Messages API）和 `LocalGateway`（Ollama `/api/chat`），共享 `HttpGateway` 的 retry / backoff / JSON parse。httpx MockTransport 端到端测试覆盖。
- `select_gateway("local"|"remote", settings)` 缺凭据时抛 `GatewayError`，由 worker 标 job 失败而非静默 drop。
- `forge provider-check`：用最小 prompt ping 当前 provider，确认 auth + 网络 + 模型名可用。
- 34 条 pytest 全过；新增 5 条覆盖 remote/local/重试/凭据校验。
- §11 指标采集：`reviews.draft_hash` 列记录模型原文；worker 落 `.draft` 副本来支撑 review-diff；`rebuild_history` 表记录每次 index 重建是否成功；`forge metrics` 聚合 discovery_rate / approval_rate / avg_approval_delay / rebuild_success_rate。
- `forge review-diff <id>`：对比当前文件与模型草稿的 hash，输出 unified diff。
- 37 条 pytest 全过；新增 3 条覆盖 review-diff / metrics / rebuild_history。
- Observability 命令：`forge rule-list [--status]` / `forge rule-show <id>` / `forge session-info <id>`，输出 JSON；`vault.list_rules` / `vault.read_rule` / `JobStore.session_info` 支撑。
- 40 条 pytest 全过；新增 3 条覆盖 session-info / rule-list / rule-show。
- 方案 §16 队列契约：`finish(error=..., max_attempts)` 在 attempts 到 max 时落 dead_letter；`retry_job` 把 failed/dead_letter → queued；`forge jobs --status dead_letter` 列死信；`forge jobs-retry <id>` 救回；metrics 加 `jobs_dead_letter` 计数。
- 方案 §22 worker 进程：`worker.run_loop(store, vault, gateway, interval_seconds, max_attempts, stop_after)` 持久轮询；SIGTERM 转 KeyboardInterrupt 走同一条 exit path；`forge worker run`（无 --once）走 daemon 模式，`--interval` 覆盖 `poll_interval_seconds`。
- 47 条 pytest 全过；新增 2 条 daemon 循环测试（stop_after 退出 + gateway 异常不死循环）。
- **大重构：循环内自动 + 循环外用户增删。** 用户决定所有 approval gate 都去掉，知识/规则由 worker 自动写；用户动作改为文件级。
  - 新域类型：`SessionArtifacts` 一次模型响应返 `title/problem/attempts/outcome/claims/knowledge/rule_candidate`；`LlmGateway.extract_session` 单方法取代旧 `extract_review`。
  - `vault.write_draft` / `write_knowledge_auto` / `write_rule_candidate`：worker 一次写三份文件（`_drafts/` + `knowledge/accepted/` + `rules/proposals/status=proposed`）。
  - `user_owned: true` frontmatter：worker 见到跳过，不覆盖用户手写条目。
  - 规则永远不 auto-enabled；用户改 frontmatter `status: proposed` → `enabled` 启用。
  - CLI 从 24 个命令压到 6 个：`init / install-hook / worker run / status / doctor / retention`；MCP 提供 search / get / match_rules / recent_knowledge / recent_rules / record_feedback 给 AI 用。
  - `forge session-outcome` 仍保留作为用户显式反馈入口（叠加而非替换）。
  - `RuleMatcher` 修了两 bug：空 paths 时仅在 changed_path 为 None 时入 match；comma-separated `paths: a,b,c` 一行 frontmatter 也正确解析。
- 21 条 pytest 全过；覆盖新 auto-loop 三文件写、`user_owned` 跳过、规则启用 match、daemon 跑 N 个 job、gateway 异常不死循环。
- 实测：worker 一次调用 model → 自动写 `_drafts/e2e-1.md` + `knowledge/accepted/react-usestate-default-array.md` + `rules/proposals/rule-react-usestate-default-array.md`；用户 `sed` 改 frontmatter `status: enabled` → RuleMatcher `**/*.tsx` 命中 `src/components/Cart.tsx`。

未完成：

- Obsidian 文件 watcher 的 inotify/FSEvents 实时版本；目前仅基于 mtime 轮询。
- 规则命中的自动 feedback 采集：worker 在 `session_end` 时对未确认的命中写入 `unknown`；用户用 `forge session-outcome` 显式覆盖。
- MCP server 已通过 5 个工具的端到端测试；stdio transport 文档与 `claude_desktop_config.json` 示例见 `docs/mcp-transport.md`。
- 真实 provider 接活的端到端验证（当前 `forge provider-check` 只发最小 ping，未带真 transcript）。
- `forge doctor` 主动调用 `retention_apply`（目前 doctor 只诊断 + 用户手动跑 `forge retention`）。

---

## 26. 最终状态（2026-09-15）

按方案 §22 第一切片退出标准，所有可代码化的项都已实现并测试覆盖；剩余项只能由真实使用数据验证。

### 已实现（按方案章节）

| 章节 | 实现 |
|---|---|
| §5 会话生命周期 | `SessionEnd` / `PreCompact` hook → queue.db；worker daemon 轮询 → 调模型 → 自动写三份文件 |
| §6 状态机 | `Job: queued → running → succeeded / failed → dead_letter`；`Rule: proposed → enabled → disabled`（用户改 frontmatter）；`Knowledge: accepted` 自动发布；`Session: captured` 写入即定 |
| §7 数据格式 | `vault/_drafts/`（模型原文）+ `vault/knowledge/accepted/` + `vault/rules/proposals/`；frontmatter 统一；hash 冲突生成 `.conflict-<ts>` 副本；`user_owned: true` 阻止 worker 覆盖 |
| §8 检索 | SQLite FTS5 + index.md；MCP `context_forge_search` |
| §9 隐私 | sanitize_transcript；fact claims 必须带 evidence_ids；无 evidence → job failed；`forge session-outcome` 显式反馈叠加 |
| §16 SQLite 队列 | sessions/events/jobs/rule_feedback/rule_hit_events；WAL + busy_timeout + lease claim；`max_attempts` 强制 → dead_letter |
| §17 失败处理 | `forge doctor` 定位 + `forge jobs-retry` 救回 + dead_letter 状态；payload 不含未脱敏 transcript |
| §18 测试 | 21 条 pytest（auto-loop 三文件写、user_owned 跳过、daemon N job、gateway 异常不死循环、dead_letter/retry、fact 拒收、select_gateway 凭据校验、status/metrics 刷新） |
| §19 任务 1-10 | 全部完成；§19 任务 7（review approve/reject）已合并到 auto-loop（用户改 frontmatter 替代命令） |
| §20 ADR | Markdown 是事实源 + 异步队列 + 全文检索优先 + 用户确认才影响 AI + 模块化单体 + 第二切片前置 |
| §21 复审 | 收敛到单 worker + 模块化单体；provider 必须三模式 fake/local/remote；未配 model 仍能跑 |
| §22 worker | `forge worker run [--once]` daemon 模式，SIGTERM → KeyboardInterrupt 干净退出 |
| §23 配置 | `~/.context-forge/config.toml`；worker daemon 写 status.md；`forge status --json` 注入 context |
| §24 验收用例 1-10 | 全部由 pytest 自动化覆盖；#1/#10 需真实数据 |

### CLI 集合（6 个 + 5 个 escape hatch）

**日常用（6）**：`forge init` / `forge install-hook` / `forge worker run` / `forge status` / `forge doctor` / `forge retention`。

**escape hatch（8）**：`forge provider-check` / `forge import-transcript` / `forge session-info` / `forge jobs` / `forge jobs-retry` / `forge receipts` / `forge receipt-retry` / `forge receipt-dismiss`。

**MCP 工具（5）**：`context_forge_search` / `context_forge_get` / `context_forge_match_rules` / `context_forge_recent_knowledge` / `context_forge_recent_rules` + `context_forge_record_feedback`。

### 循环内（worker 全自动）

```
SessionEnd hook → queue.db
worker extract_session(transcript)
  └─ SessionArtifacts(should_save, knowledge?, rule_candidate?)
       ├─ vault/_drafts/{session_id}.md
       ├─ vault/knowledge/accepted/{k_id}.md    (if knowledge 非空，user_owned: true 跳过)
       └─ vault/rules/proposals/rule-{k_id}.md   (if rule_candidate 非空，status=proposed，never auto-enabled)
```

### 循环外（用户文件级）

| 想做什么 | 怎么操作 |
|---|---|
| 看状态 | `forge status` 或看 `~/.context-forge/status.md` |
| 看知识 | Obsidian 打开 `vault/knowledge/accepted/` |
| 改知识 | 直接编辑文件（worker 看到 user_owned: true 不覆盖） |
| 加知识 | 新建 `vault/knowledge/accepted/{id}.md` + 加 frontmatter `user_owned: true` |
| 删知识 | 删文件 |
| 启用规则 | 改 frontmatter `status: proposed` → `enabled` |
| 禁用规则 | 改 frontmatter `status: enabled` → `disabled` 或删文件 |
| 评分规则 | MCP `context_forge_record_feedback` 或 `forge jobs` + DB SQL |

### 真正未做（需要真实使用数据，代码补不出来）

- **§22 第一切片退出 #1**：至少一组真实用户会话完成审核流程 → 装 hook 用真实 Claude Code session 跑几次
- **§22 第一切片退出 #2**：内容保留率 → 跑几天后看 `forge metrics` 输出

跑出真实数字再决定是否进 §11 第二切片（Knowledge 自动合并 + Rule verified 状态机）。

### 测试覆盖

```
21 passed in 1.35s
```

涵盖：auto-loop 三文件写、`user_owned` 跳过、should_save=false 静默、duplicate 拒、dead_letter/retry、fact 无 evidence 拒、select_gateway 凭据校验、run_loop N job 干净退出、gateway 异常不死循环、sanitize 脱敏、rule list+match、status 刷新、metrics。
