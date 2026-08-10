"""Warehouse-backed Tier 3 loaders — read fundamental data directly from
the customer's ERP data warehouse (aipdp_data_warehouse_prod @ 10.10.10.49).

These loaders bypass the PG external-feed store and read directly from MySQL
tables that the customer updates daily. They serve as automatic fallbacks
for the Wave 3 PG-backed loaders (OperatingRateLoader, InventoryLoader,
ImportPriceLoader) which return empty when no external data has been
ingested into PG.

Design rules (mirrors exogenous_loaders.py):
- NEVER leak future data: queries use only rows with FDATE <= today.
- All quantities in tons (FBASEQTY / FREALQTY are already in base unit).
- Material mapping: product_key → Chinese name patterns, matched via LIKE
  against ``material.material_name`` after JOINing on
  ``FMATERIALID = CAST(material.material_id AS UNSIGNED)``.
- Graceful no-op: return empty DataFrame on any error (zero regression risk).

Tables used:
  T3.1 Production Throughput:
    erp_t_sp_instock (header: FID, FDATE) 
    JOIN erp_t_sp_instockentry (line: FMATERIALID, FREALQTY)
    JOIN material (FMATERIALID → material_name)
  T3.2 Inventory:
    erp_t_stk_inventory (FMATERIALID, FBASEQTY, FUPDATETIME)
    JOIN material
  T3.3 Purchase Price:
    erp_t_stk_instock (header: FID, FDATE, FSUPPLIERID)
    JOIN erp_t_stk_instockentry (line: FMATERIALID, FREALQTY, FALLAMOUNTEXCEPTDISCOUNT)
    JOIN material + erp_t_bd_supplier
  T3.4 Upstream Market:
    oilchem_data (price_date, brent_crude, japan_naphtha, industrial_cracking_c5, industrial_cracking_c9)
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

# product_key → list of Chinese name patterns for LIKE matching.
# Multiple patterns per product handle grade variants (e.g. "30#工业用裂解碳五").
_PRODUCT_NAME_PATTERNS: dict[str, list[str]] = {
    "isoprene":               ["异戊二烯"],
    "piperylene":             ["间戊二烯"],
    "dcpd":                   ["双环戊二烯"],
    "cracked_c5":             ["裂解碳五", "裂解C5"],
    "cracked_c9":             ["裂解碳九", "裂解C9"],
    "sis":                    ["SIS"],
    "blowing_agent":          ["戊烷"],
    "mixed_trimethylbenzene": ["混三甲苯"],
    "styrene":                ["苯乙烯"],
    "c5_resin":               ["碳五石油树脂", "石油树脂"],
    "ethylene_carbon_black":  ["乙烯炭黑"],
    "industrial_hexane":      ["工业己烷"],
    "raffinate_c5":           ["抽余碳五"],
}


def _resolve_mysql_engine() -> Engine | None:
    try:
        from app.core.mysql_db import get_mysql_engine
        return get_mysql_engine()
    except Exception:
        return None


def _name_patterns(product_id: str) -> list[str]:
    """Return Chinese name LIKE patterns for a product_key."""
    return _PRODUCT_NAME_PATTERNS.get(product_id, [])


def _build_name_where(patterns: list[str]) -> tuple[str, dict]:
    """Build a WHERE clause fragment for material name LIKE matching."""
    if not patterns:
        return "1=1", {}
    clauses = []
    params: dict = {}
    for i, p in enumerate(patterns):
        key = f"nm{i}"
        clauses.append(f"m.material_name LIKE :{key}")
        params[key] = f"%{p}%"
    return " OR ".join(clauses), params


# ---------------------------------------------------------------------------
# T3.1 Production Throughput Loader
# ---------------------------------------------------------------------------

class WarehouseProductionLoader:
    """Load daily production volume from ERP production instock records.

    Reads ``erp_t_sp_instock`` (header, has FDATE) JOIN ``erp_t_sp_instockentry``
    (line items, has FREALQTY + FMATERIALID) JOIN ``material`` (name mapping).

    Returns DataFrame with columns ``['date', 'production_t']`` — daily SUM
    of FREALQTY (actual received quantity) for the given product.

    This is a direct production throughput proxy: when daily throughput
    drops, operating rate has dropped.
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
        return "warehouse_production"

    @property
    def engine(self) -> Engine | None:
        return self._engine or _resolve_mysql_engine()

    def load(self) -> pd.DataFrame:
        """Return DataFrame ['date', 'production_t'] — daily production volume."""
        if self.engine is None:
            return pd.DataFrame(columns=["date", "production_t"])

        patterns = _name_patterns(self.product_id)
        if not patterns:
            return pd.DataFrame(columns=["date", "production_t"])

        name_where, name_params = _build_name_where(patterns)
        cutoff = date.today() - timedelta(days=self.lookback_days)

        sql = text(f"""
            SELECT h.FDATE AS ds,
                   SUM(e.FREALQTY) AS qty
            FROM erp_t_sp_instock h
            JOIN erp_t_sp_instockentry e ON e.FID = h.FID
            JOIN material m ON e.FMATERIALID = CAST(m.material_id AS UNSIGNED)
            WHERE h.FDATE >= :cutoff
              AND h.FDATE <= :today
              AND ({name_where})
            GROUP BY h.FDATE
            ORDER BY h.FDATE ASC
        """)

        params = {
            **name_params,
            "cutoff": cutoff.isoformat(),
            "today": date.today().isoformat(),
        }

        try:
            with self.engine.connect() as conn:
                rows = conn.execute(sql, params).fetchall()
        except Exception as exc:
            logger.warning(
                "WarehouseProductionLoader failed for %s: %s",
                self.product_id, exc,
            )
            return pd.DataFrame(columns=["date", "production_t"])

        if not rows:
            return pd.DataFrame(columns=["date", "production_t"])

        df = pd.DataFrame(rows, columns=["date", "production_t"])
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df["production_t"] = pd.to_numeric(df["production_t"], errors="coerce")
        df = df.dropna(subset=["date", "production_t"])
        if df.empty:
            return pd.DataFrame(columns=["date", "production_t"])

        # Resample to daily (fill gaps with 0 — no production = 0 throughput)
        df = df.set_index("date").resample("D").sum().reset_index()
        return df


