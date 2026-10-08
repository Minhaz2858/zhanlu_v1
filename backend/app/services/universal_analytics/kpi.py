"""KPI computation engine.

Builds and executes SQL for YoY (year-over-year), MoM (month-over-month),
and period-over-period KPI comparisons against any connected database.
"""

from __future__ import annotations

from app.services.db.dialect import maybe_profile
from app.services.forecasting.datasource_registry import quote_identifier


# KPI-type → SQL building strategy
_KPI_TYPES = ("yoy", "mom", "period")


def _period_minus_one_year(expr: str, db_type: str | None) -> str:
    """Dialect-aware ``expr`` shifted back by one calendar year.

    Used for YoY so the "prior" value is the SAME period a year earlier, not a
    fixed number of rows back.
    """
    db = (db_type or "").lower()
    if db in ("mysql", "mariadb"):
        return f"DATE_SUB({expr}, INTERVAL 1 YEAR)"
    if db == "sqlite":
        return f"DATE({expr}, '-1 year')"
    if db == "oracle":
        return f"ADD_MONTHS({expr}, -12)"
    if db in ("mssql", "sqlserver", "sql server"):
        return f"DATEADD(year, -1, {expr})"
    # postgres / default — ANSI interval arithmetic
    return f"({expr} - INTERVAL '1 year')"


def _month_bucket(expr: str, db_type: str | None) -> str:
    """Dialect-aware month-truncation expression.

    The MoM branch used to hardcode Postgres ``DATE_TRUNC('month', …)``,
    which does not exist on MySQL or SQLite — the KPI query failed outright
    for those sources.  Borrow the dialect capability table instead of
    re-deriving per-vendor syntax here.
    """
    prof = maybe_profile(db_type)
    if prof is None:
        # Unknown dialect — fall back to the ANSI/Postgres spelling.
        return f"DATE_TRUNC('month', {expr})"
    return prof.month_bucket.format(c=expr)


def build_kpi_sql(
    table: str,
    time_column: str,
    measure: str,
    db_type: str | None,
    dimensions: list[str] | None = None,
    kpi_type: str = "yoy",
) -> str:
    """Build a KPI SQL query with the given time granularity.

    Args:
        table:       Table name.
        time_column: Time/date column name.
        measure:     Numeric measure column.
        db_type:     Database type (mysql, postgres, etc.) for quoting.
        dimensions:  Optional list of dimension columns (e.g. ["region"]).
        kpi_type:    "yoy", "mom", or "period".

    Returns:
        A SELECT SQL string that computes current-period and prior-period
        values side-by-side.
    """
    dims = dimensions or []
    q = lambda n: quote_identifier(n, db_type)  # noqa: E731
    dim_cols = ", ".join(q(d) for d in dims)
    dim_select = f"{dim_cols}, " if dim_cols else ""
    # Every SELECTed dimension must also be GROUPed — otherwise the statement
    # is invalid under ONLY_FULL_GROUP_BY (MySQL 5.7+ default) and returns
    # arbitrary dimension values on engines that permit it.
    dim_group = f", {dim_cols}" if dim_cols else ""

    period_col = q(time_column)
    measure_col = q(measure)
    table_ref = q(table)

    if kpi_type == "yoy":
        # Year-over-Year: compare each period with the SAME period one year
        # earlier via DATE arithmetic (self-join on period − 1 year). A
        # `LAG(..., 365)` window is a ROW offset, not a date offset, and is
        # only correct for a series with exactly one row per calendar day —
        # wrong for monthly, hourly, sparse or multi-row-per-day data.
        agg = (
            f"SELECT {period_col} AS period, {dim_select}"
            f"SUM({measure_col}) AS v "
            f"FROM {table_ref} "
            f"WHERE {measure_col} IS NOT NULL "
            f"GROUP BY {period_col}{dim_group}"
        )
        prev_year = _period_minus_one_year("cur.period", db_type)
        cur_dims = "".join(f", cur.{q(d)} AS {q(d)}" for d in dims)
        join_dims = "".join(f" AND prev.{q(d)} = cur.{q(d)}" for d in dims)
        return (
            f"SELECT cur.period AS period{cur_dims}, "
            f"cur.v AS current_value, prev.v AS prior_value "
            f"FROM ({agg}) cur "
            f"LEFT JOIN ({agg}) prev "
            f"ON prev.period = {prev_year}{join_dims} "
            f"ORDER BY cur.period"
        )
    elif kpi_type == "mom":
        # Month-over-Month: compare with previous month.  The bucket
        # expression is dialect-specific (DATE_FORMAT on MySQL, STRFTIME on
        # SQLite, TO_CHAR/TRUNC elsewhere) — never hardcode DATE_TRUNC.
        bucket = _month_bucket(period_col, db_type)
        return (
            f"SELECT {bucket} AS period, {dim_select}"
            f"SUM({measure_col}) AS current_value, "
            f"LAG(SUM({measure_col}), 1) OVER ("
            f"ORDER BY {bucket}"
            f") AS prior_value "
            f"FROM {table_ref} "
            f"WHERE {measure_col} IS NOT NULL "
            f"GROUP BY {bucket}{dim_group} "
            f"ORDER BY period"
        )
    else:  # period (generic)
        return (
            f"SELECT {period_col} AS period, {dim_select}"
            f"SUM({measure_col}) AS total, "
            f"AVG({measure_col}) AS average, "
            f"MIN({measure_col}) AS min_val, "
            f"MAX({measure_col}) AS max_val, "
            f"COUNT(*) AS cnt "
            f"FROM {table_ref} "
            f"WHERE {measure_col} IS NOT NULL "
            f"GROUP BY {period_col}{dim_group} "
            f"ORDER BY {period_col}"
        )
