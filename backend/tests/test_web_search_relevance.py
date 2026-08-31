"""Relevance filter for web_search results (2026-08-31).

Regression test for the poisoned-SERP failure: the C5/C9 market deck's
web_search returned 200 OK with irrelevant rows (C5驾驶证 / CSGO item
markets / military transport plane) for a C5/C9 petroleum resin query.
The agent trusted them, wasted a turn, and fell back to internal data —
the "not informative vs Kimi/Claude" gap. ``_filter_relevant`` must drop
junk so a provider that returns garbage is treated as empty instead of
poisoning the agent's context.
"""

from app.services.tool_handlers.web_search_tool import (
    _filter_relevant,
    _significant_tokens,
)

# The ACTUAL results the C5/C9 deck turn received (Bing, 2026-08-31).
_JUNK_EN = [
    {"title": "C5驾驶证_百度百科", "url": "https://baike.baidu.com/...", "description": "C5驾驶证，中华人民共和国机动车驾驶证的一种类型。"},
    {"title": "C5 Game 评测：手续费、支付、KYC 及安全性 | CS2.IO", "url": "https://cs2.io/zh-CN/markets/c5", "description": "C5 Game 靠谱吗？C5 Game 是真实运营的平台。"},
    {"title": "c5平台现在靠谱吗【csgo吧】_百度贴吧", "url": "https://tieba.baidu.com/p/8977701030", "description": "c5平台现在靠谱吗..本人新手，刚在c5买了把刀。"},
    {"title": "C5GAME_百度百科", "url": "https://baike.baidu.com/item/C5GAME/61308749", "description": "C5开始执行交易收费，成为国内第一家针对道具交易的收费平台。"},
    {"title": "C5运输机有多大？与C17放一起比才明白，为何称超大型", "url": "https://news.qq.com/rain/a/20241030A03XCX00", "description": "C5“银河”重型战略运输机与安124对比。"},
    {"title": "90秒认识C5GAME，从此交易不迷路！_哔哩哔哩", "url": "https://www.bilibili.com/video/...", "description": "CSGO饰品交易就来c5game.com。"},
]

_JUNK_ZH = [
    {"title": "裂解反应_百度百科", "url": "https://baike.baidu.com/...", "description": "广义地说，凡是有机化合物在高温下分子发生分解的反应过程都称为裂解。"},
    {"title": "什么是石油的裂解？与裂化有何区别？ - 知乎", "url": "https://zhuanlan.zhihu.com/p/...", "description": "裂解和裂化一样都是在高温下，将大分子转化为小分子。"},
    {"title": "裂解和热解的区别", "url": "https://cp.baidu.com/landing/...", "description": "裂解和热解虽然都是将大分子物质转化为小分子物质的过程。"},
    {"title": "石油裂解_百度百科", "url": "https://baike.baidu.com/item/...", "description": "燃料油由导管喷入炉内燃烧把反应管加热至900℃左右。"},
    {"title": "裂化与裂解有何区别？ - 知乎", "url": "https://www.zhihu.com/question/464761643", "description": "裂解就是把大分子变成一部分更小的分子。"},
]

_GOOD_EN = [
    {"title": "C5 C9 Petroleum Resin Market Size Report 2030 | Grand View Research", "url": "https://www.grandviewresearch.com/...", "description": "The global C5/C9 petroleum resin market size was estimated at USD 3.2 billion in 2025 and is projected to grow at a CAGR of 5.8%."},
    {"title": "C9 Hydrocarbon Resin Market - Global Forecast 2026 | MarketsandMarkets", "url": "https://www.marketsandmarkets.com/...", "description": "C9 hydrocarbon resin demand is driven by adhesive and rubber industries in China."},
    {"title": "C5 Resin Market Analysis, Size & Forecast | ChemAnalyst", "url": "https://www.chemanalyst.com/...", "description": "C5 petroleum resin price trends and capacity outlook for 2026."},
]

