# Design — 混合召回（第 3 层）

依据：`decisions.md` 的 **问题 5**、**F 段⑤**（embedding 出境）、**8a 红线**。本文只写设计，不含实现。

---

## 1. 实测基线（本机，2026-10-08）

| 量 | 数字 |
|---|---|
| 真实库规模 | `memories` 314 行 / `memory_versions` 317 行；`session_episodes` 10 行 |
| 纯 Python 余弦全扫（1024 维 float32） | 317 条 **38 ms** / 1000 条 **232 ms** / 10000 条 **1448 ms** |
| numpy（本机 2.5.3）同规模 matmul | 10000 条 **11.9 ms**；2000 条 0.7 ms |
| numpy float16 存储 | 10000 条 **20.5 MB** |
| numpy 是否在依赖里 | **没有**。`pyproject.toml` 只声明 pydantic / typer / python-frontmatter（+ optional `mcp`），本机有 numpy 属环境巧合 |
| 仓里现有的 embedding/vector 能力 | **零命中**（`grep -i embedding|vector context_forge/*.py` 无结果），整条通路是新建 |

**并更正决策文件里的一处错误**：问题 5 写的"1 万条暴力扫描 1–5ms"是**未实测的估计**。实测：纯 Python **1448 ms**，numpy **11.9 ms**。数量级差了近 300 倍。

---

## 2. 三件因实测而变的设计结论

1. **numpy 进 optional extra，向量通道是可选件**：`semantic = ["numpy>=1.26"]`。不把它变成硬依赖，否则 `pip install context-forge` 就背离 local-first 的轻量安装。
2. **不提供"纯 Python 语义扫描"这条降级路**。1000 条就要 232 ms，已经吃掉整个召回预算——一条"能跑但慢到不能用"的降级路是假降级，只会掩盖问题。**降级只有一档**：没有 numpy 或没配 embedding → 走纯词法（= 现状行为，功能完整）。
3. **今天的收益是召回质量，不是速度**。317 条规模下所有性能护栏（缓存、上限、失效检测）都是为增长准备的，所以取"简单 + 可失效"，**不预先上 sqlite-vec / HNSW**。

---

## 3. 数据布局

- `memory_versions` 加两列：`embedding_model TEXT NOT NULL DEFAULT ''`、`embedding BLOB`。
  BLOB = float32 小端打包（`array('f')` / `struct`）。**维度不写进 BLOB**：由 `embedding_model` 决定；
  解包时长度与该模型登记的维度不符 → 视为失效行（跳过 + 计数），不猜。
- 两列是**派生缓存**，权威仍是 `snapshot_json`（符合 spec：不为已有快照数据造第二权威）。
- 迁移走显式 `MemoryStore.migrate()`：**`SCHEMA_VERSION` 7 → 8** + 在线备份 + `dry_run` 默认，
  普通连接打开旧库仍然 raise（照 risk 列那次的形状，勿在连接时隐式迁移）。
- `forge reindex-embeddings` 是**独立命令**，**绝不挂进 `rebuild_fts`**：后者会 `DROP TABLE memory_fts`
  并顺带 `_rebuild_projections`，挂进去等于每次重建 FTS 都烧一遍远端 embedding 调用。

---

## 4. 三条通道，一个管道

1. **BM25 全 AND** —— 现有，高精度。
2. **关键词降级放宽** —— 新增中间档：当通道 1 结果为空或少于阈值时，按 token 逐轮丢弃"最冷"的字继续查，
   取并集；总候选池仍受现有 `candidate_limit=100` 硬上界。
3. **语义 top-K** —— 新增：numpy 可用且 embedding 已配置时，用缓存矩阵算余弦 top-K（默认 K=50）。

三路并集之后**全部走现有管道**：`boundary_visible` → `admitted` → 五维打分 → 四段解释 → 字节预算。
**不新增第二条打分路径**（问题 5 的管道纪律）。

---

## 5. 缓存、失效与失败语义（这里最容易埋雷）

