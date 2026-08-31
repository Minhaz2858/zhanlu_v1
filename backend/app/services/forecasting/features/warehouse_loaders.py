"""Warehouse-backed Tier 3 loaders — read fundamental data directly from
the enterprise's ERP data warehouse mirror (external MySQL, configured per app).

These loaders bypass the PG external-feed store and read directly from MySQL
tables that the enterprise updates daily. They serve as automatic fallbacks
for the Wave 3 PG-backed loaders (OperatingRateLoader, InventoryLoader,
ImportPriceLoader) which return empty when no external data has been
ingested into PG.

Design rules (mirrors exogenous_loaders.py):
- NEVER leak future data: queries use only rows with date <= today.
- All quantities in tons (base-unit columns are already converted).
- Material mapping: product_key → name patterns, matched via LIKE against the
  configured material-name column after JOINing on the configured ids.
- Graceful no-op: return empty DataFrame on any error (zero regression risk).

Schema is fully config-driven. The app's domain config supplies a
"warehouse_schema" block naming the tables/columns for each loader, e.g.::

    {
      "production": {"header": "erp_production", "line": "erp_production_line",
                     "material": "material", "date_col": "FDATE",
                     "material_id_col": "FMATERIALID", "qty_col": "FREALQTY",
                     "material_name_col": "material_name"},
      "inventory":  {"table": "erp_inventory", "material": "material",
                     "update_col": "FUPDATETIME", "qty_col": "FBASEQTY",
                     "material_id_col": "FMATERIALID",
                     "material_name_col": "material_name"},
      "purchase":   {"header": "erp_purchase", "line": "erp_purchase_line",
                     "material": "material", "supplier": "erp_supplier",
                     "date_col": "FDATE", "material_id_col": "FMATERIALID",
                     "qty_col": "FREALQTY", "amount_col": "FALLAMOUNTEXCEPTDISCOUNT",
                     "supplier_id_col": "FSUPPLIERID",
                     "supplier_name_col": "FNAME",
                     "currency_col": "CURRENCY_NAME",
                     "material_name_col": "material_name"}
    }

Empty config (or a missing block) = loaders no-op cleanly — fully generic.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.services.domain_config import get_domain_config

logger = logging.getLogger(__name__)

# product_key → list of material-name patterns for LIKE matching. Patterns
# come from the app's domain config ("warehouse_grade_patterns" block);
# empty config = empty dict → no grade matching (loaders no-op cleanly).
_PRODUCT_NAME_PATTERNS: dict[str, list[str]] = dict(
    (get_domain_config("") or {}).get("warehouse_grade_patterns") or {}
)

# Table/column schema for each loader, from the app's domain config
# ("warehouse_schema" block). Empty config = no loader can run.
_WAREHOUSE_SCHEMA: dict[str, dict[str, str]] = dict(
    (get_domain_config("") or {}).get("warehouse_schema") or {}
)


def _name_patterns(product_id: str) -> list[str]:
    return _PRODUCT_NAME_PATTERNS.get(product_id, [])


def _build_name_where(patterns: list[str]) -> tuple[str, dict]:
    clauses = []
    params: dict[str, str] = {}
    for i, pat in enumerate(patterns):
        key = f"p{i}"
        clauses.append(f"m.{_WAREHOUSE_SCHEMA_JOIN_MATERIAL_NAME_COL} LIKE :{key}")
        params[key] = f"%{pat}%"
    return " OR ".join(clauses), params


# Columns used for material-name matching. Keep in sync with the schema
# material_name_col value so name filters reference the configured column.
_WAREHOUSE_SCHEMA_JOIN_MATERIAL_NAME_COL = (
    (_WAREHOUSE_SCHEMA.get("production") or {}).get("material_name_col")
    or (_WAREHOUSE_SCHEMA.get("inventory") or {}).get("material_name_col")
    or (_WAREHOUSE_SCHEMA.get("purchase") or {}).get("material_name_col")
    or "material_name"
)


def _resolve_mysql_engine() -> Engine | None:
    try:
        from app.core.mysql_db import get_mysql_engine
        return get_mysql_engine()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# T3.1 Production Throughput Loader
# ---------------------------------------------------------------------------


class WarehouseProductionLoader:
    """Load daily production volume from the ERP instock ledger.

    Reads the configured production header/line/material tables and sums the
    configured qty column per date. Requires a domain-config "warehouse_schema"
    block plus material-name patterns; otherwise returns empty (no-op).
    """

    def __init__(
        self,
        product_id: str,
        lookback_days: int = 30,
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

        schema = _WAREHOUSE_SCHEMA.get("production")
        if not schema:
            return pd.DataFrame(columns=["date", "production_t"])

        hdr = schema["header"]
        line = schema["line"]
        mat = schema["material"]
        dcol = schema["date_col"]
        mid = schema["material_id_col"]
        qty = schema["qty_col"]
        mname = schema["material_name_col"]

        name_where, name_params = _build_name_where(patterns)
        cutoff = date.today() - timedelta(days=self.lookback_days)

        sql = text(f"""
            SELECT h.{dcol} AS ds,
                   SUM(e.{qty}) AS qty
            FROM {hdr} h
            JOIN {line} e ON e.FID = h.FID
            JOIN {mat} m ON e.{mid} = CAST(m.material_id AS UNSIGNED)
            WHERE h.{dcol} >= :cutoff
              AND h.{dcol} <= :today
              AND ({name_where})
            GROUP BY h.{dcol}
            ORDER BY h.{dcol} ASC
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
    """Load inventory levels over time from the ERP stock ledger.

    Reads the configured inventory table (update time + qty columns) JOIN the
    configured material table for name mapping.

    Returns DataFrame with columns ``['date', 'inventory_t']`` — inventory
    level (sum of qty across all lots/warehouses) per update date.
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

        schema = _WAREHOUSE_SCHEMA.get("inventory")
        if not schema:
            return pd.DataFrame(columns=["date", "inventory_t"])

        tbl = schema["table"]
        mat = schema["material"]
        ucol = schema["update_col"]
        qty = schema["qty_col"]
        mid = schema["material_id_col"]
        mname = schema["material_name_col"]

        name_where, name_params = _build_name_where(patterns)
        cutoff = date.today() - timedelta(days=self.lookback_days)

        sql = text(f"""
            SELECT DATE(i.{ucol}) AS ds,
                   SUM(i.{qty}) AS qty
            FROM {tbl} i
            JOIN {mat} m ON i.{mid} = CAST(m.material_id AS UNSIGNED)
            WHERE i.{ucol} >= :cutoff
              AND i.{ucol} <= :today
              AND ({name_where})
            GROUP BY DATE(i.{ucol})
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

    Reads the configured purchase header/line/material/supplier tables and
    computes unit_price = amount / qty per transaction. The ``is_import``
    flag is True when the supplier's configured currency column differs from
    the configured local-currency value (default CNY).

    Returns DataFrame with columns:
      ``['date', 'purchase_price', 'supplier_name', 'is_import']``
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

        schema = _WAREHOUSE_SCHEMA.get("purchase")
        if not schema:
            return pd.DataFrame(
                columns=["date", "purchase_price", "supplier_name", "is_import"]
            )

        hdr = schema["header"]
        line = schema["line"]
        mat = schema["material"]
        sup = schema.get("supplier")
        dcol = schema["date_col"]
        mid = schema["material_id_col"]
        qty = schema["qty_col"]
        amount = schema["amount_col"]
        sup_id = schema["supplier_id_col"]
        sup_name = schema["supplier_name_col"]
        cur_col = schema["currency_col"]
        mname = schema["material_name_col"]
        local_currency = schema.get("local_currency", "人民币")

        name_where, name_params = _build_name_where(patterns)
        cutoff = date.today() - timedelta(days=self.lookback_days)

        if sup:
            sql = text(f"""
                SELECT h.{dcol} AS ds,
                       e.{amount} AS amount,
                       e.{qty} AS qty,
                       s.{sup_name} AS supplier_name,
                       s.{cur_col} AS currency
                FROM {hdr} h
                JOIN {line} e ON e.FID = h.FID
                JOIN {mat} m ON e.{mid} = CAST(m.material_id AS UNSIGNED)
                LEFT JOIN {sup} s ON h.{sup_id} = s.{sup_id}
                WHERE h.{dcol} >= :cutoff
                  AND h.{dcol} <= :today
                  AND ({name_where})
                  AND e.{qty} > 0
                  AND e.{amount} > 0
                ORDER BY h.{dcol} ASC
            """)
        else:
            sql = text(f"""
                SELECT h.{dcol} AS ds,
                       e.{amount} AS amount,
                       e.{qty} AS qty,
                       '' AS supplier_name,
                       NULL AS currency
                FROM {hdr} h
                JOIN {line} e ON e.FID = h.FID
                JOIN {mat} m ON e.{mid} = CAST(m.material_id AS UNSIGNED)
                WHERE h.{dcol} >= :cutoff
                  AND h.{dcol} <= :today
                  AND ({name_where})
                  AND e.{qty} > 0
                  AND e.{amount} > 0
                ORDER BY h.{dcol} ASC
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
        df["is_import"] = df["currency"].fillna(local_currency) != local_currency
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
