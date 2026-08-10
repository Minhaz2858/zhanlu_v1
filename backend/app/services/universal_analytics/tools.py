"""Universal analytics tool handlers.

Registers 6 tools with enabled_by_default=True so that ANY agent
automatically gets forecast/KPI/trend/query capabilities when a
database is connected.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.services.tool_registry import registry
from app.services.universal_analytics.context import (
    check_enabled,
    missing_config_response,
    get_bound_kbs,
    get_first_db_kb,
)

logger = logging.getLogger(__name__)


# ── Tool handler functions ──────────────────────────────────────────

async def _universal_describe(
    args: dict, db: Session, user_id: str | None, context: dict | None = None
) -> dict:
    """Describe the schema of bound databases."""
    if not check_enabled():
        return missing_config_response()
    kbs = get_bound_kbs(context, db)
    if not kbs:
        return {"success": False, "error": "No database connected to this agent."}

    from app.services.db.schema_service import SchemaService

    kb_id = args.get("kb_id") or kbs[0].id
    table_name = args.get("table")
    try:
        schema_svc = SchemaService(db)
        if table_name:
            info = schema_svc.describe_table(kb_id, table_name)
            return {"success": True, "table": table_name, "schema": info}
        else:
            all_tables = schema_svc.describe_all(kb_id)
            return {"success": True, "tables": all_tables}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


async def _universal_discover(
    args: dict, db: Session, user_id: str | None, context: dict | None = None
) -> dict:
    """Scan bound databases for forecastable time series."""
    if not check_enabled():
        return missing_config_response()
    kb = get_first_db_kb(context, db)
    if not kb:
        return {"success": False, "error": "No database connected to this agent."}

    from app.services.forecasting.discovery import discover

    try:
        candidates = discover(db, kb.id)
        targets = [
            {
                "table": c.get("table"),
                "time_column": c.get("time_column"),
                "measure": c.get("measure"),
                "dimensions": c.get("dimensions", []),
                "row_count": c.get("row_count"),
            }
            for c in candidates
        ]
        return {"success": True, "candidates": targets, "count": len(targets)}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


async def _universal_query(
    args: dict, db: Session, user_id: str | None, context: dict | None = None
) -> dict:
    """Execute a read-only SQL query against bound databases."""
    if not check_enabled():
        return missing_config_response()

    kb = get_first_db_kb(context, db, kb_id=args.get("kb_id"))
    if not kb:
        return {"success": False, "error": "No database connected to this agent."}

    from app.services.universal_analytics.query import validate_sql
    from app.services.db.query_service import QueryService

    sql = args.get("sql", "")
    validation_error = validate_sql(sql, kb.db_type)
    if validation_error:
        return {"success": False, "error": validation_error}

    max_rows = args.get("max_rows", 100)
    try:
        query_svc = QueryService(db)
        result = query_svc.execute(kb.id, sql, timeout_s=30)
        rows = result.get("rows", [])
        return {
            "success": True,
            "rows": rows[:max_rows],
            "count": len(rows[:max_rows]),
            "total_count": len(rows),
        }
    except Exception as exc:
        return {"success": False, "error": str(exc)}


async def _universal_kpi(
    args: dict, db: Session, user_id: str | None, context: dict | None = None
) -> dict:
    """Compute KPI aggregations with YoY/MoM deltas."""
    if not check_enabled():
        return missing_config_response()

    kb = get_first_db_kb(context, db, kb_id=args.get("kb_id"))
    if not kb:
        return {"success": False, "error": "No database connected to this agent."}

    from app.services.universal_analytics.kpi import build_kpi_sql
    from app.services.db.query_service import QueryService

    table = args.get("table")
    time_column = args.get("time_column")
    measure = args.get("measure")
    if not table or not time_column or not measure:
        return {"success": False, "error": "table, time_column, and measure are required."}

    kpi_type = args.get("kpi_type", "yoy")
    dimensions = args.get("dimensions", [])

    try:
        sql = build_kpi_sql(table, time_column, measure, kb.db_type, dimensions, kpi_type)
        query_svc = QueryService(db)
        result = query_svc.execute(kb.id, sql, timeout_s=30)
        return {
            "success": True,
            "kpi_type": kpi_type,
            "rows": result.get("rows", []),
            "sql": sql,
        }
    except Exception as exc:
        return {"success": False, "error": str(exc)}


async def _universal_trend(
    args: dict, db: Session, user_id: str | None, context: dict | None = None
) -> dict:
    """Analyze trend direction, slope, and seasonality for a time series."""
    if not check_enabled():
        return missing_config_response()

    kb = get_first_db_kb(context, db, kb_id=args.get("kb_id"))
    if not kb:
        return {"success": False, "error": "No database connected to this agent."}

    from app.services.forecasting.datasource_registry import GenericKBStrategy

    table = args.get("table")
    time_column = args.get("time_column")
    measure = args.get("measure")
    if not table or not time_column or not measure:
        return {"success": False, "error": "table, time_column, and measure are required."}

    from unittest.mock import MagicMock
    target = MagicMock()
    target.id = f"trend-{table}-{measure}"
    target.org_id = kb.org_id
    target.datasource = {
        "source": "generic_kb",
        "kb_id": kb.id,
        "table": table,
        "time_column": time_column,
        "measure": measure,
        "dimensions": args.get("dimensions", []),
    }

    try:
        strategy = GenericKBStrategy()
        series = strategy.fetch(target, db)
        if series is None or len(series) == 0:
            return {"success": False, "error": "No data returned for trend analysis."}

        from app.services.universal_analytics.trend import analyze_trend

        window = args.get("window", 0)
        trend_result = analyze_trend(series, window=window)
        return {"success": True, "trend": trend_result}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


async def _universal_forecast(
    args: dict, db: Session, user_id: str | None, context: dict | None = None
) -> dict:
    """Run a forecast using the 8-model ensemble against bound databases."""
    if not check_enabled():
        return missing_config_response()

    kb = get_first_db_kb(context, db, kb_id=args.get("kb_id"))
    if not kb:
        return {"success": False, "error": "No database connected to this agent."}

    from app.models.forecasting import ForecastTarget
    from app.services.forecasting.engine import ForecastEngine

    product_key = args.get("product_key")
    table = args.get("table")
    time_column = args.get("time_column")
    measure = args.get("measure")
    horizon = args.get("horizon", 30)

    try:
        engine = ForecastEngine(db)

        if product_key:
            target = db.query(ForecastTarget).filter(
                ForecastTarget.product_key == product_key,
                ForecastTarget.org_id == kb.org_id,
            ).first()
            if not target:
                return {"success": False, "error": f"Target '{product_key}' not found."}
        elif table and time_column and measure:
            # Auto-build a target from the user's table/column args
            target = ForecastTarget(
                product_key=f"universal-{table}-{measure}",
                name=f"{table}.{measure}",
                org_id=kb.org_id,
                datasource={
                    "source": "generic_kb",
                    "kb_id": kb.id,
                    "table": table,
                    "time_column": time_column,
                    "measure": measure,
                    "dimensions": args.get("dimensions", []),
                },
                status="ad_hoc",
                source="universal_analytics",
            )
        else:
            return {"success": False, "error": "Provide product_key or (table, time_column, measure)."}

        run = engine.compute_target(target, horizon=horizon)
        db.commit()

        return {
            "success": True,
            "forecast": {
                "horizons": horizon,
                "confidence": run.confidence,
                "explanation": run.explanation,
                "forecasted_value": str(run.forecasted_value) if run.forecasted_value else None,
                "forecasted_change_pct": str(run.forecasted_change_pct) if run.forecasted_change_pct else None,
            },
        }
    except Exception as exc:
        db.rollback()
        return {"success": False, "error": str(exc)}


# ── Tool registration ───────────────────────────────────────────────

_TOOLS: list[tuple[str, dict, callable, str, str]] = [
    (
        "universal_describe",
        {
            "type": "object",
            "properties": {
                "kb_id": {"type": "string", "description": "Optional specific KB id to describe"},
                "table": {"type": "string", "description": "Optional table name to describe in detail"},
            },
        },
        _universal_describe,
        "List tables and columns in bound databases",
        "🗄️",
    ),
    (
        "universal_discover",
        {
            "type": "object",
            "properties": {
                "kb_id": {"type": "string", "description": "Optional specific KB id to scan"},
            },
        },
        _universal_discover,
        "Scan bound databases for forecastable time series",
        "🔍",
    ),
    (
        "universal_query",
        {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "SELECT SQL query to execute"},
                "max_rows": {"type": "integer", "description": "Max rows to return (default 100)"},
                "kb_id": {"type": "string", "description": "Optional specific KB id"},
            },
            "required": ["sql"],
        },
        _universal_query,
        "Execute a read-only SQL query against bound databases",
        "📊",
    ),
    (
        "universal_kpi",
        {
            "type": "object",
            "properties": {
                "table": {"type": "string", "description": "Table name"},
                "time_column": {"type": "string", "description": "Time/date column"},
                "measure": {"type": "string", "description": "Numeric measure column"},
                "kpi_type": {
                    "type": "string",
                    "enum": ["yoy", "mom", "period"],
                    "description": "KPI comparison type",
                },
                "dimensions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional dimension columns for grouping",
                },
                "kb_id": {"type": "string", "description": "Optional specific KB id"},
            },
            "required": ["table", "time_column", "measure"],
        },
        _universal_kpi,
        "Compute KPI aggregations (YoY, MoM, period) against bound databases",
        "📈",
    ),
    (
        "universal_trend",
        {
            "type": "object",
            "properties": {
                "table": {"type": "string", "description": "Table name"},
                "time_column": {"type": "string", "description": "Time/date column"},
                "measure": {"type": "string", "description": "Numeric measure column"},
                "dimensions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional dimension columns for filtering",
                },
                "window": {"type": "integer", "description": "Moving average window size"},
                "kb_id": {"type": "string", "description": "Optional specific KB id"},
            },
            "required": ["table", "time_column", "measure"],
        },
        _universal_trend,
        "Analyze trend direction, slope, and seasonality of a time series",
        "📉",
    ),
    (
        "universal_forecast",
        {
            "type": "object",
            "properties": {
                "product_key": {"type": "string", "description": "ForecastTarget product_key to forecast"},
                "table": {"type": "string", "description": "Table name (if no product_key)"},
                "time_column": {"type": "string", "description": "Time column (if no product_key)"},
                "measure": {"type": "string", "description": "Measure column (if no product_key)"},
                "dimensions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional dimension columns",
                },
                "horizon": {"type": "integer", "description": "Forecast horizon in days (default 30)"},
                "kb_id": {"type": "string", "description": "Optional specific KB id"},
            },
        },
        _universal_forecast,
        "Run a forecast using the 8-model ensemble against bound databases",
        "🔮",
    ),
]

# Register all tools at module-import time (same pattern as edia_delegation_tools.py)
for _name, _schema, _handler, _desc, _emoji in _TOOLS:
    registry.register(
        name=_name,
        schema=_schema,
        handler=_handler,
        category="universal_analytics",
        toolset="universal_analytics",
        enabled_by_default=True,
        is_async=True,
        description=_desc,
        emoji=_emoji,
    )
