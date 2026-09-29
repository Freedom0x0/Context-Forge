# 沉淀洪泛治理：Episode、会话终结与记忆巩固

> 本文取代 `prd.md` 的候选方向以及本文旧版的“每个静默切片直接沉淀”方案。
> 目标不是少写几篇 Markdown，而是让一次经历先完整保留，再在明确边界上形成少量、可追溯、可修正的长期理解。

## 1. 结论

本期采用两层记忆：

1. **Episode / Checkpoint**：保留一次会话的经历、阶段摘要与处理水位；允许重复尝试，不进入默认长期召回。
2. **Semantic / Procedural Memory**：会话明确结束后，从完整 Episode 一次性巩固出的长期理解或处理程序；进入正式召回。

`settle_seconds=60` 只表示 transcript 暂时稳定、可以读取完整行，**不再触发长期记忆提炼**。长期巩固只由 `SessionEnd`、用户显式 `finalize`，或满足严格条件的异常结束兜底触发。

本期不实现 `workflow_completed`，不监听 Trellis、GitHub、CI 或其他第三方工作流。

## 2. 已证实的根因

### 2.1 静默不是认知阶段结束

当前 watcher 在文件静默 60 秒且出现新增完整行时调用 `process_session`。复杂任务中的思考、测试、工具等待和临时离开都会产生 60 秒静默，因此一个两小时会话会被拆成几十次模型提炼。

实测会话 `7cb7ab68-3030-4455-83a0-a7b74447ff4d` 在约 70 分钟内沉淀 32 次，形成 32 份 knowledge 和 32 份 rule，其中 31 份 knowledge 表达同一教训。

### 2.2 当前身份描述来源，不描述理解

当前 knowledge ID 使用 `(session_id, 模型自由命名的 item.id)`，rule ID 使用 `(session_id, instruction)`。换会话、换标签或改写措辞都会创建新身份。

### 2.3 Vault 与 DB 双写缺少共同提交点

Worker 先写 Markdown，再逐条写 `memory.db`，最后推进 `lines_processed`。任一步失败都可能形成半提交；重试会重新调用模型，可能生成另一组 ID 和正文。

### 2.4 相似不是等价

现有 `MemoryMerge` 能安全自动处理的只有“规范化正文完全相同且完整决策边界相同”。token 相似度、共同技术标签或模型判断只能产生关系候选，不能证明两条长期记忆应共享身份。

## 3. 不变量

1. 自动采集、巩固和有依据的修正是主流程；人工阅读、修改、抑制和删除是可选旁路。
2. `memory.db` 是长期记忆权威数据源；Markdown 是可读投影与显式纠正入口。
3. 一次静默、一次回复或一次工具成功都不等于会话结束。
4. Episode 可以重复；默认召回面中的长期语义/程序记忆应收敛。
5. 只有可独立召回、独立验证、独立修正的断言才成为独立长期记忆。
6. 相似只产生候选、关系或呈现分组，永不自动证明等价、冲突或取代。
7. 来源 project/session/path 始终保留在 `Provenance`；project 不恢复为默认硬过滤。
8. 自动任务不得覆盖 `user_owned`、恢复 suppressed/disabled、扩大 access、永久删除历史，或在无工具证据时升为 verified。
9. 模型曝光、共同召回和 `tool_returned` 共现不产生强化。
10. 重试必须复用第一次持久化的巩固结果，不得重新抽取另一组写入计划。

## 4. 领域模型

### 4.1 Episode

Episode 是一次 session 的可追加经历容器，不是长期判断：

```python
class Episode:
    session_id: str
    project: str | None
    transcript_path: str
    collected_through_line: int
    transcript_digest: str
    state: Literal['open', 'finalizing', 'finalized', 'reopened']
    started_at: str
    last_activity_at: str
    end_kind: Literal['explicit', 'manual', 'inferred'] | None
```

采集只追加已换行结束的 transcript 内容，或保存其不可变引用与摘要。`collected_through_line` 是采集水位，不表示这些内容已经形成长期记忆。