_GOOD_ZH = [
    {"title": "碳五石油树脂价格行情_生意社", "url": "https://www.100ppi.com/...", "description": "碳五石油树脂价格最新报价，2026年8月市场行情走势分析。"},
    {"title": "裂解碳五市场价格 2026 - 百川盈孚", "url": "https://www.baiinfo.com/...", "description": "裂解碳五市场报价、供需分析、装置动态。"},
]


def test_significant_tokens_c5_query():
    toks = _significant_tokens("C5 C9 petroleum resin market price 2026 trends China")
    # Stopwords (market/price/trends/china/2026) dropped; C5/C9 kept.
    assert "c5" in toks and "c9" in toks
    assert "petroleum" in toks and "resin" in toks
    assert "market" not in toks and "2026" not in toks


def test_significant_tokens_cjk_bigrams():
    toks = _significant_tokens("裂解碳五 石油树脂 价格 市场 2026年8月")
    for bigram in ("碳五", "树脂", "价格", "市场"):
        assert bigram in toks


def test_filter_drops_actual_bing_junk_en():
    kept = _filter_relevant(_JUNK_EN, "C5 C9 petroleum resin market price 2026 trends China")
    assert kept == [], f"expected all junk dropped, got {[r['title'] for r in kept]}"


def test_filter_drops_actual_bing_junk_zh():
    # The hard junk (1-token overlap: 裂解反应/裂解和热解/裂化与裂解) is
    # dropped.  Topic-adjacent chemistry explainers that share TWO tokens
    # (石油+裂解) may survive — acceptable: the agent sees they are not
    # market data and moves on; the poisonous class (zero/one-token overlap)
    # is gone.
    kept = _filter_relevant(_JUNK_ZH, "裂解碳五 石油树脂 价格 市场 2026年8月")
    kept_titles = [r["title"] for r in kept]
    assert "裂解反应_百度百科" not in kept_titles
    assert "裂解和热解的区别" not in kept_titles
    assert "裂化与裂解有何区别？ - 知乎" not in kept_titles
    assert len(kept) <= 2


def test_filter_keeps_relevant_en():
    kept = _filter_relevant(_GOOD_EN, "C5 C9 petroleum resin market price 2026 trends China")
    assert len(kept) == 3
    assert kept[0]["title"].startswith("C5 C9 Petroleum Resin")


def test_filter_keeps_relevant_zh():
    kept = _filter_relevant(_GOOD_ZH, "裂解碳五 石油树脂 价格 市场 2026年8月")
    assert len(kept) == 2
    assert "生意社" in kept[0]["title"]


def test_filter_mixed_keeps_relevant_drops_junk():
    mixed = _JUNK_EN[:2] + _GOOD_EN[:1]
    kept = _filter_relevant(mixed, "C5 C9 petroleum resin market price 2026 trends China")
    assert len(kept) == 1
    assert "Grand View" in kept[0]["title"]


def test_filter_short_query_requires_one_overlap():
    results = [
        {"title": "Weather forecast for Paris today", "url": "u", "description": "sunny 22C"},
        {"title": "Top 10 restaurants", "url": "u", "description": "food guide"},
    ]
    kept = _filter_relevant(results, "weather today")
    assert len(kept) == 1
    assert "Weather" in kept[0]["title"]


def test_filter_empty_and_blank_pass_through():
    assert _filter_relevant([], "anything") == []
    assert _filter_relevant([{"title": "x", "url": "u", "description": ""}], "") == [{"title": "x", "url": "u", "description": ""}]


def test_keyed_provider_without_key_falls_back_to_keyless(monkeypatch):
    """SEARCH_PROVIDER=bocha with an empty SEARCH_API_KEY must NOT hard-fail
    the tool — the keyless fallback chain (bing/duckduckgo) stays usable so
    the tool degrades instead of dying the moment someone sets a keyed
    provider before pasting the key."""
    from app.config import settings
    from app.services.tool_handlers import web_search_tool
    monkeypatch.setattr(settings, "SEARCH_PROVIDER", "bocha")
    monkeypatch.setattr(settings, "SEARCH_API_KEY", "")
    providers = web_search_tool._providers_to_try()
    names = [type(p).__name__ for p in providers]
    assert "BochaProvider" not in names
    assert "BingProvider" in names
    assert "DuckDuckGoProvider" in names
