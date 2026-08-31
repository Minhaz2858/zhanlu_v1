"""A/B backtest for domain signals (Phase F3 Task F3).

Compares forecast MAPE/directional accuracy with domain signal overlay
ON vs OFF on walk-forward backtest data. The domain signal overlay
encodes supply-chain economics (feedstock→intermediate→downstream
elasticities + per-product seasonal rules).

Decision logic:
  - If ON beats OFF by >= 5% relative MAPE improvement on >= 60% of
    products → recommend enabling by default.
  - If ON is neutral or hurts → recommend leaving OFF.
  - If mixed (some products win, others lose) → leave OFF until per-product
    gating is added.

Usage:
    cd backend && venv/bin/python -m pytest tests/test_domain_signals_ab.py -v -s
    # OR run the ab_backtest() function with real warehouse data
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

from app.services.forecasting.domain_signals import (
    compute_causal_chain_adjustment,
    compute_seasonal_adjustment,
)

logger = logging.getLogger(__name__)


_IMPROVEMENT_THRESHOLD = 0.05  # 5% relative MAPE reduction
_WIN_FRACTION_THRESHOLD = 0.60  # >= 60% of products must improve


@dataclass
class ABResult:
    product_id: str
    n_origins: int
    mape_off: float
    mape_on: float
    improvement_pct: float  # (off - on) / off; positive = domain signals helped
    dir_acc_off: float
    dir_acc_on: float

    @property
    def wins(self) -> bool:
        return self.improvement_pct > _IMPROVEMENT_THRESHOLD


def apply_overlay(
    forecast_value: float,
    product_id: str,
    as_of_date: pd.Timestamp,
    naphtha_pct_change: float | None = None,
) -> float:
    """Apply the combined seasonal + causal-chain overlay to a forecast value.

    Returns the adjusted forecast (forecast * (1 + total_pct / 100)).
    """
    seasonal = compute_seasonal_adjustment(product_id, as_of_date.month)
    causal = compute_causal_chain_adjustment(product_id, naphtha_pct_change)
    total_pct = seasonal + causal
    return forecast_value * (1.0 + total_pct / 100.0)


def ab_evaluate(
    y: pd.Series,
    feedstock_pct: pd.Series | None,
    forecast_fn,
    product_id: str,
    horizons: tuple[int, ...] = (7,),
    min_train: int = 60,
) -> ABResult:
    """A/B backtest on a single series with a forecast function.

    Parameters
    ----------
    y : pd.Series
        Target price series, datetime index.
    feedstock_pct : pd.Series | None
        Naphtha price series (same index), or None if not available.
    forecast_fn : callable
        ``forecast_fn(train_y, h) -> np.ndarray`` of length h.
    product_id : str
        Used for product-specific elasticities/seasonal rules.
    horizons : tuple
        Forecast horizons to evaluate (currently only the first).
    min_train : int
        Minimum training window size.

    Returns
    -------
    ABResult with MAPE/dir_acc on vs off and the improvement metric.
    """
    h = horizons[0]
    n = len(y)
    if n < min_train + h + 1:
        return ABResult(
            product_id=product_id, n_origins=0,
            mape_off=float("nan"), mape_on=float("nan"),
            improvement_pct=0.0,
            dir_acc_off=float("nan"), dir_acc_on=float("nan"),
        )

    abs_errs_off: list[float] = []
    abs_errs_on: list[float] = []
    dir_correct_off: list[int] = []
    dir_correct_on: list[int] = []

    # 5 origins evenly spaced across the tail
    test_origins = list(range(min_train, n - h, max(1, (n - h - min_train) // 5)))[-5:]

    for origin in test_origins:
        train_y = y.iloc[:origin]
        actual = float(y.iloc[origin + h - 1])
        # Skip if actual is zero/NaN (MAPE undefined)
        if not np.isfinite(actual) or abs(actual) < 1e-9:
            continue
        last_train = float(train_y.iloc[-1])

        try:
            fc = forecast_fn(train_y, h)
        except Exception as exc:
            logger.debug("forecast_fn failed: %s", exc)
            continue

        pred_off = float(fc[-1])

        # Compute feedstock % change in the training window
        naphtha_pct = None
        if feedstock_pct is not None:
            n_train = feedstock_pct.iloc[:origin].dropna()
            if len(n_train) >= 2:
                naphtha_pct = (n_train.iloc[-1] - n_train.iloc[0]) / n_train.iloc[0] * 100.0

        as_of = y.index[origin + h - 1] if hasattr(y.index, "__getitem__") else None
        if as_of is None:
            as_of = pd.Timestamp.utcnow()

        pred_on = apply_overlay(
            pred_off, product_id=product_id,
            as_of_date=as_of, naphtha_pct_change=naphtha_pct,
        )

        # Track absolute percentage errors
        abs_errs_off.append(abs(pred_off - actual) / abs(actual))
        abs_errs_on.append(abs(pred_on - actual) / abs(actual))

        # Track directional accuracy: did forecast go up/down vs last train?
        actual_dir = 1 if actual > last_train else (0 if actual < last_train else 0.5)
        off_dir = 1 if pred_off > last_train else (0 if pred_off < last_train else 0.5)
        on_dir = 1 if pred_on > last_train else (0 if pred_on < last_train else 0.5)
        dir_correct_off.append(int(off_dir == actual_dir))
        dir_correct_on.append(int(on_dir == actual_dir))

    if not abs_errs_off:
        return ABResult(
            product_id=product_id, n_origins=0,
            mape_off=float("nan"), mape_on=float("nan"),
            improvement_pct=0.0,
            dir_acc_off=float("nan"), dir_acc_on=float("nan"),
        )

    mape_off = float(np.mean(abs_errs_off))
    mape_on = float(np.mean(abs_errs_on))
    improvement = (mape_off - mape_on) / mape_off if mape_off > 0 else 0.0
    return ABResult(
        product_id=product_id,
        n_origins=len(abs_errs_off),
        mape_off=mape_off,
        mape_on=mape_on,
        improvement_pct=improvement,
        dir_acc_off=float(np.mean(dir_correct_off)),
        dir_acc_on=float(np.mean(dir_correct_on)),
    )


def decide(results: list[ABResult]) -> dict:
    """Decide whether to enable domain signals based on A/B results.

    Returns a dict with the decision and supporting statistics.
    """
    valid = [r for r in results if r.n_origins > 0 and np.isfinite(r.mape_off)]
    if not valid:
        return {
            "recommendation": "leave_off",
            "reason": "no valid results",
        }

    n_total = len(valid)
    n_wins = sum(1 for r in valid if r.wins)
    win_fraction = n_wins / n_total
    avg_improvement = float(np.mean([r.improvement_pct for r in valid]))
    median_improvement = float(np.median([r.improvement_pct for r in valid]))

    if win_fraction >= _WIN_FRACTION_THRESHOLD and median_improvement >= _IMPROVEMENT_THRESHOLD:
        rec = "enable"
        reason = (
            f"{n_wins}/{n_total} ({win_fraction:.0%}) products improved by "
            f">{_IMPROVEMENT_THRESHOLD:.0%} relative MAPE; median improvement "
            f"{median_improvement:+.1%}."
        )
    elif win_fraction <= 0.40:
        rec = "leave_off"
        reason = (
            f"Only {n_wins}/{n_total} ({win_fraction:.0%}) products improved; "
            f"median improvement {median_improvement:+.1%}. Domain signals "
            f"are net-neutral or harmful on this dataset."
        )
    else:
        rec = "leave_off"
        reason = (
            f"Mixed results: {n_wins}/{n_total} ({win_fraction:.0%}) won; "
            f"median improvement {median_improvement:+.1%} (below "
            f"{_IMPROVEMENT_THRESHOLD:.0%} threshold). Need per-product "
            f"gating before enabling."
        )

    return {
        "recommendation": rec,
        "reason": reason,
        "n_products": n_total,
        "n_wins": n_wins,
        "win_fraction": win_fraction,
        "median_improvement_pct": median_improvement,
        "mean_improvement_pct": avg_improvement,
        "details": [
            {
                "product_id": r.product_id,
                "n_origins": r.n_origins,
                "mape_off": r.mape_off,
                "mape_on": r.mape_on,
                "improvement_pct": r.improvement_pct,
                "wins": r.wins,
            }
            for r in valid
        ],
    }