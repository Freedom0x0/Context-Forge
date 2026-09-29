# 长期记忆数据库与迁移
## Goal
长期记忆数据库与迁移，遵循父任务自动运行、人工可纠正的总协议。
父任务：.trellis/tasks/09-21-forge-recall-rank-and-merge
## Dependencies
无。
## Requirements
MemoryStore、schema 迁移、版本/来源/事件/关系/作用域、中文 FTS5、在线备份恢复和 Markdown 导出。
读工具不因查询创建或迁移数据库；数据库不可读明确诊断，不静默视为空库。
幂等导入旧 vault/反馈，保留 user_owned 与禁用状态，unknown 项目隔离。
## Acceptance Criteria
- [x] schema/约束/版本竞争和故障回滚 的正常、边界及故障测试通过。
- [x] FTS token 化、安全查询与重建 的正常、边界及故障测试通过。
- [x] 导入 dry-run/重复导入/旧快照保护 的正常、边界及故障测试通过。
- [x] WAL 备份恢复与单向导出 的正常、边界及故障测试通过。
- [x] python -m pytest tests/ -q 通过。
## Out of Scope
模型参数训练、专用向量服务、强制人工审批。

## 持续学习定位下的职责
来源、版本、认知角色、适用条件和版本化证据关系。条件 unknown 与 false 分开；索引派生可重建，权威库备份可恢复。
### 新增验收
- [x] 关系/条件事务一致；重复来源不会成为多份独立证据；旧版本与来源可追溯。
