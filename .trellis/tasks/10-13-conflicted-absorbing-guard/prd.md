# conflicted 记忆不得被非人写入清除

## Goal

让人工负反馈打出的 `conflicted`（待复核）成为一个**吸收态**：机器可以继续富集它，但**不能把它移出 `conflicted`**。
修掉一个现有语义缺陷——`MemoryLifecycle.publish` 会无条件把模型写的记忆置为 `active`，从而把人工的
`conflicted` 覆写回 `active`。

## Background

- 存储层现有非人写入保护集是 `suppressed / user_owned / disabled`（`memory.py:1494` 的 `put`，
  `memory_lifecycle.py:279` 的 `publish`）。**`conflicted` 不在其中。**
- `MemoryLifecycle.publish`（`memory_lifecycle.py:283`）对模型写入**无条件**
  `model_copy(update={"status": "active"})`。人工用 `record_feedback(harmful)` 把一条记忆打成
  `conflicted`（`memory.py:1372-1376`）后，下一次模型 publish 会把它覆写回 `active` —— 人工否决不是吸收态，
  **只有 `suppress`（disabled）才拦得住**。
- 其它模型写入（`support` / `weaken` / `extend`）都**保留原状态**，不清除冲突；`overturn` 写的是新 id。
  所以"清状态"这一点目前只发生在 `publish`，以及 `finalization.py:60` 的直接 `put(actor="model")`。
- 生产触发点：`worker.py:133` 的 `publish_session`，由 watcher 驱动（watcher 当前停用，故路径休眠，但这是设计路径）。

## Requirements

### R1 存储层不变量：`conflicted` 对非人写入是吸收的

`MemoryStore.put` 在非 `human` actor 下，若**当前记录是 `conflicted` 且写入会把状态改成非 `conflicted`**，
必须拒绝（`ProtectedMemory`）。允许的只有两类：
- 状态仍为 `conflicted` 的写入（如 `support` 追加证据，状态不变）——放行；
- `human` actor 的写入——放行（这是人工解除冲突的唯一途径，经 `restore` / `revise`）。

这条同时覆盖 `publish` 与 `finalization` 两条 model 写入路径。

### R2 自动通过必须**跳过**而不是抛（避免重试放大器）

任何**无人值守的批处理**若遇到 `conflicted`，必须**跳过该条并继续**，不得把 `ProtectedMemory` 抛穿：
- `MemoryMerge.apply_exact`：`conflicted` 的重复项按现有 `user_owned/suppressed/disabled` 同款处理——
  原样返回、不归档（`memory_merge.py:338`）。
- `MemoryLifecycle.publish`：模型对 `conflicted` 现记录的再次发布 → **返回现状、跳过**，不得抛
  （`worker.py` 中 `publish_session` 早于 `set_lines_processed` 调用，抛错会跳过偏移量推进 → 同一片重试，
  即沉淀洪泛任务里的那个放大器）。
- `Finalization`：若冻结操作的目标是 `conflicted` 现记录 → 跳过并把该操作**标记为已提交**（带跳过说明），
  使重跑不会卡在同一操作上。

### R3 不变量的可见与不可绕

- 人工解除冲突仍然走得通：`MemoryLifecycle.restore`（actor='human'）能把 `conflicted` 改回 `active`。
- 冲突本身仍由人工负反馈产生，语义不变（`harmful` 仅在非保护、非 conflicted 时置 conflicted）。

## Acceptance Criteria

- [x] AC1 `put`：actor='model' 把一条 `conflicted` 记录写成 `active` → `ProtectedMemory`，且记录**未变**
      （状态、版本均不动）。
- [x] AC2 `put`：actor='model' 写入但**保持** `conflicted`（如追加证据）→ 放行，版本 +1，状态仍 `conflicted`。
- [x] AC3 `put`：actor='human' 把 `conflicted` 改成 `active`（restore 路径）→ 放行。
- [x] AC4 `MemoryLifecycle.publish`：模型对一条 `conflicted` 记忆再次发布（内容不同）→ **不抛**，返回现状，
      状态仍 `conflicted`；且**同一批里后续候选仍能正常发布**（跳过是局部的，不中断整批）。
- [x] AC5 `MemoryMerge.apply_exact`：`conflicted` 的精确重复项 → 原样返回、不归档、不抛。
- [x] AC6 `Finalization`：冻结操作目标为 `conflicted` → 跳过且该操作被标记为已提交，重跑不再卡在该操作
      （操作计数不增长 / 不重复尝试）。
- [x] AC7 回归：现有 `suppressed / user_owned / disabled` 的非人写入保护**行为不变**（原测试不红）。

## Out of Scope

- 不改"何时产生 conflicted"（仍是人工 harmful 反馈）。
- 不改 `publish` 对**新 id** 记忆置 `active` 的既有行为（那是"自动沉淀"的设计选择，本次不动）。
- 不引入 persona / 用户模型 / 主动推送等（那是另开的路线图）。
- 不动 `access_broader` 那条保护。

## Constraints

- 触及存储与持久化路径：全量测试 + 每个新守卫**变异验证**（破坏 → RED → 还原）。
- **不得操作真实生产库**；测试一律 `tmp_path`。
- 跑全量测试带 `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1`（否则 `test_egress` 的 doctor 检查会联网卡死）。

## 收尾记录（2026-10-13）

- 实现：trellis-implement；独立复核：trellis-check。AC1–AC7 全过，无缺陷。
- 收口点：`MemoryStore.put` 的非人写入保护旁加一条「非人写入不得把记录**移出** `conflicted`」。
  一次性覆盖 `publish` 与 `finalization` 两条 model 写入路径（另有一处 `import` 早已被既有
  `imported_version` 守卫拦下）。
- 连带跳过（防重试循环）：`apply_exact` 把 conflicted 重复项加进既有跳过列表；`publish` 对 conflicted
  返回现状（不抛，因为它在 worker 的 `set_lines_processed` 之前、且批次是生成器）；`finalization`
  跳过并把冻结操作标记为已提交。
- 变异验证（实现方 5 处 + 复核方复跑 4 处，各自 RED 后还原）：`put` 条件、`_put` 的「移出」子句、
  `apply_exact` 跳过集、`publish` 跳过、`finalization` 跳过。
- 全量：**454 passed**（`HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1`）。
- 复核纠正一处**文档过满**：finalization 的「否则无限重试」只在记录于 freeze 与 commit 之间变争议时成立；
  AC6 夹具（冲突早于 plan）下 `put` 不抛，跳过的价值是避免无意义的新版本。`design.md` §3 已改准。
- 回归：`suppressed / user_owned / disabled` 三条既有保护逐字节未动（AC7）。
