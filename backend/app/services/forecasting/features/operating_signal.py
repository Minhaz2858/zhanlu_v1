"""Operating-rate signal computation — pure functions with zero I/O (Wave 3 T3.1).

Computes downstream operating-rate metrics (rolling mean, YoY change, regime
classification, divergence vs price) from ``OperatingRateLoader.load()`` output.
Mirrors ``demand_signal.py`` structure (dataclass + pure function).

Used by:
- AI Brief 供需研判 (supply-demand judgment) section
- (Future) feature_builder exogenous columns
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd


_MIN_ROWS_FOR_SIGNAL = 8
_ROLLING_WINDOW_DAYS = 28
_YOY_WINDOW_DAYS = 364
_REGIME_TIGHT_THRESHOLD = 80.0
_REGIME_LOOSE_THRESHOLD = 55.0


@dataclass
class OperatingSignal:
    """Downstream operating-rate metrics for one product at a point in time.

    Attributes
    ----------
    product_id : str
        Forecast product identifier.
    rolling_4wk_op_rate : Optional[float]
        Mean op_rate over the latest 4 weeks (%).
    yoy_change_pct : Optional[float]
        % change in op_rate vs the same 4-week window one year earlier.
    op_rate_vs_price_divergence : Optional[float]
        op_rate_pct_change - price_pct_change over the latest 4 weeks.
        Positive = op_rate rising while price is flat/down (loosening supply).
        Negative = op_rate flat/down while price is rising (tightening).
    utilization_regime : str
        ``"tight"`` (≥ 80%), ``"loose"`` (≤ 55%), or ``"normal"`` (in between).
    has_sufficient_data : bool
        True when ``len(df) >= 8`` and core metrics were computed.
    """
    product_id: str
    rolling_4wk_op_rate: Optional[float] = None
    yoy_change_pct: Optional[float] = None
    op_rate_vs_price_divergence: Optional[float] = None
    utilization_regime: str = "normal"
    has_sufficient_data: bool = False


def _pct_change(recent_avg: float, prior_avg: float) -> Optional[float]:
    """% change from prior to recent; None if prior is zero/None."""
    if prior_avg is None or recent_avg is None or prior_avg == 0:
        return None
    return float(np.around((recent_avg - prior_avg) / prior_avg * 100, 2))


def compute_operating_signal(
    op_rate_df: pd.DataFrame | None,
    price_df: pd.DataFrame | None = None,
    *,
    product_id: str = "",
) -> OperatingSignal:
    """Compute operating-rate metrics from a DataFrame of (date, op_rate) rows.

    Pure function — zero I/O. Mirrors ``compute_demand_signal`` signature.

    Parameters
    ----------
    op_rate_df : pd.DataFrame
        Output of ``OperatingRateLoader.load()``. Expected columns:
        ``['date', 'op_rate']``.
    price_df : pd.DataFrame, optional
        Domestic price history, columns ``['date', 'price']``. When provided,
        divergence vs price is computed.
    product_id : str
        Forwarded into the result dataclass for traceability.

    Returns
    -------
    OperatingSignal
    """
    sig = OperatingSignal(product_id=product_id)

    if op_rate_df is None or op_rate_df.empty:
        return sig
    if len(op_rate_df) < _MIN_ROWS_FOR_SIGNAL:
        return sig

    df = op_rate_df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["op_rate"] = pd.to_numeric(df["op_rate"], errors="coerce")
    df = df.dropna(subset=["date", "op_rate"]).sort_values("date")
    if len(df) < _MIN_ROWS_FOR_SIGNAL:
        return sig

    # Rolling 4-week mean (most recent)
    recent_avg = float(df["op_rate"].iloc[-_ROLLING_WINDOW_DAYS:].mean())
    sig.rolling_4wk_op_rate = round(recent_avg, 2)
    sig.has_sufficient_data = True

    # YoY change: same window one year earlier
    if len(df) >= _YOY_WINDOW_DAYS + _ROLLING_WINDOW_DAYS:
        prior_avg = float(df["op_rate"].iloc[
            -(_YOY_WINDOW_DAYS + _ROLLING_WINDOW_DAYS):-_YOY_WINDOW_DAYS
        ].mean())
        sig.yoy_change_pct = _pct_change(recent_avg, prior_avg)

    # Regime classification
    if recent_avg >= _REGIME_TIGHT_THRESHOLD:
        sig.utilization_regime = "tight"
    elif recent_avg <= _REGIME_LOOSE_THRESHOLD:
        sig.utilization_regime = "loose"
    else:
        sig.utilization_regime = "normal"

    # Divergence vs price (optional)
    if price_df is not None and not price_df.empty:
        try:
            pdf = price_df.copy()
            pdf["date"] = pd.to_datetime(pdf["date"], errors="coerce")
            # Accept either 'price' or 'y' column name
            price_col = "price" if "price" in pdf.columns else (
                "y" if "y" in pdf.columns else None
            )
            if price_col is not None:
                pdf[price_col] = pd.to_numeric(pdf[price_col], errors="coerce")
                pdf = pdf.dropna(subset=["date", price_col]).sort_values("date")
                if len(pdf) >= _ROLLING_WINDOW_DAYS * 2:
                    recent_price = float(pdf[price_col].iloc[-_ROLLING_WINDOW_DAYS:].mean())
                    prior_price = float(pdf[price_col].iloc[-_ROLLING_WINDOW_DAYS * 2:-_ROLLING_WINDOW_DAYS].mean())
                    op_change = _pct_change(recent_avg, recent_avg - (recent_avg - (recent_avg * (1 - sig.yoy_change_pct / 100 if sig.yoy_change_pct else 0))))
                    # Simpler: compute op_change over the same 4-week window
                    op_prior = float(df["op_rate"].iloc[-_ROLLING_WINDOW_DAYS * 2:-_ROLLING_WINDOW_DAYS].mean())
                    op_change = _pct_change(recent_avg, op_prior)
                    price_change = _pct_change(recent_price, prior_price)
                    if op_change is not None and price_change is not None:
                        sig.op_rate_vs_price_divergence = round(
                            op_change - price_change, 2
                        )
        except Exception:
            sig.op_rate_vs_price_divergence = None

    return sig