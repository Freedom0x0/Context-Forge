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
| 放宽段的额外 SQL（400 条 / 60 词库，本机） | df 计数 3–6 词 **0.4–6.4 ms**、AND **0.2–1.7 ms**、并集+LIMIT 100 **1.1–1.3 ms**；单次 `search_scored` 全程 28–50 ms，**大头是逐条读记录，不是放宽** |

**并更正决策文件里的一处错误**：问题 5 写的"1 万条暴力扫描 1–5ms"是**未实测的估计**。实测：纯 Python **1448 ms**，numpy **11.9 ms**。数量级差了近 300 倍。

---

## 2. 三件因实测而变的设计结论

1. **numpy 进 optional extra，向量通道是可选件**：`semantic = ["numpy>=1.26"]`。不把它变成硬依赖，否则 `pip install context-forge` 就背离 local-first 的轻量安装。
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

- 送进 embedding 的文本 = `sanitize_text(body + ' ' + summary)`，与提取模型同一份脱敏、同一套三态；
- `doctor.check_egress()` 现在只报提取通路 → 本任务**必须把 embedding endpoint 一并列出**，
  否则等于新开一条没人看见的出境路；
- embedding 未配置 → 出境面不变（不多一条路）。

**配置契约（2026-10-08 定，因为 Anthropic 根本没有 embeddings 产品）**：
embedding 通路**绝不复用**提取模型的 `ANTHROPIC_*` / `remote_api_url` 配置——沿用同一条配置去猜
embedding 端点，只会得到一个 404 或者打错端点。单开三项，与提取通路彼此独立：

| 配置项 | 含义 | 默认 |
|---|---|---|
| `embedding_base_url` | OpenAI 兼容的 `/v1/embeddings` 所在基址 | 空 = 语义通道关闭 |
| `embedding_api_key` | 该基址的密钥，**只走环境变量或本地配置，不入库、不进 Git**（同提取通路那条约束）；本地 Ollama 可空 | 空 |
| `embedding_model` | 模型名，同时决定向量维度 | 空 |
| `embedding_dimensions` | **请求的维度**（Voyage-4 可选 256/512/1024/2048）——同模型可选维度，所以维度不能只由模型名推出；§3 的 BLOB 长度失效校验需要它做对照 | 空 = 用模型默认 |

按这个形状就能一套代码四种落地（详见 §10 的已验证端点）：OpenAI 远端、本地 Ollama（**不出境**）、
Voyage、以及任何 OpenAI 兼容网关。**差异只允许出现在请求体的附加字段上**（如 Voyage 的 `input_type`），
响应解析统一走 `data[i].embedding`，不为某个 provider 分叉整条管道。

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

## 10. 端点事实（2026-10-08 本机探测 + 官方文档核对）

**本机现状（实测，不是估计）**

- `ollama` 不在 PATH；`http://127.0.0.1:11434/api/tags` **连接超时** → 本地无 embedding 进程。
- 提取通路是 Anthropic 形状（`provider_http.py`：`x-api-key` 头、默认基址 `api.anthropic.com`）。
- Anthropic **自己没有 embeddings 端点**，官方文档把嵌入这件事指向第三方（用户告知：首选 Voyage AI）。
  ⚠️ 我没能读到原文——`docs.anthropic.com/.../embeddings` 重定向后被区域封锁
  （307 → `anthropic.com/app-unavailable-in-region`）。所以这条按"用户提供的信息"记录，**不是我验证的**。

**第三方端点（今日读官方文档核过，✅ = 文档原文确认）**

| provider | 端点 | 请求字段 | 响应 | 备注 |
|---|---|---|---|---|
| Ollama 原生 | ✅ `POST /api/embed` | `model`, `input` | ✅ `embeddings` | 本地跑 = **不出境**，是最省心的默认路 |
| Ollama 兼容层 | ✅ `POST /v1/embeddings` | 未逐字段核对 | — | 与 OpenAI 同形状 |
| Voyage | ✅ `POST https://api.voyageai.com/v1/embeddings` | ✅ `input`, `model`, `input_type` | ✅ `data[].embedding` 未解析过 | 比 OpenAI 多一个 `input_type`；模型如 `voyage-4` |
| OpenAI | ⚠️ 未在本机/文档逐字段核对 | 行业事实是 `input`/`model` | ⚠️ | 换 provider 时的默认假设 |

**仍未验证，不得当事实写进代码**：各家的批量上限、向量维度、错误体形状，以及 `data[i].embedding`
这个解析在 Voyage 与 OpenAI 上是否真的完全一致。所以接通路的第一步**不是写代码**，是一次**只读探针**：
拿一条与真实记忆无关的假字符串打一次 `/v1/embeddings`，把维度与响应字段名抄下来，再决定解析层。
探针不写库、不迁移、不碰真实数据。

**2026-10-08 补：路线定为远端 Voyage（用户不装 Ollama）**，核到的官方文档数字：

| 项 | 数字 | 对本设计的影响 |
|---|---|---|
| 端点 | `POST https://api.voyageai.com/v1/embeddings` | 与我们钉的 OpenAI 兼容形状一致 |
| 请求字段 | `input`, `model`, `input_type`（可为 `null`/`query`/`document`） | **写入用 `document`、查询用 `query`**，同一份代码差一个字段——正是 §6 说的"差异只在请求体附加字段" |
| 批量上限 | **1,000 条文本/请求** | `reindex-embeddings` 回填要分批，314 条历史一次就够 |
| 维度 | Voyage-4 系列可选 **256/512/1024/2048** | ⚠️ **必须新增第四项配置或把维度写死在探针结论里**：§3 定的是"维度不进 BLOB、由 `embedding_model` 决定"，而同一模型可选维度 → 没有登记的维度就没法做长度失效校验 |
| 响应 | 文档同时出现 `embeddings` 与 `total_tokens`，OpenAI 形状是 `data[i].embedding` | **正是探针要解决的那件事**：SDK 属性名 ≠ 原始 JSON 字段名，不能猜 |
| 价格 | 每百万 token **$0.02–$0.18**，免费额度 **50M–200M token** | 314 条全量回填约 10–20 万 token → **不到一美分**；一万条量级仍在免费额度内 |
| 归属 | ⚠️ 纠正一处听起来像事实的错误：Anthropic 文档推荐 Voyage **不等于** Anthropic 拥有 Voyage | 2025-02 报道是 **MongoDB 以 2.2 亿美元收购 Voyage**（新闻源，非官方文档）；别在文档里写"Anthropic 的 embedding 服务" |

一次边界后果要用户明确授权：接 Voyage = **出境面从 1 条变成 2 条**（提取通路之外多一份"记忆正文的向量请求"）。
红线次序不变：`doctor.check_egress()` 先把 Voyage 列出来 → 只读探针 → 才谈 schema 与写入。
