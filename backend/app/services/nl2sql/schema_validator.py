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

from sqlalchemy.orm import Session
import sqlglot
from sqlglot import expressions as exp

from app.models.knowledge_base import KnowledgeBase
from app.services.db.schema_graph import _map_dialect
from app.services.db.schema_service import SchemaService

logger = logging.getLogger(__name__)


def _extract_tables(parsed: exp.Expression) -> set[str]:
    return {t.name for t in parsed.find_all(exp.Table) if t.name}


def _extract_alias_map(parsed: exp.Expression) -> dict[str, str]:
    """Map lowercase alias -> real table name."""
    alias_map: dict[str, str] = {}
    for a in parsed.find_all(exp.Alias):
        if isinstance(a.this, exp.Table) and a.alias and a.this.name:
            alias_map[a.alias.lower()] = a.this.name
    return alias_map


def validate_against_schema(sql: str, kb_id: str, db: Session) -> dict:
    """Validate ``sql`` against the schema of KnowledgeBase ``kb_id``.

    Returns:
        ``{"is_valid": bool, "errors": [str, ...],
           "available_columns": {table: [col, ...]}}``.
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

    tables = _extract_tables(parsed)
    if not tables:
        return {"is_valid": False, "errors": ["no tables referenced"], "available_columns": {}}

    alias_map = _extract_alias_map(parsed)
    service = SchemaService(db)

    available_columns: dict[str, list[str]] = {}
    errors: list[str] = []
    for t in sorted(tables):
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
            # Unqualified column: valid if present in any referenced table.
            if not any(name in cols for cols in available_columns.values()):
                errors.append(f"column '{name}' not found in any referenced table")

    return {
        "is_valid": len(errors) == 0,
        "errors": errors,
        "available_columns": available_columns,
    }
