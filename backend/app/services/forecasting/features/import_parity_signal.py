"""Import-parity signal computation — pure functions with zero I/O (Wave 3 T3.3).

Computes import-vs-domestic parity metrics:
- ``import_parity_gap`` = (latest_domestic - latest_import) / latest_domestic
    Positive → import is cheaper than domestic → ceiling pressure on domestic
    prices (imports cap upward movement).
- ``ceiling_pressure`` True when gap > 0 (imports are pressuring domestic from
  above — limiting domestic price upside).
- ``import_window_open`` True when gap >= +5% (import is materially cheaper,
  making imports attractive enough to cap domestic prices).

Mirrors ``demand_signal.py`` structure (dataclass + pure function).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd


_MIN_ROWS_IMPORT = 4
_MIN_ROWS_DOMESTIC = 4
_WINDOW_OPEN_THRESHOLD = 0.05   # >= +5% gap → import window open


@dataclass
class ImportParitySignal:
    """Import-vs-domestic parity metrics for one product at a point in time.

    Attributes
    ----------
    product_id : str
        Forecast product identifier.
    import_parity_gap : Optional[float]
        (latest_domestic - latest_import) / latest_domestic. Positive = import
        is cheaper than domestic → domestic prices face ceiling pressure.
    import_window_open : bool
        True when ``import_parity_gap >= +5%`` (imports materially cheaper).
    ceiling_pressure : bool
        True when ``import_parity_gap > 0`` (any positive gap means imports
        are pressuring domestic prices from above).
    has_sufficient_data : bool
        True when both ``import_df`` and ``domestic_price_df`` (when provided)
        have enough rows.
    """
    product_id: str
    import_parity_gap: Optional[float] = None
    import_window_open: bool = False
    ceiling_pressure: bool = False
    has_sufficient_data: bool = False


def _latest_value(df: pd.DataFrame, col: str) -> Optional[float]:
    if df.empty:
        return None
    val = df[col].iloc[-1]
    return float(val) if not pd.isna(val) else None


def compute_import_parity_signal(
    import_df: pd.DataFrame | None,
    domestic_price_df: pd.DataFrame | None = None,
    *,
    product_id: str = "",
) -> ImportParitySignal:
    """Compute import parity metrics from import + optional domestic price DFs.

    Pure function — zero I/O.

    Parameters
    ----------
    import_df : pd.DataFrame
        Output of ``ImportPriceLoader.load()``. Columns: ``['date', 'import_price_cny']``.
    domestic_price_df : pd.DataFrame, optional
        Domestic spot price history, columns ``['date', 'price']``.
        If absent, gap and pressure cannot be computed (they default to None / False).
    product_id : str
        Forwarded into the result dataclass.

    Returns
    -------
    ImportParitySignal
    """
    sig = ImportParitySignal(product_id=product_id)

    if import_df is None or import_df.empty:
        return sig
    if len(import_df) < _MIN_ROWS_IMPORT:
        return sig

    idf = import_df.copy()
    idf["date"] = pd.to_datetime(idf["date"], errors="coerce")
    idf["import_price_cny"] = pd.to_numeric(idf["import_price_cny"], errors="coerce")
    idf = idf.dropna(subset=["date", "import_price_cny"]).sort_values("date")
    if len(idf) < _MIN_ROWS_IMPORT:
        return sig

    if domestic_price_df is None or domestic_price_df.empty:
        # Only import data is available — no gap can be computed
        sig.has_sufficient_data = True
        return sig

    pdf = domestic_price_df.copy()
    pdf["date"] = pd.to_datetime(pdf["date"], errors="coerce")
    pdf["price"] = pd.to_numeric(pdf["price"], errors="coerce")
    pdf = pdf.dropna(subset=["date", "price"]).sort_values("date")
    if len(pdf) < _MIN_ROWS_DOMESTIC:
        return sig

    sig.has_sufficient_data = True

    latest_import = _latest_value(idf, "import_price_cny")
    latest_domestic = _latest_value(pdf, "price")
    if latest_import is None or latest_domestic is None or latest_domestic == 0:
        return sig

    gap = float(np.around((latest_domestic - latest_import) / latest_domestic, 4))
    sig.import_parity_gap = gap

    if gap > 0:
        sig.ceiling_pressure = True
    if gap >= _WINDOW_OPEN_THRESHOLD:
        sig.import_window_open = True

    return sig