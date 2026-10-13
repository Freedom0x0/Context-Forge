# 通道标注接线与语义降级外显

## Goal

收掉 `10-08-hybrid-recall` 归档时记下的两条设计残余（design §5 / §7），使语义通道的
「失败」与「命中」两个事实都对外**可读**，而不是埋在中文解释串里或静默吞掉。

来源：`.trellis/tasks/archive/2026-10/10-08-hybrid-recall/prd.md` 的「已知残余（2026-10-13）」。

## Background

- 混合召回三条通道（BM25 全 AND → 关键词放宽 → 语义 top-K）已实现，语义通道的评分、
  身份过滤、失败计数、`text=max(词法,语义)` 均已在 `10-08` 收尾时补齐。
- 但设计里两条「对外可见性」要求没有落地：
  1. **查询期语义失败静默降级**（design §5）：`memory.py` 语义段是 `except Exception: pass`，
     语义通道查挂时无声退回纯词法，解释里看不出降级。
  2. **通道标注无消费方**（design §7）：`bm25`/`widened`/`semantic` 只拼进四段解释的中文串，
     没有任何报表聚合它，标注白存。

## Requirements

### R1 语义通道降级必须外显（design §5）

- 语义通道**已在配置中**（`self.embedder is not None`）但本次查询在语义段失败
  （encode 抛错 / 维度不符 / 解包异常）时：本次请求仍返回通道 1+2 的结果（不得因此报错或返回空），
  且**每条结果的解释里必须写明语义通道未启用**。
- 未配置 embedder（`self.embedder is None`）是**正常的纯词法模式**，不是失败 —— 不得因此加降级标记
  （保持既有行为，`search` 的纯词法断言不能被打上标记）。
- 降级不得改变结果集与排序（对照无 embedder 的同一查询）。

### R2 通道标注成为可消费的结构化事实（design §7 / §9 步 8）

- 命中通道从「只存在于解释串」提升为**结构化字段**：`ScoreBreakdown` 携带 `channel`（并保留两个原始分
  `lexical_score` / `semantic_score` 的可读性），随召回结果落进 `retrieval_events.explanation_json`，
  消费方无需解析中文串。
- 新增一个**极小的通道计数报表**，给标注一个真实消费方：按通道统计召回条数
  （`bm25` / `widened` / `semantic`），数据源为 `retrieval_events`。它同时补上 design §9 步骤 8
  被证伪后挂起的度量问题：「语义通道到底带进来多少条」。
- 报表**只做计数，不做比率**（样本不足时的纪律与 `bucket_report` 一致）；不新增 `signal_counts`
  的信号枚举（design §8 禁令）。

## Acceptance Criteria

- [x] AC1 语义段抛错时 `search_scored` 仍返回词法结果，且每条 `explanation` 含「语义通道未启用」标记。
      （测试：配一个 `encode` 会抛的假 embedder → 断言非空结果 + 标记存在）
- [x] AC2 未配 embedder 时结果解释里**不出现**该标记（纯词法模式不打降级标）。
- [x] AC3 降级前后结果 id 集合与顺序一致（降级不改结果）。
- [x] AC4 `ScoreBreakdown.channel` 是结构字段，取值 `bm25`/`widened`/`semantic`，与解释串一致。
- [x] AC5 召回事件落库后，通道计数报表按通道给出正确条数（构造 bm25 与 semantic 各一次召回）。
- [x] AC6 报表样本不足时不给比率；不存在 `signal_counts` 新增枚举。

## Out of Scope

- 不发明语义分的归一函数（design §7 留待评测数据）。
- 不做通道的**质量**度量（命中增量 / 相关性），只做**条数**计数。
- 不改 `bucket_report` 的既有分桶语义（粒度是「每条记忆」，通道是「每次召回」，两者不同轴）。
- 不动 `signal_counts` 桶语义（design §8）。

## Constraints

- 触及生产召回打分路径：改动后必须跑全量测试，且新增守卫需**变异验证**（破坏守卫 → 确认 RED → 还原）。
- 假 embedder 必须是**内容相关**的（同 `tests/test_embedding.py::HashingEmbedder` 的形状），
  不能像旧 `FakeEmbedder` 那样对所有输入返回同一向量。
- 不得操作真实生产库；测试一律用 `tmp_path`。

## 收尾记录（2026-10-13）

- 实现：trellis-implement；独立复核：trellis-check。六条 AC 全过。
- 变异验证：`except Exception: pass`（去标记）、`channels={}`、事件 payload 去 channel 各自使对应测试 RED；另复核 `channel_mix` 的 `NULLIF` 使空串渠道折进 `unknown`（去掉即 RED）。
- 全量：**447 passed**（`HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1`）。
- 环境注记：不带 offline 变量时 `tests/test_egress.py::test_egress_and_scheduled_are_wired_into_doctor` 会卡住——`doctor.run_all()` 含 `check_local_embedding()`，缺失本地模型时会去 HuggingFace hub 拉取并阻塞。这是环境/网络问题，与本次改动无关（该测试与 doctor 均未被本任务触碰）；offline 变量下该文件 12 passed / 23s。
- 残留（范围外，留档）：`legacy_filter=True` 回滚档仍不带通道标注与降级标记；其写出的空串渠道现由 `channel_mix` 折入 `unknown`。
