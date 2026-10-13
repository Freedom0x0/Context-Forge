"""Small bilingual corpus for semantic retrieval smoke evaluations.

Each case deliberately uses a query whose wording differs from the relevant
memory.  ``distractors`` are lexically attractive but semantically unrelated.
The corpus is intentionally checked in so changes to an embedding model can be
compared repeatably without relying on production memories.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SemanticCase:
    name: str
    query: str
    positive: str
    distractors: tuple[str, ...]


CASES: tuple[SemanticCase, ...] = (
    SemanticCase(
        "cache-invalidation",
        "How do we make old browser data disappear after a settings change?",
        "Invalidate the client cache whenever the user changes account settings.",
        ("Cache API responses for five minutes to reduce server load.",
         "The settings page uses a two-column responsive layout."),
    ),
    SemanticCase(
        "transaction-retry",
        "数据库写入失败时，应该怎样避免重复提交？",
        "写入超时后使用幂等键重试，确保同一个请求只产生一条记录。",
        ("数据库连接池应限制最大连接数。", "提交表单前显示字段校验错误。"),
    ),
    SemanticCase(
        "access-boundary",
        "Which rule keeps private project notes out of another team's results?",
        "Private memories are visible only to their owner and are filtered at the access boundary.",
        ("Rank memories by freshness after full-text search.",
         "Use a team-wide dashboard to share project metrics."),
    ),
    SemanticCase(
        "tool-timeout",
        "工具调用一直没有返回，监控应该何时认为它已经失效？",
        "只有进程已退出、没有活动工具并且静默期稳定结束，才能最终化会话。",
        ("工具结果需要记录到审计日志。", "增加网络客户端的连接超时时间。"),
    ),
)

