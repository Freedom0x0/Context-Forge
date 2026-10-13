# 长期记忆机制改进决策脑暴

## Goal

对已识别的 9 个长期记忆机制不足逐题脑暴、逐题决策，结论持久化到 `decisions.md`，防止长对话压缩丢失。本任务产出的是**决策记录**，不是实现；每题定案后如需落地再拆子任务。

## Requirements

- 一次只讨论一个问题，用户确认后才写入 `decisions.md` 并进入下一题
- 每个决策必须附：理由、已知短板、依赖关系、状态与日期
- 决策可被后续推翻，但不回改历史条目，标注取代关系

## Acceptance Criteria

- [x] 问题 1（沉淀质量评估）定案：事后投票三层机制
- [x] 问题 2–9 逐题定案或明确挂起（见 `decisions.md` 各「状态」行）
- [x] `decisions.md` 完整反映所有讨论结论（含 2026-09-29 晚「跨题关联与实施分层」与 2026-09-30 F5 补丁）

## Notes

- 问题 1 的隐式票采集依赖问题 2 打通，二者顺序已在讨论中确认。
- 本任务产出为决策记录，非实现；各题的落地已拆成独立任务（3→hygiene-executor、
  4/9→risk-classifier、5→hybrid-recall、8b→per-db-instance-lock、8c→inferred-finalization-wiring、
  10→scheduled-run-ledger 等）。挂起项：问题 7 出库闸门（待云端立项）、问题 9 第 4 条（待用户选边）。
  2026-10-13 状态归一审核：产出完整，可归档。
