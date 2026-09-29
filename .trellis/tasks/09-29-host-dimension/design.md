# Design — host 维度打通

## 规范符合性（先说这条，因为它决定迁移写法）

`.trellis/spec/backend/database-guidelines.md` 把两个库分权：`memory.db` 是**权威库**，
"绝不在打开普通连接时隐式迁移"；`index-*.db` / **`queue.db` 是派生库**（可重建、永不权威）。

`session_episodes` 在 **queue.db**，且 `queue._migrate()`（`queue.py:107-122`）本来就是
"PRAGMA 查列 → `ALTER TABLE ADD COLUMN`"的隐式迁移模式 → **本任务沿用该模式合规**。
（对照：T0.1 的 risk 列在 memory.db，必须走显式 `migrate()`，形状不同，勿互相照抄。）

host 不是 `snapshot_json` 里的数据（episode 属于派生库，与记忆快照无关），不触发双真相规则。

## 关键实现约束（易错点）

`save_episode` 用的是**位置式插入**：`INSERT INTO session_episodes VALUES (:session_id, ... :retention_deadline)`
（`queue.py:196-207`），**没有列名清单**。因此新列必须：

1. 在 CREATE 语句里**追加在最后**（`retention_deadline` 之后）；
2. 老库经 `ALTER TABLE ADD COLUMN` 添加时，SQLite 同样把新列**放在最后**；
3. INSERT 的参数列表在末尾追加 `:host`，`ON CONFLICT DO UPDATE` 加 `host=excluded.host`。

这样"新建库"与"迁移库"的列序**必然一致**。若把 host 插在中间，老库会错位且不报错——
静默写坏数据，属于本仓规范里反复警告的那类 silent failure。

## 改动面

| 文件 | 改动 |
|---|---|
| `domain.py:170` | `EpisodeRecord` 增加 `host: str = 'claude_code'` |
| `queue.py:71-79` | CREATE `session_episodes` 末尾加 `host TEXT NOT NULL DEFAULT 'claude_code'` |
| `queue.py:107-122` | `_migrate()` 增加 `session_episodes` 的 host 列检查 + ALTER |
| `queue.py:193-208` | `save_episode` 的 VALUES 末尾加 `:host`，DO UPDATE 加 `host=excluded.host` |
| `queue.py` | 新增 `host_counts() -> dict[str, int]`：`SELECT host, COUNT(*) FROM session_episodes GROUP BY host` |
| `episodes.py:23` | `collect(...)` 增加**必填** `host` 参数（不给默认值，避免把"谁该传"这件事变模糊），写入 `EpisodeRecord` |
| `watcher.py` | 新增模块常量 `HOST = "claude_code"`（紧贴 `transcript_root()`，即 8a 里"每家宿主一个适配器"的接缝），`collect()` 传入 |
| `hooks/claude_code.py:58` | `collect(..., host=event.source)`（source 已是该事件字段） |
| `cli.py:76`（`forge event ingest`） | `collect(..., host=event.source)` |
| `status.py` | 返回体加 `"episodes_by_host": "claude_code=42"`（保持 `dict[str, int \| str]` 的扁平类型，不嵌套） |

必填参数会波及三个调用点，全部一并改；不给 `host` 默认值是有意的——8a 的验收标准是
"能自动产出 episode"，把 host 写成必填能让"接入新宿主必须显式声明自己是谁"变成编译期就漏不掉的事。

## 测试计划

1. `collect(..., host="codex")` → `latest_episode(...).host == "codex"`（新采集路径）。
2. **迁移测试**：手工建一张**不含 host 列**的旧形状 `session_episodes` 并插一行，重开 `JobStore`
   → 断言列已存在、老行 `host == 'claude_code'`、`save_episode` 仍能 upsert。
3. **列序测试**（守位置式插入的雷）：`PRAGMA table_info(session_episodes)` 断言 `host` 在最后一位，
   新建库与迁移库两条路径都断言。
4. `host_counts()` 按 host 分组正确（两个 host 各若干条）。
5. `status.collect(store)` 输出含 `episodes_by_host` 且值为 `host=n` 形式。
6. 全量 pytest 绿。

## 回滚

queue.db 是派生库：删掉 `queue.db` 重建即可，不触碰权威数据。代码层回滚即 revert 本提交，
无数据损失（多出来的列在被删库时随之消失）。

## 不做

第二家宿主适配器、`transcript_root()` 多 root 配置化、host 枚举白名单、完整"沉默报警"报表（第 2 层）。
