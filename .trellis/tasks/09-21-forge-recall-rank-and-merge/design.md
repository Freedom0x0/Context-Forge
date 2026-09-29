# 长期记忆总体设计

> **2026-09-22 变更**：召回边界模型已更新，见 `.trellis/tasks/09-22-falsifiable-memory-recall/` 与 `docs/technical-design-falsifiable-memory.md`。
> 要点：`project` 默认只作来源（provenance）和弱排序，移出默认硬过滤路径；项目级访问限制改由显式 `AccessPolicy` 表达；自动沉淀与有依据的修正为主流程，人工阅读/修改/抑制/删除是可选旁路。本文档以下与 `project` 硬过滤、人工闸门相关的表述以新文档为准。
## 选型
SQLite＋FTS5，复用 sqlite3、现有队列与模型网关。暂不引入 PostgreSQL、Redis、图数据库。以后向量召回只作为可重建候选通道。
memory.db 作为权威来源，queue.db 保存任务。Markdown 单向导出，显式 import-correction 才将外部编辑转为数据库纠正，禁止双向自动同步。
## 数据模型
- memories：稳定 id、类型、当前版本指针、状态、人工抑制。
- memory_versions：正文、摘要、价值/可信度/风险判断、模型及提示词版本，不原地覆盖。
- memory_sources：证据片段/引用、hash、session、提取时刻。关键来源不只依赖会被清理的队列表。
- memory_events：事件 id、幂等键、actor、对象版本、payload、时间和依据；human/user 统一为 human。
- memory_relations：版本感知 derived_from、duplicate_of、overlaps、conflicts_with、supersedes、supports；支持撤销。
- memory_scopes：显式 global/project、project id、路径模式；unknown 不等于 global。
- retrieval_events：request id、阶段、memory id/version、解释、可得的 session id，未知为 NULL。
- memory_fts：中文 bigram＋ASCII token 派生索引；原文单独保存。
当前状态是直接可查投影；事件和版本支持追溯，不要求完整事件溯源框架。
## 读写协议
MemoryStore 统一写入。版本、事件、当前指针和 FTS 同事务更新；使用外键、唯一约束、参数化 SQL、WAL、有限 busy_timeout 和版本比较更新。
模型调用在事务外。每操作连接明确关闭，短事务适应 SQLite 单写者。
队列 job id＋处理版本作为发布幂等键：领取任务 -> memory.db 提交 -> queue.db 完成。中间崩溃重试复用结果，不假设跨库原子提交。
模型先生成 proposal，再校验结构/来源/当前版本，自动 keep/discard/uncertain。自评非校准概率，不确定自动保留候选或低影响状态，不等待人审核。
## 检索协议
search_memory(query, project, changed_path, limit) 返回摘要、id/version、解释；get_memory(id, version) 按需取正文、来源、适用条件和冲突提示。
状态与作用域过滤在候选截断前完成。规则保留现有 glob 语义；global 必须明确标注，unknown 不跨项目泄露。
索引与查询使用相同 token 化，安全构造 MATCH；BM25 越小越优。相关性/作用域优先，证据等级及冲突作解释调整，id 全序收尾，不堆未经标定的权重。
默认 top-K 8，可显式 unlimited，并设返回字节预算。短中文单字提供范围/条数受限字面兜底。模型关键词扩展可选、有预算、失败本地兜底。
工具返回临时进入上下文是模型读取记忆的方式，不后台全量注入。结果是参考数据，不是高权限指令。
## 反馈与治理
tool_returned 不等于 presented；仅宿主能确认实际呈现。模型自述最多记录 referenced/self-report；采用和验证必须有对应动作/检查依据，无依据则 unknown。
曝光不提高记忆价值。人工纠正是约束，但主观保留不证明技术正确。
完全重复且作用域兼容建立 canonical/alias；相似度只生成候选，不证明同义或冲突。近似内容可用模型生成新版本提案，应用前校验版本、来源和范围，不能确定则保持分立。
新时间戳不自动证明 supersedes。纠正后派生规则失效或重新生成；人工抑制不得通过合并复活。
扫描来源去重与显式反馈分开，不能全局按 rule/outcome/note 吞掉合法重复反馈。
## 迁移与恢复
只读旧 vault 快照，以旧 id/path/hash 建映射，保留 user_owned/disabled、项目与来源；旧反馈按原来源导入，不虚构质量信号。
dry-run -> SQLite 在线备份 -> 导入事务 -> 核对数量/状态/来源/检索 -> 切换入口。旧快照重导入不能覆盖新纠正。
SQLite backup API 才是完整在线备份；活动 WAL 主文件不能独立随意复制。FTS 可重建，memory.db 不可删除后仅靠索引恢复。
回退保留切换后写入，验证完整导出后才能恢复旧文件入口。导出失败不回滚权威数据。
保留策略允许用户明确删除正文、索引、导出和来源，并说明备份删除周期；“保留历史”不等于永久保留私人会话。

## 持续学习协议
本节替代将“自动保存和整理”视为完整学习闭环的描述。
流程：采集观察 -> 提出解释 -> 查找支持/反对证据 -> 形成有适用边界的理解 -> 按需应用 -> 关联结果 -> 修正或保持未知。
记忆至少区分 observation、claim、hypothesis、preference、procedure；这些是认知角色，不与现有 knowledge/rule 业务分类混用。模型生成的归纳属于 claim/hypothesis，必须携带推导来源。
存储内容、适用条件、来源和证据等级；条件判断 true/false/unknown，未知不等于成立。访问作用域与适用条件分离。
关系支持 supports、contradicts、refines、exception_to、supersedes、derived_from；关联版本、条件和证据。同一内容重复出现不代表独立支持；新时间不自动证明替代。
新信息先分类为补充/细化/冲突/替代/无关，再决定新增或修订。保留历史不等于永不修正当前状态，也不要求每次反馈都增添一条可召回规则。

### 验证与结果归因
使用预期结果、动作引用、检查对象及实际结果连接一次应用；无法证明关联时只记录观察。
模型自评、模型自述引用、工具直接观测、人工偏好是不同证据，不用单一置信分数替代它们。
可复用已有工具做有界验证；危险、昂贵或超出任务授权的动作不由记忆学习自动触发。预算耗尽保留未知，不循环自问自答制造“证据”。

### 检索与宿主
FTS 召回后展开相关有效关系，再逐节点应用权限/作用域/状态过滤。以理解、限制、证据和未决冲突为一个结果单元返回。
预算不足不能只保留结论丢弃限制：返回受限摘要或要求按需展开，明确未知。
Agent 按当前意图和证据选择采用、不采用、进一步验证或澄清；记忆不是强制指令。
宿主需在有关决策前检索并回传可得结果；不能仅靠提供 MCP 工具宣称自动学习已接通。
当前用户明确指令不因旧记忆自动反问；只有未解决且影响决策的信息缺口需要提问。无人答复保留未决，不将沉默视为同意。

### 实现取舍
保留 SQLite/FTS5 与现有模型网关。增加证据与关系语义，不因产品定位引入图数据库或复杂事件溯源框架。
先证明经历能改变后续行为且反证能纠偏，再扩大自动归纳和整理力度。索引、存储正确性与学习有效性分别验收。
