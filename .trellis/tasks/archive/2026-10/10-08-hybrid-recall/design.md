# Design — 混合召回（第 3 层）

> **Current implementation decision (2026-10-12):** the embedding provider is local
> `sentence-transformers` with `all-MiniLM-L6-v2` (384 dimensions), loaded only from
> the local cache. The remote Voyage exploration below is historical research and is
> not an active implementation path.

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
| 放宽段的额外 SQL（400 条 / 60 词库，本机） | df 计数 3–6 词 **0.4–6.4 ms**、AND **0.2–1.7 ms**、并集+LIMIT 100 **1.1–1.3 ms**；单次 `search_scored` 全程 28–50 ms，**大头是逐条读记录，不是放宽** |

**并更正决策文件里的一处错误**：问题 5 写的"1 万条暴力扫描 1–5ms"是**未实测的估计**。实测：纯 Python **1448 ms**，numpy **11.9 ms**。数量级差了近 300 倍。

---

## 2. 三件因实测而变的设计结论

1. **本地模型进 optional extra，向量通道不阻塞主链路**：`semantic = ["numpy>=1.26", "sentence-transformers>=3,<4"]`。模型只从缓存加载。
2. **不提供"纯 Python 语义扫描"这条降级路**。1000 条就要 232 ms，已经吃掉整个召回预算——一条"能跑但慢到不能用"的降级路是假降级，只会掩盖问题。**降级只有一档**：没有 numpy 或没配 embedding → 走纯词法（= 现状行为，功能完整）。
3. **今天的收益是召回质量，不是速度**。317 条规模下所有性能护栏（缓存、上限、失效检测）都是为增长准备的，所以取"简单 + 可失效"，**不预先上 sqlite-vec / HNSW**。

---

## 3. 数据布局

- `memory_versions` 加两列：`embedding_model TEXT NOT NULL DEFAULT ''`、`embedding BLOB`。
  BLOB = float32 小端打包（`array('f')` / `struct`）。**维度不写进 BLOB**：由配置的
  `embedding_model` + `embedding_dimensions` 决定（2026-10-08 补：Voyage-4 同一模型可选 256/512/1024/2048，
  只靠模型名推不出维度）；
  解包时长度与登记的维度不符 → 视为失效行（跳过 + 计数），不猜。
- 两列是**派生缓存**，权威仍是 `snapshot_json`（符合 spec：不为已有快照数据造第二权威）。
- 迁移走显式 `MemoryStore.migrate()`：**`SCHEMA_VERSION` 7 → 8** + 在线备份 + `dry_run` 默认，
  普通连接打开旧库仍然 raise（照 risk 列那次的形状，勿在连接时隐式迁移）。
- `forge reindex-embeddings` 是**独立命令**，**绝不挂进 `rebuild_fts`**：后者会 `DROP TABLE memory_fts`
  并顺带 `_rebuild_projections`，挂进去等于每次重建 FTS 都烧一遍远端 embedding 调用。

---

## 4. 三条通道，一个管道

1. **BM25 全 AND** —— 现有，高精度。
2. **关键词放宽（已实施，2026-10-08）** —— 新增中间档，两段且**不按"能丢几个词"计数**：
   - **2a 剔掉匹配不上的词再 AND**：某个 token 在 `memory_fts` 里 `df=0`，它永远不可能满足 AND，
     丢掉它是零成本的。测过的反例：固定丢弃预算（3 轮）时，带 **≥4 个未知词**的查询恢复率 **0%**，
     直到预算超过未知词数量才动 —— 也就是说"轮数"这个规则本身就是错的。
   - **2b 词并集兜底**：2a 之后仍不足 `wanted` 才走，把**全部**词用 `OR` 连起来按 BM25 排名，
     上限 `WIDEN_RESULT_CAP = 100`。**限制的是结果条数，不是丢词数量**——匹配得上的词一个都不丢。
   - 两条都只补**新**命中，已有命中保持原标签；每条被放宽带进来的记忆在解释里写明是哪一段带进来的
     （`去掉 N 个匹配不上的词` / `词并集兜底：命中其中一个词即可`）。
   - 单 token 查询不放宽（无词可丢，并集等于自己）。
   - 试过并**被测试证伪**的三种排序：先丢高频词、先丢低频词、df=0 + 高频带上限——见 `decisions.md` 的更正记录。
3. **语义 top-K** —— 新增：numpy 可用且 embedding 已配置时，用缓存矩阵算余弦 top-K（默认 K=50）。