### 4.2 Checkpoint

Checkpoint 用于跨上下文和崩溃恢复：

```python
class EpisodeCheckpoint:
    checkpoint_id: str
    session_id: str
    start_line: int
    end_line: int
    input_digest: str
    problem: str
    attempted: tuple[str, ...]
    verified: tuple[str, ...]
    rejected: tuple[str, ...]
    unresolved: tuple[str, ...]
    created_at: str
```

Checkpoint 可由 `PreCompact` 或输入超过安全上下文上限触发。它不写 knowledge/rule，不进入默认召回，也不提升证据等级。相同 `input_digest` 的 checkpoint 幂等。

### 4.3 长期记忆的粒度

长期层沿用 `MemoryDraft`：

- `role=observation` 表示可独立召回的具体观察；
- `role=claim/hypothesis` 表示语义理解；
- `role=procedure` 表示可采用的处理程序；
- 反例默认进入 `applicability`、`scope.constraints` 或 `scope.exclusions`；
- 只有反例本身可独立采用或独立修正时，才创建新记忆并用 `exception_to` / `conflicts_with` 关联。

问题机制和处理程序也不固定拆成两条。若机制只是程序的解释，保存在 summary/explanation；只有二者可以独立召回、验证或被不同证据修正时，才分别建为 claim 与 procedure。

### 4.4 稳定实体 ID 与内容指纹分离

`memory_id` 是稳定实体标识，创建后不因正文或适用范围修订而改变。新实体使用随机/单调 UUID 或等价的不可变 ID，不再把 session、模型标签或可变正文编码进 ID。

另存可重算的精确指纹：

```python
canonical_fingerprint = sha256(canonical_json({
    'kind': kind,
    'role': role,
    'body': normalize(body),
    'applicability': canonical_applicability,
    'scope': canonical_scope,
    'space': canonical_space,
    'access': canonical_access,
}))
```

`normalize(body)` 仅做 NFC、折叠连续空白和去除首尾空白，不折叠大小写、不删除标点、不做语义改写。`Provenance`、sources、evidence、confidence、状态和模型元数据不进入指纹，因为它们应追加或版本化。

指纹仅用于发现“当前版本完全相同的断言与完整决策边界”，不是实体主键。边界被新证据收窄时，保留 `memory_id` 并创建新版本，同时重算该版本指纹。

### 4.5 关系是记忆图，不是主题图

长期记忆保持独立记录，用现有 `memory_relations` 形成版本化轻量记忆图：

| 关系 | 含义 | 相似度可否自动确立 |
|---|---|---|
| `derived_from` | 一条长期记忆由哪些其他记忆抽象而来 | 否，必须有来源 |
| `supports` | 一条独立证据支持另一断言 | 否，必须有证据 |
| `refines` | 新记忆收窄或细化旧记忆 | 否 |
| `exception_to` | 新记忆是旧规则的可独立采用例外 | 否 |
| `conflicts_with` / `contradicts` | 两条断言在相同条件下冲突 | 否 |
| `overlaps` | 有部分内容重叠，但未证明等价 | 只可作为可撤销候选 |
| `duplicate_of` | 正文与完整边界精确相同 | 仅确定性判据可自动建立 |
| `supersedes` | 新记忆明确取代旧结论 | 否 |

共享 `technologies=[antd]` 只表示主题/适用技术相同，用于检索和排序，不自动连边。例如 Select 弹层定位与 Form `preserve` 都属于 antd，但彼此无语义关系；Modal 内 Select 的具体处理若收窄通用 Select 规则，才可建立 `refines`。

Episode 不是 `memory_relations` 节点。长期记忆通过 `sources` 和 `Provenance.sessions` 指回 Episode；
`derived_from` 只连接两个版本化 `MemoryRecord`，避免把运行日志伪装成知识节点。

## 5. 结束与触发状态机

### 5.1 明确结束

第一版支持：

