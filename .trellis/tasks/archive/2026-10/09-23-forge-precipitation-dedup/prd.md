# 沉淀洪泛治理：内容等价、触发频率与重试幂等

## Goal

同一教训被反复沉淀为改写版：一个持续会话在 70 分钟内被沉淀 32 次，31 条知识属同一条教训。
治理内容等价检查、触发频率闸门与失败重试幂等，使"新证据能收窄理解"而不是把它放大。

## 实测证据（2026-09-23）

**现象**。会话 `7cb7ab68-3030-4455-83a0-a7b74447ff4d`（project `platform-web-main`）在
11:30–12:40 约 70 分钟内被沉淀 **32 次**，产出 **64 个真正新写的文件** = 32 knowledge
+ 32 rule（一次沉淀恰好一份 knowledge + 一份 rule，见 `SessionArtifacts` 的单数字段）。
其中 **32 条 knowledge 里有 31 条属同一条教训**——"用户改自己角色时，所有已缓存的
admin/权限信号必须失效重取"——只有 `platform-web-dev-prod-proxy-asymmetry` 是别的。

```
self-role-change-guard            self-role-change-permission-refresh
self-role-change-refresh-and-guard   admin-self-role-change-refresh-and-guard
authz-self-mutation-refresh-and-guard   cached-admin-check-refresh-keyed-on-self-identity
kb-self-role-auth-refresh   k-self-role-refresh   frontend-admin-recheck-trigger-narrowing
unified-self-permission-refresh   unify-auth-refresh-signal-across-predicates   ... （共 31）
```

**口径警告**（本任务的第一版测量错过一次，务必沿用正确口径）：区分"真写"与"重写"必须用
**创建时间 vs 修改时间**，不能只看 mtime。该 vault 308 篇里 **240 篇是重写**（创建时间远早于
修改时间，迁移/retag 批量改 frontmatter 所致），只有 68 篇是真写。按 mtime 统计会把 240 篇
迁移产物算成沉淀产物。

**两道"以为存在"的闸门，实际都不控频率**：

1. `DEFAULT_SETTLE = 60.0` 的语义是「**这个文件已静默 60 秒**」（防读到半行；`count_complete_lines`
   只数换行结尾的行，同一目的），判据为 `current - transcript.stat().st_mtime < settle_seconds: continue`。
   **它不是「等会话结束」**。持续数小时的会话只要反复静默 ≥60 秒，就被反复沉淀。
2. `EventType.SESSION_END`（`domain.py:15`）用于**反馈归因**，不是闸门：
   `worker.py:169` 在 event_type 为 SESSION_END 时调 `auto_record_feedback_for_session`
   （→ `queue.py:252` "auto: session ended without explicit feedback"）。且 SessionEnd hook
   **故意不装**：`install_hook.py` 注释 "Precipitation is the watcher's job, so SessionEnd /
   PreCompact hooks would be dead weight"，`HOOK_EVENTS = ()`。

**于是没有任何「该会话已沉淀过」的闩**。`watcher.scan_once` 的判据只有三条：mtime ≥ baseline、
静默 ≥ settle、`count_complete_lines > get_lines_processed`。满足即再沉淀一次。

**后半段把重复固化成新文件**：`id` 由模型**每轮重新起名**（措辞变了 id 就不同），而 vault
对已有内容**没有任何等价检查**。同 id 覆盖有 `user_owned` 保护，改写 id 的全部存活。

**一个独立放大器**（机制在代码中成立，本次未隔离其贡献量）：`worker.py:139-145` 的失败与限流
路径**不推进偏移量**（`ratelimited` 的注释明说"下一轮重试同一切片"）。若异常发生在
`_write_session_artifacts` 之后（例如 `publish_session` 抛错），**vault 文件已经写了**，下一轮
同一切片再写一遍、再起一个新 id。`last_run.md` 曾记录 `1 failed`，说明该路径被走到过。

