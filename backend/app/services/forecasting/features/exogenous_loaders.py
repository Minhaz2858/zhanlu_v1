"""Exogenous feature loaders: feedstock prices, FX rates, and event flags.

Each loader exposes a `.load()` method returning a DataFrame with a datetime
index covering `window_start` to `window_end`.

Design rules:
- NEVER leak future data: loaders use only data <= window_end.
- All prices in CNY. FX loader converts via USDCNY rate snapshot.
- Event flags are binary columns (1 = relevant event within lookback).
- For Phase 1, data is fetched from EDIA MySQL via raw SQL (matching
  EdiaMysqlDataSource pattern), NOT via ORM.

Phase F2: Added ``ErpTxLoader`` — reads ERP transaction prices from
``sale_erp_v_{product}_data`` tables. These go back to 2018 (vs ~88 days
for ``md_t_lz_price``) and serve as exogenous features for XGBoost,
NOT as forecast targets (transaction prices ≠ market quotations).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

# Sentinel values matching EdiaMysqlDataSource
_SENTINEL_VALUES = {"F7", "NaN", "nan", "None", "", "null", "NULL"}


def _resolve_mysql_engine() -> Engine | None:
    try:
        from app.core.mysql_db import get_mysql_engine
        return get_mysql_engine()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Feedstock Price Loader
# ---------------------------------------------------------------------------

class FeedstockLoader:
    """Loads historical prices for feedstock products from EDIA MySQL.

    In Phase 1, the loader reads `md_t_lz_price` via raw SQL (same path
    as EdiaMysqlDataSource). For each feedstock key, it queries
    WHERE FMATERIAL_NAME LIKE '%{key}%' and returns the aggregated
    mean price per day.

    Returns a DataFrame with columns `{key}` for each feedstock key,
    indexed by date.
    """

    def __init__(self, engine: Engine | None = None):
        self._engine = engine

    @property
    def engine(self) -> Engine | None:
        return self._engine or _resolve_mysql_engine()

    def load(
        self,
        feedstock_keys: list[str],
        window_start: date,
        window_end: date,
    ) -> pd.DataFrame:
        """Fetch feedstock prices for the given keys.

        Returns empty DataFrame if no keys or no engine.
        """
        if not feedstock_keys or self.engine is None:
            return pd.DataFrame()

        frames: dict[str, pd.Series] = {}
        for key in feedstock_keys:
            try:
                sql = text(
                    "SELECT FDATE AS ds, FTAXPRICE AS y "
                    "FROM md_t_lz_price "
                    "WHERE FMATERIAL_NAME LIKE :pattern "
                    "AND FDATE >= :start AND FDATE <= :end "
                    "ORDER BY FDATE ASC"
                )
                with self.engine.connect() as conn:
                    rows = conn.execute(sql, {
                        "pattern": f"%{key}%",
                        "start": window_start.isoformat(),
                        "end": window_end.isoformat(),
                    }).fetchall()
                if not rows:
                    continue
                df = pd.DataFrame(rows, columns=["ds", "y"])
                df["ds"] = pd.to_datetime(df["ds"], errors="coerce")
                df = df[~df["y"].astype(str).isin(_SENTINEL_VALUES)]
                df["y"] = pd.to_numeric(df["y"], errors="coerce")
                df = df.dropna(subset=["ds", "y"])
                if df.empty:
                    continue
                # Aggregate mean per day (multiple suppliers may report same day)
                daily = df.groupby("ds")["y"].mean()
                frames[key] = daily
            except Exception as exc:
                logger.warning("Failed to load feedstock %s: %s", key, exc)

        if not frames:
            return pd.DataFrame()

        # Merge all series on date index
        combined = pd.DataFrame(frames)
        combined = combined.sort_index()
        # Return only rows within [window_start, window_end] (inclusive)
        mask = (combined.index >= pd.Timestamp(window_start)) & (
            combined.index <= pd.Timestamp(window_end)
        )
        return combined.loc[mask]


# ---------------------------------------------------------------------------
# FX Rate Loader (static v1)
# ---------------------------------------------------------------------------

class FxLoader:
    """Static FX loader — returns hardcoded USDCNY=7.10 for Phase 1.

    In Phase 2, this may be replaced with a dynamic (e.g., ECB/SAFE) fetch.
    """

    FX_RATE: float = 7.10

    def load(self, window_start: date, window_end: date) -> pd.DataFrame:
        """Return a single-column DataFrame with FX rate per date."""
        dates = pd.date_range(window_start, window_end, freq="D")
        return pd.DataFrame({"usdcny": self.FX_RATE}, index=dates)


# ---------------------------------------------------------------------------
# Event Flag Loader
# ---------------------------------------------------------------------------

class EventFlagLoader:
    """Converts intelligence events into binary feature flags.

    For each date in the window, sets event_type column = 1 if any confirmed
    event of that type occurred within lookback_days before that date.
    """

    EVENT_TYPES_OF_INTEREST = [
        "supply_disruption",
        "turnaround",
        "policy_change",
        "inventory_anomaly",
        "price_spike",
    ]

    def __init__(self, db_session=None, lookback_days: int = 30):
        self.db = db_session
        self.lookback_days = lookback_days

    def load(self, window_start: date, window_end: date) -> pd.DataFrame:
        """Return a DataFrame with binary event flag columns."""
        date_range = pd.date_range(window_start, window_end, freq="D")
        flag_df = pd.DataFrame(
            np.zeros((len(date_range), len(self.EVENT_TYPES_OF_INTEREST)), dtype=int),
            index=date_range,
            columns=self.EVENT_TYPES_OF_INTEREST,
        )
        if self.db is None:
            return flag_df

        try:
            from app.models.intelligence import IntelligenceEvent

            events = (
                self.db.query(IntelligenceEvent)
                .filter(
                    IntelligenceEvent.created_date >= window_start - timedelta(days=self.lookback_days),
                    IntelligenceEvent.created_date <= window_end,
                    IntelligenceEvent.event_type.in_(self.EVENT_TYPES_OF_INTEREST),
                    IntelligenceEvent.review_status == "approved",
                )
                .all()
            )

            for ev in events:
                ev_date = ev.created_date.date() if hasattr(ev.created_date, "date") else ev.created_date
                for d in date_range:
                    delta = (d.date() - ev_date).days
                    if 0 <= delta <= self.lookback_days:
                        col = ev.event_type
                        if col in flag_df.columns:
                            flag_df.loc[d, col] = 1
        except Exception as exc:
            logger.warning("EventFlagLoader failed: %s", exc)

        return flag_df


# ---------------------------------------------------------------------------
# ERP Transaction Price Loader (Phase F2)
# ---------------------------------------------------------------------------

class ErpTxLoader:
    """Loads ERP transaction prices from ``sale_erp_v_{product}_data``.

    ERP transactions (sales order actuals) differ from market quotations:
      - Market quotation (md_t_lz_price): daily published price for the
        product across regions/suppliers.
      - ERP transaction: actual realized price on a sales order line item.

    ERP tx prices are useful as exogenous features because they reflect
    real execution prices — but they should NEVER be used as the
    forecast target (transaction prices ≠ market prices, and orders are
    sparse / intermittent).

    Schema (from ``erp_mysql_schema.txt``)::

        sale_erp_v_异戊二烯_data(
            Unnamed: 0, qty, date, price, amount, product, supplier, ...
        )

    Returns a DataFrame with column ``erp_price`` (daily mean of all
    transactions on that day), indexed by date.
    """

    def __init__(self, engine: Engine | None = None):
        self._engine = engine

    @property
    def engine(self) -> Engine | None:
        return self._engine or _resolve_mysql_engine()

    def load(
        self,
        erp_table: str,
        product_filter: str,
        window_start: date,
        window_end: date,
    ) -> pd.DataFrame:
        """Fetch ERP transaction prices for the given product.

        Parameters
        ----------
        erp_table : str
            Table name, e.g. ``"sale_erp_v_异戊二烯_data"``.
        product_filter : str
            Value of the ``product`` column to filter by, e.g.
            ``"异戊二烯"``. May be empty to take all rows.
        window_start, window_end : date
            Inclusive date range.

        Returns
        -------
        DataFrame with single ``erp_price`` column, indexed by date.
        Empty DataFrame on failure or no data.
        """
        if self.engine is None or not erp_table:
            return pd.DataFrame()

        # Build WHERE clause with optional product filter
        where = "WHERE `date` >= :start AND `date` <= :end"
        params: dict = {
            "start": window_start.isoformat(),
            "end": window_end.isoformat(),
        }
        if product_filter:
            where += " AND `product` = :product"
            params["product"] = product_filter

        sql = text(
            f"SELECT `date` AS ds, `price` AS y "
            f"FROM `{erp_table}` "
            f"{where} "
            f"ORDER BY `date` ASC"
        )

        try:
            with self.engine.connect() as conn:
                rows = conn.execute(sql, params).fetchall()
        except Exception as exc:
            logger.warning(
                "ErpTxLoader failed for %s (filter=%s): %s",
                erp_table, product_filter, exc,
            )
            return pd.DataFrame()

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows, columns=["ds", "y"])
        df["ds"] = pd.to_datetime(df["ds"], errors="coerce")
        df = df[~df["y"].astype(str).isin(_SENTINEL_VALUES)]
        df["y"] = pd.to_numeric(df["y"], errors="coerce")
        df = df.dropna(subset=["ds", "y"])
        if df.empty:
            return pd.DataFrame()

        # Daily mean (multiple transactions per day)
        daily = df.groupby("ds")["y"].mean().rename("erp_price")
        daily = daily.sort_index()
        mask = (daily.index >= pd.Timestamp(window_start)) & (
            daily.index <= pd.Timestamp(window_end)
        )
        return pd.DataFrame(daily.loc[mask])