1. 宿主 `SessionEnd` 适配器写入结构化结束事件；
2. 用户执行 `forge session finalize [session_id]`；
3. 异常结束兜底：客户端进程已不存在、transcript 连续稳定至少 30 分钟、没有待执行工具或活跃锁。

第三种记录 `end_kind='inferred'`，不得伪装成明确结束。若之后同一 transcript 恢复追加，创建新的 episode 段或把状态改为 `reopened`，不能覆盖已完成的 finalization receipt。

### 5.2 不属于结束的信号

- 静默 60 秒；
- 一次 assistant 回复结束；
- 测试通过或命令退出码为 0；
- transcript 暂时无变化；
- `PreCompact`；
- 模型声称“任务完成”。

### 5.3 第三方工作流

本期不实现 `workflow_completed`，不读取 `.trellis/` 或其他插件状态。未来若需要，由第三方通过通用事件协议主动通知；Context Forge 核心不依赖特定工作流产品。

## 6. 写入路径

### 6.1 会话进行中

```text
watcher.scan_once
  ├ baseline / 完整行 / settle 检查
  ├ 读取新增完整行
  ├ 追加或引用到 open Episode
  └ 更新 collected_through_line
```

这一阶段不调用最终巩固模型，不写长期 `MemoryDraft`，不生成 knowledge/rule Markdown。

### 6.2 最终巩固

```text
finalize(session_id, finalization_id)
  ├ 冻结 Episode 输入范围与 digest
  ├ 创建 FinalizationReceipt(status='extracting')
  ├ 读取 checkpoints + 尚未覆盖的 transcript
  ├ 模型一次输出 0..K 个“彼此可独立采用”的候选
  ├ 校验候选、边界、来源与候选间关系
  ├ 持久化 extraction_json 与 write_plan
  ├ 对每个候选执行确定性匹配或创建
  ├ memory DB 内以单操作事务写版本、来源和关系
  ├ queue DB 记录每个 operation 结果并核验全部完成
  ├ receipt 状态推进为 committed
  └ 事务提交后刷新 Markdown 投影
```

`K` 是单次巩固的写放大安全上限，不是质量指标。第一版默认 5。模型必须同时返回 `deferred_topics`；
若候选超过 K，校验失败并重试一次要求合并粒度，而不是任意截取前 K 条。仍无法满足时 finalization 失败，
完整材料继续保留在 Episode，不能靠丢弃候选换取成功。

### 6.3 模型输出动作

模型输出候选内容与有依据的关系建议，但不能指定“复用哪个长期 ID”。发布器执行：

1. **精确命中**：`canonical_fingerprint` 相同，且 kind/完整决策边界相同 → 保留旧正文和 ID，追加去重后的 provenance/sources；必要时创建新版本。
2. **相关但不等价**：创建新实体；关系经结构校验后记录 `refines`、`exception_to`、`supports` 或 `overlaps`。关系目标必须来自本次检索候选，模型不得凭空指定 ID。
3. **不确定或无命中**：创建新实体；不为追求去重而误合。

相似度、FTS 命中和模型选择均不能进入第 1 条。

### 6.4 FinalizationReceipt 与幂等

```python
class FinalizationReceipt:
    finalization_id: str
    session_id: str
    episode_digest: str
    end_kind: str
    extraction_json: str | None
    write_plan_json: str | None
    status: Literal['extracting', 'planned', 'committed', 'projected', 'failed']
    error: str | None
```

- `(session_id, episode_digest)` 唯一，两个 worker 只能有一个获得 finalization 所有权；
- 模型成功返回后先持久化 `extraction_json`；
- `planned` 之后的重试只重放 write plan，不再调用模型；
- 每个 memory operation 在 `memory.db` 内事务一致；receipt 位于 `queue.db`，通过 §12.3 的可恢复提交
  协议推进为 `committed`，不宣称两个 SQLite 文件具有原子事务；
