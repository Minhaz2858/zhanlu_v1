"""Demand signal computation — pure functions with zero I/O (Phase F1).

Computes demand-side metrics from volume data (ErpVolumeLoader output).
These metrics feed the AI Brief for genuine 供需研判 (supply-demand judgment)
and optionally feed the XGBoost exogenous features in Wave 1.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd


@dataclass
class DemandSignal:
    """Rich demand-side metrics for a single product at a point in time."""
    product_id: str
    rolling_4wk_vol: Optional[float] = None
    yoy_change_pct: Optional[float] = None
    vol_price_divergence: Optional[float] = None
    demand_trend: str = "stable"  # rising / falling / stable
    recent_vol: Optional[float] = None
    vol_momentum_4wk: Optional[float] = None
    has_sufficient_data: bool = False


def compute_demand_signal(
    volume_df: pd.DataFrame,
    price_df: pd.DataFrame | None = None,
    *,
    product_id: str = "",
    rolling_window: int = 28,
    yoy_window: int = 364,
) -> DemandSignal:
    """Compute demand metrics from volume and optional price data.

    Args:
        volume_df: pd.DataFrame with columns ['date', 'volume'].
                   Output of ErpVolumeLoader.
        price_df: Optional pd.DataFrame with columns ['date', 'price'].
                  Used for volume-price divergence computation.
        product_id: Identifier string.
        rolling_window: Days for rolling average (default 28 = 4 weeks).
        yoy_window: Days for YoY comparison (default 364).

    Returns:
        DemandSignal dataclass.
    """
    if volume_df.empty:
        return DemandSignal(product_id=product_id)

    df = volume_df.copy()
    df = df.sort_values("date")
    df = df.set_index("date")

    total_rows = len(df)
    if total_rows < 7:  # need at least a week
        return DemandSignal(product_id=product_id)

    ds = DemandSignal(product_id=product_id)
    ds.has_sufficient_data = True

    # --- Rolling 4-week average volume ---
    if total_rows >= rolling_window:
        ds.rolling_4wk_vol = round(
            float(df["volume"].rolling(rolling_window).mean().iloc[-1]), 1
        )

    # --- Recent volume (latest day) ---
    ds.recent_vol = float(df["volume"].iloc[-1])

    # --- Volume momentum (4-week change) ---
    if total_rows >= rolling_window:
        recent_avg = df["volume"].iloc[-rolling_window:].mean()
        prior_avg = df["volume"].iloc[-(rolling_window * 2):-rolling_window].mean()
        if prior_avg > 0 and not np.isnan(prior_avg):
            ds.vol_momentum_4wk = round(
                float((recent_avg - prior_avg) / prior_avg * 100.0), 1
            )

    # --- YoY volume change ---
    if total_rows >= yoy_window:
        recent_yoy = df["volume"].iloc[-rolling_window:].mean()
        prior_yoy = df["volume"].iloc[-(yoy_window + rolling_window):-yoy_window].mean()
        if prior_yoy > 0 and not np.isnan(prior_yoy):
            ds.yoy_change_pct = round(
                float((recent_yoy - prior_yoy) / prior_yoy * 100.0), 1
            )

    # --- Demand trend classification ---
    if ds.vol_momentum_4wk is not None:
        if ds.vol_momentum_4wk > 10:
            ds.demand_trend = "rising"
        elif ds.vol_momentum_4wk < -10:
            ds.demand_trend = "falling"

    # --- Volume-price divergence ---
    if price_df is not None and not price_df.empty:
        p_df = price_df.copy().sort_values("date").set_index("date")
        # Align dates
        common_dates = df.index.intersection(p_df.index)
        if len(common_dates) >= rolling_window:
            vol_chg = (
                df.loc[common_dates]["volume"].iloc[-rolling_window:].mean()
                / df.loc[common_dates]["volume"].iloc[-(rolling_window * 2):-rolling_window].mean()
                - 1.0
            )
            price_chg = (
                p_df.loc[common_dates]["price"].iloc[-rolling_window:].mean()
                / p_df.loc[common_dates]["price"].iloc[-(rolling_window * 2):-rolling_window].mean()
                - 1.0
            )
            if not np.isnan(vol_chg) and not np.isnan(price_chg):
                # Positive divergence: volume rising while price falling (hidden demand)
                # Negative divergence: volume falling while price rising (demand weakening)
                ds.vol_price_divergence = round(
                    float(vol_chg - price_chg) * 100.0, 1
                )

    return ds


def compute_supplier_ladder_signal(
    dispersion_df: pd.DataFrame,
    *,
    product_id: str = "",
    recent_days: int = 30,
) -> dict:
    """Derive supplier-ladder signal from dispersion data.

    Args:
        dispersion_df: pd.DataFrame with columns ['date', 'spread', 'supplier_count'].
                       Output of SupplierDispersionLoader.
        recent_days: Window for recent trend analysis.

    Returns:
        dict with spread parameters.
    """
    if dispersion_df.empty:
        return {"has_data": False, "product_id": product_id}

    df = dispersion_df.sort_values("date")
    recent = df.tail(recent_days)

    return {
        "has_data": True,
        "product_id": product_id,
        "avg_spread": round(float(recent["spread"].mean()), 2),
        "max_spread": round(float(recent["spread"].max()), 2),
        "avg_supplier_count": round(float(recent["supplier_count"].mean()), 1),
        "spread_trend": (
            "widening"
            if float(recent["spread"].iloc[-1]) > float(recent["spread"].iloc[0]) * 1.15
            else (
                "narrowing"
                if float(recent["spread"].iloc[-1]) < float(recent["spread"].iloc[0]) * 0.85
                else "stable"
            )
        ),
        "recent_days": recent_days,
    }
