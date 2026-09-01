"""Helpers for detecting data-query intent (used by FSM routing override)."""
from __future__ import annotations

import re

# Data tools that mark an agent as data-capable
_DATA_TOOL_NAMES = frozenset({
    "ask_data_agent",
    "execute_query",
    "execute_sql",
    "sql_query",
    "ask_erp_kpi",
})


def _agent_has_data_tool(agent_tool_names: set[str]) -> bool:
    """True if the agent's tool set contains any data-producing tool."""
    return bool(agent_tool_names & _DATA_TOOL_NAMES)


# English + Chinese data-query keywords — conservative set that
# unambiguously signals "I want data, not a chat" intent.
_DATA_QUERY_KEYWORDS = (
    # English — sales / supply chain / inventory / reports
    "sales report", "supply chain", "supply-chain",
    "revenue", "gross margin", "margin", "inventory",
    "top ", "rank", "compare", "breakdown", "trend",
    "give me", "show me", "tell me",
    "by product", "by customer", "by month", "by quarter",
    "last 30 days", "last 7 days", "last quarter", "month over month",
    "month-over-month", "yoy", "qoq", "vs last",
    "kpi", "metric", "volume", "outbound",
    "data for", "data of", "sales data", "inventory data",
    # Chinese
    "销售报告", "销售数据", "销售报表", "营收", "营业收入",
    "供应链", "库存", "销量", "毛利率", "毛利",
    "排名", "前几", "排行", "对比", "环比", "同比",
    "明细", "汇总", "清单",
    "给我", "看看",
    "上个月", "过去", "最近",
    "产品收入", "客户收入", "月度",
)

# Strong data intent regex — at least one keyword present
_DATA_INTENT_RE = re.compile(
    r"(?:"
    r"\b(?:sales|supply|revenue|inventory|top|rank|compare|breakdown|trend|"
    r"kpi|metric|volume|outbound|margin|product|customer|month|quarter|"
    r"july|june|august|september|october|november|december|january|"
    r"february|march|april|may|2024|2025|2026|2027|2028|last|past)"
    r"\b\s+[\w\s\-]+)"
    r"|"
    r"\b(?:give|show|tell)\s+me\b"
    r"|"
    r"\b(?:data|report|analysis|summary|overview|breakdown)\b",
    re.IGNORECASE,
)


def _is_unambiguous_data_query(user_content: str) -> bool:
    """True when the user message is unambiguously a data query.

    Conservative guard:
    - Length ≥ 4 words AND (matches regex OR contains a keyword), OR
    - Length ≥ 8 words AND looks like a data question (numeric / date terms)

    Greetings and short non-data prompts ("hi", "thanks") always return False
    via the upstream ``_is_non_data_intent`` check.
    """
    if not user_content:
        return False
    text = user_content.strip()
    words = text.split()
    if len(words) < 3:
        return False
    lowered = text.lower()
    if any(kw in lowered for kw in _DATA_QUERY_KEYWORDS):
        return True
    if _DATA_INTENT_RE.search(text):
        return True
    return False