- Markdown 投影失败不回滚权威 DB，receipt 保持 `committed` 并可重试到 `projected`；
- 相同 `finalization_id` 携带不同 digest 时返回 `IdempotencyConflict`。

### 6.5 来源身份

来源 identity 必须稳定且不会在同一 session 的不同片段间冲突：

```text
episode:{session_id}:{episode_digest}
checkpoint:{session_id}:{input_digest}
evidence:{session_id}:{episode_digest}:{ordinal}:{excerpt_digest}
```

不得继续用会随每轮标题变化的单一 `session:{session_id}` 映射不同 source 内容。

### 6.6 Markdown 投影与人工纠正

DB 提交后按稳定 `memory_id` 渲染 Markdown，frontmatter 带 `memory_id` 与 `version`。文件名可以是可读 slug，但不参与身份；slug 冲突时附加 ID 短后缀。

人工编辑作为显式 correction 导入：校验 expected version → 以 human actor 写 DB 新版本 → 重新投影。后台投影不得覆盖未导入的 `user_owned` 编辑；检测到差异时产生明确冲突记录，而不是静默覆盖。

## 7. 召回路径

### 7.1 默认召回面

默认只召回长期 semantic/procedural memory，不召回原始 Episode 和 Checkpoint。用户显式要求审计来源或展开 `derived_from` 时，才读取对应 Episode 摘要/引用。

### 7.2 检索与边界

沿用现有 Gen2 契约：

1. FTS 获取文本候选；
2. owner/privacy/`AccessPolicy`/明确命中的 exclusion 做硬过滤；
3. principle 直接准入，pattern 需 technologies/domains/constraint 命中，instance 需 provenance project/path 命中；
4. 按适用性、来源弱加分、证据、时效、可归因反馈和文本相关度排序；
5. 冲突关系只产生风险提示和惩罚，不隐藏任一结论。

Project 继续只作 provenance、instance 准入和弱排序；显式 `AccessPolicy` 才是项目访问边界。

### 7.3 关系展开

对最终 seed 只展开一跳、且每个节点重新做访问边界检查：

- `derived_from` / `supports` / `refines`：作为解释上下文；
- `exception_to` / `conflicts_with` / `contradicts`：单独进入风险与分歧区；
- `duplicate_of` / `supersedes`：用于版本和审计，不扩大候选；
- 共现不产生新关系或排序权重。

### 7.4 防御性同族折叠

理想状态是一条稳定长期理解对应多份 Episode 证据。若历史、迁移或漏判仍留下多条改写级长期记忆，这是待治理的重复，不是正常写入策略。

召回层可以用保守 token-Jaccard 生成**临时呈现族**，但：

- 不修改 DB，不自动建立 `duplicate_of`；
- 族大小、成员 ID、边界差异必须可见并可展开；
- 只折叠边界兼容的候选；不确定时分开显示；
- 必须先取得有界但未做最终 top-K/byte-budget 的候选，完成分族后再应用预算；
- 该能力是防御网，验收仍监测长期层重复率，不能用折叠掩盖写入缺陷。

## 8. 示例：两小时权限缓存任务

10:00 到 12:00 的会话中，用户先怀疑后端缓存，随后排除该假设，最终确认客户端存在多份权限派生缓存，并通过统一失效信号修复。期间即使出现 30 次超过 60 秒的停顿，也只采集 Episode；若 11:00 发生 `PreCompact`，只生成 checkpoint。

12:00 收到 `SessionEnd` 后执行一次 finalization。合理产出可能是：

1. **procedure**：当前用户修改自身角色后，应统一失效所有客户端权限派生缓存。
2. 可选 **claim**：多份权限派生缓存会使自角色变更后的旧 admin 判断继续存活。

若 claim 只是 procedure 的解释，不需要独立召回，则只保留 procedure，并把机制写入 summary。二者确需独立存在时，procedure 可用 `derived_from` 指向 claim；若要使用 `supports`，还必须为关系附上本版本持有的独立 source evidence。

