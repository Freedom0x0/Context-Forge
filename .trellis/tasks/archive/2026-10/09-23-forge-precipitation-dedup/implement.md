# 沉淀洪泛治理实现计划

> 执行方式：当前 Codex 会话内按任务顺序执行；每项严格走 RED → GREEN → 回归。不得恢复生产 watcher，直到任务 8 的上线闸门通过。

> 实施结果（2026-09-24）：代码与自动化测试已完成；生产 watcher 保持未自动启用。详细证据见 `docs/testing/forge-precipitation-dedup.tdd.md`。

**目标**：把静默即发布替换为 Episode 采集、明确结束后可恢复巩固、`memory.db` 权威投影和分阶段召回，并满足可推广上线门槛。

**架构**：`queue.db` 保存事件、Episode、Checkpoint、FinalizationReceipt 与逐操作日志；`memory.db` 保存版本化长期记忆、精确指纹和关系；模型调用位于事务外，成功结果先冻结，随后用稳定 operation key 写长期库；Markdown 是可重建投影和显式人工纠正入口。

**技术栈**：Python 3.12、Pydantic 2、stdlib SQLite、Typer、pytest；不新增运行时依赖。

## 全局约束

- 普通打开不得隐式迁移 `memory.db`；schema 变更必须显式 dry-run、在线备份、可恢复。
- `memory.db` 是长期权威；`queue.db` 的跨库一致性依赖 operation log，不宣称跨库原子事务。
- 模型调用不进入数据库事务；相同 receipt 在模型成功后不得再次调用模型。
- 静默 60 秒只采集，不发布长期记忆；`PreCompact` 只生成 checkpoint。
- 自动任务不得覆盖人工保护、扩大访问、恢复 suppressed/disabled 或无工具证据升 verified。
- 不实现 `workflow_completed`、第三方工作流监听、向量库或实体知识图谱。

## 任务 1：事件、Episode 与 Checkpoint 底座

**文件**：修改 `context_forge/queue.py`、`context_forge/domain.py`、`context_forge/watcher.py`、`context_forge/worker.py`、`context_forge/hooks/claude_code.py`、`context_forge/install_hook.py`、`context_forge/cli.py`；新增 `context_forge/episodes.py`；测试 `tests/test_episodes.py`、`tests/test_context_forge.py`。

**接口**：

```python
class SessionLifecycleEvent(BaseModel):
    event_id: str
    type: Literal['session_end', 'pre_compact']
    source: str
    session_id: str
    transcript_path: str
    observed_at: str
    final_line_count: int | None = None
    reason: str

JobStore.ingest_session_event(event) -> Literal['created', 'existing']
JobStore.collect_episode(...) -> EpisodeRecord
JobStore.create_checkpoint(...) -> CheckpointRecord
EpisodeCollector.collect(session_id, transcript_path, project) -> EpisodeRecord
```

- [ ] 写事件同 key 同 payload 幂等、同 key 异 payload冲突、增量采集、静默不发布、PreCompact checkpoint 的失败测试。
- [ ] 运行 `pytest tests/test_episodes.py tests/test_context_forge.py -q`，确认失败来自缺失接口/旧发布行为。
- [ ] 实现 queue 表与 CAS 状态、EpisodeCollector、通用事件解析和 hook 投递；watcher 改为只采集。
- [ ] 重跑同一测试集至通过；再跑 `pytest tests/test_runtime_sync.py -q` 防止旧同步被意外触发。
- [ ] 检查点：提交事件/Episode 底座及 RED/GREEN 证据。

## 任务 2：SessionEnd、人工 finalize 与 inferred 状态机

**文件**：修改 `context_forge/episodes.py`、`context_forge/watcher.py`、`context_forge/cli.py`、`context_forge/doctor.py`、`context_forge/install_hook.py`；测试 `tests/test_episodes.py`、`tests/test_runtime_sync.py`。

**接口**：