**后果**。这 31 条全部进入 FTS 检索面 → 召回被同义文档淹没，token 成本随之上升。更关键的是它
触发父任务自己写的失败模式：父 PRD `R12`「不以记忆数量或候选数量为验收目标」，父验收第 4 条
「新证据能够**收窄或修正**理解」——当前系统在**反向**工作。

**为什么既有机制没挡住**：P4 的 `MemoryMerge` 只合并 memory.db 内**内容完全一致**的记录，且
洪泛发生在它**上游**的 vault 写入路径，合并无从触及。

## Requirements

- R1 **经历与长期理解分层**：会话进行中只增量采集 Episode；静默 60 秒仅保证安全读取，不发布长期
  记忆。会话明确结束后，才从完整 Episode 一次性巩固 0～少量可独立采用的长期理解。
- R2 **明确结束触发**：第一版只接受宿主 `SessionEnd`、用户 `forge session finalize`，以及“客户端进程
  消失 + transcript 稳定至少 30 分钟 + 无活跃工具”的异常兜底。`PreCompact` 只生成 checkpoint。
  本期不实现 `workflow_completed`，不监听 Trellis 或其他第三方工作流。
- R3 **重试幂等**：模型输出和写入计划必须先持久化到 FinalizationReceipt；模型成功后的任何重试只
  重放同一计划，不重新提炼。每个 memory operation 在 `memory.db` 内事务一致；跨 `queue.db` receipt
  通过逐操作日志和 request hash 可恢复提交，Markdown 在 committed 后作为投影重试。
- R4 **稳定身份与精确复用**：长期 `memory_id` 是不可变实体 ID，内容/边界指纹另存。只有规范化正文
  和完整决策边界确定性相同，才可复用实体并追加来源；相似或模型判断不得证明等价。
- R5 **独立记忆与显式关系**：只有能独立召回、验证、修正的断言才单独成条。共享技术/主题只作标签；
  `derived_from`、`supports`、`refines`、`exception_to`、冲突等关系必须有明确语义与来源。
- R6 **不误杀**：相似 ≠ 等价。适用条件、access、scope 或 applicability 不同不得精确复用；不确定时
  新建并可记录 `overlaps`，不得为减少数量而错误合并。
- R7 **可审计**：明确/人工/推断结束、checkpoint、精确复用、关系丢弃、写入失败与投影失败均留痕，
  能区分“无长期产出”“没有形成可复用理解”和“处理失败”。
- R8 **存量治理与召回防御**：既有改写级长期重复进入卫生报告；只有精确重复可自动可逆归档。召回层
  可临时折叠边界兼容的疑似同族，但必须显示族大小、差异并可展开，不能掩盖写入缺陷。
- R9 **不以数量为验收**：不得以“vault 篇数下降”作为成功标准（父 `R12`）；同时评估误复用、关系
  误连、召回覆盖、错误迁移、模型调用次数和成本。
- R10 **事件入口可诊断**：提供通用 session 事件 schema、幂等 ingest、SessionEnd/PreCompact hook 的
  install/uninstall/status/self-test；宿主不支持自动结束时必须明确降级，不能伪报正常。
- R11 **Episode 隐私与遗忘**：冻结输入持久化前脱敏；已完成 Episode 默认保留 30 天且可配置、查询、
  删除。删除来源后长期记忆保留最小 `source_deleted` 审计，不得伪造仍可读取的证据。
- R12 **逐操作恢复**：每个 finalization write operation 有稳定 ID、request hash、expected/result version 和
  状态；跨 queue DB / memory DB 崩溃恢复靠操作日志核对，不靠内容扫描猜测。
- R13 **模型契约固定**：finalizer 使用版本化 JSON schema；候选、evidence ref、关系目标、deferred topic
  和零产出原因均可校验。模型不能引用冻结输入和系统候选之外的证据或既有 ID。
- R14 **人工编辑闭环**：Markdown 投影带 version/hash；检测人工修改时不覆盖，形成可诊断冲突，并提供
  import-correction、keep-db 和 expected-version 处理流程。
- R15 **召回阶段化**：候选池、边界与准入、评分、呈现分族、代表选择、最终 top-K/byte budget、关系展开
  分阶段执行；截断显式可见，相同快照下结果确定。