“权限完全由服务端逐请求计算、客户端不缓存”默认进入 procedure 的 exclusion；只有它形成一条可独立采用的替代策略时，才创建新 procedure 并用 `exception_to` 连接。

以后另一个项目出现“组织角色修改后菜单仍显示管理员”时，召回根据 authorization/cache-invalidation、前端技术栈和文本相关度命中 procedure，同时展示来源、限制与例外。若旧数据中仍有四条改写，默认显示一个临时代表和 `family_size=4`，但允许展开，并把重复列入卫生报告。

## 9. 失败矩阵

| 失败点 | 行为 |
|---|---|
| transcript 暂时不可读 | 不推进采集水位，下轮重试 |
| checkpoint 模型失败 | Episode 保持 open；不影响后续最终巩固 |
| finalization 模型失败 | receipt=`failed`，保留固定 episode digest；重试沿用同一 finalization ID |
| 模型成功后进程退出 | 从已持久化 extraction/write plan 重放，不再次调用模型 |
| 两 worker 同时 finalize | 唯一约束只允许一个 owner，另一个读取既有 receipt |
| 精确复用撞 VersionConflict | 重读并重算一次；仍冲突则失败重试，不降级成另一条重复记忆 |
| 关系建议目标不在候选集 | 丢弃关系并留审计；记忆本体仍可新建 |
| 目标 protected | 不修改目标；新建候选或记录失败，绝不恢复/覆盖目标 |
| Markdown 投影失败 | DB 保持 committed；记录投影失败并幂等重试 |
| inferred end 后 transcript 恢复 | 新建 episode 段或标记 reopened，不改写已提交 receipt |
| 同族阈值判断不确定 | 分开呈现，不隐藏内容 |

## 10. 数据迁移

1. 新增 Episode、Checkpoint、FinalizationReceipt 与版本指纹存储；schema 迁移必须显式、先备份、支持 dry-run 和回滚。
2. 存量 memory ID 不重写，避免打断 relations、applications、imports 和 retrieval events。
3. 为存量当前版本计算 `canonical_fingerprint`；只有精确正文和完整边界相同者才能自动建立可撤销的 `duplicate_of` / 归档操作。
4. 相似存量只生成卫生报告或临时呈现族，不自动归档。
5. 存量 vault 回填 `memory_id` 和 `version`，默认 dry-run，显式 `--apply` 后落盘。
6. 上线前保持当前 watcher 停用；迁移和回放通过后再显式恢复。

## 11. 测试与验收

| 契约 | 必须通过的测试 | 关键变异（应 RED） |
|---|---|---|
| 静默只采集 | 两小时模拟会话含 30 次静默，结束前长期记忆为 0 | 恢复静默即 publish |
| 明确结束 | 一个 SessionEnd 只产生一个 finalization receipt | 同事件重复生成 receipt |
| PreCompact | 只生成 checkpoint，不进入长期召回 | checkpoint 被 memory search 返回 |
| 异常兜底 | 仅在进程消失 + 30 分钟稳定 + 无活跃工具时 inferred finalize | 只靠 mtime 即结束 |
| 输出粒度 | 机制仅作解释时不强拆；可独立验证时允许拆分并关联 | 固定每会话生成 knowledge+rule |
| 精确复用 | 同正文、同完整边界追加来源，不新增实体 | 仅 token 相似即复用 |
| 边界保护 | exclusions/access 任一不同不得精确复用 | 去掉 access 后仍复用 |
| 稳定 ID | scope 收窄后 ID 不变、版本递增、指纹变化 | 用内容哈希替换实体 ID |
| 关系语义 | 同为 antd 但无语义依赖的两条不连边 | 共享 technology 自动连边 |
| 重试幂等 | 模型返回后任一点崩溃，重试不再次调用模型且无重复版本 | 删除 extraction receipt |
| 并发 | 两 worker finalize 同一 digest，只有一个提交 | 删除唯一约束 |
| 权威顺序 | DB 成功、投影失败后召回可见且投影可重试 | 先写 Markdown 再写 DB |
| 召回预算 | 历史重复先临时分族再占 top-K/byte budget | 先截断再分族 |
| 不强化曝光 | retrieved/tool_returned/presented 不改变权重与证据 | 共现次数进入排序 |
| 可审计 | 每次复用、关系丢弃、inferred end、投影失败均有事件 | 静默吞掉判定 |

