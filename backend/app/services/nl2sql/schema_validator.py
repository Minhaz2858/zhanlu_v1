"""Structural SQL validator — grounds generated SQL against the real schema.

Parses SQL with sqlglot, extracts referenced tables and columns, and checks
each against live ``information_schema`` metadata (lazily, one
``describe_table`` per referenced table, TTL-cached via ``SchemaService``).

Returns ``available_columns`` feedback on failure so the caller (agent loop
or NLAnswerService pipeline) can self-correct. Pure structure — zero business
keywords.
"""

from __future__ import annotations

import logging
import re

from sqlalchemy.orm import Session
import sqlglot
from sqlglot import expressions as exp

from app.models.knowledge_base import KnowledgeBase
from app.services.db.schema_graph import _map_dialect
from app.services.db.schema_service import SchemaService

logger = logging.getLogger(__name__)

# Measure-column stems (generic, boundary-anchored) — SUM over one of these
# across a row-multiplying join is the classic fan-out bug.
_MEASURE_STEM_RE = re.compile(
    r"(amount|qty|quantity|price|value|sales|revenue|volume|count|sum|total)",
    re.IGNORECASE,
)


def _has_fanout_risk(parsed: exp.Expression) -> str | None:
    """Return a warning string when the query aggregates a measure column with
    SUM() over a JOIN without a pre-aggregating CTE/subquery on either side.

    Structural heuristic only (no business keywords): joins may be explicit
    (``JOIN``) or implicit (multi-table ``FROM``); pre-aggregation is any CTE
    or subquery anywhere in the statement.
    """
    joins = list(parsed.find_all(exp.Join))
    tables = _extract_tables(parsed)
    if not joins and len(tables) < 2:
        return None
    sums = list(parsed.find_all(exp.Sum))
    if not sums:
        return None
    measure_cols: set[str] = set()
    for s in sums:
        for col in s.find_all(exp.Column):
            if col.name and _MEASURE_STEM_RE.search(col.name):
                measure_cols.add(col.name)
    if not measure_cols:
        return None
    # Pre-aggregation present anywhere (CTE or subquery) → assume cardinality
    # is controlled; do not warn.
    if list(parsed.find_all(exp.Subquery)) or list(parsed.find_all(exp.CTE)):
        return None
    return (
        "potential fan-out: SUM(" + ", ".join(sorted(measure_cols))
        + ") over a JOIN without pre-aggregation on either side — aggregate "
        "each side in a CTE/subquery before joining"
    )


def _extract_tables(parsed: exp.Expression) -> set[str]:
    return {t.name for t in parsed.find_all(exp.Table) if t.name}


# Introspection statements execute natively on the engine; the structural
# validator has nothing to check for them (blocking SHOW/DESCRIBE/EXPLAIN
# previously made the agent burn its tool-loop budget on retries).
_INTROSPECTION_CLASSES = (
    exp.Show,
    exp.Describe,  # also covers EXPLAIN in the mysql dialect
    exp.Analyze,
    exp.Pragma,
    exp.Command,  # catch-all for engine-native statements (CALL, USE, ...)
)


def _is_introspection_statement(parsed: exp.Expression) -> bool:
    return isinstance(parsed, _INTROSPECTION_CLASSES)


# Write/DDL statement classes — this KB is read-only, so none may execute.
# NOTE: sqlglot parses TRUNCATE TABLE to ``exp.TruncateTable`` (there is no
# ``exp.Truncate``) in this version.
_WRITE_OR_DDL_CLASSES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.Merge,
    exp.TruncateTable,
)


def _is_write_or_ddl(parsed: exp.Expression) -> bool:
    """True when the statement tree contains any write/DDL expression."""
    return any(
        next(parsed.find_all(cls_), None) is not None
        for cls_ in _WRITE_OR_DDL_CLASSES
    )


_READ_ONLY_ERROR = (
    "query contains write/DDL statements; this knowledge base is read-only"
)


def check_read_only_sql(sql: str, dialect: str = "mysql") -> str | None:
    """Return the read-only error string when ``sql`` contains write/DDL.

    Defensive gate for callers that run the validator path (db_tools).
    Returns ``None`` for read-only statements and for anything sqlglot
    cannot parse (the parse error is surfaced by the validator itself).
    """
    try:
        parsed = sqlglot.parse_one(sql, dialect=dialect)
    except Exception:
        return None
    if _is_write_or_ddl(parsed):
        return _READ_ONLY_ERROR
    return None