- **进程内缓存**：`{embedding_model: (matrix, row_ids, fingerprint)}`，
  `fingerprint = (MAX(rowid), COUNT(*))` of embedding rows —— 亚毫秒校验，不一致即重建。
  317 条时重建是"一次读 + 一次 matmul"，比维护增量结构的复杂度便宜。
- **不做逐条增量更新矩阵**：numpy 行插入要重分配，换来的一致性风险不值得。`_put` 后把缓存标脏即可。
- **embedding 调用失败（网络 / 额度 / 超时）→ 该条留 NULL，写入照常成功**。
  向量是加速器，**不能成为沉淀的前置条件**——否则 provider 一挂，记忆就不再产生，那是把主链路押在概率事件上（8a 同源）。
  失败计数与最近原因进 `doctor`。
- **查询期失败**（矩阵构建异常、维度不符、无 numpy）→ 本次请求只用通道 1+2，
  并在解释里**明说语义通道未启用**，不静默降级。
- **模型漂移**：`embedding_model` 与当前配置不一致 → 报表计数 + 提示跑 reindex，**不自动重算**
  （花钱的动作不由后台触发）。

---

## 6. 出境约束（F⑤ 的具体形式）

- 送进 embedding 的文本 = `sanitize_text(body + ' ' + summary)`，与提取模型同一份脱敏、同一套三态；
- `doctor.check_egress()` 现在只报提取通路 → 本任务**必须把 embedding endpoint 一并列出**，
  否则等于新开一条没人看见的出境路；
- embedding 未配置 → 出境面不变（不多一条路）。

---

## 7. 解释与打分

- 五维打分里 `text` 维度改为 `max(词法分, 语义分)`，四段解释新增一行 `语义相似 0.xx`，
  并标注命中通道：`bm25` / `widened` / `semantic`。
  通道标注是给第 2 层已有的分桶报表直接用的，比再造一个统计口径便宜。
- **语义分怎么归一，本设计不定死**：不同 embedding 模型的余弦分布差别很大，现在就发明归一函数
  是凭空气味。第一版**直接用余弦原值**（0..1）作为 text 维度下界，同时把两个原始数都显示出来，
  等评测数据说话再决定要不要标定 —— 列为下面的待验证项。

---

## 8. 不做清单

- 不引入 sqlite-vec、任何独立向量服务、自建 HNSW/IVF
- 不做 rerank 模型、不做 LLM 查询改写
- 不做纯 Python 向量扫描这条假降级路
- 不自动回填历史向量（`reindex-embeddings` 必须显式跑）
- 不在本任务做候选批量解包优化（那是第 4 层，且它依赖本任务的产物）
- 不动 `signal_counts` 的桶语义（通道标注走解释，不新增 signal 枚举）

---

## 9. 拆步（每步独立可提交、独立可测）

1. schema v8 + BLOB 打包/解包 + 维度失效判定（纯函数，可单测，无 provider 也能测）
2. **spike**：本机是否真能拿到 embedding 通路（见 §10），拿到之前不写第 3 步的代码
3. embedding gateway 能力 + `_put` 写向量 + 失败留 NULL（含"provider 挂了沉淀不受影响"的测试）
4. 进程内缓存与指纹失效 + 无 numpy 时的行为
5. 关键词放宽通道（可独立于语义先行落地——**它不需要任何新依赖，可能就能解决大半漏召回**）
6. 语义候选通道 + 解释合并与通道标注
7. `reindex-embeddings` 命令 + doctor/status 可见性（含 egress 那行）
8. 评测：扩 `tests/recall_eval_cases.py` 加"语义近、字面远"用例，量化 5 与 6 各自的命中增量

顺序上 1、4、5、8 不依赖 embedding 通路，可以先做完再谈语义通道 —— 这是把"不确定的外部依赖"推到最后的办法。

---

## 10. 待验证（**未本机确认，不得当作事实写进代码或文档**）

- 本机 Ollama 是否可用、其 embedding 接口的**路径与响应字段名**（我并未验证过，不能凭记忆写）
- 已配置的 remote provider 是否提供 embeddings（**我不知道**，需查该 provider 的实际接口）
- 若上面两条都不成立 → 语义通道保持"设计就绪但默认关闭"，本任务的可用收益由第 5 步（关键词放宽）交付