- R16 **迁移与上线门槛**：schema、存量治理和写入切换均支持备份、dry-run、批次审计与回滚；只有技术设计
  §14 的故障、隐私、召回和运维闸门全部通过后才恢复 watcher 并宣称可推广上线。

## 候选方向（**已被 `design.md` 取代，本节仅作讨论历史保留**）

> 2026-09-23：三条方向已由 `design.md` 收敛为「写侧防（内容派生身份 + 写前短查询复用）+ 读侧限
> （族归并）+ 处理水位改切片哈希」。保留本节是为了记住取舍过程与当时各自的理由。
> 下列 B、C 两条的原始描述仍是对应实现的依据，A 的原始描述（"复用 P4 判据"）已被证否——
> 见 Notes 的实测约束第 1 条。

用户要求三条都写下来，不预先删掉任何一条。三者不互斥，可组合。

### A. 沉淀前召回比对

写新文件前，用拟写内容对现有 vault 做相似度检索；过阈则不写，改为对已有条目追加证据。
- **治的是**：结果——不管触发几次，同一课不再产生第二篇。
- **证据支撑**：31 条同义的存在本身就是"缺少此步"的直接后果。
- **代价/风险**：阈值是新的可被调参的面；过松等于没做，过紧会误杀相似但适用条件不同的条目（R4）。
- **如何验证**：造一份含同一教训两次改写的输入，断言只产生一篇且第二次记为追加证据；
  变异：移除等价判据 → 必须 RED。

### B. 触发频率闸门 / 真正的 session-end 触发

承认 `settle=60s` 只是防半行。可选形态：把静默阈值提到有意义的量级（如数分钟）、按会话记录
"已沉淀内容指纹"而非仅行号、或改为真正的会话结束触发。
- **治的是**：源头——一个会话被沉淀 32 次这件事本身。
- **证据支撑**：32 次沉淀的间隔分布 1–3 分钟，即"反复静默 ≥60 秒"被反复当成新会话。
- **代价/风险**：延迟上升（更晚才沉淀）；"会话结束"本身难以判定（CC 不保证发出可用的结束信号，
  这也是 SessionEnd hook 当初不装的原因）。
- **如何验证**：给定一份模拟"反复静默"的 transcript 序列，断言沉淀次数有上限且不随静默次数增长。

### C. 重试幂等

修 `worker.py` 失败/限流路径不推进偏移量的问题：保证"产物已写"与"偏移已推进"不会脱节
（要么同事务，要么以内容哈希而非行号判定"这段已处理过"）。
- **治的是**：放大器——重试本身就是重复写的来源。
- **证据支撑**：`ratelimited` 分支注释明确"偏移故意不推进"；`last_run.md` 有 `1 failed`。
- **代价/风险**：需要想清"部分成功"的语义（vault 写了、memory.db 失败时算不算已处理）。
- **如何验证**：在 publish 阶段注入一次异常，断言重试后 vault 不出现重复文件；
  变异：让偏移量照旧不推进 → 必须 RED。

## Acceptance Criteria

- [x] 含 30 次静默的两小时模拟会话，在结束事件前长期记忆新增数为 0；静默只推进 Episode 采集水位
      —— 2026-10-13 补：`tests/test_precipitation_simulation.py::test_two_hour_thirty_silence_session_writes_nothing_until_it_ends`
      （驱动真 `watcher.scan_once` 30 轮，注入时钟/mtime；变异：去掉静默闸门 → 第 1 轮即出现 receipt → RED）
- [x] `PreCompact` 只生成幂等 checkpoint；`SessionEnd` / 人工 finalize 各只生成一个 finalization receipt
      —— `tests/test_episodes.py::{test_precompact_creates_checkpoint_without_long_term_write, test_manual_finalize_is_idempotent_and_marks_episode, test_repeated_hook_session_end_reuses_first_observed_at}`
