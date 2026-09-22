# 跨项目记忆与召回竞品调研

日期：2026-09-22

## 问题

Context Forge 当前把 `project` 当成召回硬过滤条件。本文调查同类系统如何划分记忆边界、组织长期记忆，以及是否允许跨项目联想。

## 核心结论

市场上不存在一种统一的“项目作用域”方案，但形成了两类明显模式：

1. 编码助手通常按 Git 仓库或 workspace 隔离自动记忆，简单、安全，但跨项目迁移弱。
2. 通用 Agent 记忆系统通常按 user、agent 或 memory bank 做最高层硬隔离；项目只是 metadata、namespace 或图中的上下文，不必成为召回边界。

对 Context Forge 最有价值的不是取消所有边界，而是把当前单一的 `project scope` 拆成：

- **安全边界**：用户、组织、Agent 或私密 memory bank，硬过滤。
- **来源上下文**：project、session、path，保留溯源，不决定可见性。
- **适用条件**：技术栈、任务类型、约束、环境和反例，参与排序与采用判断。

## 竞品对比

| 系统 | 最高层边界 | 项目如何处理 | 召回机制 | 对 Context Forge 的启示 |
|---|---|---|---|---|
| Claude Code Auto Memory | Git 仓库派生的 project memory directory | 每个项目独立目录；同仓库 worktree/subdirectory 共享 | 启动加载 `MEMORY.md` 索引，topic 文件按需读取 | 是典型“项目文件柜”；适合项目指令，不适合作为个人跨项目学习模型 |
| Mem0 | `user_id` / session / agent | 项目可由调用方作为过滤 metadata 表达，不是内建认知边界 | 新算法并行融合 semantic、BM25、entity linking，并加入 temporal reasoning | 身份是边界，项目是检索线索；符合个人长期大脑方向 |
| Hindsight | Memory Bank；bank 之间严格隔离 | 一个 bank 可代表 user、agent 或 project；bank 内 metadata 可继续过滤 | semantic、BM25、graph、temporal 四路并行，RRF 合并并重排 | Bank 应对应“一个大脑/主体”，不应默认一个项目一个 bank；其 observation 和 mental model 分层值得借鉴 |
| Graphiti / Zep | Context graph / group，由应用负责治理 | 项目可作为 group 或图属性，但核心是实体、关系、事件和时间有效期 | semantic、keyword、graph traversal 混合检索，可按 graph distance 重排 | 项目不是知识本体；来源 episode 与当前事实分开，跨项目可通过共享实体产生联想 |
| LangGraph Store | 应用定义的 Store namespace | namespace 可表达 user、应用或任意层级；长期记忆明确跨 thread | Store 检索由应用定义，可配置语义检索 | namespace 是寻址与权限机制，不应直接等同于知识适用范围 |

## 逐项观察

### Claude Code：项目隔离优先

Claude Code 将自动记忆存到 `~/.claude/projects/<project>/memory/`。项目标识来自 Git 仓库；同一仓库的 worktree 和子目录共享目录。启动时只加载索引，topic 文件按需读取。

这套设计优化的是“当前仓库连续工作”，而不是个人知识迁移。用户偏好虽然被列为一种 memory type，仍存入项目目录，因此天然存在重复和跨项目不可见问题。它适合作为 Context Forge 的项目缓存或工作记忆参考，不适合作为长期记忆的最高层模型。

### Mem0：身份边界加多信号召回

Mem0 把 User、Session、Agent 作为多层记忆标识。官方示例在写入和搜索时使用 `user_id`，说明主体身份是首要检索边界。其 2026 年公开算法同时计算 semantic、BM25 keyword 和 entity matching，再融合结果，并加入时间推理。

关键不是具体向量数据库，而是：先确定“是谁的记忆”，再根据当前问题寻找相关内容。项目可以作为 metadata 或排序特征，而不必把同一用户在不同项目中的知识隔断。

### Hindsight：最接近“一个主体一个大脑”

Hindsight 的 Memory Bank 是严格隔离单元，可代表一个 user、agent 或 project。Bank 内区分 world facts、experiences、observations 和 mental models。原始事实不会永远保持平铺：后台把相关事实整理为带精确证据和 proof count 的 observation，新证据用于强化、削弱或扩展，而不是静默覆盖。

