"""KPI computation engine.

Builds and executes SQL for YoY (year-over-year), MoM (month-over-month),
and period-over-period KPI comparisons against any connected database.
"""

from __future__ import annotations

from app.services.forecasting.datasource_registry import quote_identifier


# KPI-type → SQL building strategy
_KPI_TYPES = ("yoy", "mom", "period")


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
    dim_select = ", ".join(q(d) for d in dims)
    if dim_select:
        dim_select += ", "

    period_col = q(time_column)
    measure_col = q(measure)
    table_ref = q(table)

    if kpi_type == "yoy":
        # Year-over-Year: compare with same day 365 days ago
        return (
            f"SELECT {period_col} AS period, {dim_select}"
            f"SUM({measure_col}) AS current_value, "
            f"LAG(SUM({measure_col}), 365) OVER "
            f"(ORDER BY {period_col}) AS prior_value "
            f"FROM {table_ref} "
            f"WHERE {measure_col} IS NOT NULL "
            f"GROUP BY {period_col} "
            f"ORDER BY {period_col}"
        )
    elif kpi_type == "mom":
        # Month-over-Month: compare with previous month
        return (
            f"SELECT DATE_TRUNC('month', {period_col}) AS period, {dim_select}"
            f"SUM({measure_col}) AS current_value, "
            f"LAG(SUM({measure_col}), 1) OVER ("
            f"ORDER BY DATE_TRUNC('month', {period_col})"
            f") AS prior_value "
            f"FROM {table_ref} "
            f"WHERE {measure_col} IS NOT NULL "
            f"GROUP BY DATE_TRUNC('month', {period_col}) "
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
            f"GROUP BY {period_col} "
            f"ORDER BY {period_col}"
        )
