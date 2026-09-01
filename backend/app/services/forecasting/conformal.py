"""Split-conformal prediction intervals (Phase D).

Calibrated from walk-forward residuals (Phase C ``BacktestResult.residuals_by_horizon``).
For each horizon ``h``, the half-width = ``quantile(|residual_h|, 1-alpha)``.
This gives empirical coverage ~ ``1-alpha`` under exchangeability of residuals.

Regime-aware variant: maintains separate calibration sets per regime label
(bull, bear, volatile, sideways) so intervals are wider during volatile periods.

Usage::

    from app.services.forecasting.conformal import calibrate
    cal = calibrate(bt.residuals_by_horizon, alpha=0.1)
    lo, hi = cal.interval(point_forecast, horizon=7)  # 90% interval
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

_DEFAULT_ALPHA = 0.1  # 90% intervals
_FALLBACK_SIGMA_FRAC = 0.10  # ±10% of |point| when no residuals


@dataclass
class ConformalCalibration:
    """Per-horizon conformal half-widths for prediction intervals."""

    half_widths: dict[int, float]  # horizon -> half-width
    alpha: float = _DEFAULT_ALPHA
    fallback_sigma_frac: float = _FALLBACK_SIGMA_FRAC

    def interval(
        self, point_forecast: pd.Series, horizon: int
    ) -> tuple[pd.Series, pd.Series]:
        """Return (lower, upper) bound series for the given horizon.

        If no calibration exists for ``horizon``, falls back to
        ``±fallback_sigma_frac * |point|``.
        """
        h = min(horizon, len(point_forecast))
        hw = self.half_widths.get(horizon)
        if hw is None or not np.isfinite(hw):
            hw = self.fallback_sigma_frac * (
                float(np.abs(point_forecast.iloc[:h]).mean()) + 1e-10
            )
        base = point_forecast.iloc[:h]
        return (base - hw), (base + hw)

    def var(
        self, point_forecast: float, horizon: int, var_alpha: float = 0.05
    ) -> float:
        """Value-at-Risk (downside) at horizon ``h``.

        VaR = point_forecast - half_width at ``var_alpha``.
        For var_alpha=0.05 this is the 5th percentile (VaR-95%).

        If no calibration exists for ``horizon``, falls back to
        ``±fallback_sigma_frac * |point|``.
        """
        hw = self.half_widths.get(horizon)
        if hw is None or not np.isfinite(hw):
            hw = self.fallback_sigma_frac * (abs(point_forecast) + 1e-10)
        # Scale half-width from calibration alpha to var_alpha.
        # Approximate: quantile scales roughly linearly in normal tail.
        # Better: re-compute from raw residuals if available.
        # For now, linear scaling is a pragmatic approximation.
        scale = (1.0 - var_alpha) / (1.0 - self.alpha)
        return point_forecast - (hw * scale)

    def expected_magnitude(self, horizon: int) -> float:
        """Expected absolute price change magnitude at horizon ``h``.

        Approximated as the calibrated half-width (mean absolute residual).
        """
        hw = self.half_widths.get(horizon)
        if hw is None or not np.isfinite(hw):
            return float("nan")
        return hw


@dataclass
class RegimeAwareConformalCalibration:
    """Regime-aware conformal calibration with per-regime half-widths.

    During volatile or trend-break regimes, residuals are typically larger,
    so the calibration should use regime-specific residuals for wider intervals.

    When a regime has insufficient calibration data, falls back to the
    global calibration (all regimes combined).
    """

    regime_half_widths: dict[str, dict[int, float]]  # regime -> {horizon: hw}
    global_half_widths: dict[int, float]  # fallback global calibration
    alpha: float = _DEFAULT_ALPHA
    fallback_sigma_frac: float = _FALLBACK_SIGMA_FRAC
    # Multiplier applied when no regime-specific calibration exists
    regime_fallback_multiplier: float = 1.5

    def interval(
        self, point_forecast: pd.Series, horizon: int, regime: str | None = None
    ) -> tuple[pd.Series, pd.Series]:
        """Return (lower, upper) bounds using regime-aware half-widths."""
        h = min(horizon, len(point_forecast))
        hw = self._get_half_width(horizon, regime)
        if hw is None or not np.isfinite(hw):
            hw = self.fallback_sigma_frac * (
                float(np.abs(point_forecast.iloc[:h]).mean()) + 1e-10
            )
        base = point_forecast.iloc[:h]
        return (base - hw), (base + hw)

    def var(
        self, point_forecast: float, horizon: int, regime: str | None = None, var_alpha: float = 0.05
    ) -> float:
        """Value-at-Risk using regime-aware half-widths."""
        hw = self._get_half_width(horizon, regime)
        if hw is None or not np.isfinite(hw):
            hw = self.fallback_sigma_frac * (abs(point_forecast) + 1e-10)
        scale = (1.0 - var_alpha) / (1.0 - self.alpha)
        return point_forecast - (hw * scale)

    def expected_magnitude(self, horizon: int, regime: str | None = None) -> float:
        """Expected magnitude using regime-aware half-widths."""
        hw = self._get_half_width(horizon, regime)
        if hw is None or not np.isfinite(hw):
            return float("nan")
        return hw

    def _get_half_width(self, horizon: int, regime: str | None) -> float | None:
        """Get half-width for horizon, preferring regime-specific if available."""
        if regime and regime in self.regime_half_widths:
            regime_hw = self.regime_half_widths[regime]
            hw = regime_hw.get(horizon)
            if hw is not None and np.isfinite(hw):
                return hw
        # Fall back to global calibration with multiplier for uncertain regimes
        global_hw = self.global_half_widths.get(horizon)
        if global_hw is not None and np.isfinite(global_hw):
            # If we asked for a regime but didn't have it, widen the interval
            if regime and regime not in self.regime_half_widths:
                return global_hw * self.regime_fallback_multiplier
            return global_hw
        return None


def calibrate(
    residuals_by_horizon: dict[int, list[float]],
    alpha: float = _DEFAULT_ALPHA,
) -> ConformalCalibration:
    """Calibrate per-horizon half-widths from walk-forward residuals.

    Parameters
    ----------
    residuals_by_horizon : dict[int, list[float]]
        Horizon -> list of (actual - predicted) residuals from the
        walk-forward backtest (``BacktestResult.residuals_by_horizon``).
    alpha : float
        Miscoverage rate.  ``alpha=0.1`` -> 90% intervals.

    Returns
    -------
    ConformalCalibration
    """
    half_widths: dict[int, float] = {}
    for h, res in residuals_by_horizon.items():
        if not res or len(res) < 2:
            continue
        abs_res = np.abs(np.array(res, dtype=float))
        abs_res = abs_res[np.isfinite(abs_res)]
        if len(abs_res) == 0:
            continue
        half_widths[h] = float(np.quantile(abs_res, 1 - alpha))
    return ConformalCalibration(half_widths=half_widths, alpha=alpha)


def calibrate_regime_aware(
    residuals_by_regime_and_horizon: dict[str, dict[int, list[float]]],
    global_residuals_by_horizon: dict[int, list[float]],
    alpha: float = _DEFAULT_ALPHA,
    min_samples_per_regime: int = 5,
) -> RegimeAwareConformalCalibration:
    """Calibrate regime-aware per-horizon half-widths.

    Parameters
    ----------
    residuals_by_regime_and_horizon : dict[str, dict[int, list[float]]]
        Regime label -> {horizon -> list of residuals}.
    global_residuals_by_horizon : dict[int, list[float]]
        Global (all regimes combined) residuals for fallback.
    alpha : float
        Miscoverage rate.
    min_samples_per_regime : int
        Minimum number of residuals required to use regime-specific calibration.

    Returns
    -------
    RegimeAwareConformalCalibration
    """
    # Calibrate per regime
    regime_half_widths: dict[str, dict[int, float]] = {}
    for regime, res_by_h in residuals_by_regime_and_horizon.items():
        hw: dict[int, float] = {}
        for h, res in res_by_h.items():
            if not res or len(res) < min_samples_per_regime:
                continue
            abs_res = np.abs(np.array(res, dtype=float))
            abs_res = abs_res[np.isfinite(abs_res)]
            if len(abs_res) < min_samples_per_regime:
                continue
            hw[h] = float(np.quantile(abs_res, 1 - alpha))
        if hw:
            regime_half_widths[regime] = hw

    # Global fallback calibration
    global_hw: dict[int, float] = {}
    for h, res in global_residuals_by_horizon.items():
        if not res or len(res) < 2:
            continue
        abs_res = np.abs(np.array(res, dtype=float))
        abs_res = abs_res[np.isfinite(abs_res)]
        if len(abs_res) == 0:
            continue
        global_hw[h] = float(np.quantile(abs_res, 1 - alpha))

    return RegimeAwareConformalCalibration(
        regime_half_widths=regime_half_widths,
        global_half_widths=global_hw,
        alpha=alpha,
    )
