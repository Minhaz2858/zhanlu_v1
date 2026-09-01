"""Naive-baseline honesty gate.

The critical check: if the ensemble's backtest MAPE >= seasonal_naive
MAPE, the ensemble is not adding value.  In that case we:

1. Set ``below_naive_baseline=true`` (stored as schema field)
2. Publish the seasonal_naive forecast instead of the ensemble
3. Emit a warning log

This ensures the platform never silently ships bad forecasts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class GuardResult:
    """Result of the honesty gate evaluation."""

    below_naive_baseline: bool
    published_forecast: pd.Series  # ensemble or naive fallback (or blend)
    ensemble_mape: float
    naive_mape: float
    blend_ratio: float = 1.0  # 1.0 = pure ensemble, 0.0 = pure naive, 0.5 = equal blend


def evaluate_guard(
    ensemble_forecast: pd.Series,
    naive_forecast: pd.Series,
    ensemble_mape: float,
    naive_mape: float,
    soft_blend_enabled: bool = False,
    soft_blend_margin_pct: float = 2.0,
) -> GuardResult:
    """Compare ensemble vs seasonal_naive backtest error.

    Parameters
    ----------
    ensemble_forecast : pd.Series
        The ensemble's point forecast (h steps).
    naive_forecast : pd.Series
        The seasonal_naive point forecast (h steps).
    ensemble_mape : float
        Ensemble backtest error (MAPE).
    naive_mape : float
        SeasonalNaive backtest error (MAPE).
    soft_blend_enabled : bool
        If True, blend ensemble with naive when MAPE is within margin% instead
        of hard-discarding the ensemble entirely.
    soft_blend_margin_pct : float
        Percentage margin (default 2.0).  When ensemble MAPE exceeds naive MAPE
        by <= margin% of naive MAPE, the forecasts are proportionally blended.

    Returns
    -------
    GuardResult
    """
    # If naive didn't produce a valid error, assume ensemble is better
    if naive_mape >= float("inf"):
        logger.debug("Guard: naive MAPE is inf — skipping gate, using ensemble")
        return GuardResult(
            below_naive_baseline=False,
            published_forecast=ensemble_forecast,
            ensemble_mape=ensemble_mape,
            naive_mape=naive_mape,
        )

    # If ensemble failed entirely, fall back to naive
    if ensemble_mape >= float("inf"):
        logger.warning("Guard: ensemble MAPE is inf — falling back to naive")
        return GuardResult(
            below_naive_baseline=True,
            published_forecast=naive_forecast,
            ensemble_mape=ensemble_mape,
            naive_mape=naive_mape,
            blend_ratio=0.0,
        )

    below = ensemble_mape >= naive_mape
    blend_ratio = 1.0

    if not below:
        # Ensemble wins — use pure ensemble
        logger.info(
            "Guard: ensemble beats naive (%.4f < %.4f) — using ensemble forecast.",
            ensemble_mape,
            naive_mape,
        )
        published = ensemble_forecast
    elif soft_blend_enabled and naive_mape > 0:
        # Soft-blend gate: blend proportionally within margin
        excess = ensemble_mape - naive_mape
        margin_abs = (soft_blend_margin_pct / 100.0) * naive_mape
        if excess <= margin_abs:
            # Within margin — blend ensemble with naive
            blend_ratio = max(0.0, 1.0 - excess / margin_abs)
            published = ensemble_forecast * blend_ratio + naive_forecast * (1.0 - blend_ratio)
            logger.info(
                "Guard: soft-blend — ensemble=%.4f, naive=%.4f, "
                "margin=%.2f%%, blend_ratio=%.2f",
                ensemble_mape, naive_mape, soft_blend_margin_pct, blend_ratio,
            )
            below = False  # don't mark as below_naive for soft-blend
        else:
            logger.warning(
                "Guard: **HONESTY GATE TRIGGERED** — ensemble MAPE %.4f >= "
                "naive MAPE %.4f (exceeds %.1f%% margin).  Falling back to seasonal_naive.",
                ensemble_mape, naive_mape, soft_blend_margin_pct,
            )
            published = naive_forecast
            blend_ratio = 0.0
    else:
        logger.warning(
            "Guard: **HONESTY GATE TRIGGERED** — ensemble MAPE %.4f >= "
            "naive MAPE %.4f.  Falling back to seasonal_naive.",
            ensemble_mape,
            naive_mape,
        )
        published = naive_forecast
        blend_ratio = 0.0

    return GuardResult(
        below_naive_baseline=below,
        published_forecast=published,
        ensemble_mape=ensemble_mape,
        naive_mape=naive_mape,
        blend_ratio=blend_ratio,
    )
