# 混合召回：词法放宽 + 可选语义通道

## Goal

让"换个问法就搜不到"这件事可测量地变少：BM25 全 AND + 关键词降级放宽 + 可选语义 top-K 三路并集，
全部汇入**现有的**边界过滤 → 五维打分 → 四段解释 → 字节预算。

设计见同目录 `design.md`（含本机实测数字与三条因实测而变的设计结论）。决策依据：`decisions.md` 问题 5。

## Requirements

摘自设计，实施时以 `design.md` 为准：

1. `memory_versions` 加 `embedding BLOB` + `embedding_model`，**派生缓存**（权威仍是 `snapshot_json`）；
   迁移走显式 `migrate()`，`SCHEMA_VERSION` 7 → 8，含在线备份与 `dry_run` 默认。
2. **降级只有一档**：无 numpy 或未配 embedding → 纯词法（现状行为）。**不做纯 Python 向量全扫**。
3. numpy 进 optional extra `semantic`，不进硬依赖。
4. **embedding 失败不影响沉淀**：该条留 NULL、写入照常成功、失败计数进 doctor；
   查询期异常 → 退回词法并在解释里明说未启用。
5. 语义通路出境 = `sanitize_text` 后文本，且 `doctor.check_egress()` 必须把 embedding endpoint 一并列出。
6. 每条结果的解释标注命中通道（`bm25` / `widened` / `semantic`）与两个原始分数。
7. 步骤顺序把**不依赖外部通路的部分先做完**：schema/打包 → 关键词放宽 → 评测 → 再谈语义通道（§9）。

## Acceptance Criteria

- [ ] BLOB 打包/解包 + 维度不符判为失效行：纯函数单测，无需 provider 也能跑
- [ ] v7 → v8 迁移：`dry_run` 不改字节；实跑后备份可恢复；普通连接打开旧库仍 raise
- [x] 关键词放宽：2a 剔除 `df=0` 的词 + 2b 词并集兜底（`WIDEN_RESULT_CAP=100`），命中带来源标签，
      边界过滤不绕。**注意**：原先计划用 `tests/recall_eval_cases.py` 量"字面远、语义近"的命中增量，
      实测该语料是迁移正确性（`right`/`wrong`）标注，没有换问法这一维，量不了放宽收益；
      改为自建语料 8 例 + 现有评测门槛不回归 + 三处变异验证。
- [x] 单 token 查询不放宽；已有高精度命中不被放宽标签覆盖
- [ ] 未配 embedding / 无 numpy 时全部测试通过且行为等于现状（**语义通道不得成为任何现有测试的前提**）
- [ ] provider 挂掉时沉淀仍然成功（断言 receipt 状态与 NULL 向量），失败计数在 doctor 可见
- [ ] `doctor` 的 egress 行同时列出提取与 embedding 两条通路
- [ ] 解释里能看到通道与两个分数；五维 `text` 取 `max(词法, 语义)`
- [ ] 全量测试绿

## Blocked / 待验证（2026-10-08 已探测，事实见 `design.md` §10）

- 本机无 embedding 进程：`ollama` 不在 PATH、`127.0.0.1:11434` 超时 → **语义通道在这台机器上一次调用都跑不起来**
- 提取通路是 Anthropic 形状，而 Anthropic 无 embeddings 产品（官方文档把嵌入指向第三方；
  原文本机区域封锁，此条记为"用户告知"而非我验证）
- **本地模型路线已确定**：默认使用 `all-MiniLM-L6-v2`，384 维，`sentence-transformers` 通过
  `.[semantic]` 安装；模型只从本地缓存加载，绝不联网下载。未安装/未缓存时向量通道失败，
  写入继续成功并回退到词法检索。
- 历史记忆不自动重算；安装并缓存模型后显式运行 `forge reindex-embeddings`。

## Out of Scope

sqlite-vec / 独立向量服务 / HNSW；rerank；LLM 查询改写；自动回填历史向量；候选批量解包优化（第 4 层）。
