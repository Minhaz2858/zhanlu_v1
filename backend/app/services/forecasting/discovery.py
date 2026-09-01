"""Auto-discovery of forecastable time series from any datasource.

Scans every table in a KnowledgeBase, identifies time + measure +
dimension columns, generates candidate series via time-bucket
aggregation, filters non-forecastable candidates, and writes
surviving candidates as ``ForecastTarget`` rows in Section 1 tables.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from app.models.forecasting import ForecastTarget
from app.services.db.query_service import QueryService
from app.services.db.schema_service import SchemaService

logger = logging.getLogger(__name__)

# Minimum observations required per granularity
_MIN_POINTS = {
    "daily": 14,
    "weekly": 8,
    "monthly": 6,
}

# Minimum coverage ratio (ratio of non-null to total possible observations)
_MIN_COVERAGE = 0.70

# Patterns for identifying time columns by name
_TIME_COLUMN_PATTERNS = re.compile(
    r"date|time|day|week|month|quarter|year|period|dt|timestamp",
    re.IGNORECASE,
)

# Types that are clearly date/time
_TIME_TYPES = {"date", "datetime", "timestamp", "timestamptz", "time"}

# Maximum distinct values for a categorical dimension
_MAX_DIM_DISTINCT = 50
# Maximum dimension ratio (distinct / total) for a categorical column
_MAX_DIM_RATIO = 0.30


def discover(
    db: Session,
    kb_id: str,
    max_tables: int = 50,
    sample_limit: int = 1000,
) -> list[dict]:
    """Scan a KnowledgeBase for forecastable time series.

    Parameters
    ----------
    db : Session
        SQLAlchemy ORM session.
    kb_id : str
        KnowledgeBase UUID string.
    max_tables : int
        Maximum tables to scan (cap schema service).
    sample_limit : int
        Maximum rows to sample per candidate query.

    Returns
    -------
    list[dict]
        Each dict contains keys: ``table``, ``time_column``, ``measure``,
        ``dimensions``, ``granularity``, ``n_points``, ``coverage``,
        ``sample_mean``.  Suitable for writing to ``ForecastTarget``.
    """
    schema_svc = SchemaService(db)
    query_svc = QueryService(db)

    schema = schema_svc.describe_all(kb_id, max_tables=max_tables)
    tables = schema.get("tables", [])

    candidates: list[dict] = []

    for table_info in tables:
        table_name = table_info.get("table", "")
        columns = table_info.get("columns", [])
        if "error" in table_info or not columns:
            logger.debug("Skipping table %s: no columns or error", table_name)
            continue

        # Classify columns
        time_cols, measure_cols, dim_cols = _classify_columns(columns)

        if not time_cols or not measure_cols:
            logger.debug(
                "Skip %s: time_cols=%d measure_cols=%d",
                table_name, len(time_cols), len(measure_cols),
            )
            continue

        # For each (time, measure, dims) combination, attempt materialization
        for tc in time_cols:
            for mc in measure_cols[:3]:  # limit measures per table
                for dim_combo in _dimension_combinations(dim_cols, max_dims=3):
                    try:
                        result = _materialize_candidate(
                            query_svc,
                            kb_id,
                            table_name,
                            tc,
                            mc,
                            dim_combo,
                            sample_limit,
                        )
                        if result:
                            candidates.append(result)
                    except Exception as exc:
                        logger.debug(
                            "Materialize %s.%s x %s failed: %s",
                            table_name, mc["name"], tc["name"], exc,
                        )

    return candidates


# ── column classification ──────────────────────────────────────────────

def _classify_columns(
    columns: list[dict],
) -> tuple[list[dict], list[dict], list[dict]]:
    """Split columns into (time, measure, dimension) buckets."""
    time_cols: list[dict] = []
    measure_cols: list[dict] = []
    dim_cols: list[dict] = []

    for col in columns:
        name = str(col.get("name", ""))
        dtype = str(col.get("type", "")).lower()

        # Time: type-based or name-based
        if _is_time_type(dtype) or _TIME_COLUMN_PATTERNS.search(name):
            time_cols.append(col)
            continue

        # Measure: numeric, not an ID column, not boolean
        if _is_numeric_type(dtype) and not _is_id_column(name):
            measure_cols.append(col)
            continue

        # Dimension: text, enum, or low-cardinality numeric
        dim_cols.append(col)

    return time_cols, measure_cols, dim_cols


def _is_time_type(dtype: str) -> bool:
    """Check if SQL type is a date/time family type."""
    return any(t in dtype for t in _TIME_TYPES)


def _is_numeric_type(dtype: str) -> bool:
    """Check if SQL type is numeric."""
    numeric = {"int", "float", "double", "decimal", "numeric", "real", "number"}
    return any(n in dtype for n in numeric)


def _is_id_column(name: str) -> bool:
    """Heuristic: column named like an ID should not be a measure."""
    patterns = re.compile(r"(^|_)id$|_id$|^pk$|^key$", re.IGNORECASE)
    return bool(patterns.search(name))


def _dimension_combinations(
    dim_cols: list[dict],
    max_dims: int = 3,
) -> list[list[dict]]:
    """Generate dimension group combinations (power set of dim columns)."""
    import itertools

    combos: list[list[dict]] = [[]]  # no dimensions
    for r in range(1, min(max_dims, len(dim_cols)) + 1):
        for combo in itertools.combinations(dim_cols, r):
            combos.append(list(combo))
    return combos


# ── candidate materialization ──────────────────────────────────────────

def _materialize_candidate(
    query_svc: QueryService,
    kb_id: str,
    table: str,
    time_col: dict,
    measure_col: dict,
    dim_cols: list[dict],
    sample_limit: int,
) -> dict | None:
    """Run a time-bucket query and validate the candidate series."""
    tc_name = _quote(time_col["name"])
    mc_name = _quote(measure_col["name"])
    tb_name = _quote(table)

    # Detect granularity
    granularity = _detect_granularity(query_svc, kb_id, tb_name, tc_name)

    # Build time-bucket expression
    bucket_expr = _time_bucket_expr(tc_name, granularity)

    # Dimension columns in SELECT
    dim_selects = ", ".join(_quote(d["name"]) for d in dim_cols)
    dim_group = ", ".join(_quote(d["name"]) for d in dim_cols) if dim_cols else ""

    group_clause = f"{bucket_expr}"
    if dim_group:
        group_clause += f", {dim_group}"

    sql = (
        f"SELECT {bucket_expr} AS t, "
        f"{dim_selects + ', ' if dim_selects else ''}"
        f"AVG({mc_name}) AS y "
        f"FROM {tb_name} "
        f"WHERE {tc_name} IS NOT NULL AND {mc_name} IS NOT NULL "
        f"GROUP BY {group_clause} "
        f"ORDER BY t "
        f"LIMIT {sample_limit}"
    )

    logger.debug("Discovery SQL: %s", sql)
    result = query_svc.execute(kb_id, sql, max_rows=sample_limit, timeout_s=30)
    rows = result.get("rows", [])

    if len(rows) < _MIN_POINTS.get(granularity, 14):
        return None

    # Convert to Series for validation
    df = pd.DataFrame(rows)
    if "t" not in df.columns or "y" not in df.columns:
        return None

    y = df["y"].astype(float)
    if y.var() < 1e-10:
        return None  # constant series — not forecastable

    n_points = len(y)
    # Coverage: check for gaps within the time range
    t_series = pd.to_datetime(df["t"], errors="coerce").dropna()
    if len(t_series) >= 2:
        expected_points = _expected_points(t_series, granularity)
        coverage = n_points / max(expected_points, 1)
    else:
        coverage = 1.0

    if coverage < _MIN_COVERAGE:
        return None

    return {
        "table": table,
        "time_column": time_col["name"],
        "measure": measure_col["name"],
        "dimensions": [d["name"] for d in dim_cols],
        "granularity": granularity,
        "n_points": n_points,
        "coverage": round(coverage, 4),
        "sample_mean": round(float(y.mean()), 4),
    }


# ── granularity helpers ────────────────────────────────────────────────

def _detect_granularity(
    query_svc: QueryService,
    kb_id: str,
    table: str,
    time_col: str,
) -> str:
    """Detect whether the time column is daily, weekly, or monthly."""
    sql = (
        f"SELECT MIN({time_col}) AS mn, MAX({time_col}) AS mx "
        f"FROM {table} WHERE {time_col} IS NOT NULL"
    )
    try:
        result = query_svc.execute(kb_id, sql, max_rows=1, timeout_s=10)
        rows = result.get("rows", [])
        if not rows:
            return "daily"
        mn = pd.to_datetime(rows[0].get("mn"), errors="coerce")
        mx = pd.to_datetime(rows[0].get("mx"), errors="coerce")
        if pd.isna(mn) or pd.isna(mx):
            return "daily"
        days = (mx - mn).days
        n_points = days + 1
        # Query count of distinct time points
        cnt_result = query_svc.execute(
            kb_id,
            f"SELECT COUNT(DISTINCT {time_col}) AS cnt FROM {table}",
            max_rows=1,
            timeout_s=10,
        )
        cnt_rows = cnt_result.get("rows", [])
        actual = int(cnt_rows[0].get("cnt", 0)) if cnt_rows else 0
        if actual == 0:
            return "daily"
        avg_interval_days = days / max(actual, 1)
        if avg_interval_days < 2:
            return "daily"
        if avg_interval_days < 14:
            return "weekly"
        return "monthly"
    except Exception:
        return "daily"


def _time_bucket_expr(col: str, granularity: str) -> str:
    """SQL expression to truncate a date/time column to thegiven granularity."""
    if granularity == "daily":
        return f"DATE({col})"
    if granularity == "weekly":
        return f"DATE_TRUNC('week', {col})"
    return f"DATE_TRUNC('month', {col})"


def _expected_points(t_series: pd.DatetimeIndex, granularity: str) -> float:
    """Expected number of observations between min and max dates."""
    if len(t_series) < 2:
        return 1.0
    days = (t_series.max() - t_series.min()).days
    if granularity == "daily":
        return float(days + 1)
    if granularity == "weekly":
        return float(days / 7 + 1)
    return float(days / 30 + 1)


def _quote(name: str) -> str:
    """Backtick-quote an identifier for MySQL safety. Idempotent on already-quoted input."""
    if name.startswith("`") and name.endswith("`"):
        return name
    return f"`{name}`"
