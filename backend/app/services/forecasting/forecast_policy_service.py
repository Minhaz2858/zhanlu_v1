from __future__ import annotations

"""Forecast Policy Service — adaptive bias correction and volatility scaling.

P2.14: This module owns the SINGLE bias-correction path with a combined
absolute cap of ±2.5%.  HITL author deltas are routed through the same
budget (they compete for the cap, never compound on top).

Canonical volatility thresholds (single source of truth):
  HIGH_VOL_THRESHOLD = 5.0%   (daily std)
  MODERATE_VOL_THRESHOLD = 1.5%  (daily std)

Ported from the legacy forecast policy service and adapted for
Zhanlu's PostgreSQL-based ForecastAccuracyLog.

Provides:
- bias correction from historical mean signed error (MAPE/accuracy log)
- volatility regime detection (NORMAL / MODERATE / HIGH)
- diagnosis signal bias integration
- horizon-scaled adjustments with caps
"""

# P2.14: Module-level constants for import by other modules / tests
HIGH_VOL_THRESHOLD = 5.0     # 5% daily std (canonical source of truth)
MODERATE_VOL_THRESHOLD = 1.5  # 1.5% daily std
FORECAST_BIAS_CAP_PCT = 2.5  # combined ±2.5% absolute cap

import logging
import statistics
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class VolatilityRegime(str, Enum):
    NORMAL = "normal"
    MODERATE = "moderate"
    HIGH = "high"


@dataclass
class ForecastPolicyMetrics:
    """Bias + volatility diagnostics from recent forecast history."""

    bias_pct: float = 0.0
    volatility_regime: VolatilityRegime = VolatilityRegime.NORMAL
    vol_multiplier: float = 1.0
    diagnosis_bias: float = 0.0
    sample_count: int = 0
    mean_signed_error: float = 0.0

    # Volatility std (for reference)
    daily_vol_std: float = 0.0

    @classmethod
    def create_baseline(cls) -> ForecastPolicyMetrics:
        return cls()


