"""Price-change probability (Phase D).

Derives ``P(price[T+h] > price[T])`` from the conformal interval +
point forecast.  Assumes forecast errors are ~Gaussian centered on the
point forecast (conformal half-width ≈ ``z_{1-alpha/2} * sigma``), so
sigma is recovered from the half-width and the normal CDF gives the
probability.

Usage::

    from app.services.forecasting.conformal import calibrate
    from app.services.forecasting.price_change_probability import compute
    cal = calibrate(bt.residuals_by_horizon, alpha=0.1)
    pcp = compute(point_forecast, last_actual=y.iloc[-1],
                  calibration=cal, horizon=7)
    print(f"{pcp.p_rise:.0%} chance price rises")
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.stats import norm

from app.services.forecasting.conformal import ConformalCalibration

_DEFAULT_THRESHOLDS = [0.0, 0.02, 0.05]
# half-width at alpha=0.1 ≈ 1.645 * sigma  (z_{0.95})
_Z_90 = 1.645


@dataclass
class PriceChangeProbability:
    """Probabilistic price-change statement for one horizon."""

    p_rise: float | None  # P(price[T+h] > price[T]); None when gate published naive fallback
    p_rise_gt: dict[float, float] = field(default_factory=dict)  # threshold -> P(rise > thr)
    expected_change_pct: float = 0.0


def compute(
    point_forecast: pd.Series,
    last_actual: float,
    calibration: ConformalCalibration,
    horizon: int,
    thresholds: list[float] | None = None,
) -> PriceChangeProbability:
    """Compute the probability that price[T+h] > price[T].

    Parameters
    ----------
    point_forecast : pd.Series
        The published point forecast (ensemble or naive fallback).
    last_actual : float
        The last observed actual price (price[T]).
    calibration : ConformalCalibration
        Per-horizon half-widths from conformal calibration.
    horizon : int
        Forecast horizon (steps ahead).
    thresholds : list[float] or None
        Price-rise thresholds (as fractions, e.g. 0.05 = 5%) for which
        ``p_rise_gt`` is computed.  Default ``[0.0, 0.02, 0.05]``.

    Returns
    -------
    PriceChangeProbability
    """
    if thresholds is None:
        thresholds = _DEFAULT_THRESHOLDS

    h = min(horizon, len(point_forecast))
    point_h = float(point_forecast.iloc[h - 1])

    hw = calibration.half_widths.get(horizon)
    if hw is None or not np.isfinite(hw) or hw <= 0:
        hw = calibration.fallback_sigma_frac * (abs(point_h) + 1e-10)
    # Recover sigma from the conformal half-width.
    sigma = hw / _Z_90 if hw > 0 else abs(point_h) * 0.05 + 1e-6

    delta = point_h - last_actual
    p_rise = float(norm.cdf(delta / sigma)) if sigma > 0 else (1.0 if delta > 0 else 0.0)

    p_rise_gt: dict[float, float] = {}
    for thr in thresholds:
        # P(rise > thr) = P(delta > thr * last_actual)
        threshold_val = thr * last_actual
        if sigma > 0:
            p_rise_gt[thr] = float(norm.sf((threshold_val - delta) / sigma))
        else:
            p_rise_gt[thr] = 1.0 if delta > threshold_val else 0.0

    expected_change_pct = (delta / last_actual) if last_actual else 0.0

    return PriceChangeProbability(
        p_rise=p_rise,
        p_rise_gt=p_rise_gt,
        expected_change_pct=expected_change_pct,
    )


# ---------------------------------------------------------------------------
# P0.3: Empirical residual-CDF p_rise (replaces Gaussian assumption)
# ---------------------------------------------------------------------------

def compute_empirical(
    point_forecast_delta: float,
    residuals_by_horizon: dict[int, list[float]],
    horizon: int,
    *,
    last_actual: float = 0.0,
    thresholds: list[float] | None = None,
    below_naive_flat: bool = False,
    min_samples: int = 10,
    fallback_half_width: float | None = None,
) -> PriceChangeProbability:
    """Compute P(price[T+h] > price[T]) from the empirical residual CDF.

    Instead of assuming Gaussian errors, we directly count what fraction of
    past residuals exceed ``-delta`` (the required error for the actual to
    fall below the last price).  Laplace smoothing ``(k+1)/(n+2)`` prevents
    extreme 0/1 probabilities.

    When ``below_naive_flat=True`` (gate published naive fallback, delta ≈ 0),
    returns ``p_rise=None`` so callers can render "—" instead of 0.50.

    Falls back to the Gaussian path when ``len(residuals) < min_samples``.

    Parameters
    ----------
    point_forecast_delta : float
        ``point_forecast[h-1] - last_actual`` (already computed by caller).
    residuals_by_horizon : dict[int, list[float]]
        Walk-forward residuals per horizon from ``BacktestResult``.
    horizon : int
        Forecast horizon.
    last_actual : float
        The last observed price (for expected_change_pct).
    thresholds : list[float] or None
        Rise-threshold fractions (default [0.0, 0.02, 0.05]).
    below_naive_flat : bool
        True when honesty gate published the naive fallback.
    min_samples : int
        Minimum residual count for empirical computation.
    fallback_half_width : float or None
        Conformal half-width for Gaussian fallback when < min_samples.
    """
    if thresholds is None:
        thresholds = _DEFAULT_THRESHOLDS

    # When gate published naive fallback, suppress the misleading 0.50
    if below_naive_flat:
        return PriceChangeProbability(p_rise=None, expected_change_pct=0.0)

    residuals = residuals_by_horizon.get(horizon, [])
    residuals = [r for r in residuals if np.isfinite(r)]
    delta = point_forecast_delta

    # --- Empirical path: enough residuals ---
    if len(residuals) >= min_samples:
        # p_rise = P(residual > -delta)  with Laplace smoothing
        k = sum(1 for r in residuals if r > -delta)
        n = len(residuals)
        p_rise = (k + 1) / (n + 2)

        # Threshold probabilities: P(rise > thr * last_actual) = P(residual > thr*last - delta)
        p_rise_gt: dict[float, float] = {}
        for thr in thresholds:
            threshold_val = thr * last_actual if last_actual else 0.0
            k_thr = sum(1 for r in residuals if r > (threshold_val - delta))
            p_rise_gt[thr] = (k_thr + 1) / (n + 2)

        expected_change_pct = (delta / last_actual) if last_actual else 0.0
        return PriceChangeProbability(
            p_rise=p_rise,
            p_rise_gt=p_rise_gt,
            expected_change_pct=expected_change_pct,
        )

    # --- Fallback: Gaussian (few residuals) ---
    if fallback_half_width is not None and fallback_half_width > 0:
        sigma = fallback_half_width / _Z_90
    elif residuals:
        sigma = float(np.std(residuals))
    else:
        sigma = abs(delta) * 0.05 + 1e-6

    p_rise = float(norm.cdf(delta / sigma)) if sigma > 0 else (1.0 if delta > 0 else 0.0)

    p_rise_gt: dict[float, float] = {}
    for thr in thresholds:
        threshold_val = thr * last_actual if last_actual else 0.0
        if sigma > 0:
            p_rise_gt[thr] = float(norm.sf((threshold_val - delta) / sigma))
        else:
            p_rise_gt[thr] = 1.0 if delta > threshold_val else 0.0

    expected_change_pct = (delta / last_actual) if last_actual else 0.0
    return PriceChangeProbability(
        p_rise=p_rise,
        p_rise_gt=p_rise_gt,
        expected_change_pct=expected_change_pct,
    )