回放真实 64 条洪泛语料时同时报告：长期实体重复率、误复用数、关系误连数、召回覆盖、错误迁移率、模型调用次数和 token 成本。**误复用必须为 0**；不得以 vault 篇数下降作为成功标准。

## 12. 上线所需工程契约

### 12.1 通用会话事件协议

核心只消费通用本地事件，不识别第三方工作流目录：

```json
{
  "event_id": "host-stable-event-id",
  "type": "session_end|pre_compact",
  "source": "claude-code|manual|host-adapter",
  "session_id": "stable-session-id",
  "transcript_path": "absolute-path",
  "observed_at": "RFC3339",
  "final_line_count": 1234,
  "reason": "explicit|manual|context_compaction"
}
```

事件通过 `forge event ingest`（stdin JSON）或等价的本地函数入口写入 queue DB。`event_id` 唯一；同 ID、
同 payload 返回原结果，同 ID、不同 payload 返回 `IdempotencyConflict`。Hook 只投递事件，不调用模型。

安装器必须提供 install/uninstall/status/self-test，并由 `forge doctor` 检查：hook 是否存在、事件入口能否写入、
最近一次消费错误、未处理事件数。宿主不支持 SessionEnd 时明确降级为人工 finalize，不得显示“自动沉淀正常”。

### 12.2 Episode 存储、隐私与删除

Episode/Checkpoint/Receipt 存入 `queue.db` 的独立表；长期记忆继续存入 `memory.db`。Episode 默认不复制完整
原始 transcript，只保存稳定路径、行范围、输入 digest 和经过现有 `sanitize_text` 的 checkpoint/finalization
输入快照。这样能够重放已冻结输入，又不无界复制宿主日志。

- 默认保留已 finalized Episode 30 天，可配置为 0（提交后立即清除快照）至 365 天；open/failed receipt 不受
  自动清理，直到成功、显式放弃或用户删除；
- 凭据、token、私有 URL 在持久化 checkpoint/finalization 输入前再次脱敏；脱敏失败则拒绝持久化和提炼；
- `forge session inspect/list/delete` 提供审计和删除；删除 Episode 不删除长期记忆，而是把相关 source 标记为
  `source_deleted` 并保留最小审计事实（source identity、digest、删除时间），不得伪造仍可读取的证据；
- `forge memory forget` 负责长期记忆的显式遗忘，沿用版本化、可审计策略；
- 清理任务按 receipt 状态和 retention deadline 运行，输出删除计数与失败记录。

### 12.3 持久化 Schema

迁移至少新增：

```text
session_episodes(session_id, segment, project, transcript_path,
                 collected_through_line, transcript_digest, state,
                 started_at, last_activity_at, end_kind, retention_deadline,
                 PRIMARY KEY(session_id, segment))

episode_checkpoints(checkpoint_id, session_id, segment, start_line, end_line,
                    input_digest UNIQUE, snapshot_json, created_at)

session_events(event_id PRIMARY KEY, request_hash, type, source, session_id,
               payload_json, status, error, created_at, consumed_at)

finalization_receipts(finalization_id PRIMARY KEY, session_id, segment,
                      episode_digest, end_kind, extraction_json, write_plan_json,
                      status, error, created_at, updated_at,
                      UNIQUE(session_id, segment, episode_digest))

finalization_operations(operation_id PRIMARY KEY, finalization_id, ordinal,
                        kind, target_memory_id, expected_version, request_hash,
                        status, result_version, error,
                        UNIQUE(finalization_id, ordinal))

projection_conflicts(conflict_id PRIMARY KEY, memory_id, db_version,
                     projected_hash, edited_hash, path, status,
                     created_at, resolved_at)
```

