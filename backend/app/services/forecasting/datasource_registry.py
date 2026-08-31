"""DataSource strategy registry for the forecasting engine.

P1: Extracts the two data-fetching paths from ForecastEngine._fetch_series()
into a Strategy pattern, so new source types can be registered without
modifying the engine.

- EdiaMysqlStrategy  → delegates to mysql_data_source.MysqlDataSource (fast path)
- GenericKBStrategy  → generic KB-backed path via QueryService

Usage::

    strategy = get_datasource(ds.get("source", "edia_mysql"))
    series = strategy.fetch(target, db)
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Optional

import pandas as pd
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Identifier quoting rules per database dialect.
# Default is double-quote (Postgres style).
_IDENTIFIER_QUOTES: dict[str, tuple[str, str]] = {
    "mysql": ("`", "`"),
    "mariadb": ("`", "`"),
    "sqlite": ('"', '"'),
    "postgres": ('"', '"'),
    "postgresql": ('"', '"'),
    "mssql": ("[", "]"),
    "sqlserver": ("[", "]"),
}


def quote_identifier(name: str, db_type: str | None) -> str:
    """Quote an SQL identifier for the given db_type.

    MySQL/MariaDB → backticks, MSSQL → brackets, others → double quotes.
    Falls back to double quotes for unknown/None db_type.
    """
    if not name:
        return name
    open_q, close_q = _IDENTIFIER_QUOTES.get(
        (db_type or "").lower(), ('"', '"')
    )
    return f"{open_q}{name}{close_q}"


# ── Strategy ABC ───────────────────────────────────────────────────


class BaseDataSource(ABC):
    """Strategy interface for fetching time-series data.

    Each concrete strategy knows how to pull a raw pd.Series (indexed by
    datetime) from its specific data source type.
    """

    @abstractmethod
    def fetch(self, target: "ForecastTarget", db: Session) -> "pd.Series | None":
        """Pull the raw time series for a forecast target.

        Returns a pd.Series indexed by datetime, or None on failure.
        """
        ...


# ── External MySQL strategy ────────────────────────────────────────────


class EdiaMysqlStrategy(BaseDataSource):
    """Fast path for the external MySQL mirror.

    Delegates to the existing mysql_data_source.MysqlDataSource, which
    reads directly from the external MySQL mirror (md_t_lz_price etc.)
    bypassing the KB/QueryService path.
    """

    def fetch(self, target, db: Session) -> pd.Series | None:
        from app.services.forecasting.mysql_data_source import MysqlDataSource

        try:
            src = MysqlDataSource()
            df = src.read_history(target.datasource or {})
            return pd.Series(df["y"].values, index=df["ds"])
        except Exception as exc:
            logger.warning(
                "EdiaMysqlStrategy failed for target %s: %s",
                target.id, exc,
            )
            return None


# ── Generic KB strategy ────────────────────────────────────────────


class GenericKBStrategy(BaseDataSource):
    """Generic path for any KnowledgeBase-backed database.

    Reads table/time_column/measure/dimensions from target.datasource,
    builds SQL with db_type-aware identifier quoting, and executes
    via QueryService.
    """

    def fetch(self, target, db: Session) -> pd.Series | None:
        ds = target.datasource or {}

        table = ds.get("table")
        tc = ds.get("time_column")      # time column
        mc = ds.get("measure")           # measure column
        dims = ds.get("dimensions", [])
        kb_id = ds.get("kb_id") or target.org_id

        if not table or not tc or not mc:
            logger.warning("Target %s: missing datasource config", target.id)
            return None

        # Look up the KB to learn its db_type for correct identifier quoting.
        from app.models.knowledge_base import KnowledgeBase

        kb = db.query(KnowledgeBase).filter(
            KnowledgeBase.id == kb_id,
            KnowledgeBase.is_deleted == False,
        ).first()
        db_type = (kb.db_type if kb else None) or None

        q = lambda n: quote_identifier(n, db_type)  # noqa: E731

        from app.services.db.query_service import QueryService

        query_svc = QueryService(db)

        dim_selects = ", ".join(q(d) for d in dims)
        dim_group = ", ".join(q(d) for d in dims) if dims else ""

        group_clause = q(tc)
        if dim_group:
            group_clause += f", {dim_group}"

        sql = (
            f"SELECT {q(tc)} AS t, "
            f"{dim_selects + ', ' if dim_selects else ''}"
            f"AVG({q(mc)}) AS y "
            f"FROM {q(table)} "
            f"WHERE {q(tc)} IS NOT NULL AND {q(mc)} IS NOT NULL "
            f"GROUP BY {group_clause} "
            f"ORDER BY t"
        )

        try:
            result = query_svc.execute(kb_id, sql, timeout_s=30)
        except Exception as exc:
            logger.error(
                "GenericKBStrategy fetch failed for target %s: %s",
                target.id, exc,
            )
            return None

        rows = result.get("rows", [])
        if not rows:
            return None

        df = pd.DataFrame(rows)
        if "t" not in df.columns or "y" not in df.columns:
            return None

        y = df.set_index("t")["y"].astype(float)
        return y


# ── Registry ───────────────────────────────────────────────────────

_DATASOURCE_REGISTRY: dict[str, type[BaseDataSource]] = {
    "edia_mysql": EdiaMysqlStrategy,
    "generic_kb": GenericKBStrategy,
}


def get_datasource(source_type: str) -> BaseDataSource:
    """Factory: return the strategy instance for the given source type.

    Unknown types fall back to EdiaMysqlStrategy for backward compatibility.
    """
    cls = _DATASOURCE_REGISTRY.get(source_type, EdiaMysqlStrategy)
    return cls()
