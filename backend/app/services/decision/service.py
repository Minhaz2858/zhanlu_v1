"""Ecisco BI Decision Service — post-pricing synthesis.

Port of EDIA 5.1.2 backend/src/service/decision_service.py.
Adapted: LangGraph pipeline → class-based assemble+critic pattern.
The original EDIA code used a LangGraph state graph (6-stage pipeline).
In Zhanlu, the pipeline stages (perception → diagnosis → forecast → pricing)
are already handled by individual tools. The DecisionService here focuses on:

1. Assemble the full decision context from pipeline outputs
2. Run the critic (contradiction / confidence checks)
3. Produce the final synthesis with evidence bundle
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .critic import DecisionCritic

logger = logging.getLogger(__name__)


class DecisionService:
    """Assemble pipeline outputs into a final decision with critic checks."""

    def __init__(self, db=None, org_id: str | None = None, llm_caller=None):
        self.db = db
        self.org_id = org_id
        self.llm = llm_caller
        self.critic = DecisionCritic()

    def process_pipeline(
        self,
        product_id: str,
        perception: dict | None = None,
        diagnosis: dict | None = None,
        forecast: dict | None = None,
        pricing_decision: dict | None = None,
        intelligence_context: dict | None = None,
        weekly_report_context: dict | None = None,
    ) -> dict:
        """Assemble full pipeline outputs → final decision report.

        Returns a DecisionReport-compatible dict with:
        - product_id, product_name
        - market_regime, primary_driver
        - pricing (recommendation, range, action, confidence)
        - evidence_bundle (bullish/bearish score, contradictions, dominant view)
        - critic_result (flags, recommendations)
        - weekly_summary (if available)
        - generated_at (ISO 8601)
        - prompt_text (LLM-ready prose block)
        """
        pricing = pricing_decision or {}
        diag = diagnosis or {}
        perc = perception or {}

        product_name = pricing.get("product_name_cn") or product_id

        # ── Build evidence bundle ───────────────────────────────────
        evidence = self._build_evidence_bundle(diag, forecast, pricing, intelligence_context)

        # ── Run critic ──────────────────────────────────────────────
        critic_result = self.critic.review(
            diagnosis=diag,
            forecast=forecast,
            pricing_decision=pricing,
            evidence_bundle=evidence,
        )

        # ── Derive action from pricing ──────────────────────────────
        action = pricing.get("action", "hold")
        recommended_price = pricing.get("recommended_price")
        price_range = pricing.get("price_range", [])
        confidence = pricing.get("confidence", 0.7)

        # ── Weekly report context ───────────────────────────────────
        weekly_summary = None
        if weekly_report_context:
            weekly_summary = weekly_report_context.get("weekly_summary") or \
                weekly_report_context.get("recommendation")

        # ── Build final report ──────────────────────────────────────
        report = {
            "product_id": product_id,
            "product_name": product_name,
            "market_regime": diag.get("market_regime", "neutral"),
            "primary_driver": diag.get("primary_driver", {}),
            "recommended_action": action,
            "recommended_price": recommended_price,
            "price_range": price_range,
            "confidence": confidence,
            "evidence_bundle": evidence,
            "critic_result": critic_result,
            "weekly_summary": weekly_summary,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

        # ── Build prompt text ───────────────────────────────────────
        report["prompt_text"] = self._to_prompt_text(report)

        return report

    def _build_evidence_bundle(
        self,
        diagnosis: dict | None,
        forecast: dict | None,
        pricing: dict | None,
        intelligence_context: dict | None,
    ) -> dict:
        """Build an evidence bundle with bullish/bearish score and contradictions."""
        diag = diagnosis or {}
        fcast = forecast or {}
        price = pricing or {}

        bullish_score = 0
        bearish_score = 0

        # From diagnosis
        regime = diag.get("market_regime", "neutral")
        if regime == "bullish":
            bullish_score += 2
        elif regime == "bearish":
            bearish_score += 2

        net_signal = diag.get("net_signal", "neutral")
        if net_signal == "bullish":
            bullish_score += 1
        elif net_signal == "bearish":
            bearish_score += 1

        # From forecast direction
        fcast_dir = fcast.get("direction", "flat")
        if fcast_dir == "up":
            bullish_score += 1
        elif fcast_dir == "down":
            bearish_score += 1

        # From pricing action
        action = price.get("action", "hold")
        if action in ("raise_price", "hold_upper"):
            bullish_score += 1
        elif action in ("reduce_price", "hold_lower"):
            bearish_score += 1

        # From intelligence
        intel = intelligence_context or {}
        intel_bias = intel.get("bias", "neutral") if isinstance(intel, dict) else "neutral"
        if intel_bias == "bullish":
            bullish_score += 1
        elif intel_bias == "bearish":
            bearish_score += 1

        # Dominant view
        if bullish_score > bearish_score + 1:
            dominant = "bullish"
        elif bearish_score > bullish_score + 1:
            dominant = "bearish"
        else:
            dominant = "mixed"

        # Contradictions
        contradictions = []
        if bullish_score >= 2 and bearish_score >= 2:
            contradictions.append("mixed_signals_high")
        if regime == "bullish" and action in ("reduce_price", "hold_lower"):
            contradictions.append("bullish_regime_bearish_action")
        if regime == "bearish" and action in ("raise_price", "hold_upper"):
            contradictions.append("bearish_regime_bullish_action")

        # Residual uncertainty
        residual_uncertainty = "low"
        if contradictions:
            residual_uncertainty = "high"
        elif dominant == "mixed":
            residual_uncertainty = "medium"

        return {
            "bullish_score": bullish_score,
            "bearish_score": bearish_score,
            "dominant_view": dominant,
            "contradictions": contradictions,
            "residual_uncertainty": residual_uncertainty,
            "market_regime": regime,
            "net_signal": net_signal,
        }

    @staticmethod
    def _to_prompt_text(report: dict) -> str:
        """Build a compact DECISION SYNTHESIS block for LLM context."""
        pid = report["product_id"]
        name = report["product_name"]
        action = report["recommended_action"]
        price = report.get("recommended_price")
        prange = report.get("price_range", [])
        conf = int((report.get("confidence") or 0.7) * 100)
        regime = report.get("market_regime", "neutral")
        driver = report.get("primary_driver", {})
        driver_name = driver.get("hypothesis", "") if isinstance(driver, dict) else ""

        evidence = report.get("evidence_bundle", {})
        bull = evidence.get("bullish_score", 0)
        bear = evidence.get("bearish_score", 0)
        dominant = evidence.get("dominant_view", "mixed")
        contradictions = evidence.get("contradictions", [])
        uncertainty = evidence.get("residual_uncertainty", "low")

        critic = report.get("critic_result", {})
        critic_level = critic.get("critic_level", "pass")
        critic_flags = critic.get("flags", [])
        critic_recs = critic.get("recommendations", [])

        weekly = report.get("weekly_summary", "")

        lines = [
            "DECISION SYNTHESIS (STAGE 5):",
            f"Product: {name} ({pid}) | Regime: {regime} | Driver: {driver_name}",
        ]
        if price:
            p_low = prange[0] if len(prange) > 0 else "?"
            p_high = prange[1] if len(prange) > 1 else "?"
            lines.append(
                f"Recommended: {price:,} 元/吨 [{p_low:,} – {p_high:,}] | "
                f"Action: {action} | Confidence: {conf}%"
            )
        lines.append(
            f"Evidence: Bullish={bull} Bearish={bear} → {dominant} "
            f"(uncertainty: {uncertainty})"
        )
        if contradictions:
            lines.append(f"Contradictions: {', '.join(contradictions)}")
        lines.append(f"Critic: {critic_level}")
        if critic_flags:
            lines.append(f"  Flags: {', '.join(critic_flags[:4])}")
        if critic_recs:
            lines.append(f"  Recs: {'; '.join(critic_recs[:3])}")
        if weekly:
            lines.append(f"Weekly Report: {weekly}")

        return "\n".join(lines)


# ── Module-level convenience ──────────────────────────────────────

def assemble_decision(
    product_id: str,
    perception: dict | None = None,
    diagnosis: dict | None = None,
    forecast: dict | None = None,
    pricing_decision: dict | None = None,
    intelligence_context: dict | None = None,
    weekly_report_context: dict | None = None,
) -> dict:
    """Convenience: run the full decision assembly in one call."""
    svc = DecisionService()
    return svc.process_pipeline(
        product_id=product_id,
        perception=perception,
        diagnosis=diagnosis,
        forecast=forecast,
        pricing_decision=pricing_decision,
        intelligence_context=intelligence_context,
        weekly_report_context=weekly_report_context,
    )