# ---------------------------------------------------------------------------
# T3.2 Inventory Time-Series Loader
# ---------------------------------------------------------------------------

class WarehouseInventoryLoader:
    """Load inventory levels over time from ERP stock ledger.

    Reads ``erp_t_stk_inventory`` (50K rows, FUPDATETIME, FBASEQTY)
    JOIN ``material`` for name mapping.

    Returns DataFrame with columns ``['date', 'inventory_t']`` — inventory
    level (sum of FBASEQTY across all lots/warehouses) per update date.

    Unlike the PG-backed ``InventoryLoader`` (which reads from
    ``forecast_external_points``), this reads the raw ERP stock ledger
    directly — no ingestion pipeline needed.
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
        return "warehouse_inventory"

    @property
    def engine(self) -> Engine | None:
        return self._engine or _resolve_mysql_engine()

    def load(self) -> pd.DataFrame:
        """Return DataFrame ['date', 'inventory_t'] — inventory level per date."""
        if self.engine is None:
            return pd.DataFrame(columns=["date", "inventory_t"])

        patterns = _name_patterns(self.product_id)
        if not patterns:
            return pd.DataFrame(columns=["date", "inventory_t"])

        name_where, name_params = _build_name_where(patterns)
        cutoff = date.today() - timedelta(days=self.lookback_days)

        sql = text(f"""
            SELECT DATE(i.FUPDATETIME) AS ds,
                   SUM(i.FBASEQTY) AS qty
            FROM erp_t_stk_inventory i
            JOIN material m ON i.FMATERIALID = CAST(m.material_id AS UNSIGNED)
            WHERE i.FUPDATETIME >= :cutoff
              AND i.FUPDATETIME <= :today
              AND ({name_where})
            GROUP BY DATE(i.FUPDATETIME)
            ORDER BY ds ASC
        """)

        params = {
            **name_params,
            "cutoff": cutoff.isoformat(),
            "today": date.today().isoformat(),
        }

        try:
            with self.engine.connect() as conn:
                rows = conn.execute(sql, params).fetchall()
        except Exception as exc:
            logger.warning(
                "WarehouseInventoryLoader failed for %s: %s",
                self.product_id, exc,
            )
            return pd.DataFrame(columns=["date", "inventory_t"])

        if not rows:
            return pd.DataFrame(columns=["date", "inventory_t"])

        df = pd.DataFrame(rows, columns=["date", "inventory_t"])
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df["inventory_t"] = pd.to_numeric(df["inventory_t"], errors="coerce")
        df = df.dropna(subset=["date"])
        if df.empty:
            return pd.DataFrame(columns=["date", "inventory_t"])

        # Forward-fill inventory (level persists until next update)
        df = df.set_index("date").resample("D").last().ffill().reset_index()
        return df


# ---------------------------------------------------------------------------
# T3.3 Purchase Price Loader
# ---------------------------------------------------------------------------

class WarehousePurchasePriceLoader:
    """Load purchase unit prices from ERP procurement instock records.

    Reads ``erp_t_stk_instock`` (header: FID, FDATE, FSUPPLIERID)
    JOIN ``erp_t_stk_instockentry`` (line: FMATERIALID, FREALQTY,
    FALLAMOUNTEXCEPTDISCOUNT) JOIN ``material`` + ``erp_t_bd_supplier``.

    Computes unit_price = FALLAMOUNTEXCEPTDISCOUNT / FREALQTY per transaction.

    Returns DataFrame with columns:
      ``['date', 'purchase_price', 'supplier_name', 'is_import']``

    The ``is_import`` flag is True when the supplier's CURRENCY_NAME is not
    "人民币" (CNY), indicating a foreign-currency transaction (import proxy).
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
        return "warehouse_purchase_price"

    @property
    def engine(self) -> Engine | None:
        return self._engine or _resolve_mysql_engine()

    def load(self) -> pd.DataFrame:
        """Return DataFrame with purchase prices per transaction."""
        if self.engine is None:
            return pd.DataFrame(
                columns=["date", "purchase_price", "supplier_name", "is_import"]
            )

        patterns = _name_patterns(self.product_id)
        if not patterns:
            return pd.DataFrame(
                columns=["date", "purchase_price", "supplier_name", "is_import"]
            )

        name_where, name_params = _build_name_where(patterns)
        cutoff = date.today() - timedelta(days=self.lookback_days)

        sql = text(f"""
            SELECT h.FDATE AS ds,
                   e.FALLAMOUNTEXCEPTDISCOUNT AS amount,
                   e.FREALQTY AS qty,
                   s.FNAME AS supplier_name,
                   s.CURRENCY_NAME AS currency
            FROM erp_t_stk_instock h
            JOIN erp_t_stk_instockentry e ON e.FID = h.FID
            JOIN material m ON e.FMATERIALID = CAST(m.material_id AS UNSIGNED)
            LEFT JOIN erp_t_bd_supplier s ON h.FSUPPLIERID = s.FSUPPLIERID
            WHERE h.FDATE >= :cutoff
              AND h.FDATE <= :today
              AND ({name_where})
              AND e.FREALQTY > 0
              AND e.FALLAMOUNTEXCEPTDISCOUNT > 0
            ORDER BY h.FDATE ASC
        """)

        params = {
            **name_params,
            "cutoff": cutoff.isoformat(),
            "today": date.today().isoformat(),
        }

        try:
            with self.engine.connect() as conn:
                rows = conn.execute(sql, params).fetchall()
        except Exception as exc:
            logger.warning(
                "WarehousePurchasePriceLoader failed for %s: %s",
                self.product_id, exc,
            )
            return pd.DataFrame(
                columns=["date", "purchase_price", "supplier_name", "is_import"]
            )

        if not rows:
            return pd.DataFrame(
                columns=["date", "purchase_price", "supplier_name", "is_import"]
            )

        df = pd.DataFrame(
            rows,
            columns=["date", "amount", "qty", "supplier_name", "currency"],
        )
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
        df["qty"] = pd.to_numeric(df["qty"], errors="coerce")
        df = df.dropna(subset=["date", "amount", "qty"])
        if df.empty:
            return pd.DataFrame(
                columns=["date", "purchase_price", "supplier_name", "is_import"]
            )

        df["purchase_price"] = df["amount"] / df["qty"]
        df["is_import"] = df["currency"].fillna("人民币") != "人民币"
        df["supplier_name"] = df["supplier_name"].fillna("")

        return df[["date", "purchase_price", "supplier_name", "is_import"]]

    def load_daily(self) -> pd.DataFrame:
        """Return daily mean purchase price — ``['date', 'purchase_price']``.

        Convenience method for use as exogenous feature in XGBoost.
        """
        df = self.load()
        if df.empty:
            return pd.DataFrame(columns=["date", "purchase_price"])
        daily = (
            df.groupby("date")["purchase_price"]
            .mean()
            .reset_index()
        )
        return daily


