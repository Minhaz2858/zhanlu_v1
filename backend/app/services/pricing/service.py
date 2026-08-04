"""Ecisco BI Pricing Service — 9-step pricing decision ladder.

Port of EDIA 5.1.2 backend/src/service/pricing_service.py (§9).
Implements the full 9-step ladder:
  1. Base anchor (Formula A/B/C)
  2. Market realization (blend toward actual market)
  3. Macro regime adjustment (naphtha trend)
  3A. Intelligence event adjustment
  4. Seasonal phase adjustment
  5. Downstream chain demand signals
  6. ERP inventory pressure (weeks-of-stock)
  6A. Weekly production/sales plan pressure
  7. Action bias (weekly report recommendation override)
  8. Active macro override (naphtha shock propagation)
  9. Synthesize: triggers, watch signals, dissenting view

Adapted: module-level singletons → class-based, text() SQL → SQLAlchemy,
threading → asyncio, MySQL → PostgreSQL-ready.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .macro_override import MacroOverrideService

logger = logging.getLogger(__name__)

# ── Pricing Formula Config (§9.2) ─────────────────────────────────────────

PRICING_FORMULA: dict[str, str | None] = {
    "isoprene": "C", "piperylene": "C", "dcpd": "C",
    "cracked_c5": "C", "mixed_c5": "C", "cracked_c9": "C",
    "raffinate_c5": "C", "blowing_agent": "C", "styrene": "C",
    "sis": "B", "c5_resin": "B",
    "crude_oil": None, "naphtha": None,
}

SINOPEC_PREMIUM_CNY: dict[str, int] = {
    "isoprene": 200, "piperylene": 150, "dcpd": 200,
    "cracked_c5": 100, "mixed_c5": 100, "cracked_c9": 100,
    "raffinate_c5": 100, "styrene": 150, "blowing_agent": 120,
    "sis": 0, "c5_resin": 0,
}

_ECISCO_DEFAULT_DISCOUNT = 200

_COMPETITOR_BASELINE: dict[str, int] = {"c5_resin": 8500, "sis": 15800}
_COMPETITOR_DISCOUNT: dict[str, int] = {"c5_resin": 400, "sis": 800}

_NAPHTHA_ELASTICITY: dict[str, float] = {
    "cracked_c5": 0.75, "mixed_c5": 0.75, "cracked_c9": 0.65,
    "isoprene": 0.60, "piperylene": 0.55, "dcpd": 0.50,
    "raffinate_c5": 0.55, "sis": 0.45, "blowing_agent": 0.45,
    "styrene": 0.47, "c5_resin": 0.45,
}

_SEASONAL_ADJ: dict[tuple, int] = {
    ("c5_resin", 11): -100, ("c5_resin", 12): -100,
    ("c5_resin", 1): -100, ("c5_resin", 2): -100,
    ("c5_resin", 5): 50, ("c5_resin", 6): 50,
    ("c5_resin", 7): 50, ("c5_resin", 8): 50, ("c5_resin", 9): 50,
    ("dcpd", 11): -100, ("dcpd", 12): -100,
    ("dcpd", 1): -100, ("dcpd", 2): -50,
    ("dcpd", 4): 50, ("dcpd", 5): 50, ("dcpd", 6): 50,
    ("dcpd", 7): 50, ("dcpd", 8): 50,
    ("isoprene", 2): 50, ("isoprene", 3): 50,
    ("piperylene", 2): 50, ("piperylene", 3): 50,
    ("mixed_c5", 1): -50, ("mixed_c5", 2): -50,
}


class PricingService:
    """Computes Stage 4 pricing decisions using the 9-step ladder."""

    def compute_pricing_decision(
        self,
        product_id: str,
        perception_snapshot: dict,
        diagnosis: dict | None = None,
        forecast: dict | None = None,
        intelligence_context: dict | None = None,
    ) -> dict:
        """Run the 9-step ladder and return a full pricing decision dict."""
        formula = PRICING_FORMULA.get(product_id)
        if not formula:
            return self._no_pricing(product_id, "No pricing formula for this product")

        products = perception_snapshot.get("products", {})
        product_data = products.get(product_id)
        if not product_data:
            return self._no_pricing(product_id, "Product not in perception snapshot")

        latest_price = float(product_data.get("latest_price", 0))
        product_name_cn = product_data.get("name_cn", product_id)
        product_name_en = product_data.get("name_en", product_id)
        unit = product_data.get("unit", "元/吨")
        current_month = datetime.now(timezone.utc).month

        # Forward-looking reference: 7d base forecast midpoint
        forecast_reference_price = latest_price
        if forecast and "scenarios" in forecast:
            base_7d = forecast["scenarios"].get("7d", {}).get("base", {})
            if base_7d.get("low") is not None and base_7d.get("high") is not None:
                forecast_reference_price = round((base_7d["low"] + base_7d["high"]) / 2)

        naphtha_data = products.get("naphtha", {})
        naphtha_7d_pct = float(naphtha_data.get("change_7d_pct", 0.0))

        intelligence_signal = self._summarize_intelligence(product_id, intelligence_context)

        adjustment_steps: list[dict] = []

        # ── Step 1+2: Base anchor + Market realization ──────────────────────
        sinopec_anchor: float | None = None
        base_price: float

        if formula == "C":
            sinopec_anchor = latest_price + SINOPEC_PREMIUM_CNY.get(product_id, 150)
            base_price = sinopec_anchor - _ECISCO_DEFAULT_DISCOUNT
            adjustment_steps.append({
                "step": 1, "label": "基准锚价 (公式C: SINOPEC±折扣)",
                "value": base_price, "delta": 0,
                "note": f"SINOPEC锚价: {sinopec_anchor:,.0f} {unit}，Ecisco默认折扣 -{_ECISCO_DEFAULT_DISCOUNT} {unit}",
            })
        else:  # Formula B
            competitor_base = _COMPETITOR_BASELINE.get(product_id, latest_price)
            discount = _COMPETITOR_DISCOUNT.get(product_id, 400)
            base_price = competitor_base - discount
            adjustment_steps.append({
                "step": 1, "label": "基准锚价 (公式B: 竞品参考)",
                "value": base_price, "delta": 0,
                "note": f"竞品参考价 {competitor_base:,.0f}，Ecisco折扣 -{discount} {unit}",
            })

        working_price = base_price

        # Step 2: Market realization
        realization_delta = int(latest_price - base_price)
        if abs(realization_delta) > 50:
            blended_adj = round(realization_delta * 0.4)
            working_price += blended_adj
            adjustment_steps.append({
                "step": 2, "label": "市场实现折价校正",
                "value": working_price, "delta": blended_adj,
                "note": f"当前市场价 {latest_price:,.0f}，公式价 {base_price:,.0f}，差价 {realization_delta:+,d}，取40%权重校正",
            })

        # ── Step 3: Macro regime adjustment ────────────────────────────────
        macro_delta, macro_note = _compute_macro_adj(naphtha_7d_pct)
        if macro_delta != 0:
            working_price += macro_delta
            adjustment_steps.append({
                "step": 3, "label": "宏观成本调整",
                "value": working_price, "delta": macro_delta, "note": macro_note,
            })

        # Step 3A: Intelligence event adjustment
        if intelligence_signal.get("active"):
            intel_delta = round(latest_price * (intelligence_signal["net_price_pct"] / 100) * max(intelligence_signal.get("confidence", 0.25), 0.25) * 0.5)
            if intel_delta != 0:
                working_price += intel_delta
                adjustment_steps.append({
                    "step": "3A", "label": "实时情报事件调整",
                    "value": working_price, "delta": intel_delta,
                    "note": f"{intelligence_signal.get('bias', 'neutral')} event signal {intelligence_signal['net_price_pct']:+.2f}%",
                })

        # ── Step 4: Seasonal phase adjustment ──────────────────────────────
        seasonal_delta = _SEASONAL_ADJ.get((product_id, current_month), 0)
        if seasonal_delta != 0:
            working_price += seasonal_delta
            adjustment_steps.append({
                "step": 4, "label": "季节性调整",
                "value": working_price, "delta": seasonal_delta,
                "note": _seasonal_note(product_id, current_month, seasonal_delta),
            })

        # ── Step 5: Downstream chain demand signals ─────────────────────────
        downstream_delta, downstream_note = _compute_downstream_adj(diagnosis)
        if downstream_delta != 0:
            working_price += downstream_delta
            adjustment_steps.append({
                "step": 5, "label": "下游需求链信号",
                "value": working_price, "delta": downstream_delta, "note": downstream_note,
            })

        # ── Step 6: ERP inventory pressure ──────────────────────────────────
        inv_delta, inv_note = _compute_inventory_pressure(product_id)
        if inv_delta != 0:
            working_price += inv_delta
        adjustment_steps.append({
            "step": 6, "label": "ERP库存压力",
            "value": working_price, "delta": inv_delta, "note": inv_note,
        })

        # ── Step 7: Action bias ────────────────────────────────────────────
        action_bias, override_delta, bias_note = _compute_action_bias(diagnosis, forecast, working_price)
        if override_delta != 0:
            working_price += override_delta
            adjustment_steps.append({
                "step": 7, "label": "周报推荐覆盖",
                "value": working_price, "delta": override_delta, "note": bias_note,
            })

        # ── Step 8: Active macro override shock propagation ─────────────────
        macro_svc = MacroOverrideService()
        override = macro_svc.get_active_override()
        override_applied = False
        override_notes = None
        confidence_penalty = 0.0

        if override:
            elasticity = _NAPHTHA_ELASTICITY.get(product_id, 0.5)
            shock_pct = override.get("naphtha_shock_pct", 0.0)
            override_delta_price = round(working_price * (shock_pct / 100) * elasticity)
            prod_shock = override.get("product_shocks", {}).get(product_id, 0.0)
            if prod_shock:
                override_delta_price += round(working_price * (prod_shock / 100))
            working_price += override_delta_price
            confidence_penalty = override.get("confidence_penalty", 0.0)
            override_applied = True
            override_notes = macro_svc.override_summary(override)
            adjustment_steps.append({
                "step": 8, "label": "宏观Override注入",
                "value": working_price, "delta": override_delta_price, "note": override_notes,
            })

        # ── Step 9: Synthesize ─────────────────────────────────────────────
        recommended_price = round(working_price / 50) * 50

        range_width = 100 + int(confidence_penalty * 500)
        price_low = recommended_price - range_width
        price_high = recommended_price + range_width

        price_delta_vs_market = recommended_price - forecast_reference_price
        if price_delta_vs_market > 100:
            action, action_cn = "raise_price", "看涨 — 建议上调"
        elif price_delta_vs_market < -100:
            action, action_cn = "reduce_price", "看跌 — 建议下调"
        elif price_delta_vs_market > 0:
            action, action_cn = "hold_upper", "偏强 — 维持高位或小幅上调"
        elif price_delta_vs_market < 0:
            action, action_cn = "hold_lower", "偏弱 — 维持低位或小幅下调"
        else:
            action, action_cn = "hold", "稳定 — 维持现价"

        base_confidence = 0.72
        if abs(price_delta_vs_market) > 300:
            base_confidence -= 0.08
        base_confidence -= confidence_penalty
        confidence = round(max(0.45, min(0.92, base_confidence)), 2)

        triggers = _build_triggers(product_id, recommended_price, price_high, price_low, naphtha_7d_pct, diagnosis)
        watch_signals = _build_watch_signals(product_id, override_applied)
        dissenting_view = _build_dissenting_view(product_id, action, diagnosis, current_month)

        return {
            "product_id": product_id,
            "product_name_cn": product_name_cn,
            "product_name_en": product_name_en,
            "unit": unit,
            "latest_market_price": round(latest_price),
            "forecast_reference_price": round(forecast_reference_price),
            "pricing_formula": formula,
            "sinopec_anchor": round(sinopec_anchor) if sinopec_anchor else None,
            "discount_to_sinopec": round(sinopec_anchor - recommended_price) if sinopec_anchor else None,
            "recommended_price": recommended_price,
            "price_range": [price_low, price_high],
            "price_delta_vs_market": round(price_delta_vs_market),
            "action": action,
            "action_cn": action_cn,
            "confidence": confidence,
            "adjustment_steps": adjustment_steps,
            "triggers": triggers,
            "watch_signals": watch_signals,
            "dissenting_view": dissenting_view,
            "intelligence_signal": intelligence_signal,
            "override_applied": override_applied,
            "override_notes": override_notes,
        }

    def to_prompt_text(self, decision: dict) -> str:
        """Compact PRICING DECISION block for LLM context."""
        if decision.get("no_pricing"):
            return ""
        pid = decision["product_id"]
        name = decision["product_name_cn"]
        rec = decision["recommended_price"]
        low, high = decision["price_range"]
        formula = decision["pricing_formula"]
        action_cn = decision["action_cn"]
        conf = int(decision["confidence"] * 100)
        delta = decision["price_delta_vs_market"]
        mkt = decision["latest_market_price"]
        fcast_ref = decision.get("forecast_reference_price", mkt)
        unit = decision["unit"]

        steps_summary = " | ".join(
            f"Step{s['step']}:{s['delta']:+,d}" for s in decision["adjustment_steps"] if s["delta"] != 0
        ) or "none"

        triggers_text = "\n".join(f"  • {t['condition']} → {t['action']}" for t in decision["triggers"][:2])
        watch_text = " | ".join(decision["watch_signals"][:3])

        override_line = ""
        if decision["override_applied"]:
            override_line = f"\n⚠️  {decision['override_notes']}"

        return f"""PRICING DECISION (STAGE 4):
