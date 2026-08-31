"""Exogenous feature loaders: feedstock prices, FX rates, and event flags.

Each loader exposes a `.load()` method returning a DataFrame with a datetime
index covering `window_start` to `window_end`.

Design rules:
- NEVER leak future data: loaders use only data <= window_end.
- All prices in CNY. FX loader converts via USDCNY rate snapshot.
- Event flags are binary columns (1 = relevant event within lookback).
- For Phase 1, data is fetched from the external MySQL via raw SQL (matching
  MysqlDataSource pattern), NOT via ORM.

Phase F2: Added ``ErpTxLoader`` — reads ERP transaction prices from
``sale_erp_v_{product}_data`` tables. These go back to 2018 (vs ~88 days
for ``md_t_lz_price``) and serve as exogenous features for XGBoost,
NOT as forecast targets (transaction prices ≠ market quotations).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.services.domain_config import get_domain_config

logger = logging.getLogger(__name__)

# Sentinel values matching MysqlDataSource
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
    """Loads historical prices for feedstock products from the external MySQL.

    In Phase 1, the loader reads `md_t_lz_price` via raw SQL (same path
    as MysqlDataSource). For each feedstock key, it queries
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
    """FX rate loader — dynamic with fallback chain.

    P2.15: Tries to fetch the latest USDCNY from the intelligence/price
    tables via the DB session.  Fallback chain: live DB → last cached →
    hardcoded 7.10 (with warning log).

    The existing ``load(window_start, window_end)`` instance method is
    preserved for backward compatibility; the new ``FxLoader.load()`` class
    method returns a single float for the current rate.
    """

    FX_RATE_FALLBACK: float = 7.10  # P2.15: explicit fallback constant
    FX_RATE: float = 7.10           # backward compat alias

    _cached_rate: float | None = None

    @classmethod
    def load(cls, session=None) -> float:
        """Return the current USDCNY rate.

        Fallback chain: live DB query → cached value → hardcoded 7.10.
        """
        # Try 1: live DB query
        if session is not None:
            try:
                from sqlalchemy import text
                row = session.execute(
                    text("SELECT y FROM md_t_lz_price WHERE product_id = 'usdcny' ORDER BY ds DESC LIMIT 1")
                ).first()
                if row and row[0] and row[0] > 0:
                    rate = float(row[0])
                    cls._cached_rate = rate
                    return rate
            except Exception as exc:
                logger.warning("FxLoader DB query failed: %s — using fallback", exc)

        # Try 2: last cached value
        if cls._cached_rate is not None and cls._cached_rate > 0:
            logger.info("FxLoader using cached rate: %.4f", cls._cached_rate)
            return cls._cached_rate

        # Try 3: hardcoded fallback
        logger.warning("FxLoader: no live/cached data — using hardcoded %.2f", cls.FX_RATE_FALLBACK)
        return cls.FX_RATE_FALLBACK

    def fetch(self, window_start: date, window_end: date, session=None) -> pd.DataFrame:
        """Return a single-column DataFrame with FX rate per date (backward compat).

        Uses the dynamic rate if available, else the fallback.
        """
        rate = FxLoader.load(session=session)
        dates = pd.date_range(window_start, window_end, freq="D")
        return pd.DataFrame({"usdcny": rate}, index=dates)

    # Backward compat: keep old signature working
    def load_series(self, window_start: date, window_end: date) -> pd.DataFrame:
        """Deprecated: use fetch() instead."""
        return self.fetch(window_start, window_end)


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

        sale_erp_v_<product>_data(
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
            Table name, e.g. ``"sale_erp_v_<product>_data"``.
        product_filter : str
            Value of the ``product`` column to filter by (e.g. the product
            display name). May be empty to take all rows.
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

# Product_id → ERP table name map comes from the app's domain config
# ("warehouse_table_map" block). Empty config = generic fallback that builds
# ``sale_erp_v_<product_id>_data`` from the product id.
_ERP_TABLE_MAP: dict[str, str] = dict(
    (get_domain_config("") or {}).get("warehouse_table_map") or {}
)


class ErpVolumeLoader:
    """Load daily sales volume from the ERP mirror (qty column — demand leading indicator).

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

        table = _ERP_TABLE_MAP.get(self.product_id, f"sale_erp_v_{self.product_id}_data")
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


# ===========================================================================
# Wave 3 T3.1 / T3.2 / T3.3 — External-feed loaders
# ===========================================================================
#
# All three read from the Wave 3 external-feed store
# (``forecast_external_points`` JOIN ``forecast_external_series``) filtered by
# domain (operating_rate | inventory | import_price). They mirror the
# ``ErpVolumeLoader`` shape (constructor + ``.load() -> pd.DataFrame``) so
# they slot into existing call sites without refactoring.
#
# The new abstraction is the DB session — Wave 3 loaders take an explicit
# SQLAlchemy ``Session`` (injected at call time) because the data lives in
# the Wave 3 external-feed store (PG), not in MySQL like Wave 0/1 loaders.

class _ExternalSeriesBase:
    """Shared scaffolding for the three Wave 3 loaders.

    Subclasses set ``DOMAIN`` and ``METRIC_COLUMN`` (the DataFrame column
    name to use for the loaded value).
    """
    DOMAIN: str = ""
    METRIC_COLUMN: str = ""

    def __init__(
        self,
        product_id: str,
        lookback_days: int = 365,
        org_id: str | None = None,
        db_session=None,
    ):
        self.product_id = product_id
        self.lookback_days = lookback_days
        self.org_id = org_id
        self._db = db_session

    def _empty_df(self) -> pd.DataFrame:
        return pd.DataFrame(columns=["date", self.METRIC_COLUMN])

    def _query(self) -> pd.DataFrame:
        """Query the external-feed store. Returns empty df if no data.

        Guards against missing session, missing domain, or any backend error.
        """
        if self._db is None:
            return self._empty_df()
        if not self.DOMAIN:
            return self._empty_df()

        try:
            from app.models.forecasting import (
                ForecastExternalPoint,
                ForecastExternalSeries,
            )

            today = datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0,
            )
            cutoff = today - timedelta(days=self.lookback_days)

            q = (
                self._db.query(ForecastExternalPoint.date, ForecastExternalPoint.value)
                .join(ForecastExternalSeries,
                      ForecastExternalPoint.series_id == ForecastExternalSeries.id)
                .filter(ForecastExternalSeries.domain == self.DOMAIN)
                .filter(ForecastExternalPoint.date >= cutoff)
                .filter(ForecastExternalPoint.date <= today)
            )
            if self.org_id is not None:
                q = q.filter(ForecastExternalSeries.org_id == self.org_id)
            if self.product_id:
                q = q.filter(ForecastExternalSeries.product_key == self.product_id)

            rows = q.order_by(ForecastExternalPoint.date.asc()).all()
            if not rows:
                return self._empty_df()

            df = pd.DataFrame(rows, columns=["date", self.METRIC_COLUMN])
            df["date"] = pd.to_datetime(df["date"])
            return df
        except Exception as exc:
            logger.warning(
                "%s failed for product=%s: %s",
                type(self).__name__, self.product_id, exc,
            )
            return self._empty_df()

    def load(self) -> pd.DataFrame:
        """Return DataFrame ``['date', <metric>]`` for this product's series.

        Tries the PG external-feed store first. If empty, falls back to
        the MySQL warehouse loaders (which read directly from the customer's
        ERP tables — no ingestion pipeline needed).

        Empty DataFrame (with correct columns) when no data is available.
        No future leakage — only rows with ``date <= today`` are returned.
        """
        df = self._query()
        if not df.empty:
            return df
        # Fallback: try MySQL warehouse loaders
        return self._warehouse_fallback()

    def _warehouse_fallback(self) -> pd.DataFrame:
        """Try loading from MySQL warehouse when PG store is empty.

        Maps each Wave 3 domain to the corresponding warehouse loader:
          operating_rate → WarehouseProductionLoader (production throughput)
          inventory      → WarehouseInventoryLoader (stock ledger)
          import_price   → WarehousePurchasePriceLoader (purchase prices)
        """
        try:
            from app.services.forecasting.features.warehouse_loaders import (
                WarehouseProductionLoader,
                WarehouseInventoryLoader,
                WarehousePurchasePriceLoader,
            )
            common = dict(
                product_id=self.product_id,
                lookback_days=self.lookback_days,
                org_id=self.org_id,
            )
            if self.DOMAIN == "operating_rate":
                wdf = WarehouseProductionLoader(**common).load()
                if not wdf.empty:
                    logger.info(
                        "Warehouse production fallback for %s: %d days",
                        self.product_id, len(wdf),
                    )
                    return wdf.rename(columns={"production_t": self.METRIC_COLUMN})
            elif self.DOMAIN == "inventory":
                wdf = WarehouseInventoryLoader(**common).load()
                if not wdf.empty:
                    logger.info(
                        "Warehouse inventory fallback for %s: %d days",
                        self.product_id, len(wdf),
                    )
                    return wdf.rename(columns={"inventory_t": self.METRIC_COLUMN})
            elif self.DOMAIN == "import_price":
                wdf = WarehousePurchasePriceLoader(**common)
                daily = wdf.load_daily()
                if not daily.empty:
                    logger.info(
                        "Warehouse purchase-price fallback for %s: %d days",
                        self.product_id, len(daily),
                    )
                    return daily.rename(columns={"purchase_price": self.METRIC_COLUMN})
        except Exception as exc:
            logger.warning(
                "Warehouse fallback failed for %s/%s: %s",
                self.DOMAIN, self.product_id, exc,
            )
        return self._empty_df()


class OperatingRateLoader(_ExternalSeriesBase):
    """Wave 3 T3.1 — Load downstream operating rates (开工率).

    Reads from ``forecast_external_points`` JOIN ``forecast_external_series``
    filtered by ``domain='operating_rate'`` and ``product_key = product_id``.
    Returns ``['date', 'op_rate']`` DataFrame, sorted ascending, no future
    leakage. Empty DataFrame when no data is available (graceful no-op).
    """
    DOMAIN = "operating_rate"
    METRIC_COLUMN = "op_rate"


class InventoryLoader(_ExternalSeriesBase):
    """Wave 3 T3.2 — Load port / social inventory levels (库存).

    Reads from ``forecast_external_points`` JOIN ``forecast_external_series``
    filtered by ``domain='inventory'`` and ``product_key = product_id``.
    Returns ``['date', 'inventory_t']`` DataFrame.
    """
    DOMAIN = "inventory"
    METRIC_COLUMN = "inventory_t"


class ImportPriceLoader(_ExternalSeriesBase):
    """Wave 3 T3.3 — Load import / competitor prices (进口价格).

    Reads from ``forecast_external_points`` JOIN ``forecast_external_series``
    filtered by ``domain='import_price'`` and ``product_key = product_id``.
    Returns ``['date', 'import_price_cny']`` DataFrame.
    """
    DOMAIN = "import_price"
    METRIC_COLUMN = "import_price_cny"
