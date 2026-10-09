# 高危规则在召回时扣正文

召回遇到 `risk=high` 且没人批过的规则，只返回摘要和一个句柄，不返回正文。
人在终端批准（`forge approve`）之后，宿主再调 `context_forge_memory_confirm(token)` 换正文。
结论先说：这是一条**执行闸门**，不是保密闸门。它保证的是"没人读过的指令不会变成可执行文本"，
不保证"这条指令的内容对谁都是秘密"。

## Status

accepted — 2026-10-09

## 为什么只有沉淀闸门不够

沉淀闸门只管新沉淀那一条路。另外两条路它挡不住：

1. 闸门上线之前已经发布的高危规则还在库里，`active`，照常召回。
2. 人自己写进来的规则不经过闸门：`memory_import` 把 vault 里 `accepted` 的文件直接映射成
   `active`（memory_import.py:112），`MemoryLifecycle.revise`（memory_lifecycle.py:295）也是人改完就落新版本。
   写入门只会在沉淀时把高危规则按回 `candidate`，这两条路都不走沉淀。

所以判定放在读的一侧：看这条记录本身该不该被扣，而不是看它是谁、从哪条路写进来的。
两边共用同一个判断 `risk.requires_confirmation(draft)`，避免沉淀门和召回门对"什么叫高危"给出两个答案。

## 否决掉的四个方案

1. **只靠提示词提醒**。改动前召回的解释里就写着"高危指令：先向人确认，未确认不得执行"。
   这句话只能被读到，不能被强制——不理它的宿主照旧拿到可执行正文。所以这条是被替换掉的，不是被补充的。
2. **永久信封加密**。正文加密存放，批准只解一次。密钥和服务在同一台机器、同一个用户身份下，
   拿到进程权限的人就拿到密钥，对能读文件的宿主拦不住任何东西。
   代价却是实的：vault 里的投影文件不再是明文，人在 Obsidian 里只看到密文，
   还要新增一套密钥体系并把存量正文迁一遍。
3. **桌面弹窗当第二道确认**。MCP server 是被宿主拉起的 stdio 子进程，身后没有终端，弹不出来。
   要做就得新写跨平台通知并另起进程，而且它真的会打断人——F4 当时已经为"不打断"选过边。
   理由记录在 decisions.md 的 F4 段，这里不重复展开。
4. **批准按句柄记账**。那样每次换正文都要先有人答一次，同一条没改过的规则也会被反复拦。
   现在按 `(memory_id, version)` 记账：一个版本批一次，同版本的后续召回正常返回正文；
   改一个字就是新版本，重新待批。

## 扣掉哪些字段，留下哪些

留下的：`id`、`version`、`kind`、`role`、`status`、`evidence_level`、`summary`、
`body`（值固定是 `null`）、`confirmation_required`、`token`、`expires_at`、`note`。
按用途分就三类：这条记忆是谁（id/version）、它是什么类（kind/role/status/evidence_level）、
怎么换取（summary/body/confirmation_required/token/expires_at/note）。没有一条能当指令执行。

去掉的分两种原因：

会漏内容的——`body`、`sources`、`applicability`、`scope`、`explanation`。

- `sources` 里存的是原文摘录，沉淀路径下摘录经常就是那句话本身。
- `applicability` 和 `scope` 的条件串是人写的短句，一条"回滚生产库前"式的条件等于半句指令。
- `explanation` 会把命中的词和适用条件念出来。

没用又不该给的——`provenance`、`score`、`relations`、`expanded`。
`provenance` 带出项目名、session 和路径，路径本身是脱敏的关注面，而这条目现在不可用，讲清来源也没有落点；
`score` 是五个打分维度的数字，对一条还不能用的项目毫无意义；`relations` 和 `expanded` 是通往别的记忆的跳板，
留着只会诱导宿主绕去试 `context_forge_memory_get`——那条路同样扣正文（`get()` 和 `search()` 走同一个判断），
但少一个入口就少一次试探。

已知的漏：`summary` 是唯一可能带出指令内容的保留字段。风险判定读的是 `body`，不读 `summary`
（risk.py:59），而沉淀提示词把整个 draft 的 JSON schema 发给模型（provider_http.py:267），`summary` 就是其中一个可写字段。
所以模型把整句指令抄进 `summary` 是可能发生的，发生了就随召回漏出去，**而且现在没有任何检查会报**。
`note` 里写了"不要按摘要猜测正文"，那是对宿主要求，不是拦截。真要堵是把 `requires_confirmation` 的判定
同时读 `summary`，本轮没做。

## 重复换取为什么要计数，以及为什么两个门必须一样严

句柄一次性。第二次拿同一个句柄来换，服务端拒绝，同时把那行的 `replay_attempts` 加一，`forge doctor` 会打出来。
计数不为拦人，为看见：F3 写明"重复确认尝试本身即信号，提示宿主在自循环自动确认"。
不记的话，第二次尝试和第一次在库里长得一模一样，事后无从分辨。

召回放行和换取放行是同一条查询（`_approved_pairs`）。两边都要求 `approved_at` 有值**且** `approved_from_tty` 为真。
如果只让换取查 tty，一条手写进库的 `approved_at` 就能从召回这一侧溜过去。保证的强度由两个门里松的那个决定。

## 两条挡不住的路

1. 正文和来源摘录在本地是明文：`memory.db` 的 `snapshot_json`、vault 的投影文件，读到文件就读到内容。
   按问题 7 的"保险箱对内敞开"，这是现状，不是漏洞。
2. 有 shell 权限的宿主可以自己去跑 `forge approve`。挡法是抬高成本加可审计，不是挡住：
   不是真终端就拒绝、批准要回读挑战码、批准记录进台账（`approved_from_tty`、时间、原因）。
   真要查，看的是事后模式：高危规则秒批、且上下文里没有人类输入的迹象。

这两条决定了这条约束的价值边界：它改变的是"默认路径"，不是"能力上限"。

## 相关文档

API 事实不放这里：函数名、列名、schema 版本号、TTL 数值、扣字段后的响应形状，都在
`docs/memory-store.md` 的确认台账一节和 `docs/context-forge-usage.md`。
decisions.md 继续当工作日志，按时间追加，不搬。
