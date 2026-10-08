# 采集解析失败计数：补齐沉默报警的另一半

## Goal

`8a.3` 要求"每 host 最近一次成功投料时间 **＋ 解析失败次数**"。时间维度已由
`10-05-vote-report-buckets` 交付（`doctor.check_host_silence`），失败计数**目前没有数据可报**。

## 实测缺口（2026-09-30）

`watcher.scan_once`（`watcher.py:120-141`）对这些情况是**静默 `continue`**，不留任何痕迹：

- `transcript.stat()` 抛 `OSError`
- `read_session_meta(transcript)` 返回 `None`（读不出 cwd/project）
- 文件没有完整行（`count_complete_lines` 为 0）

因此"某家宿主升级后换了 transcript 格式"这种失效，今天只能表现为"该 host 突然没有新 episode"，
和"用户不用了"无法区分。

## Requirements

1. 失败按 `(host, reason 类别)` 计数并落库（派生库 queue.db 是合理归属，与 `scheduled_runs` 同层）
2. 类别要稳定（例如 `unreadable` / `no-meta` / `empty`），不要把异常原文当维度，否则桶会碎
3. `doctor` 与 `memory-report` 能读出"该 host 近期失败次数"，与沉默时长并排呈现
4. 采集不得改变现有跳过语义：失败仍然跳过该文件，只是**留痕**

## Acceptance Criteria

- [ ] 制造一份无 meta 的 transcript → 跑一轮 → 计数 +1，且该文件仍不被沉淀
- [ ] 计数按 host 与类别分桶，跨轮累积、可被报表读取
- [ ] `doctor` 在"有失败计数"时能点名 host 与类别（可失败，不是只读展示）
- [ ] 全量测试绿

## Out of Scope

- 自动重试或格式猜测
- 第二家宿主的适配器（8a 已定下迭代）

## Notes

- 决策依据见 `decisions.md` 的 **问题 8a 第 3 条**与其实施记录里对该缺口的标注。