```python
EpisodeService.request_finalize(session_id, *, end_kind, event_id) -> FinalizationReceipt
EpisodeService.advance_inferred(now, process_alive, active_tools) -> list[FinalizationReceipt]
forge session list|inspect|finalize|delete
forge event ingest
```

- [ ] 写重复/乱序 SessionEnd、人工 finalize、30+30 分钟 inferred pending、恢复追加取消 pending、新 segment 的失败测试。
- [ ] 写 hook install/status/self-test 和 doctor 显式降级的失败测试；所有路径重定向到临时目录。
- [ ] 运行目标测试确认 RED。
- [ ] 实现状态机与 CLI/doctor；默认 inferred pending 和宽限均为 30 分钟，可配置。
- [ ] 重跑目标测试至 GREEN，并执行 `pytest tests/test_context_forge.py tests/test_runtime_sync.py -q`。

## 任务 3：Finalizer Schema、Receipt 与逐操作恢复

**文件**：修改 `context_forge/domain.py`、`context_forge/provider_http.py`、`context_forge/gateways.py`、`context_forge/queue.py`、`context_forge/memory_models.py`、`context_forge/memory_schema.py`、`context_forge/memory.py`、`context_forge/memory_lifecycle.py`；新增 `context_forge/finalization.py`；测试 `tests/test_finalization.py`、`tests/test_memory_store.py`、`tests/test_memory_boundary.py`。

**接口**：

```python
class FinalizationOutput(BaseModel):
    candidates: tuple[FinalMemoryCandidate, ...]
    deferred_topics: tuple[DeferredTopic, ...]
    no_memory_reason: str | None

FinalizationService.finalize(finalization_id: str) -> FinalizationResult
MemoryStore.find_exact(fingerprint: str) -> MemoryRecord | None
MemoryStore.put_planned(operation: FinalizationOperation) -> MemoryRecord
```

- [ ] 写 JSON schema 校验测试：未知 evidence ref、越权 existing ID、无依据 observation、不合法关系、零产出无原因、超过 K 均失败。
- [ ] 写模型成功后各故障点恢复、两个 worker 竞争、request hash 冲突、VersionConflict 不降级重复的失败测试。
- [ ] 写 memory schema v3→v4 dry-run/backup/restore、fingerprint 投影和 `find_exact` 测试并确认 RED。
- [ ] 实现 finalizer prompt/schema、冻结 extraction/write plan、逐 operation 状态、稳定 UUID、精确指纹和来源 identity。
- [ ] 重跑目标测试至 GREEN；执行 memory store/lifecycle 全套回归。

## 任务 4：DB 权威与 Markdown 投影/人工纠正

**文件**：新增 `context_forge/memory_projection.py`；修改 `context_forge/vault.py`、`context_forge/runtime_sync.py`、`context_forge/worker.py`、`context_forge/watcher.py`、`context_forge/cli.py`、`context_forge/doctor.py`；测试 `tests/test_memory_projection.py`、`tests/test_runtime_sync.py`、`tests/test_memory_integration.py`。

**接口**：

```python
MemoryProjector.project(memory_id: str) -> ProjectionResult
MemoryProjector.project_pending() -> ProjectionReport
MemoryProjector.import_correction(path, expected_version) -> MemoryRecord
MemoryProjector.resolve_keep_db(conflict_id) -> ProjectionResult
```

- [ ] 写 DB committed/投影失败仍可召回、重试投影、hash 相同安全覆盖、人工差异形成冲突、user_owned 不覆盖的失败测试。
- [ ] 写旧 vault-first 和 watcher 轮后 `runtime_sync` 不再参与新写入的集成测试并确认 RED。
- [ ] 实现 projection hash/version、冲突表与 correction CLI；保留显式 legacy import，但从 watcher 新路径移除。
- [ ] 重跑投影、同步、集成测试至 GREEN；验证 doctor 显示待处理冲突。

## 任务 5：分阶段召回与防御性呈现族

