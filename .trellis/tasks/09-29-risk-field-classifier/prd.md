# risk 字段落地：枚举化 + 确定性分类器 + 可过滤列

## Goal

把 `risk` 从一个**休眠字段**变成可用的闸门：由确定性词表分类器赋值、落成 SQL 可过滤的列、
高危未批准时天然不可召回。

依据：`09-29-memory-improvement-brainstorm/decisions.md` 的 **A①**（含"已存在但休眠"的核实更正）、
**问题 4 第 2 条**、**问题 9 决策 2**、**F4**。这三处都写在 `risk=high` 上，而它今天不可用。

## 背景与事实（已 grep 核实）

- `MemoryDraft.risk` 已定义于 `memory_models.py:102`，类型 `str | None` 自由文本，**全项目仅此一处出现，
  无人写、无人读**。
- 召回解释里另有自由文本 `risks: Sequence[str]`（`memory.py:346/401/413`）与 `ScoreBreakdown.risk`，
  与本字段无关，不要混用。
- `status` 的 CHECK 已含 `candidate`（`memory_schema.py:7`），而召回 SQL 只认
  `status IN ('active','conflicted')`（`memory.py:1202`）→ **高危待审不需要新增状态值**。
- 加列的先例：`memory.py:605-611` 用 `PRAGMA table_info(memories)` →
  `ALTER TABLE memories ADD COLUMN weight REAL NOT NULL DEFAULT 1.0`。

## Requirements

1. **枚举化**：`MemoryDraft.risk` 改为 `Literal['low', 'normal', 'high'] = 'normal'`。
2. **确定性分类器**：新建 `context_forge/risk.py`，单一纯函数 `classify_risk(text) -> str` + 词表常量
   （删除 / 重置 / 迁移 / 回滚 / 生产环境 等操作词）。放独立模块是因为 `validate.py` 是 vault 自检，
   不适合承载它。纯函数、可单测，形状照 `canonical_fingerprint`（`memory.py:52`）。
3. **赋值点**：finalization 的校验/规划阶段（`finalization.py` `_validate_output` / `_plan`）用分类器赋值。
   **模型自报仅作参考**：与分类器不一致时以分类器为准，并记一条事件（供问题 1 的报表观察）。
4. **落成可过滤列（形状受规范约束，2026-09-29 晚按 `.trellis/spec/backend/database-guidelines.md` 修正）**：
   - 规范红线："**绝不**为 `snapshot_json` 里已有的数据再加第二个**权威**列（双真相）；新列只能是可重建的投影或运营缓存"。
     → 权威仍在 `MemoryDraft.risk`（`snapshot_json`），**列只是派生缓存**，且必须能从快照确定性地重算出来。
   - 列放 **`memory_versions.risk TEXT NOT NULL DEFAULT 'normal'`**（版本级），**不是** `memories.risk`。
     理由与先例：`risk` 是 draft 的字段、按版本而变，与 `canonical_fingerprint` 同构——后者正是加在
     `memory_versions` 上（`memory_schema.py:16`），并在 `migrate()` 里**遍历所有版本、从 snapshot_json 反序列化后回填**
     （`memory.py:609-616`）。照这个形状做，就不违反双真相规则。
   - 迁移**必须在显式 `MemoryStore.migrate()` 内**：`SCHEMA_VERSION` bump + 先做在线备份 + `dry_run=True` 默认 +
     可恢复。规范明令"**绝不在打开普通连接时隐式迁移**"（普通连接必须 raise）——所以 `queue._migrate()`
     那种隐式 ALTER 模板**只适用于派生库 queue.db**，不能搬进 memory.db。
5. **待审不可召回**：`risk=high` 且未获批准时 `status` 停在 `candidate`。本任务只保证
   "risk 可过滤 + candidate 不在召回集"；**批准通道本身属 F3/F4，另立任务**。
6. **附带（审阅 ⑧，经用户确认提到本任务）**：`PROMPT_VERSION` 常量移到与 prompt 文本同一模块，
   并加一条 pytest——断言 prompt 文本哈希等于期望值，不一致即失败并提示"改 prompt 必须 bump
   `PROMPT_VERSION`"。理由：`worker.py:131` 现在把版本硬编码成一个串，不钉住则问题 1 的
   "按 prompt_version 分桶"永远只有一个桶。

## Acceptance Criteria

- [ ] 含词表词的候选 → `risk=high`；不含 → `normal`/`low`；词表每类都有单测用例
- [ ] 分类器是无状态纯函数，同一输入两次调用结果一致（不依赖时间/随机）
- [ ] `risk=high` 且 `status=candidate` 的记忆，`forge search` 与 MCP `memory_search` 都查不到；
      转为 `active` 后查得到