# ---------------------------------------------------------------------------
# T3.4 Upstream Market Data Loader (隆众资讯 / Oilchem)
# ---------------------------------------------------------------------------

class WarehouseUpstreamLoader:
    """Load daily upstream market prices from oilchem_data table.

    Reads ``oilchem_data`` (scraped from oilchem.net / 隆众资讯):
      - brent_crude: Brent crude oil price (USD/bbl)
      - japan_naphtha: Japan naphtha price
      - industrial_cracking_c5: Cracking C5 price (CNY)
      - industrial_cracking_c9: Cracking C9 price (CNY)

    Returns DataFrame indexed by date with all four columns.

    Note: The price columns may be NULL in some rows (only source_status
    JSON is populated). This loader filters to rows where at least one
    price column is non-null.
    """

    def __init__(self, lookback_days: int = 365):
        self.lookback_days = lookback_days
        self._engine: Engine | None = None

    @property
    def source_label(self) -> str:
        return "warehouse_upstream"

    @property
    def engine(self) -> Engine | None:
        return self._engine or _resolve_mysql_engine()

    def load(self) -> pd.DataFrame:
        """Return DataFrame with upstream market prices, indexed by date."""
        if self.engine is None:
            return pd.DataFrame()

        cutoff = date.today() - timedelta(days=self.lookback_days)

        sql = text("""
            SELECT price_date AS ds,
                   brent_crude,
                   japan_naphtha,
                   industrial_cracking_c5,
                   industrial_cracking_c9
            FROM oilchem_data
            WHERE price_date >= :cutoff
              AND price_date <= :today
            ORDER BY price_date ASC
        """)

        try:
            with self.engine.connect() as conn:
                rows = conn.execute(sql, {
                    "cutoff": cutoff.isoformat(),
                    "today": date.today().isoformat(),
                }).fetchall()
        except Exception as exc:
            logger.warning("WarehouseUpstreamLoader failed: %s", exc)
            return pd.DataFrame()

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(
            rows,
            columns=["ds", "brent_crude", "japan_naphtha",
                     "cracking_c5", "cracking_c9"],
        )
        df["ds"] = pd.to_datetime(df["ds"], errors="coerce")
        for col in ["brent_crude", "japan_naphtha", "cracking_c5", "cracking_c9"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df = df.dropna(subset=["ds"])
        # Keep rows where at least one price is non-null
        price_cols = ["brent_crude", "japan_naphtha", "cracking_c5", "cracking_c9"]
        df = df.dropna(subset=price_cols, how="all")
        if df.empty:
            return pd.DataFrame()

        df = df.set_index("ds").sort_index()
        return df[price_cols]