- [x] 同一结束事件并发或崩溃重试时，模型至多成功提炼一次，DB 无重复实体/版本，Markdown 可独立重试
      —— `tests/test_finalization.py::{test_finalization_freezes_model_output_and_recovers_without_second_call, test_two_workers_only_one_claims_model_extraction}`；`tests/test_memory_projection.py::test_db_commit_survives_projection_failure_and_retry`
- [x] 完全相同正文与完整边界追加来源；仅措辞相似、access 或 applicability 不同的条目不被误复用（变异 RED）
      —— `tests/test_finalization.py::{test_a_case_variant_lands_on_the_same_memory, test_a_second_project_widens_provenance_instead_of_replacing_it}`；`tests/test_memory_hygiene.py::test_merge_candidates_split_conflicts_from_compatible`
- [x] 机制、程序、反例按“能否独立召回/验证/修正”决定是否拆分，不固定生成 knowledge + rule
      —— finalizer 输出是**候选列表**（0～K 条、`kind` 自由），无强制配对；`output()` 夹具本身即单候选并端到端跑过
      `FinalizationService`（`tests/test_finalization.py`）。此条为结构性保证，无“专用”测试但有活体覆盖。
- [x] 同属 antd 等主题但无语义依赖的记忆不自动连边；有效 refinement/exception 可追溯且版本固定
      —— `tests/test_memory_recall_score.py:194`（`relations("noisy") == []`）、`tests/test_memory_store.py:152`
- [x] 存量 31 条进入卫生报告；只有确定性精确重复可自动可逆归档，其余仅临时分族且可展开
      —— 2026-10-13 补：`tests/test_precipitation_simulation.py::test_flood_corpus_replay_reports_rewrites_and_archives_only_exact_duplicates`
      （64 行语料：子句 A 31 条改写进 `hygiene_report().merges` 作 `overlaps`、绝不进 `exact_duplicates`；
      子句 B `apply_auto_tier()['exact_applied'] == 1`，仅精确孪生归档、31 条改写留 `active`。变异：把近重复强塞进 exact → RED）
- [x] 召回先做防御性分族再应用最终 top-K/byte budget；曝光与共同召回不提高可信度
      —— `tests/test_recall_families.py::{test_family_grouping_is_after_candidates_and_boundary_compatible, test_presentation_grouping_is_anchor_based_not_transitive}`
- [ ] 回放报告误复用、关系误连、召回覆盖、错误迁移、调用次数与成本；不以篇数下降判成功
      —— **2026-10-13 判定：转到父任务 `09-21-forge-recall-rank-and-merge`**。父任务验收第 10 条即"按研究协议
      报告收益、错误率、延迟和 token 成本"，且父任务因模型行为层验收未过本就不归档。本任务不重复承载该交付。
- [x] event ingest 对重复/冲突 ID 幂等正确；hook 安装、自检、丢失/乱序事件和显式降级均可诊断
      —— `tests/test_episodes.py::{test_lifecycle_event_is_request_idempotent, test_hook_self_test_releases_temporary_database}`
- [x] Episode 持久化前完成脱敏，retention 和删除可执行；删除后来源显示 `source_deleted`
      —— `tests/test_privacy_lifecycle.py`（3 例）
- [x] 每个 finalization operation 可在任意故障点恢复，request hash 或 expected version 不符时显式失败
      —— `tests/test_finalization.py::test_finalization_freezes_model_output_and_recovers_without_second_call`；VersionConflict 测试
- [x] finalizer JSON schema 拒绝越权 existing ID、未知 evidence ref、无依据 fact 和不合法关系
      —— `tests/test_finalization.py::{test_unknown_evidence_and_relations_are_rejected, test_output_requires_reason_and_enforces_limit}`
- [x] Markdown 人工修改永不被投影静默覆盖；correction/keep-db 两条解决路径均保留版本历史
      —— `tests/test_memory_projection.py::{test_manual_difference_conflicts_and_explicit_correction, test_conflict_can_explicitly_keep_database_version}`
- [x] 候选池达到 100 条时显式返回截断；分族确定、非传递、一个成员只属于一个族
      —— `tests/test_recall_families.py::test_presentation_grouping_is_anchor_based_not_transitive`