- [ ] 迁移走**显式** `MemoryStore.migrate()`：`dry_run=True` 默认能报出将要加的列；实跑先落在线备份；
      跑完后 `memory_versions.risk` 存在，且回填值 = 对每个版本 `snapshot_json` 运行分类器的结果
      （与 `canonical_fingerprint` 的回填同构，`memory.py:613-615`）
- [ ] 打开旧 `user_version` 的普通连接仍然 **raise、不隐式迁移**（规范红线，必须有这条测试）
- [ ] `rebuild_weights()` 等运营缓存路径不受影响
- [ ] 模型自报与分类器冲突时以分类器为准，且写入一条可被报表统计的事件
- [ ] prompt 哈希测试存在，并能在"改 prompt 未 bump 版本"时失败（用临时改动验证一次）
- [ ] 全量测试通过，无新增依赖

## Out of Scope（明确不做）

- 高危词表的"完备覆盖"，不接模型判断（问题 9 已定：规则永不完备，靠报表与坏案例反哺补）
- `forge approve` 批准通道、`pending_confirmations` 表（F3/F4，属召回侧任务）
- 召回侧"未批准不返回正文"（F4，属第 3 层召回任务）
- 敏感路径启发式（问题 9 决策 2 的另一半，依赖本任务的 risk 列，另立任务）

## Dependencies

- 前置：无（第 0 层中最独立）
- 被依赖：问题 4 双确认、问题 9 路径启发式、F4 批准通道

## 实现记录（2026-09-29）

**落地形状**
- `context_forge/risk.py`：`classify_risk(text)`（命令型正则 + 中文"销毁动词 × 生产/批量限定词"组合）与
  `risk_of(draft)`。**只有 `kind == 'rule'` 参与判定**，knowledge 恒为 normal——会被宿主"照着做"的才是规则。
- 单一派生点放在 `MemoryStore._put`（`memory.py:949` 之后）：写入前把 `draft.risk` 规范化为
  `risk_of(draft)`，因此快照与 `memory_versions.risk` 列**必然一致**，两条写路径（worker / finalization）
  不需要各自接线。全项目只有一处 `INSERT INTO memory_versions`，已核实。
- `memory_versions.risk`（版本级、派生缓存）+ `SCHEMA_VERSION` 4→5 + `migrate()` 内
  ALTER 与**从 snapshot 回填**（与 `canonical_fingerprint` 同一次循环、同一形状）。
- 今天接上的消费者：召回四段解释的"风险提示"行，`risk_of(...)=='high'` 时追加
  "高危指令：先向人确认，未确认不得执行"（`memory.py` 的 `rescored` 循环）。列本身供后续 F4 闸门做 SQL 过滤。
- 审阅⑧ 已做：`PROMPT_VERSION` 移到 `provider_http.py` 的 `_PROMPT` 旁，`worker.py` 改为引用；
  `tests/test_risk.py::test_prompt_digest_is_pinned_to_the_recorded_version` 钉住 `_PROMPT` 的 sha256。

**与原 PRD 的三处偏离（均为主动选择，非遗漏）**
1. 枚举是 `['normal','high']`，**去掉了 `low`**：没有任何行为依赖 low，留下就是一个没人赋值也没人读的档级——
   正是本任务要消灭的"休眠字段"。
2. 原 AC"模型自报与分类器冲突时记一条事件"**改为**：模型自报的非法值由 field_validator 降级为 `normal`
   （不让一次作业因字段值失败），冲突信息改由**报表里的 risk 分布**承担（第 2 层）。理由：要记录"自报值"必须
   先把 gateway 原始 payload 留存下来，那是新增数据结构，不属于本层最小集。
3. **没有改发布行为**：`risk=high` 今天仍会自动 publish。让高危停在 `candidate` 需要 F4 的批准通道，
   否则等于把它永久关进黑屋。本任务交付的是"可判定、可落库、可在解释里看见"。

**证据**
- 新增 `tests/test_risk.py` 17 例（分类正例 6 / 反例 4 / kind 门 / 自报降级 / put 派生 / 解释标注 /
  v4 迁移回填 / 普通连接拒绝隐式迁移 / dry_run 不改字节 / prompt 钉版）。
- 全量 `pytest`：**251 passed**（其中含本任务新增 17 例）。
- 变异验证：去掉 `risk_of` 的 kind 门 → `1 failed / 16 passed`；去掉 migrate 里的 risk ALTER →
  `1 failed / 16 passed`；恢复后 `17 passed`。两个守卫都真实可失败。
- **未提交**：叠加在工作区已有的未提交改动之上，未执行 git commit。

## Notes

- 参考决策文件而非本 PRD 复述的细节：`.trellis/tasks/09-29-memory-improvement-brainstorm/decisions.md`
- 实施前读 `.trellis/spec/` 对应层的编码规范
