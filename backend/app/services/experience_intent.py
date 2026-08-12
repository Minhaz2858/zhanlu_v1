"""Rule-based intent classification for the agent experience layer.

Maps a user question to one of seven intent classes using keyword/regex
matching. Deterministic, O(1), zero LLM cost. Used by:

- Recipe learning (Layer 1): which tool sequence worked for this intent.
- Semantic response cache (Layer 2): cache key includes the intent class.
- User profile (Layer 3): which product/section the user cares about.

Priority order (tie-break): weekly_report > automation > comparison >
forecast > market_analysis > price_report > conversational > general.
Weekly report outranks everything — "draft weekly report with 对比 analysis"
is a weekly report, not a comparison. Automation outranks domain queries
so "sync ERP sales data with anomaly alerts" is an automation, not a
market analysis. Domain intents beat conversational so "你好，请问价格"
is a price report, not a greeting.
"""

from __future__ import annotations

import re
from typing import Optional

# --------------------------------------------------------------------------- #
# Intent classes (exported constants — use these everywhere else)
# --------------------------------------------------------------------------- #
INTENT_WEEKLY_REPORT = "weekly_report"
INTENT_PRICE_REPORT = "price_report"
INTENT_MARKET_ANALYSIS = "market_analysis"
INTENT_FORECAST = "forecast_question"
INTENT_COMPARISON = "comparison"
INTENT_CONVERSATIONAL = "conversational"
INTENT_AUTOMATION = "automation"
INTENT_GENERAL = "general"

INTENTS = (
    INTENT_WEEKLY_REPORT,
    INTENT_PRICE_REPORT,
    INTENT_MARKET_ANALYSIS,
    INTENT_FORECAST,
    INTENT_COMPARISON,
    INTENT_AUTOMATION,
    INTENT_CONVERSATIONAL,
    INTENT_GENERAL,
)

# Which intents are "answerable" enough to be worth caching (Layer 2).
# NOTE: weekly_report is deliberately NOT cached — reports must reflect
# the current week. The response cache freshness guards would need new
# semantics to handle it correctly.
CACHEABLE_INTENTS = (
    INTENT_PRICE_REPORT,
    INTENT_MARKET_ANALYSIS,
    INTENT_FORECAST,
    INTENT_COMPARISON,
    INTENT_AUTOMATION,
)

# --------------------------------------------------------------------------- #
# Keyword rules. Chinese keywords are substring matches; English keywords are
# word-boundary regexes so "hi" does not match "this".
# --------------------------------------------------------------------------- #
_RULES: list[tuple[str, list[str]]] = [
    (INTENT_WEEKLY_REPORT, [
        "周报", "周度", "周报告", "每周报告", "本周报告", "生成周报",
        "制作周报", "写周报", "周度报告", "weekly report", "weekly summary",
        "weekly review", "week report", "generate weekly", "draft weekly",
        "produce weekly",
    ]),
    (INTENT_COMPARISON, [
        "对比", "比较", "vs", "versus", "哪个", "哪個", "差异", "区别", "区别",
        "compare", "comparison", "which one",
    ]),
    (INTENT_FORECAST, [
        "预测", "预计", "预估", "未来", "会涨", "会跌", "涨还是跌", "走势预测",
        "forecast", "predict", "prediction", "outlook", "will price", "going up",
        "going down", "rise", "fall", "drop", "next week", "next month", "next quarter",
    ]),
    (INTENT_MARKET_ANALYSIS, [
        "市场分析", "行情分析", "市场", "走势", "供需", "供需情况", "基本面",
        "趋势", "分析一下", "market", "analysis", "analyze", "trend", "supply",
        "demand", "fundamental",
    ]),
    (INTENT_PRICE_REPORT, [
        "价格", "报价", "多少钱", "单价", "现价", "最新价", "行情", "成交价",
        "price", "quote", "quotation", "how much", "cost",
    ]),
    (INTENT_AUTOMATION, [
        "同步", "增量", "异常", "告警", "定时", "自动", "调度", "监控", "推送",
        "每天", "每周", "每小时", "定期", "巡检", "数据同步", "自动同步",
        "sync", "incremental", "anomaly", "alert", "schedule", "automate",
        "cron", "monitor", "notify", "notification", "daily", "hourly",
        "recurring", "periodic", "data sync", "auto sync", "incremental update",
    ]),
    (INTENT_CONVERSATIONAL, [
        "你好", "您好", "谢谢", "感谢", "再见", "你是谁", "你能做什么", "hello",
        "hi", "hey", "thanks", "thank you", "bye", "who are you", "what can you do",
    ]),
]

# Priority: lower number = higher priority (used as tie-break).
# Documented order: weekly_report > comparison > forecast > market_analysis >
# price_report > conversational > general. Weekly report outranks everything
# because "draft weekly report with 对比 analysis" should still route to the
# weekly report fast path, not to comparison.
_INTENT_PRIORITY = {
    INTENT_WEEKLY_REPORT: 0,
    INTENT_AUTOMATION: 1,
    INTENT_COMPARISON: 2,
    INTENT_FORECAST: 3,
    INTENT_MARKET_ANALYSIS: 4,
    INTENT_PRICE_REPORT: 5,
    INTENT_CONVERSATIONAL: 6,
    INTENT_GENERAL: 7,
}

# Precompile English word-boundary patterns (lowercased on match).
_COMPILED: list[tuple[str, list[re.Pattern]]] = []
for _intent, _kws in _RULES:
    _patterns = []
    for _kw in _kws:
        if re.search(r"[\u4e00-\u9fff]", _kw):
            # Chinese keyword — plain substring match.
            _patterns.append(re.compile(re.escape(_kw)))
        else:
            # English keyword — word-boundary, case-insensitive.
            _patterns.append(re.compile(rf"\b{re.escape(_kw.lower())}\b", re.IGNORECASE))
    _COMPILED.append((_intent, _patterns))


def classify_question(text: Optional[str]) -> str:
    """Classify a user question into one of the seven intent classes.

    Deterministic scoring: each intent scores the number of its keywords
    found in the text; the highest score wins with priority tie-break.
    Empty / unmatched text returns ``INTENT_GENERAL``.
    """
    if not text or not text.strip():
        return INTENT_GENERAL
    lowered = text.lower()

    scores: dict[str, int] = {}
    for intent, patterns in _COMPILED:
        count = 0
        for pat in patterns:
            if pat.search(lowered):
                count += 1
        scores[intent] = count

    best = INTENT_GENERAL
    best_score = 0
    best_priority = len(INTENTS)
    for intent, score in scores.items():
        if score > best_score or (
            score == best_score and score > 0 and _INTENT_PRIORITY[intent] < best_priority
        ):
            best = intent
            best_score = score
            best_priority = _INTENT_PRIORITY[intent]

    if best_score == 0:
        return INTENT_GENERAL
    return best
