"""SmartEvaluator — adaptive, context-enriched alert decision engine.

Replaces the static ±3%/±6% trigger with a per-product, LLM-judged weekly
volatility baseline (cached in ``alert_product_baselines``). Every emitted
alert carries:

  - ``severity``      high / medium (with a configurable high floor)
  - ``impact``        ¥/day business impact + avg daily volume (tons)
  - ``ctx``           templated "why this matters" + recommendation steps

Pure decision layer: it never persists alerts itself. ``AlertService``
consumes the enriched dicts and persists the payload.

No-regression guarantee: when ``SMART_ALERTS_ENABLED`` is off, the worker
never calls this evaluator, so behavior is byte-identical to today's static
thresholds.
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import settings
from app.services.perception.baseline_cache import BaselineCache
from app.services.perception.impact import compute_impact

logger = logging.getLogger(__name__)

SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2}


class SmartEvaluator:
    """Evaluate a perception snapshot against per-product adaptive baselines."""

    def __init__(self, db) -> None:
        self.baselines = BaselineCache(db)

    def evaluate(self, snapshot: list[dict], pref) -> list[dict]:
        """Per product: fetch baseline, decide trigger, compute impact + context.

        Args:
            snapshot: list of product dicts with keys product_id, product_name,
                      current_price, change_7d_pct (matching worker output).
            pref:     preference snapshot; duck-typed so it works standalone —
                      exposes muted_products, min_severity, threshold_multiplier.

        Returns:
            List of enriched product dicts that should become alerts:
            {**product, severity, impact, ctx, baseline_used, alert_type}.
        """
        muted = set(pref.muted_products or [])
        min_severity = getattr(pref, "min_severity", "low") or "low"
        multiplier = float(getattr(pref, "threshold_multiplier", 1.0) or 1.0)

        enriched: list[dict] = []
        for product in snapshot:
            pid = product.get("product_id") or product.get("id")
            if not pid or pid in muted:
                continue

            baseline = self.baselines.get_or_compute(product)
            threshold = baseline.baseline_pct * multiplier
            change = abs(float(product.get("change_7d_pct") or 0.0))
            if change < threshold:
                continue

            severity = self._classify_severity(change, baseline.baseline_pct)
            if SEVERITY_ORDER.get(severity, 0) < SEVERITY_ORDER.get(min_severity, 0):
                continue

            alert_type = (
                "price_spike"
                if float(product.get("change_7d_pct") or 0.0) >= 0
                else "price_drop"
            )

            impact = None
            if settings.ALERT_IMPACT_ENABLED:
                impact = compute_impact({**product, "alert_type": alert_type})

            ctx = None
            if settings.ALERT_RECOMMENDATIONS_ENABLED:
                ctx = build_context(product, alert_type, severity, impact)

            enriched.append(
                {
                    **product,
                    "_smart": True,
                    "alert_type": alert_type,
                    "severity": severity,
                    "impact": impact,
                    "ctx": ctx,
                    "baseline_used": baseline.baseline_pct,
                    "baseline_reasoning": baseline.reasoning,
                }
            )
        return enriched

    def _classify_severity(self, change: float, baseline_pct: float) -> str:
        """high when |change| >= max(baseline*2, floor); else medium."""
        threshold = max(baseline_pct * 2, settings.ALERT_HIGH_SEVERITY_FLOOR)
        return "high" if change >= threshold else "medium"


def build_context(
    product: dict,
    alert_type: str,
    severity: str,
    impact: dict | None,
) -> dict:
    """Templated "why this matters" + recommendation for one alert.

    Keyed on (alert_type, severity, has_volume). Deterministic, drift-free —
    no LLM at trigger time.
    """
    name = product.get("product_name") or product.get("product_id")
    change = abs(float(product.get("change_7d_pct") or 0.0))
    volume = (impact or {}).get("avg_daily_volume_tons")
    impact_yuan = (impact or {}).get("impact_yuan_per_day")
    has_volume = bool(volume)

    direction = "上涨" if alert_type == "price_spike" else "下跌"
    impact_line = (
        f"按近30天日均发货量约{volume:.0f}吨估算，每天涉及约¥{impact_yuan:,.0f}的货值波动。"
        if has_volume and impact_yuan
        else "当前缺少ERP发货量数据，具体货值影响暂无法量化。"
    )

    if severity == "high":
        why = (
            f"{name} 7天价格{direction} {change:.1f}%，远超该产品近期的正常波动范围，"
            f"属于需要立即关注的异常行情。{impact_line}"
        )
        title = "立即核查采购/销售敞口"
        steps = [
            "对照在手订单与库存，确认对该原料价格变化的敞口金额。",
            "如价格仍在上行通道，评估提前锁价或补库的窗口。",
        ]
    else:
        why = (
            f"{name} 7天价格{direction} {change:.1f}%，超过其常规波动基线，"
            f"提示行情正在转势，建议保持跟踪。{impact_line}"
        )
        title = "纳入当日重点跟踪"
        steps = [
            "将价格变化列入晨会跟踪项，观察未来1-2天是否延续该趋势。",
            "价格继续偏离基线时，及时升级为高优先级处理。",
        ]

    if not has_volume:
        steps.append("补充ERP发货量数据后，可量化该波动的每日货值影响。")

    return {
        "why": why,
        "impact_line": impact_line,
        "recommendation": {"title": title, "steps": steps},
    }