状态转换使用条件更新（compare-and-set），不得仅在进程内加锁。queue DB 的 episode/receipt 与 memory DB 无法共享
SQLite 事务，因此正式提交顺序为：冻结 write plan → 每个 operation 以稳定幂等键写 memory DB → 记录 operation
结果 → 全部可验证后标记 receipt committed。崩溃恢复逐项核对 request hash 与结果版本，不通过扫描内容猜测。

### 12.4 模型输入输出 Schema

Finalizer 的结构化输出固定为：

```json
{
  "candidates": [{
    "local_id": "c1",
    "kind": "knowledge|rule",
    "role": "observation|claim|hypothesis|preference|procedure",
    "summary": "...",
    "body": "...",
    "technologies": [],
    "domains": [],
    "constraints": [],
    "exclusions": [],
    "applicability": [{"condition": "...", "result": "true|false|unknown", "evidence_refs": []}],
    "evidence_refs": ["checkpoint-or-final-input-evidence-id"],
    "related_candidate_ids": [{"local_id": "c2", "kind": "derived_from|supports|refines|exception_to|conflicts_with|overlaps"}],
    "related_existing_ids": [{"memory_id": "candidate-provided-by-system", "kind": "supports|refines|exception_to|conflicts_with|overlaps"}]
  }],
  "deferred_topics": [{"topic": "...", "reason": "..."}],
  "no_memory_reason": null
}
```

所有 evidence ref 必须来自冻结输入；所有 existing ID 必须来自系统给出的有界候选集；关系两端必须存在且
满足 kind 约束。事实性 observation 没有 evidence 时拒绝；其他无充分证据的判断保持 hypothesis/unknown，不能
自动升级。`candidates=[]` 时必须给 `no_memory_reason`。超过 K 时按 §6.2 处理。

### 12.5 Markdown 投影与纠正冲突

投影 frontmatter 至少包含 `memory_id`、`version`、`projection_hash`、`user_owned`。刷新前比较磁盘正文 hash：

- 与上次 `projection_hash` 相同：可安全覆盖为 DB 新版本；
- 不同：创建 `projection_conflicts`，保留磁盘文件，不覆盖；
- `user_owned=true`：始终进入显式 correction 流程。

`forge memory import-correction <path>` 显示 DB 当前版本与人工编辑 diff；用户确认后以 `actor=human`、
expected version 写入新版本并重新投影。`forge memory resolve-conflict --keep-db` 可放弃文件编辑并重新投影。
未解决冲突必须在 `forge doctor/status` 可见。

### 12.6 召回接口拆分

召回实现拆为明确阶段：

```text
search_candidates(query, context, candidate_limit)
  → boundary_filter_and_admit
  → score_candidates
  → group_for_presentation
  → choose_representatives
  → apply_top_k_and_byte_budget
  → expand_relations_and_render
```

默认 `candidate_limit=100`，达到上限时返回 `candidate_truncated=true` 并记录指标。分族只在 kind 相同、
AccessPolicy/space 相同且 scope/applicability 无冲突时进行；使用确定性、非传递的代表星型分组，按分数降序、
ID 破平后依次将成员与代表比较，避免 A≈B、B≈C 推导 A≈C。一个成员只属于一个呈现族。中文使用与 FTS
一致的 tokenizer。最终排序、族代表、族成员、truncated 标记必须在相同快照下稳定。

### 12.7 inferred finalize 状态机

异常兜底不直接等同明确结束：

```text
open → inferred_pending → finalizing → finalized
         ↓ transcript 恢复
       reopened/open
```

满足进程消失、无活跃工具、稳定 30 分钟后进入 `inferred_pending`；再稳定一个 30 分钟宽限期才自动 finalize。
两个时间均可配置。恢复追加立即取消 pending。若 inferred finalization 已提交后恢复，则创建新 segment；后续明确
SessionEnd 只 finalize 新 segment，并可对同一 session 的长期候选建立来源关联，不重复处理旧 digest。

