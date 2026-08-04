"""EDIA-ported forecast trust tiers.

Labels each product's forecast with a user-facing trust tier so the dashboard
doesn't present misleading daily precision for weekly-cadence products or
models that underperform a naive baseline.

Four tiers:
  - high:         Model consistently beats naive baseline. Green badge.
  - medium:       Model has some skill; use alongside qualitative judgment. Yellow.
  - directional:  Model underperforms naive OR product trades weekly. Orange.
                  Use for direction only, not point estimates.
  - low:          Data too sparse to forecast. Red badge.

Source: EDIA_5.1.2/backend/src/service/forecast_supervisor.py:150-283.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Static product classification (ported from EDIA)
# ---------------------------------------------------------------------------

# Products with documented genuine ARIMA skill (skill_vs_naive < 0 in backtest).
_HIGH_SKILL_PRODUCTS: frozenset[str] = frozenset({
    "isoprene",
    "piperylene",
    "c5_resin",
    "dcpd",
})

# Products that trade weekly or less frequently; daily forecast is misleading.
_WEEKLY_CADENCE_PRODUCTS: frozenset[str] = frozenset({
    "cracked_c9",
    "mixed_c9",
    "trimethylbenzene",
    "raffinate_c5",
})


# ---------------------------------------------------------------------------
# Trust tier computation
# ---------------------------------------------------------------------------

def compute_forecast_trust_tier(
    product_id: str,
    *,
    below_naive: Optional[bool] = None,
    cadence_class: Optional[str] = None,
    mape: Optional[float] = None,
) -> dict[str, Any]:
    """Compute a user-facing trust tier for a product's forecast.

    Args:
        product_id: The dashboard product_id (e.g. "c5_resin", "cracked_c9").
        below_naive: Whether the model's MAPE exceeds a naive last-price-hold
            baseline on this run. If None, treated as False.
        cadence_class: One of "daily", "weekly", "sparse", "unknown".
            If None, defaults to "unknown".
        mape: The model's backtest MAPE (e.g. 12.0 = 12%). Optional.

    Returns:
        dict with keys:
            tier: "high" | "medium" | "directional" | "low"
            below_naive: bool
            cadence_class: str
            mape: float | None
            reason_zh: str — human-readable explanation (Chinese)
            reason_en: str — human-readable explanation (English)
            badge_color: str — "green" | "yellow" | "orange" | "red"
            badge_label_zh: str — short badge label (Chinese)
            badge_label_en: str — short badge label (English)
    """
    if below_naive is None:
        below_naive = False
    if cadence_class is None:
        cadence_class = "unknown"

    if cadence_class in ("sparse", "unsupported", "unknown"):
        tier = "low"
        badge_color = "red"
        badge_label_zh = "数据不足"
        badge_label_en = "Insufficient Data"
        reason_zh = "历史数据稀疏或不支持预测，建议查看市场定性分析。"
        reason_en = (
            "Historical data is too sparse or unsupported for forecasting. "
            "Refer to qualitative market analysis."
        )

    elif below_naive:
        tier = "directional"
        badge_color = "orange"
        badge_label_zh = "方向性参考"
        badge_label_en = "Directional Only"
        if product_id in _WEEKLY_CADENCE_PRODUCTS:
            reason_zh = (
                "该产品周频交易，日预测精度有限；当前模型弱于简单基准。仅供参考方向。"
            )
            reason_en = (
                "This product trades weekly; daily forecast precision is limited and "
                "the current model underperforms a naive baseline. Use for direction only."
            )
        else:
            reason_zh = (
                "当前模型预测误差高于简单基准（持平最新价格），建议参考市场定性判断。"
            )
            reason_en = (
                "Current model MAPE exceeds a naive baseline (last-price hold); "
                "use qualitative market judgment alongside."
            )

    elif cadence_class == "weekly" and product_id not in _HIGH_SKILL_PRODUCTS:
        tier = "medium"
        badge_color = "yellow"
        badge_label_zh = "周参考"
        badge_label_en = "Weekly Reference"
        reason_zh = "周频产品，日预测仅供参考；建议以周均价为主要参考。"
        reason_en = (
            "Weekly-trading product; daily forecast is indicative only. "
            "Use weekly average price as the primary reference."
        )

    elif product_id in _HIGH_SKILL_PRODUCTS and not below_naive:
        tier = "high"
        badge_color = "green"
        badge_label_zh = "高置信"
        badge_label_en = "High Confidence"
        mape_str = f"（MAPE {mape:.1f}%）" if mape is not None else ""
        mape_str_en = f" (MAPE {mape:.1f}%)" if mape is not None else ""
        reason_zh = f"模型在历史回测中持续跑赢简单基准，置信度较高{mape_str}。"
        reason_en = (
            f"Model consistently beats naive baseline in backtesting; "
            f"high confidence.{mape_str_en}"
        )

    else:
        tier = "medium"
        badge_color = "yellow"
        badge_label_zh = "中置信"
        badge_label_en = "Medium Confidence"
        reason_zh = "模型有一定预测能力，但需结合市场判断使用。"
        reason_en = (
            "Model has some predictive skill; use alongside qualitative market judgment."
        )

    return {
        "tier": tier,
        "below_naive": below_naive,
        "cadence_class": cadence_class,
        "mape": mape,
        "reason_zh": reason_zh,
        "reason_en": reason_en,
        "badge_color": badge_color,
        "badge_label_zh": badge_label_zh,
        "badge_label_en": badge_label_en,
    }


def classify_cadence(product_id: str, row_count: int | None) -> str:
    """Simple cadence classifier — a pragmatic starting point.

    Returns one of: "weekly", "sparse", "daily".

    Uses the static _WEEKLY_CADENCE_PRODUCTS frozenset for known weekly
    products, and a row-count threshold for sparse detection. Daily is
    the default for everything else.
    """
    if row_count is not None and row_count < 50:
        return "sparse"
    if product_id in _WEEKLY_CADENCE_PRODUCTS:
        return "weekly"
    return "daily"
