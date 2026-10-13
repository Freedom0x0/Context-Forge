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

- [x] 三种 provider 配置下 doctor 输出正确，各有一条测试
- [x] remote 情形下 detail 里出现**具体的 provider 标识**，而不是笼统的"已配置"
- [x] doctor 该项在 none/offline 时判为 ok（不能把"没有出境"报成故障）
- [x] README/docs 里那句威胁模型与 doctor 输出一致
- [x] 无新增依赖、无新增配置项

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

## 实现记录（2026-09-29）

**落地形状**
- `gateways.egress_surface(settings) -> ('remote'|'local'|'none', destination)` 与
  `gateways.PROVIDER_NAMES`：provider 词汇表**只有一个出处**，doctor 与 status 都调它。
  这是 `spec/backend/database-guidelines.md` 里"派生只能有一份实现"教训的直接应用。
- `provider_http.resolve_remote_url(settings)`：把原先内联在 `build_remote` 里的 endpoint 优先级
  （settings 值 > 环境变量 > 宿主 settings.json > 默认）**提出来复用**，`build_remote` 改为调用它。
- `doctor.check_egress()`：remote → OK 但明说"IS SENT OUT to <endpoint>"；local → OK 且"不出机器"；
  none/offline/fake → OK"从不出境"；**provider 拼错（不在 PROVIDER_NAMES 里）→ 该行失败**，
  因为此时既不沉淀也不出境，而用户以为配好了。这条是"不可能失败的检查不是检查"的正面回答。
- `forge status` JSON 增加 `egress` 字段（同值）。**没有进 `render_text`**：那一行是 SessionStart 时打在
  输入框上方的，把 endpoint 常驻在状态行里是噪音；要看细节用 `--json` 或 `forge doctor`。
- README 配置段补一段威胁模型：出境面由 `model_provider` 决定，`none/offline/fake` 无出境，
  未来 embedding 若启用受同一约束（即 B⑤ / F 段⑤ 的那条）。

**证据**
- 新增 `tests/test_egress.py` 11 例：无配置=无出境、local 不出机器、remote 打印真实 endpoint、
  环境变量覆盖默认、settings 值优先于环境变量、默认 endpoint、**provider 拼错必须失败**、
  `build_remote` 仍尊重显式 url（重构回归）、status 字段、`run_all` 里两行都在。
  测试隔离 `CONTEXT_FORGE_HOME`、清空 `ANTHROPIC_*` 环境变量，并把 `Path.home` 与
  `_settings_env` 重定向到临时目录——不读真实的 `~/.claude/settings.json`（host-integration 的硬性要求）。
- 全量 `pytest`：**269 passed**。
- 变异验证三处：让 `check_egress` 不可能失败 → `1 failed`；status 去掉 `egress` 字段 → `1 failed`；
  把 endpoint 优先级反过来（环境变量盖过配置）→ `1 failed`；恢复后 `11 passed`。