Recall 并行执行 semantic、keyword、graph 和 temporal 四种策略，经 reciprocal rank fusion 与 cross-encoder 重排。其重要设计选择是“bank 决定安全隔离，metadata 决定局部过滤”。如果目标是个人长期学习，Context Forge 的 bank 对应用户或 Agent，而不是 project。

### Graphiti / Zep：项目让位于实体、关系和时间

Graphiti 把输入保存为 episode，再从中抽取实体和带时间有效期的事实；派生知识始终可以追溯到 episode。检索组合语义、关键词和图遍历，并可利用图距离重排。

这种模型天然支持跨项目迁移：两个项目只要涉及相同实体、技术或问题模式，就可能通过图关系连接。代价是抽取、实体消歧、图存储和维护复杂度明显高于 Context Forge 当前的 SQLite + FTS5。

### LangGraph：namespace 是应用边界，不是认知模型

LangGraph 明确区分 thread-scoped short-term memory 与 Store 中跨 thread 的 long-term memory。Store 的 namespace 由应用定义，可用于用户、应用或其他层级。它没有替开发者决定哪些知识应跨项目，但提供了正确的基础抽象：namespace 负责寻址和隔离，语义检索负责相关性。

## 三种可选方向

### A. 项目硬隔离，显式提升为全局

保留当前行为，只有人工或模型确认后才将记忆从 project 提升到 global。

- 优点：迁移风险最低，误召回少。
- 缺点：仍依赖提前分类；大量可迁移经验会被困在原项目。
- 对标：Claude Code。

### B. 主体硬隔离，项目软排序（推荐）

以 user/agent memory space 作为硬边界；所有允许访问的记忆进入候选集。project、path、技术栈、任务类型、实体、时间和验证结果共同参与排序。跨项目结果必须显示来源和迁移理由。

- 优点：能形成真正的跨项目学习，同时保留隐私与可解释性。
- 缺点：需要新的候选生成、重排和负反馈评估；初期误召回会增加。
- 对标：Mem0、Hindsight 的主体边界与混合召回。

### C. 时序知识图谱

把项目、技术、错误、决策、结果全部建成实体和关系，沿图扩散召回。

- 优点：跨项目关系表达最强，适合冲突、演化和实体联想。
- 缺点：显著增加抽取与运维复杂度；在个人规模尚未证明必要。
- 对标：Graphiti / Zep。

## 建议

选择 B，但先用现有 SQLite 实现，不立即引入向量库或图数据库：

1. 新增主体级 `memory_space` 或 `owner`，作为真正的硬隔离边界。
2. 将 `project` 从 `Scope` 中拆出，变成 provenance/context 特征。
3. 保留显式 `private/project-only` 约束，处理确实不能跨项目的内容。
4. 候选生成先采用 FTS 全库检索；同项目加分，但跨项目不再直接删除。
5. 为记忆增加可迁移描述：技术实体、任务类型、前置条件、反例和来源项目。
6. 返回结果同时给出“为什么相关”和“为什么可能不适用”。
7. 用 applied/validated/negative feedback 校准排序，不把单纯曝光当强化。

这条路线吸收了 Hindsight 和 Mem0 的核心思想，但保持 Context Forge 的本地优先、证据可追溯和低运维成本。

## 需要验证的实验

构造至少三组跨项目任务：同技术同问题、不同技术同模式、表面相似但约束冲突。比较：

- 当前 project 硬过滤；
- 全库 FTS + 同项目加权；
- 全库 FTS + 上下文重排。

主要指标应是决策帮助率、错误迁移率、首次成功率和额外 token/延迟，而不是召回条数。

## 一手来源

- Anthropic, “How Claude remembers your project”: https://code.claude.com/docs/en/memory
- Mem0 official repository and algorithm overview: https://github.com/mem0ai/mem0
- Hindsight official repository, Core Concepts: https://github.com/vectorize-io/hindsight
- Graphiti official repository: https://github.com/getzep/graphiti
- Zep / Graphiti paper: https://arxiv.org/abs/2501.13956
- LangGraph persistence documentation: https://docs.langchain.com/oss/python/langgraph/persistence

## 限制

- 部分托管产品的生产排序算法是闭源实现；本文只依据其公开文档，不推断未公开权重。
- 厂商公布的 benchmark 不宜直接横向比较，除非模型、数据集版本、检索预算和评测协议完全一致。
- 本文讨论的是信息组织和召回边界，不覆盖团队权限、云端合规或商业定价。
