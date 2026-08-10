"""Oilchem (隆众资讯) data loader — Brent, naphtha, C5/C9 daily prices.

Reads from the ``oilchem_data`` table in EDIA's MySQL warehouse
(``aipdp_data_warehouse_prod`` @ 10.10.10.49:3306) via the existing
``EDIA_MYSQL_URL`` environment variable.

Used as exogenous features for C5/C9 product forecasting — raw material
cost signals (Brent crude → Japan naphtha → C5/C9) provide leading
indicators for downstream price movements.

Flag-gated via ``FORECAST_OILCHEM_EXOG_ENABLED`` (default false).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

_DEFAULT_COLUMNS = [
    "date",
    "brent_crude",
    "japan_naphtha",
    "c5_price",
    "c9_price",
]


class OilchemLoader:
    """Load oilchem daily price data from EDIA MySQL warehouse."""

    def __init__(self, mysql_url: str | None = None):
        self._url = mysql_url or os.environ.get("EDIA_MYSQL_URL", "")
        self._data: pd.DataFrame | None = None

    def _ensure_data(self) -> pd.DataFrame:
        if self._data is not None:
            return self._data

        if not self._url:
            logger.info("OilchemLoader: EDIA_MYSQL_URL not set — skipping")
            self._data = pd.DataFrame()
            return self._data

        try:
            from sqlalchemy import create_engine, text

            engine = create_engine(self._url, pool_pre_ping=True)
            with engine.connect() as conn:
                df = pd.read_sql_query(
                    text("SELECT * FROM oilchem_data ORDER BY date ASC"),
                    conn,
                )
            engine.dispose()
        except Exception as exc:
            logger.warning("OilchemLoader: query failed — %s", exc)
            self._data = pd.DataFrame()
            return self._data

        if df.empty:
            logger.info("OilchemLoader: oilchem_data is empty")
            self._data = df
            return self._data

        # Standardize column names
        for col in ["date", "Date", "DATE"]:
            if col in df.columns:
                df["date"] = pd.to_datetime(df[col])
                break

        df = df.set_index("date").sort_index()
        # Keep only numeric columns
        num_cols = [c for c in df.columns if c not in ("date", "index")]
        df = df[num_cols].apply(pd.to_numeric, errors="coerce")

        self._data = df
        logger.info(
            "OilchemLoader: loaded %d rows, %s → %s",
            len(df), df.index[0].strftime("%Y-%m-%d") if len(df) else "n/a",
            df.index[-1].strftime("%Y-%m-%d") if len(df) else "n/a",
        )
        return self._data

    def get_dataframe(self) -> pd.DataFrame:
        """Return full oilchem DataFrame (date index, numeric columns)."""
        return self._ensure_data()

    def get_latest_prices(self) -> dict[str, float]:
        """Return dict of column → latest value."""
        df = self._ensure_data()
        if df.empty or len(df) == 0:
            return {}
        latest = df.iloc[-1]
        return {c: float(latest[c]) if pd.notna(latest[c]) else np.nan for c in df.columns}

    def get_pct_change(self, window: int = 7) -> dict[str, float]:
        """Return percentage change over the last *window* days."""
        df = self._ensure_data()
        if len(df) < window:
            return {}
        latest_window = df.iloc[-window:]
        pct = (latest_window.iloc[-1] / latest_window.iloc[0] - 1.0) * 100.0
        return {c: float(pct[c]) if pd.notna(pct[c]) else np.nan for c in df.columns}


# ---------------------------------------------------------------------------
# Singleton convenience
# ---------------------------------------------------------------------------

_oilchem_loader: OilchemLoader | None = None


def get_oilchem_loader() -> OilchemLoader:
    global _oilchem_loader
    if _oilchem_loader is None:
        _oilchem_loader = OilchemLoader()
    return _oilchem_loader