**文件**：修改 `context_forge/memory.py`、`context_forge/memory_recall.py`、`context_forge/mcp_server.py`、`context_forge/cli.py`、`context_forge/memory_merge.py`；测试 `tests/test_memory_recall.py`、`tests/test_memory_recall_score.py`、`tests/test_recall_limits.py`、新增 `tests/test_recall_families.py`。

**接口**：

```python
MemoryStore.search_candidates(..., candidate_limit=100) -> CandidatePool
group_for_presentation(scored, threshold=0.90) -> tuple[MemoryFamily, ...]
MemoryRecall.search(...) -> RecallResponse  # results + candidate_truncated
```

- [ ] 写先分族后预算、100 条截断标记、边界不兼容不分族、星型非传递、中文 tokenizer、稳定排序测试并确认 RED。
- [ ] 实现候选池/过滤准入/评分/分族/代表/预算阶段；保持旧 `search_scored` 兼容包装。
- [ ] 确保 Episode/Checkpoint 永不进入默认召回，关系展开仍逐节点检查边界，共现不加权。
- [ ] 重跑所有 recall/merge/eval 测试至 GREEN，并记录召回基线变化。

## 任务 6：Retention、删除与隐私

**文件**：修改 `context_forge/episodes.py`、`context_forge/queue.py`、`context_forge/transcript.py`、`context_forge/memory.py`、`context_forge/cli.py`；测试 `tests/test_episodes.py`、新增 `tests/test_privacy_lifecycle.py`。

- [ ] 写持久化前二次脱敏失败即拒绝、0/30/365 天 retention、open/failed 不自动清理、删除后 `source_deleted` 的失败测试。
- [ ] 实现 retention deadline、清理报告、session inspect/list/delete 与长期来源最小墓碑。
- [ ] 重跑隐私与 memory source 冲突测试至 GREEN。

## 任务 7：迁移、存量治理和回滚

**文件**：修改 `context_forge/memory.py`、`context_forge/memory_schema.py`、`context_forge/memory_merge.py`、`context_forge/cli.py`；新增 `context_forge/precipitation_migration.py`；测试 `tests/test_memory_boundary.py`、新增 `tests/test_precipitation_migration.py`。

- [ ] 写 dry-run 不改字节、在线备份可恢复、重复 apply 幂等、批次 rollback、旧引用仍有效的失败测试。
- [ ] 写“仅精确重复自动归档，相似项只报告”的变异测试并确认 RED。
- [ ] 实现 migration batch、报告和逆操作；不得重写存量 memory ID。
- [ ] 重跑迁移、merge、import 全套测试至 GREEN。

## 任务 8：上线闸门、文档与全量验证

**文件**：修改 `context_forge/doctor.py`、`docs/memory-store.md`、`docs/technical-design-falsifiable-memory.md`、`.trellis/spec/backend/*`（仅实际形成的新契约）；新增 `docs/testing/forge-precipitation-dedup.tdd.md`；测试全套。

- [ ] 增加 doctor 对 hook、事件积压、failed receipt、投影冲突、schema 和同步错误的测试。
- [ ] 构造两小时/30 次静默、SessionEnd 丢失/重复/乱序、崩溃、并发、恢复追加和 64 条洪泛语料回放。
- [ ] 对每个硬闸门执行对应变异，确认能 RED；记录真实命令和输出，不写不可触发的门槛。
- [ ] 运行 `pytest -q`；运行可用的 coverage 命令并确认新增模块覆盖率至少 80%。
- [ ] 执行迁移 dry-run、备份恢复和批次回滚的临时副本演练；不得操作真实生产库。
- [ ] 更新 TDD 证据报告、技术文档和项目 spec；最终检查 watcher 仍未自动恢复。

## 完成定义

- PRD R1–R16 与全部 Acceptance Criteria 有对应测试；
- 所有目标测试与全量测试通过，无跳过；
- 新增模块覆盖率至少 80%；
- 迁移、备份恢复、回滚、故障注入和并发演练有新鲜证据；
- 生产 watcher 保持停用，恢复必须由用户在上线闸门报告后显式执行。