三路并集之后**全部走现有管道**：`boundary_visible` → `admitted` → 五维打分 → 四段解释 → 字节预算。
**不新增第二条打分路径**（问题 5 的管道纪律）。放宽段**不绕边界**：`tests/test_recall_widening.py::test_widening_still_enforces_the_boundary`
里 `hidden` 只有靠并集才能被检索到，边界过滤仍然把它挡在外面。

一处**已改变的行为**（需要知道，不是隐藏细节）：`store.search('SQLite OR missing')` 以前返回空，
现在返回那条 SQLite 记忆并带上放宽标签。旧断言"问了库里没有的词就什么都不要给"与本轮的设计目标直接冲突，
已改成断言"要给，但必须说清楚是怎么来的"。FTS 语法泄露那条断言（`'"* - : ()'` → 空）仍然成立，未动。

排序的一个事实：并集段内部按 bm25 原值排，**不是**按"命中了几个词"排——一条含稀有词的短记忆
可以排在含四个常见词的记忆之前（本机测出过）。这是 bm25 的 idf 与长度归一的正常结果，
不打算纠正，所以测试里只钉住"常见单词命中的那条排最后"，不钉 term-count 顺序。

一个要知道的触发频率：`wanted = min(limit or 100, 100)`，而召回入口是
`memory_recall.candidate_limit=100`、MCP 的 `limit=0 → None`，所以**千条级的库里 2b 几乎每次都会跑**
（严格 AND 很少能凑满 100 条）。这不是 bug，池子就是拿来填的：并集命中分数天然更低、排在后面，
真正限制宿主看到多少的是下游字节预算；额外成本 1–3 ms 已经量过。**未在本机验证的**：真实库（314 条）
里放宽实际会带进来多少低相关条目——那个库还是 schema 4、代码 7，普通连接直接 raise，
要量得先迁库（等用户点头才能做）。

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

- 送进 embedding 的文本是 `sanitize_text(summary + '\n' + body)`。
- embedding 只在本机运行，不新增出境 endpoint；`doctor` 报告本地模型缓存状态。
- 模型下载是显式用户动作；运行时设置 Hugging Face offline，缓存缺失时回退词法，不触发联网下载。

| 配置项 | 含义 | 默认 |
|---|---|---|
| `embedding_model` | 本地 sentence-transformers 模型名 | `all-MiniLM-L6-v2` |
| `embedding_dimensions` | 本地向量维度，用于 BLOB 长度校验 | `384` |

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
5. 关键词放宽通道 —— **已落地（2026-10-08）**：`memory.py` 的 2a/2b 两段 + `tests/test_recall_widening.py`（8 例）。
   变异验证过三处：禁掉 2a → 3 例红；禁掉 2b → 4 例红；让 2b 覆盖已有命中的标签 → 6 例红（含预注册的
   `test_recall_eval.py` 门槛，说明评测集对这个改动是敏感的）。
6. 语义候选通道 + 解释合并与通道标注
7. `reindex-embeddings` 命令 + doctor/status 可见性（含 egress 那行）
8. 评测：扩 `tests/recall_eval_cases.py` 加"语义近、字面远"用例，量化 5 与 6 各自的命中增量
   —— **这一步的前提已证伪**：`recall_eval_cases.py` 是 G1/G2/G3 的**迁移正确性**语料
   （标签只有 `right`/`wrong`，没有"同义换问法"这一维），不能拿来量放宽的召回增量。
   放宽段的证据是自建语料 + 现有门槛不回归；真正的"字面远"增量测量要等有语义通道或新语料再做。

顺序上 5 已交付；1、4 在通路确认之前**不做**（给没有写入方的列建迁移是负债），8 的前提已证伪。

---

## 10. Local model operation

- Model: `sentence-transformers/all-MiniLM-L6-v2`, 384 dimensions.
- Optional dependencies: `pip install -e ".[semantic]"`.
- Explicitly populate the local Hugging Face cache with
  `python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"`.
- Runtime loading is offline-only. A missing dependency or cache leaves embeddings NULL and
  lexical recall available; `forge doctor` reports model availability.
- Existing rows are not silently re-embedded. After installing the model, run
  `forge reindex-embeddings`; this updates only derived cache columns, not memory versions.
- The optional semantic evaluation skips when the model is unavailable. The current machine has
  no local model cache, so semantic quality still requires a cached-model evaluation run.
