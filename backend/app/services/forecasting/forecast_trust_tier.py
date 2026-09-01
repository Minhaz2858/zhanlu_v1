"""Forecast trust tiers.

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
import math
from typing import Any, Optional

from app.services.domain_config import get_domain_config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config-driven product classification (HOW, not WHAT)
# ---------------------------------------------------------------------------

# Product classifications come from the app's domain config (keys
# "high_skill_products" / "weekly_cadence_products" — lists of product ids).
# Empty config = empty sets = fully generic behavior.
_DOMAIN_CFG: dict[str, Any] = get_domain_config("")

# Products with documented genuine ARIMA skill (skill_vs_naive < 0 in backtest).
_HIGH_SKILL_PRODUCTS: frozenset[str] = frozenset(
    str(p) for p in (_DOMAIN_CFG.get("high_skill_products") or [])
)

# Products that trade weekly or less frequently; daily forecast is misleading.
_WEEKLY_CADENCE_PRODUCTS: frozenset[str] = frozenset(
    str(p) for p in (_DOMAIN_CFG.get("weekly_cadence_products") or [])
)


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
        product_id: The dashboard product_id (e.g. "<product>").
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
        reason_codes = ["sparse_data"]
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
        reason_codes = ["below_naive_baseline"]
        if product_id in _WEEKLY_CADENCE_PRODUCTS:
            reason_codes.append("weekly_cadence")
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
        reason_codes = ["weekly_cadence"]
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
        reason_codes = ["model_skill_high"]
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
        reason_codes = ["model_skill_medium"]
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
        "reason_codes": reason_codes,
        "badge_color": badge_color,
        "badge_label_zh": badge_label_zh,
        "badge_label_en": badge_label_en,
    }


def classify_cadence(product_id: str, row_count: int | None) -> str:
    """Simple cadence classifier — a pragmatic starting point.

    Returns one of: "weekly", "sparse", "daily".

    Uses the config-driven _WEEKLY_CADENCE_PRODUCTS frozenset for known
    weekly products, and a row-count threshold for sparse detection. Daily
    is the default for everything else.
    """
    if row_count is not None and row_count < 50:
        return "sparse"
    if product_id in _WEEKLY_CADENCE_PRODUCTS:
        return "weekly"
    return "daily"


# ---------------------------------------------------------------------------
# P2.15: Realized-metric trust tier classification
# ---------------------------------------------------------------------------

def classify_trust(
    realized_mape: float | None = None,
    naive_mape: float | None = None,
    drift_status: str | None = None,
    cadence_row_count: int | None = None,
    product_id: str = "",
) -> dict:
    """Classify trust tier from realized metrics (when available).

    When realized_mape and naive_mape are both available, the tier is
    determined by how the model compares to naive:
      - realized_mape < naive_mape * 0.7  →  "high"  (significant edge)
      - realized_mape < naive_mape        →  "medium" (beats naive)
      - realized_mape >= naive_mape       →  "directional" (no edge)
      - drift_status = "degraded"         →  downgraded one tier

    When realized metrics are not available, falls back to the
    config-driven product lists (cold-start only).
    """
    # Cold-start fallback: no realized metrics → use config-driven lists
    if realized_mape is None or naive_mape is None or not math.isfinite(realized_mape) or not math.isfinite(naive_mape):
        if product_id in _HIGH_SKILL_PRODUCTS:
            return {"tier": "high", "badge_color": "green", "reason_codes": ["cold_start_static"]}
        return {"tier": "medium", "badge_color": "yellow", "reason_codes": ["cold_start_default"]}

    # Data-driven classification
    if realized_mape < naive_mape * 0.7:
        tier = "high"
        badge_color = "green"
    elif realized_mape < naive_mape:
        tier = "medium"
        badge_color = "yellow"
    else:
        tier = "directional"
        badge_color = "orange"

    # Drift downgrade
    reason_codes = ["realized_metrics"]
    if drift_status == "degraded":
        tier_order = {"high": "medium", "medium": "directional", "directional": "low"}
        tier = tier_order.get(tier, "low")
        badge_color = {"high": "yellow", "medium": "orange", "directional": "red"}.get(
            tier, "red"
        )
        reason_codes.append("drift_degraded")

    # Cadence adjustment
    if cadence_row_count is not None and cadence_row_count < 60:
        tier_order = {"high": "medium", "medium": "directional", "directional": "low", "low": "low"}
        tier = tier_order.get(tier, tier)
        reason_codes.append("sparse_cadence")

    return {"tier": tier, "badge_color": badge_color, "reason_codes": reason_codes}
