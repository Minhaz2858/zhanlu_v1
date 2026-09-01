"""Inventory signal computation — pure functions with zero I/O (Wave 3 T3.2).

Computes inventory-side metrics (level changes, supply pressure, divergence vs price)
from inventory data (``InventoryLoader.load()``). Mirrors ``demand_signal.py``
structure (dataclass + pure function).

These metrics feed the AI Brief for genuine 供需研判 (supply-demand judgment) —
rising inventory + falling price → high supply pressure (bearish for spot price);
falling inventory + rising price → tightening (bullish for spot price).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd


_MIN_ROWS_FOR_SIGNAL = 8
_WINDOW_DAYS = 28
_PRESSURE_HIGH_THRESHOLD = 15.0   # >= +15% → high (supply pressure)
_PRESSURE_LOW_THRESHOLD = -15.0   # <= -15% → low (tight)


@dataclass
class InventorySignal:
    """Inventory-side metrics for one product at a point in time.

    Attributes
    ----------
    product_id : str
        Forecast product identifier.
    inventory_4wk_change_pct : Optional[float]
        % change in latest 4 weeks vs prior 4 weeks. Positive = inventory build
        (supply pressure); negative = inventory drawdown (tightening).
    inventory_vs_price_divergence : Optional[float]
        inventory_pct_change - price_pct_change over the same 4-week window.
        High positive = inventory up + price down (strong supply pressure).
        High negative = inventory down + price up (tightening).
    days_of_supply : Optional[float]
        Reserved for future use (requires consumption data); always None.
    inventory_pressure : str
        ``"high"`` (supply pressure, >= +15%), ``"low"`` (tight, <= -15%),
        or ``"normal"`` (within band).
    has_sufficient_data : bool
        True when ``len(df) >= 8`` and core metrics were computed.
    """
    product_id: str
    inventory_4wk_change_pct: Optional[float] = None
    inventory_vs_price_divergence: Optional[float] = None
    days_of_supply: Optional[float] = None
    inventory_pressure: str = "normal"
    has_sufficient_data: bool = False


def _pct_change(recent_avg: float, prior_avg: float) -> Optional[float]:
    if prior_avg is None or recent_avg is None or prior_avg == 0:
        return None
    return float(np.around((recent_avg - prior_avg) / prior_avg * 100, 2))


def compute_inventory_signal(
    inventory_df: pd.DataFrame | None,
    price_df: pd.DataFrame | None = None,
    *,
    product_id: str = "",
) -> InventorySignal:
    """Compute inventory metrics from a DataFrame of (date, inventory_t) rows.

    Pure function — zero I/O.

    Parameters
    ----------
    inventory_df : pd.DataFrame
        Output of ``InventoryLoader.load()``. Columns: ``['date', 'inventory_t']``.
    price_df : pd.DataFrame, optional
        Domestic price history, columns ``['date', 'price']``.
    product_id : str
        Forwarded into the result dataclass.

    Returns
    -------
    InventorySignal
    """
    sig = InventorySignal(product_id=product_id)

    if inventory_df is None or inventory_df.empty:
        return sig
    if len(inventory_df) < _MIN_ROWS_FOR_SIGNAL:
        return sig

    df = inventory_df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["inventory_t"] = pd.to_numeric(df["inventory_t"], errors="coerce")
    df = df.dropna(subset=["date", "inventory_t"]).sort_values("date")
    if len(df) < _MIN_ROWS_FOR_SIGNAL:
        return sig

    recent_avg = float(df["inventory_t"].iloc[-_WINDOW_DAYS:].mean())
    prior_avg = float(df["inventory_t"].iloc[-_WINDOW_DAYS * 2:-_WINDOW_DAYS].mean())
    change_pct = _pct_change(recent_avg, prior_avg)
    sig.inventory_4wk_change_pct = change_pct
    sig.has_sufficient_data = True

    # Pressure classification
    if change_pct is not None:
        if change_pct >= _PRESSURE_HIGH_THRESHOLD:
            sig.inventory_pressure = "high"
        elif change_pct <= _PRESSURE_LOW_THRESHOLD:
            sig.inventory_pressure = "low"
        else:
            sig.inventory_pressure = "normal"

    # Divergence vs price
    if price_df is not None and not price_df.empty:
        try:
            pdf = price_df.copy()
            pdf["date"] = pd.to_datetime(pdf["date"], errors="coerce")
            price_col = "price" if "price" in pdf.columns else (
                "y" if "y" in pdf.columns else None
            )
            if price_col is not None:
                pdf[price_col] = pd.to_numeric(pdf[price_col], errors="coerce")
                pdf = pdf.dropna(subset=["date", price_col]).sort_values("date")
                if len(pdf) >= _WINDOW_DAYS * 2:
                    recent_price = float(pdf[price_col].iloc[-_WINDOW_DAYS:].mean())
                    prior_price = float(pdf[price_col].iloc[-_WINDOW_DAYS * 2:-_WINDOW_DAYS].mean())
                    price_change = _pct_change(recent_price, prior_price)
                    if price_change is not None and change_pct is not None:
                        sig.inventory_vs_price_divergence = round(
                            change_pct - price_change, 2
                        )
        except Exception:
            sig.inventory_vs_price_divergence = None

    return sig