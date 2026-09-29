# 出境面可见：doctor 明示正文发往哪个 provider

## Goal

问题 9 的威胁模型（"唯一真实外泄面是把文本发给远端模型 API 那一次"）要成立，前提必须是
**用户能看见自己有没有这条出境路径**。今天 `forge doctor` 不报这件事。

依据：`decisions.md` 的 **问题 9 威胁模型**、**B⑤ / F 段⑤**。

## 背景与事实（已 grep 核实）

- 出境点真实存在且已在运行：`gateways.py:45` 与 `provider_http.py:200-201` 都调
  `sanitize_transcript` 后送模型；`finalization.py:33`、`worker.py:103` 同理。
- 三态 provider 配置已有：`config.py:13` `processing_mode = "redacted_model"`，
  gateway 走 `select_gateway(provider, settings)`，provider 可为 `none` / offline / remote。
- 现有检查形状可照抄：`doctor.check_watcher()`（`doctor.py:107`）返回
  `(name, ok, detail)` 三元组，汇入 `doctor.render()` 的行表。

## Requirements

1. `doctor` 增加一项 `egress-surface`：
   - provider = remote → 明示"记忆正文（脱敏后）将发送至 `<provider/base_url>`"；
   - provider = none / offline → 报"无出境通路"。
2. `forge status`（若已有汇总面）同步这一行，避免只有 doctor 看得见。
3. 文档同步一句：出境面 = 任何 remote 通路，**包含未来的 embedding 调用**（B⑤ 的更正）。

## Acceptance Criteria

- [ ] 三种 provider 配置下 doctor 输出正确，各有一条测试
- [ ] remote 情形下 detail 里出现**具体的 provider 标识**，而不是笼统的"已配置"
- [ ] doctor 该项在 none/offline 时判为 ok（不能把"没有出境"报成故障）
- [ ] README/docs 里那句威胁模型与 doctor 输出一致
- [ ] 无新增依赖、无新增配置项

## Out of Scope

- 不改 `sanitize_text` 的任何行为（规则扩充属问题 9 的实施任务，另立）
- 不实现 embedding（属第 3 层召回任务）
- **T0.4b 转为约束条款**：embedding 落地时必须遵守——输入取 `sanitize_text` 之后的文本，
  embedding provider 复用同一套 none/offline/remote 三态，并纳入本任务的出境明示。
  这条现无代码可改，已记入 `decisions.md`。

## Dependencies

- 前置：无
- 被依赖：第 3 层 embedding 任务（受 T0.4b 约束）

## Notes

- 判断标准来自 8a 与问题 4 的同一条取向：**能观测的才算安全**。这一项不提升脱敏强度，
  只让既有的风险等级变得可见（它由 provider 配置决定，而不是由正则强弱决定）。
