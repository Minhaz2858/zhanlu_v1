"""Decision board service — plain-language weekly decision summaries.

Composes market_dashboard_service.get_market_snapshot() (current prices)
with get_forecast_base_batch_v2() (forecast + Phase D/E overlays) into one
per-product bundle with Chinese plain-language narratives for the
/market-dashboard/:section Decision Board UI.

Narrative builders are pure functions (no I/O) for unit testing.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from app.models.market_dashboard import PRODUCT_GROUP_MAP, PRODUCT_LABELS
from app.schemas.market_dashboard import (
    DecisionBoardProduct, DecisionBoardResponse, SkuBrief,
)
from app.services import market_dashboard_service as mds

ACTION_LABELS_ZH = {
    "buy": "建议备货",
    "sell": "建议出货",
    "hold": "按需跟进",
    "watch": "建议观望",
}

TRUST_LABELS_ZH = {
    "high": "高可信",
    "medium": "中等可信",
    "directional": "方向性参考",
    "low": "低可信",
}

_CONFIDENCE_ZH = {"high": "高", "medium": "中", "low": "低"}
_VOLATILITY_ZH = {"NORMAL": "正常", "MODERATE": "偏高", "HIGH": "剧烈"}
_INTEL_BIAS_ZH = {"up": "偏多", "down": "偏空", "neutral": "中性"}

_ACTION_ADVICE_ZH = {
    "buy": "建议提前备货,锁定当前价格",
    "sell": "建议抓紧出货,兑现当前高价",
    "hold": "建议按需跟进,不追涨不囤货",
    "watch": "建议保持观望,等待更明确的信号",
}


def action_label(action: Optional[str]) -> str:
    return ACTION_LABELS_ZH.get((action or "watch").lower(), "建议观望")


def _fmt_price(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:,.0f}"


def _fmt_pct(v: Optional[float]) -> str:
    """Fraction (0.029) → signed percent (+2.9%)."""
    return "—" if v is None else f"{v * 100:+.1f}%"


def _fmt_prob(v: Optional[float]) -> str:
    return "—" if v is None else f"{v * 100:.0f}%"


def build_one_liner(
    *,
    action: str,
    p_rise: Optional[float],
    expected_change_pct: Optional[float],
    intel_event_count: int = 0,
    intel_bias: str = "neutral",
) -> str:
    if p_rise is None and expected_change_pct is None:
        return "数据不足,建议观望"
    parts = []
    if expected_change_pct is not None:
        parts.append(f"预计{_fmt_pct(expected_change_pct)}")
    if p_rise is not None:
        parts.append(f"上涨概率 {_fmt_prob(p_rise)}")
    line = ",".join(parts) + f",{action_label(action)}"
    if intel_event_count > 0 and intel_bias in ("up", "down"):
        line += f"(情报{_INTEL_BIAS_ZH[intel_bias]} {intel_event_count} 条)"
    return line


def build_narrative(
    *,
    name_zh: str,
    day: int,
    current_price: Optional[float],
    forecast_base: Optional[float],
    expected_change_pct: Optional[float],
    p_rise: Optional[float],
    action: str,
    confidence: str,
    directional_accuracy: Optional[float],
    intel_event_count: int,
    intel_bias: str,
    volatility_regime: Optional[str],
) -> str:
    if forecast_base is None or current_price is None:
        return f"{name_zh}当前预测数据不足,{_ACTION_ADVICE_ZH['watch']}。"
    move = "涨至" if (expected_change_pct or 0) >= 0 else "跌至"
    s1 = (
        f"{name_zh}现价 {_fmt_price(current_price)} 元/吨,"
        f"预计未来 {day} 天{move} {_fmt_price(forecast_base)} 元/吨"
        f"({_fmt_pct(expected_change_pct)})"
    )
    if p_rise is not None:
        s1 += f",上涨概率 {_fmt_prob(p_rise)}"
    s1 += "。"
    evidence = []
    if directional_accuracy is not None:
        evidence.append(f"方向预测历史准确率 {_fmt_prob(directional_accuracy)}")
    if intel_event_count > 0:
        evidence.append(
            f"市场情报{_INTEL_BIAS_ZH.get(intel_bias, '中性')}"
            f"({intel_event_count} 条在册事件)"
        )
    if volatility_regime:
        evidence.append(
            f"波动状态{_VOLATILITY_ZH.get(volatility_regime, volatility_regime)}"
        )
    s2 = "、".join(evidence) + "。" if evidence else ""
    s3 = (
        f"{_ACTION_ADVICE_ZH.get(action, _ACTION_ADVICE_ZH['watch'])}"
        f"(置信度:{_CONFIDENCE_ZH.get(confidence, '低')})。"
    )
    return s1 + s2 + s3


def build_briefing(products: list[DecisionBoardProduct], day: int) -> str:
    strong = [
        p for p in products
        if p.action in ("buy", "sell") and p.confidence != "low"
    ]
    if not strong:
        return (
            f"未来 {day} 天市场整体平稳,{len(products)} 个产品均无强买卖信号,"
            "建议按需跟进、控制库存风险。"
        )
    segs = []
    for p in strong:
        direction = "看涨" if p.action == "buy" else "看跌"
        prob = f"(上涨概率 {_fmt_prob(p.p_rise)})" if p.p_rise is not None else ""
        segs.append(f"{p.name_zh}{direction}{prob},{_ACTION_ADVICE_ZH[p.action]}")
    others = [p for p in products if p not in strong]
    tail = (
        "其余 " + "、".join(p.name_zh for p in others) + " 建议观望或按需跟进。"
        if others else ""
    )
    return f"未来 {day} 天重点关注:" + ";".join(segs) + "。" + tail