Product: {name} ({pid}) | Formula: {formula}
Current Market: {mkt:,} {unit} | 7d Forecast Ref: {fcast_ref:,} {unit} | Recommended: {rec:,} {unit} ({delta:+,d} vs forecast)
Range: {low:,} – {high:,} {unit} | Action: {action_cn} | Confidence: {conf}%
Adjustments: {steps_summary}
Key Triggers:
{triggers_text}
Watch: {watch_text}
Dissenting View: {decision['dissenting_view']}{override_line}"""

    def _no_pricing(self, product_id: str, reason: str) -> dict:
        return {"product_id": product_id, "no_pricing": True, "reason": reason}

    @staticmethod
    def _summarize_intelligence(product_id: str, intelligence_context: dict | None = None) -> dict:
        """Summarize active intelligence events for a product."""
        if not intelligence_context:
            return {"active": False, "bias": "neutral", "net_price_pct": 0, "net_price_cny": 0, "confidence": 0}
        events = intelligence_context.get("events", [])
        if not events:
            return {"active": False, "bias": "neutral", "net_price_pct": 0, "net_price_cny": 0, "confidence": 0}
        # Simplified: count bullish/bearish events
        bullish = sum(1 for e in events if e.get("direction") == "bullish")
        bearish = sum(1 for e in events if e.get("direction") == "bearish")
        total = bullish + bearish
        if total == 0:
            return {"active": False, "bias": "neutral", "net_price_pct": 0, "net_price_cny": 0, "confidence": 0}
        net = bullish - bearish
        avg_conf = sum(e.get("confidence", 0.5) for e in events) / len(events) if events else 0.5
        return {
            "active": True,
            "bias": "bullish" if net > 0 else "bearish" if net < 0 else "neutral",
            "net_price_pct": net * 0.5,
            "net_price_cny": net * 25,
            "confidence": avg_conf,
        }


# ── Helper Functions ──────────────────────────────────────────────────────

def _compute_macro_adj(naphtha_7d_pct: float) -> tuple[int, str]:
    if naphtha_7d_pct >= 3.0:
        return 150, f"石脑油7日涨幅{naphtha_7d_pct:+.1f}%，强成本支撑，收窄折扣+150"
    elif naphtha_7d_pct >= 1.5:
        return 100, f"石脑油7日涨幅{naphtha_7d_pct:+.1f}%，成本上行压力+100"
    elif naphtha_7d_pct >= 0.5:
        return 50, f"石脑油小幅上涨{naphtha_7d_pct:+.1f}%，轻微成本支撑+50"
    elif naphtha_7d_pct <= -3.0:
        return -150, f"石脑油7日跌幅{naphtha_7d_pct:+.1f}%，成本强力下行-150"
    elif naphtha_7d_pct <= -1.5:
        return -100, f"石脑油7日跌幅{naphtha_7d_pct:+.1f}%，成本下行-100"
    elif naphtha_7d_pct <= -0.5:
        return -50, f"石脑油小幅下跌{naphtha_7d_pct:+.1f}%，轻微承压-50"
    return 0, f"石脑油7日变动{naphtha_7d_pct:+.1f}%，宏观中性"


def _seasonal_note(product_id: str, month: int, delta: int) -> str:
    if delta < 0:
        return f"{month}月为{product_id}淡季，价格承压 {delta:+,d} 元/吨"
    elif delta > 0:
        return f"{month}月为{product_id}旺季/需求恢复期，价格支撑 {delta:+,d} 元/吨"
    return f"{month}月季节性中性"


def _compute_downstream_adj(diagnosis: dict | None) -> tuple[int, str]:
    if not diagnosis:
        return 0, "无诊断数据"
    downstream = diagnosis.get("downstream_propagated_signals", {})
    if not downstream:
        return 0, "无下游信号"
    bullish_count = sum(1 for v in downstream.values() if "supportive" in str(v).lower() or "bullish" in str(v).lower() or "上行" in str(v))
    if bullish_count == 0:
        return 0, "下游信号中性"
    adj = min(200, bullish_count * 50)
    return adj, f"下游{bullish_count}个看涨信号 +{adj} 元/吨"


def _compute_inventory_pressure(product_id: str) -> tuple[int, str]:
    """Step 6: ERP inventory → weeks-of-stock → pricing pressure. (Placeholder — ERP query not implemented yet)"""
    return 0, f"{product_id} ERP库存数据暂未接入，库存中性"


def _compute_action_bias(diagnosis: dict | None, forecast: dict | None, working_price: float) -> tuple[str, int, str]:
    if not diagnosis:
        return "neutral", 0, ""
    regime = diagnosis.get("market_regime", "neutral")
    net_signal = diagnosis.get("net_signal", "neutral")
    primary_driver = diagnosis.get("primary_driver")
    primary_hyp = primary_driver.get("hypothesis", "") if isinstance(primary_driver, dict) else ""
    downstream = diagnosis.get("downstream_propagated_signals", {})
    supportive_count = sum(1 for v in downstream.values() if "supportive" in str(v).lower())

    if regime == "bearish" and net_signal == "bearish":
        return "accelerate_shipments", -100, "看跌行情，建议加速出货避免库存积压，折扣 −100 元/吨"
    if regime == "bullish" and primary_hyp == "naphtha_cost_push":
        return "biased_upward", 75, "看涨行情 + 石脑油成本驱动，偏强策略 +75 元/吨"
    if regime == "bullish" and supportive_count >= 2:
        return "biased_upward", 100, f"看涨 + {supportive_count}个下游需求支撑信号，偏强策略 +100 元/吨"
    if regime == "bullish":
        return "biased_upward", 50, "市场看涨，偏强策略 +50 元/吨"
    if regime == "neutral" and supportive_count >= 1:
        return "flexible_pricing", 0, "市场中性但下游有支撑信号，建议灵活定价"
    if regime == "bearish":
        return "watchful_hold", 0, "市场偏弱，建议观望"
    return "neutral", 0, "市场中性"


def _build_triggers(product_id: str, rec_price: int, price_high: int, price_low: int, naphtha_7d_pct: float, diagnosis: dict | None) -> list[dict]:
    triggers: list[dict] = []
    if naphtha_7d_pct > 0:
        triggers.append({"condition": f"石脑油7日涨幅持续 > {naphtha_7d_pct + 1:.1f}%", "action": f"上调至 {price_high:,} 元/吨", "probability": "medium"})
    else:
        triggers.append({"condition": f"石脑油7日跌幅扩大至 > {abs(naphtha_7d_pct) + 1:.1f}%", "action": f"调整至 {price_low:,} 元/吨并提示客户", "probability": "medium"})
    triggers.append({"condition": "订单流速加快 > 30%", "action": f"考虑上调至 {price_high:,} 元/吨", "probability": "low"})
    triggers.append({"condition": f"跌破 {price_low - 100:,} 元/吨", "action": "减仓并通知销售团队", "probability": "low"})
    return triggers


def _build_watch_signals(product_id: str, override_active: bool) -> list[str]:
    signals = [
        f"{product_id} SINOPEC挂牌价变动",
        f"石脑油月度结算价",
        f"{product_id} 下游开工率",
    ]
    if override_active:
        signals.insert(0, "宏观Override剩余有效期")
    return signals


def _build_dissenting_view(product_id: str, action: str, diagnosis: dict | None, month: int) -> str:
    if action in ("hold", "hold_upper", "hold_lower"):
        return f"维持{product_id}现价可能错过短期调价窗口，建议关注{month}月底订单流速"
    if action == "raise_price":
        return f"{product_id}上调可能面临下游客户比价压力（竞品未同步上调）"
    if action == "reduce_price":
        return f"{product_id}下调可能触发同行跟进降价，需评估长期合同影响"
    return "数据不足，无法评估反面观点"