def _extract_alias_map(parsed: exp.Expression) -> dict[str, str]:
    """Map lowercase alias -> real table name.

    sqlglot stores a plain ``FROM fact f`` alias on the Table node itself
    (``Table.alias``); the Alias node form only appears for SELECT-expression
    aliases. Read both so qualified columns on aliased tables resolve.
    """
    alias_map: dict[str, str] = {}
    for t in parsed.find_all(exp.Table):
        alias = getattr(t, "alias", None)
        if alias and t.name:
            alias_map[str(alias).lower()] = t.name
    for a in parsed.find_all(exp.Alias):
        if isinstance(a.this, exp.Table) and a.alias and a.this.name:
            alias_map[str(a.alias).lower()] = a.this.name
    return alias_map


def _cte_output_columns(cte: exp.CTE) -> list[str]:
    """Output column names of a CTE (``SELECT a, SUM(x) AS b ...`` -> [a, b]).

    CTEs are virtual tables: references to them must not be reported as
    'table does not exist', and their projected columns must resolve in the
    unqualified-column check.
    """
    cols: list[str] = []
    select = cte.this
    for e in getattr(select, "expressions", None) or []:
        if isinstance(e, exp.Alias):
            if e.alias:
                cols.append(str(e.alias))
        elif hasattr(e, "name") and e.name:
            cols.append(str(e.name))
    return cols


def validate_against_schema(sql: str, kb_id: str, db: Session) -> dict:
    """Validate ``sql`` against the schema of KnowledgeBase ``kb_id``.

    Returns:
        ``{"is_valid": bool, "errors": [str, ...],
           "warnings": [str, ...],
           "available_columns": {table: [col, ...]}}``.

    ``warnings`` never affect ``is_valid`` (structural validity is intact);
    they flag cardinality risks (fan-out) that callers may choose to act on.
    """
    kb = db.query(KnowledgeBase).filter(
        KnowledgeBase.id == kb_id, KnowledgeBase.is_deleted == False
    ).first()
    dialect = _map_dialect(kb.db_type if kb else None)

    try:
        parsed = sqlglot.parse_one(sql, dialect=dialect)
    except Exception as exc:
        logger.warning("schema_validator: unparseable SQL: %s", exc)
        return {"is_valid": False, "errors": ["unparseable SQL"], "available_columns": {}}

    if _is_write_or_ddl(parsed):
        # Read-only gate: never let write/DDL statements reach the connector.
        return {
            "is_valid": False,
            "errors": [_READ_ONLY_ERROR],
            "available_columns": {},
        }

    if _is_introspection_statement(parsed):
        # SHOW/DESCRIBE/EXPLAIN/PRAGMA/ANALYZE run natively on the engine —
        # no structural validation applies.
        return {"is_valid": True, "errors": [], "warnings": [], "available_columns": {}}

    tables = _extract_tables(parsed)
    if not tables:
        return {"is_valid": False, "errors": ["no tables referenced"], "available_columns": {}}

    alias_map = _extract_alias_map(parsed)
    service = SchemaService(db)

    # CTEs are virtual tables: register their projected columns so references
    # to them validate without a describe call.
    cte_columns: dict[str, list[str]] = {}
    for cte in parsed.find_all(exp.CTE):
        if getattr(cte, "alias", None):
            cte_columns[str(cte.alias).lower()] = _cte_output_columns(cte)

    available_columns: dict[str, list[str]] = {}
    errors: list[str] = []
    for t in sorted(tables):
        if t.lower() in cte_columns:
            available_columns[t] = list(cte_columns[t.lower()])
            continue
        try:
            desc = service.describe_table(kb_id, t)
            cols = [c.get("name") for c in (desc.get("columns") or []) if c.get("name")]
        except Exception as exc:
            logger.debug("schema_validator: describe %s failed: %s", t, exc)
            cols = []
        available_columns[t] = cols
        if not cols:
            errors.append(f"table '{t}' does not exist or has no columns")

    for col in parsed.find_all(exp.Column):
        name = col.name
        if not name or name == "*":
            continue
        qualifier = (col.table or "").lower()

        if qualifier:
            resolved = qualifier
            if qualifier not in available_columns:
                resolved = alias_map.get(qualifier)
            if resolved is None:
                # Unknown qualifier (CTE/alias edge case) — skip rather than
                # emit a false positive.
                continue
            if resolved in available_columns and name not in available_columns[resolved]:
                errors.append(f"column '{name}' not found in table '{resolved}'")
        else:
            # Unqualified column: valid if present in any referenced table
            # (real tables + CTE projections).
            if not any(name in cols for cols in available_columns.values()):
                errors.append(f"column '{name}' not found in any referenced table")

    warning: str | None = None
    try:
        warning = _has_fanout_risk(parsed)
    except Exception as exc:  # noqa: BLE001 — structural check is best-effort
        logger.debug("schema_validator: fan-out check failed: %s", exc)

    return {
        "is_valid": len(errors) == 0,
        "errors": errors,
        "warnings": [warning] if warning else [],
        "available_columns": available_columns,
    }
