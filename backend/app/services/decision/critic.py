"""Decision Critic — deterministic contradiction and confidence checks.

Port of EDIA 5.1.2 backend/src/service/decision_critic.py.
Checks: contradictory evidence, overconfidence, aggressive actions under
mixed signals, directional mismatch between action and evidence scores.
"""

from __future__ import annotations

from typing import Any


class DecisionCritic:
    """Deterministic post-check for contradictions and confidence realism."""

    def review(
        self,
        diagnosis: dict[str, Any] | None,
        forecast: dict[str, Any] | None,
        pricing_decision: dict[str, Any] | None,
        evidence_bundle: dict[str, Any] | None,
    ) -> dict[str, Any]:
        diagnosis = diagnosis or {}
        forecast = forecast or {}
        pricing_decision = pricing_decision or {}
        evidence_bundle = evidence_bundle or {}

        flags: list[str] = []
        recommendations: list[str] = []

        # ── Contradictions from evidence bundle ─────────────────
        contradictions = evidence_bundle.get("contradictions") or []
        if contradictions:
            flags.append("contradictory_evidence_present")
            recommendations.append(
                "Explain both upside and downside drivers explicitly before final recommendation."
            )

        # ── Overconfidence check ────────────────────────────────
        residual_uncertainty = str(
            evidence_bundle.get("residual_uncertainty", "high")
        ).lower()
        regime_conf = float(diagnosis.get("regime_confidence", 0.0) or 0.0)
        if residual_uncertainty == "high" and regime_conf >= 0.70:
            flags.append("potential_overconfidence")
            recommendations.append(
                "Reduce confidence wording and highlight uncertainty drivers."
            )

        # ── Action vs. evidence directional check ──────────────
        bull_score = float(evidence_bundle.get("bullish_score", 0.0) or 0.0)
        bear_score = float(evidence_bundle.get("bearish_score", 0.0) or 0.0)
        dominant = str(evidence_bundle.get("dominant_view", "mixed"))
        action = str(pricing_decision.get("action", "hold")).lower()

        if dominant == "mixed" and action in {"raise", "lower", "raise_price", "reduce_price"}:
            flags.append("aggressive_action_under_mixed_evidence")
            recommendations.append(
                "Prefer flexible pricing or provide explicit trigger conditions for directional action."
            )

        if action in {"raise", "raise_price"} and bull_score < bear_score:
            flags.append("raise_despite_bearish_score")
            recommendations.append(
                "Re-check upward recommendation because bearish evidence score dominates."
            )

        if action in {"lower", "reduce_price"} and bear_score < bull_score:
            flags.append("lower_despite_bullish_score")
            recommendations.append(
                "Re-check downward recommendation because bullish evidence score dominates."
            )

        critic_level = "pass"
        if flags:
            critic_level = "warn"
        if len(flags) >= 3:
            critic_level = "high_risk"

        return {
            "critic_level": critic_level,
            "flags": flags,
            "recommendations": recommendations,
        }

    def to_prompt_text(self, critic_result: dict[str, Any] | None) -> str:
        if not critic_result:
            return ""

        lines = [
            "DECISION CRITIC:",
            f"Level: {critic_result.get('critic_level', 'pass')}",
        ]

        flags = critic_result.get("flags") or []
        recs = critic_result.get("recommendations") or []

        if flags:
            lines.append("Flags:")
            lines.extend(f"  - {flag}" for flag in flags[:6])
        if recs:
            lines.append("Recommendations:")
            lines.extend(f"  - {rec}" for rec in recs[:6])

        if not flags:
            lines.append("Flags: none")

        return "\n".join(lines)
