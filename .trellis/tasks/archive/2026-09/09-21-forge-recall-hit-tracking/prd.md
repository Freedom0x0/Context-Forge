# 按需检索、排序与曝光
## Goal
按需检索、排序与曝光，遵循父任务自动运行、人工可纠正的总协议。
父任务：.trellis/tasks/09-21-forge-recall-rank-and-merge
## Dependencies
09-21-forge-memory-store；联调依赖 09-21-forge-memory-lifecycle。
## Requirements
数据库按需 search/get，项目/状态/路径过滤后全文候选，稳定排序，默认 8＋unlimited＋字节预算。
兼容旧 RuleMatcher/MCP 字段和 glob 语义，不增加写 rule confidence 的旁支。
曝光旁路失败不改变已计算结果；反馈读取失败降级并标明依据缺失，权威库读失败不可伪装成功。
事件区分 retrieved/tool_returned/presented；显式反馈按事件追加，来源导入幂等。
正负反馈平票保守中性，明确纠正优先；曝光不升权。
## Acceptance Criteria
- [ ] 保存规则匹配基线与隔离 fixture 的正常、边界及故障测试通过。
- [ ] 中文/ASCII 检索、短查询、安全 MATCH、BM25/范围过滤 的正常、边界及故障测试通过。
- [ ] MCP search/get、旧接口适配、事件与来源反馈 的正常、边界及故障测试通过。
- [ ] 项目隔离、旧版本/抑制过滤、故障降级、稳定性 的正常、边界及故障测试通过。
- [ ] 真实 rule-k1 清理仅列影响范围，删除另行授权 的正常、边界及故障测试通过。
- [ ] python -m pytest tests/ -q 通过。
## Out of Scope
模型参数训练、专用向量服务、强制人工审批。

## 持续学习定位下的职责
检索后展开相关限制与冲突，逐节点过滤作用域。返回理解、条件、证据及未决问题。宿主接入决策前检索和可得结果回传，明确采用/不采用/验证/澄清。
### 新增验收
- [ ] 预算不足不裸返无条件结论；不重复询问明确意图；曝光和无归因测试成功不被标为有效学习。
