# Design — `conflicted` 是吸收态

## 1. 不变量

> **非 `human` 的写入不得把一条记录移出 `conflicted`。**

`conflicted` 的语义是"有人给出了可归因的负证据，等人工复核"。它和 spec 里已有的
「三态折叠：conflict 是吸收态」是同一条纪律的两处体现——悲观状态吸收，不能被后续的乐观信号复活。

## 2. 单一收口点：`MemoryStore.put`

在 `put` 的非人保护检查旁（`memory.py:1494` 之后）加一条：

```python
if previous and actor != 'human' and previous.draft.status == 'conflicted' \
        and draft.status != 'conflicted':
    raise ProtectedMemory('a human conflict may only be cleared by a human')
```

注意条件写成「**移出** conflicted」而不是「写入 conflicted 记忆」：
- `support()` 追加证据、状态不变 → 放行（机器可以富集争议记忆）；
- 只有把状态改成 `active`/`archived`/`stale`… 的写入才被拒。

这样 `publish`（无条件置 active）与 `finalization`（`put(actor="model")`）两条路径一并覆盖，
不需要在每处各加一份守卫。

**为什么不在 `publish` 里单独修**：那是修"报告里提到的那条路径"，不是修这一类。
`finalization.py:60` 同样以 `actor="model"` 直接 `put`，能清掉状态；只堵 publish 会留同一漏洞的第二个入口。

**为什么不采用"静默强制保留 conflicted（改写调用方要的状态）"**：调用方要写 `active` 却被悄悄改成
`conflicted`，会把 bug 藏起来（"我明明 publish 成功了，为什么不是 active"）。存储层对越权写入的既有风格是
**大声拒绝**（相邻几条 guard 都是 `raise ProtectedMemory`），保持同款。

## 3. 收口点带来的连带：批处理必须跳过，不能抛

`put` 会抛，于是**无人值守的批处理遇到 conflicted 必须跳过并继续**，否则异常抛穿会触发重试循环。

- **`MemoryMerge.apply_exact`**（`memory_merge.py:338`）：现有代码已经把
  `user_owned / suppressed / disabled` 的重复项**原样返回、不归档**。给这一条列表加上 `conflicted`
  即可，形状完全对称，无需新逻辑。
- **`MemoryLifecycle.publish`**（`memory_lifecycle.py:277-288`）：模型对 `conflicted` 现记录再次发布时
  **返回现状、跳过**（不抛）。
  **理由（关键）**：`worker.py` 里 `publish_session` 在 `set_lines_processed` **之前**执行；
  若它抛错，`set_lines_processed` 被跳过 → 同一片被反复重试——正是 `09-23` 记录的洪泛放大器。
  另外 `publish_session` 用生成器逐条 publish，抛错会中断**整批**；跳过则是局部的。
  `publish` 对 `suppressed/disabled` 仍维持原行为（抛 `ProtectedMemory`）——本次**只对 `conflicted` 改为跳过**，
  以免顺手改掉既有语义（列为 AC7 回归点）。
- **`Finalization`**（`finalization.py:54-76`）：逐条处理冻结操作。若目标现记录是 `conflicted`，
  **跳过并调用 `queue.complete_finalization_operation(...)` 标记完成**（带一条跳过说明）。
  跳过要避免的是两种后果——精确地说：
  (a) 当记录在 **freeze 与 commit 之间**变为 `conflicted` 时，冻结草稿带的是非 conflicted 状态，
      `put` 会抛 `ProtectedMemory` → 该操作永远停在未提交 → 重跑重试；
  (b) 即便不抛（如 AC6 的夹具：冲突早于 plan 存在，冻结草稿本身即 conflicted，`put` 不抛），
      不跳过也会把冻结草稿重新提交、多出一个无意义的版本。

## 4. 边界与非目标

- `overturn()` 写的是**新** id（`expected_version=0`），不受影响——它本来就是"造一条矛盾记忆"的正路。
- `undo_exact` / `restore` / `revise` 都是 `actor='human'` → 放行，人工解除冲突的通路不变。
- `apply_conflict()` 写的是 `left.draft` **原样**（状态不变）→ 放行。
- 不动"新 id 记忆被 publish 直接置 active"的既有设计（另属路线图）。

## 5. 验证矩阵

| 场景 | 期望 | AC |
|---|---|---|
| model 把 conflicted 写成 active | `ProtectedMemory`，记录不变 | AC1 |
| model 写 conflicted 但保留状态（追加证据） | 放行，版本 +1，状态仍 conflicted | AC2 |
| human 把 conflicted 写成 active | 放行 | AC3 |
| model publish 覆盖 conflicted（内容不同） | 跳过返回现状，不抛；同批后续仍发布 | AC4 |
| apply_exact 遇 conflicted 重复项 | 原样返回、不归档 | AC5 |
| finalization 目标为 conflicted | 跳过 + 操作标记已提交，重跑不卡 | AC6 |
| 既有 suppressed/user_owned/disabled 保护 | 行为不变 | AC7 |
