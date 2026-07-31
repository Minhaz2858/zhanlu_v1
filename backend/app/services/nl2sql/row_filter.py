"""Row-level permission filter injection for NL2SQL queries.

Injects SQL WHERE clauses derived from ``AgentDataBinding.row_filters``
to enforce row-level access control (e.g. multi-tenant isolation,
region-based scoping).
"""

from __future__ import annotations

import logging

import sqlglot
from sqlglot import expressions as exp

logger = logging.getLogger(__name__)


def inject(
    sql: str,
    filters: list[dict[str, str]],
    dialect: str = "postgresql",
) -> str:
    """Inject row-level WHERE filters into a SELECT statement.

    Args:
        sql: The original SQL string (must be a SELECT).
        filters: List of ``{"table": "table_name", "filter": "predicate"}`` dicts.
        dialect: SQL dialect (``"postgresql"``, ``"sqlite"``, ``"mysql"``).

    Returns:
        Modified SQL with row-level predicates AND-ed onto the WHERE clause.
        Returns the original ``sql`` unchanged if ``filters`` is empty or
        if no table in the query matches a filter entry.
    """
    if not filters:
        return sql

    # Normalize dialect name for sqlglot (it expects "postgres", not "postgresql")
    _dialect = dialect.lower()
    if _dialect in ("postgresql",):
        _dialect = "postgres"

    try:
        parsed = sqlglot.parse_one(sql, dialect=_dialect)
    except Exception as exc:
        logger.warning("Could not parse SQL for row-filter injection: %s", exc)
        return sql

    # Extract referenced tables using sqlglot's scope
    referenced_tables: set[str] = set()
    for node in parsed.find_all(exp.Table):
        tbl_name = node.name
        if tbl_name:
            referenced_tables.add(tbl_name)

    # Collect filters that apply to tables in the query
    applicable: list[str] = []
    for f in filters:
        tbl = f.get("table", "")
        pred = f.get("filter", "")
        if tbl in referenced_tables and pred:
            applicable.append(pred)

    if not applicable:
        return sql

    # Build the combined filter expression
    combined = applicable[0]
    for p in applicable[1:]:
        combined = f"({combined}) AND ({p})"

    try:
        filter_expr = sqlglot.parse_one(combined, dialect=_dialect)
    except Exception:
        filter_expr = exp.Literal.string(combined)

    if isinstance(parsed, exp.Select):
        existing_where = parsed.find(exp.Where)
        if existing_where:
            # AND onto existing WHERE — rebuild the WHERE node
            new_where = exp.Where(
                this=exp.And(
                    this=exp.Paren(this=existing_where.this.copy()),
                    expression=exp.Paren(this=filter_expr),
                )
            )
            existing_where.replace(new_where)
        else:
            # No existing WHERE — inject one before any ORDER BY / LIMIT
            order_by = parsed.find(exp.Order)
            limit = parsed.find(exp.Limit)

            where_clause = exp.Where(this=filter_expr)

            if order_by:
                order_by.replace(where_clause)
                where_clause.replace(order_by)
            elif limit:
                limit.replace(where_clause)
                where_clause.replace(limit)
            else:
                parsed.set("where", where_clause)

    return parsed.sql(dialect=_dialect)
