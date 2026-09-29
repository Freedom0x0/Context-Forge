# 持续学习研究与评估协议
## 研究问题
跨任务保存有依据的理解，是否改善后续决策？修正机制是否降低错误延续和过度泛化？收益是否值得额外延迟与成本？
本文件定义评估方法，不预设系统一定优于基线，不使用单一用户叙述作为架构依据。

## 对照
相同模型、任务输入、工具权限和预算下比较：无持久记忆、平铺检索记忆、带证据/条件/修正的记忆。
构建覆盖重复任务、情境变化、反证、矛盾来源、无关信息、用户偏好变更和作用域隔离的数据集。学习阶段与评估阶段分离，避免测试答案进入记忆。
数据由明确可检查的任务契约或工具结果给出判定依据；无法客观判定的结果标记未定，不用被测模型自评充当唯一裁判。

## 指标
- 任务决策正确率和已知错误复发率。
- 条件不适用时误用率、反证后修正率、跨范围泄露率。
- 不必要提问率与关键缺失条件的漏问率。
- 来源追溯完整性、无依据升级可信度次数。
- 查询/学习延迟、模型调用数、token 和存储增长。
逐指标报告分母、样本数、模型/提示词版本、运行次数和结果分布。先建立基线再记录数值发布门槛，不凭空规定收益比例。

## 验证分层
确定性测试覆盖事务、幂等、版本、关系、检索过滤、预算与恢复。
模型评估覆盖观察/推断区分、反证处理、条件应用和澄清选择，采用多次运行。
端到端测试验证宿主确实在需要时查询，并能可靠记录实际返回/呈现/动作结果，缺少接入视为闭环未完成。
结果不优于基线时缩小自动更新范围，保留已可靠的持久化和检索能力；不通过添加更多模型自评掩盖问题。

## Deterministic integration baseline

The local deterministic integration suite now covers one session flowing through the worker into `memory.db`, a derived rule relation, a human correction, subsequent scoped recall, and a read-only merge report. It also covers retry replay: the same session does not create a second memory version. This is a plumbing baseline, not evidence of model-quality improvement.

Observed result: `98 passed` for the full repository suite on 2026-09-22 with the declared dependencies and `watchdog` installed. Model-quality metrics remain unmeasured because no controlled multi-run task dataset has been collected. Do not enable more aggressive automatic updates based on this result alone.
