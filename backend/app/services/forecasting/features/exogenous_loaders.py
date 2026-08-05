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


# ---------------------------------------------------------------------------
# ERP Volume Loader (Phase F1)
# ---------------------------------------------------------------------------

class ErpVolumeLoader:
    """Load daily sales volume from EDIA ERP (qty column — demand leading indicator).

    Unlike ErpTxLoader which reads price, this reads sales quantity and
    aggregates by daily SUM (volume is additive, not point-in-time like price).

    Schema (same as ErpTxLoader)::

        sale_erp_v_{product}_data(
            Unnamed: 0, qty, date, price, amount, product, supplier, ...
        )

    Returns a DataFrame with columns ``['date', 'volume']`` — daily SUM of qty.
    """

    def __init__(
        self,
        product_id: str,
        lookback_days: int = 365,
        org_id: str | None = None,
    ):
        self.product_id = product_id
        self.lookback_days = lookback_days
        self.org_id = org_id
        self._engine: Engine | None = None

    @property
    def source_label(self) -> str:
        return "erp_volume"

    @property
    def engine(self) -> Engine | None:
        return self._engine or _resolve_mysql_engine()

    def load(self) -> pd.DataFrame:
        """Return pd.DataFrame with columns ['date', 'volume'] — daily SUM of qty."""
        if self.engine is None:
            return pd.DataFrame(columns=["date", "volume"])

        table = f"sale_erp_v_{self.product_id}_data"
        cutoff = date.today() - timedelta(days=self.lookback_days)

        sql = text(
            f"SELECT `date`, `qty` FROM `{table}` "
            f"WHERE `date` >= :cutoff ORDER BY `date` ASC"
        )

        try:
            with self.engine.connect() as conn:
                rows = conn.execute(sql, {"cutoff": cutoff}).fetchall()
        except Exception as exc:
            logger.warning(
                "ErpVolumeLoader failed for %s: %s", table, exc,
            )
            return pd.DataFrame(columns=["date", "volume"])

        if not rows:
            return pd.DataFrame(columns=["date", "volume"])

        df = pd.DataFrame(rows, columns=["date", "volume"])
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
        df = df.dropna(subset=["date", "volume"])
        if df.empty:
            return pd.DataFrame(columns=["date", "volume"])

        # Daily SUM — volume is additive (unlike price which uses mean)
        df = df.set_index("date").resample("D").sum().reset_index()
        df = df.dropna(subset=["volume"])
        return df


# ---------------------------------------------------------------------------
# Supplier Dispersion Loader (Phase F1)
# ---------------------------------------------------------------------------

class SupplierDispersionLoader:
    """Load per-supplier price spread from md_t_lz_price (supplier-ladder signal).

    Queries daily prices per supplier for a given product, then computes the
    max-min spread across suppliers each day. A widening spread signals market
    tension or supply disruption — an early warning indicator for the AI Brief.

    Schema::

        md_t_lz_price(FDATE, FTAXPRICE, FMATERIAL_NAME)
        FMATERIAL_NAME = "{product}-{supplier}"

    Returns a DataFrame with columns ``['date', 'spread', 'supplier_count']``
    where spread = max(price) - min(price) across suppliers that day.
    """

    def __init__(
        self,
        product_id: str,
        lookback_days: int = 365,
        org_id: str | None = None,
    ):
        self.product_id = product_id
        self.lookback_days = lookback_days
        self.org_id = org_id
        self._engine: Engine | None = None

    @property
    def source_label(self) -> str:
        return "supplier_dispersion"

    @property
    def engine(self) -> Engine | None:
        return self._engine or _resolve_mysql_engine()

    def load(self) -> pd.DataFrame:
        """Return pd.DataFrame with columns ['date', 'spread', 'supplier_count']."""
        if self.engine is None:
            return pd.DataFrame(columns=["date", "spread", "supplier_count"])

        cutoff = date.today() - timedelta(days=self.lookback_days)

        sql = text(
            "SELECT FDATE, FTAXPRICE, FMATERIAL_NAME "
            "FROM md_t_lz_price "
            "WHERE FMATERIAL_NAME LIKE :pattern "
            "AND FDATE >= :cutoff "
            "ORDER BY FDATE ASC"
        )

        try:
            with self.engine.connect() as conn:
                rows = conn.execute(sql, {
                    "pattern": f"%{self.product_id}%",
                    "cutoff": cutoff.isoformat(),
                }).fetchall()
        except Exception as exc:
            logger.warning(
                "SupplierDispersionLoader failed for %s: %s",
                self.product_id, exc,
            )
            return pd.DataFrame(columns=["date", "spread", "supplier_count"])

        if not rows:
            return pd.DataFrame(columns=["date", "spread", "supplier_count"])

        df = pd.DataFrame(rows, columns=["date", "price", "material_name"])
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df[~df["price"].astype(str).isin(_SENTINEL_VALUES)]
        df["price"] = pd.to_numeric(df["price"], errors="coerce")
        df = df.dropna(subset=["date", "price"])
        if df.empty:
            return pd.DataFrame(columns=["date", "spread", "supplier_count"])

        # Extract supplier from FMATERIAL_NAME ({product}-{supplier})
        df["supplier"] = df["material_name"].str.extract(
            rf"^{self.product_id}[\s-]+(.+)", expand=False
        ).fillna(df["material_name"])

        # Per day: compute spread (max - min) and supplier count
        daily = (
            df.groupby("date")["price"]
            .agg(["max", "min", "count"])
            .reset_index()
        )
        daily["spread"] = daily["max"] - daily["min"]
        daily["supplier_count"] = daily["count"]

        return daily[["date", "spread", "supplier_count"]]