- [x] 生产副本完成迁移 dry-run、备份恢复、批次回滚和 `forge doctor` 故障呈现演练
      —— 迁移干跑/备份恢复/回滚有测试（`tests/test_precipitation_migration.py`、`test_memory_boundary.py::test_migration_backup_is_restorable`）；
      "生产副本"演练遵守"不得操作真实生产库"，用 `tmp_path` 副本等价行使。
- [x] 项目全量测试通过（父任务基线 199）—— **442 passed**（2026-10-13）

## Out of Scope

模型参数训练、专用向量数据库、实体知识图谱、`workflow_completed`、Trellis/第三方工作流监听、把阈值
调参当作治理本身、依据相似度自动合并、自动执行记忆中的危险操作。

## Notes

### 实测约束（2026-09-23，零 token 的只读探针得出，`design.md` 的契约据此写）

对真实 memory.db 做召回探针（家族 64 条，level 分布 `{pattern: 52, instance: 4, principle: 8}`）：

1. **长查询会退化成只匹配自身。** FTS 把查询的所有 token 全 AND（`memory.py:1121`）。实测：
   1 token → 同族 11 条；2–3 token → 10–11 条；**111 token（整段正文）→ 只返回 1 条，即源记录自己**。
   所以写前检索**不能用切片或整段正文当查询**，否则该机制会静默不触发。
   同时这也**证否了**「复用 P4 判据做写前抑制」：抽 5 条洪泛文档，5 条归一化正文哈希全不同，
   P4 的精确等价判据一条都拦不住。
2. **同族只有约 11/64 可达。** 召回是部分可达、不是全量 → 写前复用只能**减少**创建，不能作为保证；
   读侧归并不是可选项。
3. **准入需要上下文。** `admitted()` 中 pattern 级记录必须有技术/领域命中，instance 级必须有
   project 或路径命中。实测：只给 `project` → 同族 3/64；给全 `project + technologies + domains`
   → 11/64。而家族里 52/64 是 pattern 级。

顺带确认：**FTS 匹配与切词本身正常**——同一段正文直查 1/2/3/5 token 分别命中 24/15/4/1 行，
每次均含自身，无查询侧与索引侧不一致。

### 2026-10-13 收尾记录（状态归一复核）

代码 2026-09-24 已落地；本轮补的是两条端到端验证（`tests/test_precipitation_simulation.py`，3 例）
并逐条复核 17 条 AC 的测试归属（见 Acceptance Criteria）。全量 442 passed。

- 新测试：两小时/30 静默模拟（结束前零长期写入）、64 行洪泛语料回放（改写进报告、仅精确重复可归档）、
  同一正文重复沉淀 31 次仍归一实体。
- 变异验证两处：去静默闸门 → sim1 RED；把近重复塞进 exact → sim2 RED。
- AC#5 结构性满足（单候选夹具已活体覆盖）；AC#9 回放/成本报告转父任务 `09-21`（与其验收第 10 条重复）。
- **仍未决（需用户显式决定）**：是否恢复生产 watcher（见上"当前生产状态"与 implement.md 完成定义）。

### 其他

- **父任务关系**：本任务是 `09-21-forge-recall-rank-and-merge` 的子任务。父任务维护总协议与集成验收；
  本任务违反的正是父 `R12` 与父验收第 4 条，完成后需在父任务侧复核。
- **当前生产状态**：`watch-transcripts` 及 Startup 中的 `ContextForgeWatcher.vbs`（wscript）**已被人为
  停用**以止血（2026-09-23 12:41 起 vault 零新增）。VBS 文件仍在 Startup，**下次登录会复活**。
  本任务完成前是否恢复沉淀、或保持停用，是需显式决定的运维项——照父任务"生产切换保持显式"的约定。
- **设计已收敛**：以 `design.md` 的 Episode / Checkpoint / SessionFinalized / FinalizationReceipt 两层模型为准；
  本节早期三条候选只保留为讨论历史。
- **早期测量的一次错误**记录在本文"口径警告"一节，避免后续再次按 mtime 口径统计。