class ForecastPolicyService:
    """Adaptive forecast policy — bias correction + volatility scaling.

    Data sources (Zhanlu-adapted):
    - Bias: ForecastAccuracyLog (mean_signed_error, sample_count)
    - Volatility: daily price returns (std of log returns)
    - Diagnosis bias: from compute_target() diagnosis dict (optional)
    """

    # Thresholds for volatility regime classification (daily std of returns)
    # P2.14: import from module-level canonical constants (single source of truth)
    HIGH_VOL_THRESHOLD = HIGH_VOL_THRESHOLD   # 5% daily std
    MODERATE_VOL_THRESHOLD = MODERATE_VOL_THRESHOLD  # 1.5% daily std

    # Volatility multiplier per regime
    VOL_MULTIPLIER = {
        VolatilityRegime.NORMAL: 1.0,
        VolatilityRegime.MODERATE: 1.1,
        VolatilityRegime.HIGH: 1.25,
    }

    # Bias correction cap (±2.5% — conservative to avoid over-correction)
    BIAS_CAP_PCT = 2.5

    # Damping factor for signed error → bias conversion (0.0–1.0)
    # Lower = more conservative correction
    BIAS_DAMPING = 0.35

    @staticmethod
    def detect_volatility_regime(
        returns: list[float],
    ) -> tuple[VolatilityRegime, float]:
        """Classify volatility regime from daily log returns.

        Args:
            returns: Daily log returns (fractional, e.g. 0.02 = 2%)

        Returns:
            (regime, multiplier)
        """
        if not returns or len(returns) < 3:
            return VolatilityRegime.NORMAL, 1.0

        try:
            std = statistics.stdev(returns)
        except (statistics.StatisticsError, ZeroDivisionError):
            return VolatilityRegime.NORMAL, 1.0

        if std >= ForecastPolicyService.HIGH_VOL_THRESHOLD:
            regime = VolatilityRegime.HIGH
        elif std >= ForecastPolicyService.MODERATE_VOL_THRESHOLD:
            regime = VolatilityRegime.MODERATE
        else:
            regime = VolatilityRegime.NORMAL

        multiplier = ForecastPolicyService.VOL_MULTIPLIER[regime]
        return regime, multiplier

    @staticmethod
    def compute_bias_pct(
        signed_errors: list[float],
    ) -> float:
        """Compute bias correction percentage from signed errors.

        Positive signed_error = model over-predicted → correct downward.
        Negative signed_error = model under-predicted → correct upward.

        Formula: bias_pct = -mean_signed_error × BIAS_DAMPING × 100, capped ±2.5%.

        Args:
            signed_errors: List of (predicted - actual) / actual values.

        Returns:
            Bias correction in percentage points (applied as (1 + bias_pct/100)).
        """
        if not signed_errors or len(signed_errors) < 3:
            return 0.0

        mse = statistics.mean(signed_errors)
        raw_bias = -mse * ForecastPolicyService.BIAS_DAMPING * 100.0
        return max(
            -ForecastPolicyService.BIAS_CAP_PCT,
            min(ForecastPolicyService.BIAS_CAP_PCT, raw_bias),
        )

    @staticmethod
    def compute_from_accuracy_log(
        db: Any,
        product_key: str,
        org_id: str = "default-org",
        lookback_days: int = 30,
        diagnosis: dict | None = None,
        daily_returns: list[float] | None = None,
    ) -> ForecastPolicyMetrics:
        """Compute full policy metrics from ForecastAccuracyLog + price data.

        Args:
            db: SQLAlchemy Session (sync).
            product_key: Product identifier.
            org_id: Organization ID.
            lookback_days: Number of days to look back in accuracy log.
            diagnosis: Optional diagnosis dict from compute_target().
            daily_returns: Optional daily log returns for volatility detection.

        Returns:
            ForecastPolicyMetrics with all fields populated.
        """
        from datetime import datetime, timedelta, timezone

        from app.models.forecasting import ForecastTarget, ForecastAccuracyLog

        cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)

        # Resolve target_id from product_key (ForecastAccuracyLog has no product_key column)
        target = db.query(ForecastTarget).filter(
            ForecastTarget.product_key == product_key,
            ForecastTarget.is_deleted == False,  # noqa: E712
        ).first()
        if not target:
            rows = []
        else:
            rows = (
                db.query(ForecastAccuracyLog)
                .filter(
                    ForecastAccuracyLog.org_id == org_id,
                    ForecastAccuracyLog.target_id == target.id,
                    ForecastAccuracyLog.created_date >= cutoff,
                )
                .all()
            )

        signed_errors = [
            r.realized_error for r in rows
            if r.realized_error is not None
        ]

        bias_pct = ForecastPolicyService.compute_bias_pct(signed_errors)
        mse = statistics.mean(signed_errors) if signed_errors else 0.0

        returns_list = daily_returns or []
        regime, vol_mult = ForecastPolicyService.detect_volatility_regime(
            returns_list
        )

        diagnosis_bias = 0.0
        if diagnosis:
            diagnosis_bias = diagnosis.get("signal_bias", 0.0)

        vol_std = (
            statistics.stdev(returns_list)
            if len(returns_list) >= 3
            else 0.0
        )

        return ForecastPolicyMetrics(
            bias_pct=round(bias_pct, 3),
            volatility_regime=regime,
            vol_multiplier=round(vol_mult, 3),
            diagnosis_bias=round(diagnosis_bias, 3),
            sample_count=len(signed_errors),
            mean_signed_error=round(mse, 4),
            daily_vol_std=round(vol_std, 4),
        )

    @staticmethod
    def apply(
        forecast: dict[int, list[float]],
        metrics: ForecastPolicyMetrics,
    ) -> tuple[dict[int, list[float]], dict[str, Any]]:
        """Apply bias correction + volatility scaling to forecast horizons.

        Args:
            forecast: {horizon_days: [values]} dict.
            metrics: Computed ForecastPolicyMetrics.

        Returns:
            (adjusted_forecast, detail_dict)
        """
        # Horizon scaling: longer horizons have larger uncertainty
        HORIZON_SCALE = {3: 0.7, 7: 1.0, 30: 1.3}

        total_adj = metrics.bias_pct + metrics.diagnosis_bias
        adjusted = {}
        for horizon, values in forecast.items():
            if not values:
                adjusted[horizon] = values
                continue
            scale = HORIZON_SCALE.get(horizon, 1.0)
            adj_pct = total_adj * scale
            factor = 1.0 + adj_pct / 100.0
            # Apply volatility multiplier
            factor *= metrics.vol_multiplier
            adjusted[horizon] = [
                round(v * factor, 4) for v in values
            ]

        detail = {
            "bias_pct": metrics.bias_pct,
            "diagnosis_bias": metrics.diagnosis_bias,
            "volatility_regime": metrics.volatility_regime.value,
            "vol_multiplier": metrics.vol_multiplier,
            "sample_count": metrics.sample_count,
            "mean_signed_error": metrics.mean_signed_error,
            "horizon_adjustment": {
                h: round(total_adj * HORIZON_SCALE.get(h, 1.0), 3)
                for h in forecast
            },
        }

        return adjusted, detail


# ---------------------------------------------------------------------------
# P2.14: Unified bias correction entry point
# ---------------------------------------------------------------------------

def apply_bias_correction(
    forecast_value: float,
    accuracy_log_bias_pct: float = 0.0,
    hitl_delta_pct: float = 0.0,
) -> float:
    """Apply combined bias correction with single ±2.5% absolute cap.

    Both the accuracy-log bias and HITL author delta compete for the same
    budget; they never compound beyond the cap.

    Parameters
    ----------
    forecast_value : float
        The raw forecast value to adjust.
    accuracy_log_bias_pct : float
        Bias from ForecastAccuracyLog (damped signed error), already in pct.
    hitl_delta_pct : float
        Human-in-the-loop author override delta, in pct.

    Returns
    -------
    float
        Adjusted forecast value.
    """
    total_adj = accuracy_log_bias_pct + hitl_delta_pct
    capped_adj = max(-FORECAST_BIAS_CAP_PCT, min(FORECAST_BIAS_CAP_PCT, total_adj))
    return forecast_value * (1.0 + capped_adj / 100.0)
