"""External MySQL data source — reads time series for the forecast engine.

The forecast engine's `_fetch_series` traditionally reads from a bound
KnowledgeBase via QueryService. For dashboard products, the
authoritative price history lives in the external MySQL mirror (`md_t_lz_price`
and ERP fallback tables at 10.10.10.49). This module provides a thin
adapter that returns the same `pd.DataFrame` shape the engine expects
(columns `ds` + `y`), so the downstream pipeline (quality → models →
ensemble → guard → scenarios) runs unchanged.

Sentinel handling: `md_t_lz_price.FTAXPRICE` is a VARCHAR and contains
"F7" / "NaN" / "" sentinels for some blowing-agent rows. These are
dropped before coercion to float, matching the dashboard's
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
# before float coercion. Matches dashboard behavior.
_SENTINEL_VALUES = {"F7", "NaN", "nan", "None", "", "null", "NULL"}


class MysqlDataSource:
    """Read a time series from the external MySQL mirror into a pandas DataFrame.

    Usage::

        src = MysqlDataSource()  # uses get_mysql_engine() by default
        df = src.read_history(target.datasource)

    The `datasource` dict shape::

        {
            "source": "mysql_mirror",
            "table": "md_t_lz_price",
            "time_col": "FDATE",
            "measure": "FTAXPRICE",
            "where": "MATERIAL_NAME = '<material>'",
            "granularity": "day"
        }
    """

    def __init__(self, engine: Optional[Engine] = None) -> None:
        # Inject for tests; default to the shared external MySQL engine.
        self._engine = engine

    def _resolve_engine(self) -> Optional[Engine]:
        if self._engine is not None:
            return self._engine
        from app.core.mysql_db import get_mysql_engine
        return get_mysql_engine()

    def read_history(self, datasource: dict) -> pd.DataFrame:
        """Read history rows and return a DataFrame with columns ['ds', 'y'].

        Phase F1: When ``datasource`` includes ``extra_sources`` (list of
        additional source descriptors), this method unions them with the
        primary source, deduplicates by date, and returns the longest
        available history. Each extra_source has the same shape as a
        primary datasource (table, time_col, measure, where). Used for
        products where ``md_t_lz_price`` only covers a recent window but
        the ``lz_v_<product>_data`` views extend further back.

        Raises:
            RuntimeError: If the external MySQL mirror is unreachable or the query returns
                zero rows after sentinel filtering.
        """
        eng = self._resolve_engine()
        if eng is None:
            raise RuntimeError(
                "External MySQL unreachable: get_mysql_engine() returned None. "
                "Check MYSQL_URL in .env."
            )

        primary = _source_to_dict(datasource)
        extras = [
            _source_to_dict(extra) for extra in (datasource.get("extra_sources") or [])
        ]
        all_sources = [primary] + extras

        dfs = []
        for src in all_sources:
            try:
                df = self._read_single_source(eng, src)
                if not df.empty:
                    dfs.append(df)
            except Exception as exc:
                # Tolerate failures on extra sources — primary source must
                # succeed; extras are best-effort enrichments. Re-raise if
                # this IS the primary source.
                if src is primary:
                    raise RuntimeError(
                        f"Primary source {src.get('table')} failed: {exc}"
                    ) from exc
                logger.warning("Extra source %s failed: %s", src.get("table"), exc)

        if not dfs:
            raise RuntimeError(
                f"No rows returned from any source "
                f"({[s.get('table') for s in all_sources]})."
            )

        df = pd.concat(dfs, ignore_index=True)
        # Deduplicate by date — primary wins over extras (md_t_lz_price has
        # clean daily prices; lz_v_*_data views may have duplicate dates
        # for the same product). Primary rows come first in the concat, so
        # keep="first" preserves them on overlap.
        df = df.drop_duplicates(subset=["ds"], keep="first")
        df = df.sort_values("ds").reset_index(drop=True)

        if df.empty:
            raise RuntimeError(
                "No valid rows after sentinel/NaN filtering for any source."
            )

        return df[["ds", "y"]]

    def _read_single_source(self, eng: Engine, src: dict) -> pd.DataFrame:
        """Read a single source descriptor into a DataFrame."""
        table = src.get("table", "md_t_lz_price")
        time_col = src.get("time_col", "FDATE")
        measure = src.get("measure", "FTAXPRICE")
        where = src.get("where", "1=1")

        # NOTE: table/column names come from our own seeded datasource
        # descriptors (not user input), so interpolation is safe here.
        # SQLite (used in tests) accepts backticks as identifier quotes;
        # MySQL uses them natively.
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
                f"No rows returned from {table} WHERE {where}."
            )

        df = pd.DataFrame(rows, columns=["ds", "y"])
        df["ds"] = pd.to_datetime(df["ds"], errors="coerce")
        df = df[~df["y"].astype(str).isin(_SENTINEL_VALUES)]
        df["y"] = pd.to_numeric(df["y"], errors="coerce")
        df = df.dropna(subset=["ds", "y"])
        # Within-source dedup by date (mean of multiple entries on same day)
        df = df.groupby("ds", as_index=False).agg({"y": "mean"})
        df = df.sort_values("ds").reset_index(drop=True)
        return df


def _source_to_dict(source: dict) -> dict:
    """Normalize a datasource descriptor to the standard shape."""
    return {
        "table": source.get("table", "md_t_lz_price"),
        "time_col": source.get("time_col", "FDATE"),
        "measure": source.get("measure", "FTAXPRICE"),
        "where": source.get("where", "1=1"),
    }
