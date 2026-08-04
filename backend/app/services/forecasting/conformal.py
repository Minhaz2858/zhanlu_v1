"""Split-conformal prediction intervals (Phase D).

Calibrated from walk-forward residuals (Phase C ``BacktestResult.residuals_by_horizon``).
For each horizon ``h``, the half-width = ``quantile(|residual_h|, 1-alpha)``.
This gives empirical coverage ~ ``1-alpha`` under exchangeability of residuals.

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
