# 决策记录索引

这里放**判断理由**：为什么不选另一条路、这条保证的边界在哪。
API 事实（函数名、列名、schema 版本、TTL 数值、响应形状）不放这里，放 `docs/memory-store.md` 和
`docs/context-forge-usage.md`。`.trellis/tasks/*/decisions.md` 是实施工作日志，按时间追加，两者不互相搬运。

| 编号 | 决定了什么 | 状态 |
|---|---|---|
| [0001](0001-withhold-unapproved-high-risk-rules.md) | 高危规则在召回时扣正文：覆盖范围、四个否决方案、扣哪些字段留哪些、重复换取为什么计数、两条挡不住的路 | accepted |
