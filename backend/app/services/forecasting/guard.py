"""Naive-baseline honesty gate.

The critical check: if the ensemble's backtest MAPE >= seasonal_naive
MAPE, the ensemble is not adding value.  In that case we:

1. Set ``below_naive_baseline=true`` (stored as schema field)
2. Publish the seasonal_naive forecast instead of the ensemble
3. Emit a warning log

This ensures EDIA never silently ships bad forecasts.
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
    published_forecast: pd.Series  # ensemble or naive fallback
    ensemble_mape: float
    naive_mape: float


def evaluate_guard(
    ensemble_forecast: pd.Series,
    naive_forecast: pd.Series,
    ensemble_mape: float,
    naive_mape: float,
) -> GuardResult:
    """Compare ensemble vs seasonal_naive backtest error.

    Parameters
    ----------
    ensemble_forecast : pd.Series
        The ensemble's point forecast (h steps).
    naive_forecast : pd.Series
        The seasonal_naive point forecast (h steps).
    ensemble_mape : float
        Ensemble backtest error (MAPE or RMSE).
    naive_mape : float
        SeasonalNaive backtest error.

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
        )

    below = ensemble_mape >= naive_mape

    if below:
        logger.warning(
            "Guard: **HONESTY GATE TRIGGERED** — ensemble MAPE %.4f >= "
            "naive MAPE %.4f.  Falling back to seasonal_naive.",
            ensemble_mape,
            naive_mape,
        )
    else:
        logger.info(
            "Guard: ensemble beats naive (%.4f < %.4f) — using ensemble forecast.",
            ensemble_mape,
            naive_mape,
        )

    published = naive_forecast if below else ensemble_forecast

    return GuardResult(
        below_naive_baseline=below,
        published_forecast=published,
        ensemble_mape=ensemble_mape,
        naive_mape=naive_mape,
    )
