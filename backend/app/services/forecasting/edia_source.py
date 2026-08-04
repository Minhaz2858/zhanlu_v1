"""EDIA MySQL data source — reads time series for the forecast engine.

The forecast engine's `_fetch_series` traditionally reads from a bound
KnowledgeBase via QueryService. For Ecisco BI dashboard products, the
authoritative price history lives in EDIA's MySQL mirror (`md_t_lz_price`
and ERP fallback tables at 10.10.10.49). This module provides a thin
adapter that returns the same `pd.DataFrame` shape the engine expects
(columns `ds` + `y`), so the downstream pipeline (quality → models →
ensemble → guard → scenarios) runs unchanged.

Sentinel handling: `md_t_lz_price.FTAXPRICE` is a VARCHAR and contains
"F7" / "NaN" / "" sentinels for some blowing-agent rows. These are
dropped before coercion to float, matching `market_dashboard_service`'s
logic.
"""
from __future__ import annotations

import logging
from typing import Optional

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

# Sentinel string values that appear in FTAXPRICE and must be dropped
# before float coercion. Matches market_dashboard_service behavior.
_SENTINEL_VALUES = {"F7", "NaN", "nan", "None", "", "null", "NULL"}


class EdiaMysqlDataSource:
    """Read a time series from EDIA's MySQL mirror into a pandas DataFrame.

    Usage::

        src = EdiaMysqlDataSource()  # uses get_mysql_engine() by default
        df = src.read_history(target.datasource)

    The `datasource` dict shape::

        {
            "source": "edia_mysql",
            "table": "md_t_lz_price",
            "time_col": "FDATE",
            "measure": "FTAXPRICE",
            "where": "FMATERIAL_NAME = '裂解C5-裂解C5均价'",
            "granularity": "day"
        }
    """

    def __init__(self, engine: Optional[Engine] = None) -> None:
        # Inject for tests; default to the shared EDIA MySQL engine.
        self._engine = engine

    def _resolve_engine(self) -> Optional[Engine]:
        if self._engine is not None:
            return self._engine
        from app.core.mysql_db import get_mysql_engine
        return get_mysql_engine()

    def read_history(self, datasource: dict) -> pd.DataFrame:
        """Read history rows and return a DataFrame with columns ['ds', 'y'].

        Raises:
            RuntimeError: If EDIA MySQL is unreachable or the query returns
                zero rows after sentinel filtering.
        """
        eng = self._resolve_engine()
        if eng is None:
            raise RuntimeError(
                "EDIA MySQL unreachable: get_mysql_engine() returned None. "
                "Check EDIA_MYSQL_URL in .env."
            )

        table = datasource.get("table", "md_t_lz_price")
        time_col = datasource.get("time_col", "FDATE")
        measure = datasource.get("measure", "FTAXPRICE")
        where = datasource.get("where", "1=1")

        # NOTE: table/column names come from our own seeded datasource
        # descriptors (not user input), so interpolation is safe here.
        # The `where` clause is also hardcoded in seed_ecisco_targets.py.
        # SQLite (used in tests) accepts backticks as identifier quotes;
        # MySQL uses them natively. Postgres would need double quotes,
        # but we only ever hit EDIA MySQL (and SQLite in tests).
        sql = text(
            f"SELECT `{time_col}` AS ds, `{measure}` AS y "
            f"FROM `{table}` "
            f"WHERE {where} "
            f"ORDER BY `{time_col}` ASC"
        )

        with eng.connect() as conn:
            rows = conn.execute(sql).fetchall()

        if not rows:
            raise RuntimeError(
                f"No rows returned from {table} WHERE {where}. "
                "EDIA MySQL may be empty for this series."
            )

        df = pd.DataFrame(rows, columns=["ds", "y"])
        # Parse datetime (FDATE comes back as datetime or str depending on driver)
        df["ds"] = pd.to_datetime(df["ds"], errors="coerce")
        # Drop sentinel strings before float coercion
        df = df[~df["y"].astype(str).isin(_SENTINEL_VALUES)]
        df["y"] = pd.to_numeric(df["y"], errors="coerce")
        # Drop any rows that failed coercion (NaN y or NaT ds)
        df = df.dropna(subset=["ds", "y"])

        # Deduplicate by date: multiple ERP order entries can land on the
        # same PLANDATE with different prices. Average them into a single
        # daily mean price. For md_t_lz_price this is a no-op — duplicate
        # dates have identical prices, so AVG(price) = price.
        df = df.groupby("ds", as_index=False).agg({"y": "mean"})

        df = df.sort_values("ds").reset_index(drop=True)

        if df.empty:
            raise RuntimeError(
                f"No valid rows after sentinel/NaN filtering for {table} WHERE {where}."
            )

        return df[["ds", "y"]]
