# application 写入口：memory_apply 与派生 attribution

## Goal

补上隐式票缺的后半条腿：让"这条记忆真的被用上了、结果确实好"能落成 `memory_applications` 记录，
从而**驱动权重**。同时保证"确认"这件事不由宿主自封。

依据：`decisions.md` 的 **F5**（A 方案）、**问题 1 能见度层**、**8a 红线**（不靠自觉，要有确定性事实兜着）。

## 背景与已核实事实

- `MemoryApplication`（`memory_models.py:147-162`）字段：`application_id / task_id / memory_id /
  memory_version / action / expected_outcome / actual_outcome / validation_source_ids /
  attribution('confirmed'|'plausible'|'unknown')`。文档字符串已写明
  "`attribution='confirmed'` 是唯一能改变权重、证据或状态的取值"。
- `MemoryStore.record_application`（`memory.py:839`）已有的强制：按 `application_id` 幂等（内容不同则
  `IdempotencyConflict`）、版本必须存在、**`validation_source_ids` 必须是该版本保留来源的子集**
  （否则 `ValueError`）。→ 本任务不重做这些校验，只补入口与派生规则。
- `record_application` 目前**生产零调用者**（只有 `tests/test_memory_effectiveness.py`、
  `tests/test_recall_eval.py` 在调）。
- 实测（F5）：光有 mark 不动权重 —— `3 applied + 2 validated` → `weight 1.0 → 1.0`，
  因为 `_weight_events` 要求同 memory/version 存在 `attribution='confirmed'` 的 application。

## Requirements

1. **新 MCP 工具** `context_forge_memory_apply(memory_id, task_id, action, expected_outcome,
   request_id, version=0, actual_outcome="", validation_source_ids="", application_id="")`
   - `validation_source_ids` 逗号分隔，沿用仓库里 `technologies`/`domains` 的同种参数形状
   - `version=0` 取当前版本（与 `memory_mark` 一致）
   - `application_id` 缺省时**确定性派生**（`uuid5(task_id, memory_id, version)` 形状，沿用
     `finalization._plan` 已有的 uuid5 幂等做法），所以重试是同一条记录而不是第二条
2. **attribution 由服务端派生，不接受调用方声明**（这是本任务的核心纪律）：
   - `confirmed` 需同时满足三件：`actual_outcome` 非空、`validation_source_ids` 非空、
     且同一个 `request_id` 上**已存在 `validated` 阶段的 retrieval 事件**（即先有"被验证"的回报，
     再主张"结果真的好"）
   - 只满足前两件以内 → `plausible`；`actual_outcome` 为空 → `unknown`
   - 工具返回值里**明确报告派生出的 attribution 与没到 confirmed 的原因**，让宿主看得见而不是猜
3. **不新增依赖、不新增配置项**；复用 `record_application` 的全部既有校验。

## Acceptance Criteria

- [x] 缺 `validated` 回报时，即便宿主给了 actual_outcome + 源引用，落库 attribution 也是 `plausible`（不是 confirmed）
- [x] 齐备时落 `confirmed`，且**权重真的移动**：断言 `weight != 1.0`（正面回应 F5 那次"记得到看不见效果"的实测）
- [x] 同一 (task_id, memory_id, version, attribution) 重复调用只留一条 application（幂等派生 id）
- [x] 引用不属于该版本来源 → 沿用 `record_application` 的 `ValueError`，工具把它变成可读错误而不是崩
- [x] 真 stdio e2e（`tests/test_mcp_transport.py` 复用 `_start_server` 助手）：mark→apply→权重变化端到端
- [x] 变异验证：把 `confirmed` 的三重条件拆掉任一件（如去掉"必须有 validated 事件"）→ 对应测试变红
- [x] 全量 `pytest` 绿 —— **288 passed**

## 残余风险（写明，不掩盖）

宿主仍能引用**该记忆自己的来源**来满足"有验证来源"，这不是独立验证，只是把自封 confirmed 变成
"必须事先回报过 validated 且指得出证据身份"。再硬一层需要外部事实源（工具结果自动落库、或人来确认），
那属 F4 的批准通道与第 3 层之后的事；本任务先把"自封"这条路堵掉。

## Out of Scope

- `_weight_events` 的门槛调整（F5 已否 B 方案）
- `signal_counts` 聚合与分桶报表（`10-05-signal-aggregates` / `10-05-vote-report-buckets`）
- F4 的高危批准通道

## Dependencies

- 前置：`10-05-memory-mark-tool`（已完成）—— confirmed 依赖同一 request_id 上的 `validated` 事件
- 被依赖：第 2 层报表（隐式票要有可统计的效果）、第 5 层自动档的"刹车"数据

## 实现记录（2026-09-30）

**落地形状**
- `MemoryStore.record_host_application(...) -> (application, withheld_reason)` 紧接 `record_application`
  放置；`attribution` **在服务端派生**，`withheld_reason` 把"差哪一件"回给调用方。
- 复用 `record_application` 的全部既有校验（版本存在、来源属于该版本、幂等冲突），不重做。
- MCP 工具 `context_forge_memory_apply`：逗号分隔的 `validation_source_ids` 走既有 `_tags()` 助手，
  返回 `attribution` / `withheld_reason` / `weight`。

**实施中发现并修掉的一个设计缺陷（原 PRD 没预见到）**
`application_id` 最初只由 `(task_id, memory_id, version)` 派生，于是**先报后补证据**的正常流程会撞车：
第一次落 `plausible`，宿主补完 `validated` 再报同一 task → 内容不同 → `IdempotencyConflict`。
在 stdio e2e 里直接复现（apply 返回的是错误文本，断言以 `TypeError` 炸出）。
修法：把 `attribution` 纳入 id 种子——弱记录留在历史里，强记录另成一行；权重只看"是否存在 confirmed 行"
（`_weight_events` 的写法），所以旧行不会叠算。新增测试
`test_a_later_better_report_adds_evidence_instead_of_colliding` 钉住这条路径。

**我犯的一个错**：测试 fixture 里写了 `memory.close()`，而 `MemoryStore` **没有这个方法**（它按操作开关连接）——
是臆造 API，不是项目事实，已删除该 teardown。

**证据**
- 新增 `tests/test_memory_apply.py` 12 例：三条降级路径、齐备时 `confirmed` 且**权重真的移动**（正面回应 F5
  的实测缺口）、同键重复只留一行、引用他版本来源被拒、四个必填字段、未知记忆 `KeyError`、补证据不撞车。
- 扩展 `tests/test_mcp_transport.py`：未经 validated 的请求只能拿到 `plausible` 且原因可读；
  mark `applied`+`validated` 之后 apply 拿到 `confirmed`，且 weight 与 mark 时不同。
- 全量 `pytest`：**288 passed**。
- 变异验证三处：改成"宿主可自封 confirmed" → `4 failed`；去掉"必须有 validated 事件" → `2 failed`；
  id 退回不含 attribution → `1 failed`（即上面那次撞车）；恢复后 `12 passed`。

**残余风险不变**：宿主仍可引用该记忆自身的来源来满足"有证据"，本任务只是把"自封"降级成"必须事先回报过
validated 且指得出证据身份"。独立验证需要外部事实源（工具结果自动落库或人来确认），属 F4 通道之后的事。