## 13. 迁移、发布与回滚

本版本分五个可独立回退的阶段：

1. **事件与 Episode 底座**：部署 schema、事件入口、hook 健康检查、脱敏与 retention；旧写入仍关闭。
2. **Finalization 闭环**：部署模型 schema、receipt/operation log、精确复用和崩溃恢复；仅回放模式。
3. **DB 权威与 Markdown 投影**：启用新写入，永久关闭旧 vault-first 与轮后重复 import 路径；验证 correction 冲突。
4. **召回改造**：启用候选池、关系呈现和防御性分族；功能开关可退回旧 `search_scored`。
5. **存量治理与恢复 watcher**：备份、dry-run、回放 64 条语料、显式 apply，满足上线闸门后恢复自动采集。

每阶段记录 migration batch ID、输入快照、变更清单与逆操作。存量 ID 不重写；精确重复归档必须建立版本固定
的 `duplicate_of`，旧 retrieval/application 引用继续有效。相似存量不自动归档。

## 14. 上线闸门

以下全部满足才能宣称可推广上线：

1. 全量测试通过，且两小时/30 次静默回放在结束前不产生长期记忆；
2. SessionEnd 丢失、重复、乱序，进程崩溃和两个 worker 并发均无重复实体或版本；
3. 模型成功后所有注入故障的恢复路径不再次调用模型；
4. 真实 64 条洪泛语料回放的误复用数、错误关系自动确立数均为 0；
5. AccessPolicy、user_owned、suppressed、disabled 和 verified 保护变异测试全部 RED；
6. DB committed + Markdown 投影失败可召回、可诊断、可恢复；人工编辑从不被静默覆盖；
7. Episode 脱敏、retention、删除和 source_deleted 行为通过隐私测试；
8. 候选截断率、长期层疑似重复率、召回覆盖、错误迁移率、模型调用次数、token 成本有基线和告警阈值；
9. 迁移 dry-run、备份恢复和整批回滚在生产副本上演练成功；
10. `forge doctor` 能明确报告 hook、事件积压、failed receipt、投影冲突、同步错误和数据库版本。

上线采用显式开关，默认顺序为采集 → finalization → 投影 → 新召回；任一阶段可单独关闭。旧的“静默即长期
发布”路径不得作为回滚选项，因为它会恢复已证实的洪泛缺陷。

## 15. 本期范围与后续项

### 本期

- Episode 增量采集；
- PreCompact checkpoint；
- SessionEnd / 人工 finalize / 严格异常兜底；
- 通用事件协议、hook 安装自检与积压诊断；
- Episode 脱敏、retention、审计与删除；
- FinalizationReceipt 与可重放 write plan；
- 逐 operation 幂等、并发所有权与崩溃恢复；
- 完整 finalizer JSON schema 与证据引用校验；
- 稳定实体 ID + 版本指纹；
- 精确复用、来源追加和显式关系；
- DB 权威、Markdown 投影、人工 correction 与冲突解决；
- 分阶段召回 API、关系展示与防御性同族折叠；
- 存量治理批次、备份、dry-run、回滚与上线闸门。

### 本期不做

- `workflow_completed` 及 Trellis/第三方工作流监听；
- 向量数据库或实体知识图谱；
- 依据模型相似判断自动合并；
- 用会话记忆数量评价学习质量；
- 自动执行记忆中的危险动作。

### 配置默认值（上线前用回放校准，改变需版本化）

1. checkpoint 输入上限：按 finalizer 模型上下文的 60% 触发，最低 32 KiB；
2. 单次巩固安全上限 K：5；
3. 防御性呈现族阈值 T：0.90；
4. inferred pending：30 分钟；额外宽限期：30 分钟；
5. 未做最终 top-K/byte-budget 的候选池上限：100；
6. finalized Episode 默认保留：30 天。
