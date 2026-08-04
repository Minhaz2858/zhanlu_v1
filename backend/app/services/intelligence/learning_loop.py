"""Intelligence Learning Loop — self-calibrating feedback system (Phase 5D).

Port of EDIA 5.1.2 backend/src/intelligence/intelligence_learning_loop.py.
Adapted: datetime.utcnow → timezone-aware, module-level singleton → class-based.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


class IntelligenceLearningLoop:
    """Records event outcomes and calibrates future predictions.

    After an event resolves, record the actual outcome and use it to:
    1. Update elasticity calibration (was the price impact ±X% as predicted?)
    2. Update scenario probability base rates
    3. Add the outcome to the historical event impact database
    """

    def __init__(self, db=None, org_id: str | None = None):
        self.db = db
        self.org_id = org_id
        self._outcome_log: list[dict] = []
        # Calibration state
        self._elasticity_errors: list[float] = []
        self._calibration_bias: float = 0.0
        self._resolution_rate_cache: dict[str, dict] = {}

    def record_event_outcome(
        self,
        event_id: str,
        event_type: str,
        predicted_naphtha_pct: float,
        actual_naphtha_pct: float,
        resolution_days: int,
        resolution_type: str = "gradual",
        notes: str = "",
    ) -> dict:
        """Record what actually happened after an event was detected."""
        prediction_error = actual_naphtha_pct - predicted_naphtha_pct
        accuracy_score = max(
            0, 1.0 - abs(prediction_error) / max(abs(predicted_naphtha_pct), 0.5)
        )

        outcome = {
            "event_id": event_id,
            "event_type": event_type,
            "predicted_naphtha_pct": predicted_naphtha_pct,
            "actual_naphtha_pct": actual_naphtha_pct,
            "prediction_error": round(prediction_error, 3),
            "accuracy_score": round(accuracy_score, 3),
            "resolution_days": resolution_days,
            "resolution_type": resolution_type,
            "notes": notes,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }

        self._outcome_log.append(outcome)
        self._update_elasticity_calibration(outcome)
        self._update_scenario_base_rates(outcome)

        logger.info(
            "Outcome recorded: %s accuracy=%.2f error=%+.3f days=%d",
            event_id, accuracy_score, prediction_error, resolution_days,
        )
        return outcome

    def _update_elasticity_calibration(self, outcome: dict):
        """Track prediction errors to compute running bias."""
        self._elasticity_errors.append(outcome["prediction_error"])
        if len(self._elasticity_errors) >= 3:
            self._calibration_bias = round(
                sum(self._elasticity_errors) / len(self._elasticity_errors), 3
            )

    def _update_scenario_base_rates(self, outcome: dict):
        """Update resolution-type frequency for the event_type."""
        cache = self._resolution_rate_cache.setdefault(outcome["event_type"], {
            "total": 0, "rapid": 0, "gradual": 0, "structural": 0,
        })
        cache["total"] += 1
        rt = outcome.get("resolution_type", "gradual")
        if rt in cache:
            cache[rt] += 1

    def get_calibration_state(self) -> dict:
        """Return current calibration metrics."""
        return {
            "total_outcomes": len(self._outcome_log),
            "calibration_bias": self._calibration_bias,
            "mean_absolute_error": (
                round(sum(abs(e) for e in self._elasticity_errors) / max(len(self._elasticity_errors), 1), 3)
            ),
            "resolution_rates": self._resolution_rate_cache,
        }

    def recommend_correction(self, event_type: str) -> dict:
        """Recommend naphtha shock correction based on historical outcomes."""
        state = self.get_calibration_state()
        rates = self._resolution_rate_cache.get(event_type, {})
        return {
            "bias_correction": -self._calibration_bias,
            "recommended_action": "apply_bias_correction" if abs(self._calibration_bias) > 0.5 else "no_action",
            "event_type_rates": rates,
            "total_tracked": state["total_outcomes"],
        }
