"""P5 — governed metric computation (ONE authoritative path).

`compute_metric` is the ONLY way an ontology object, agent tool, or API
resolves a metric value:

  resolve (org + lifecycle + enabled) → build deterministic SQL from the
  MetricDefinition binding → execute through QueryService (whose boundary
  runs execution_gate.enforce UNCONDITIONALLY — no second SQL pathway) →
  typed result with metric id/version echo, provenance, and gate trace.

Invariant I2: no LLM anywhere in this module. Invariant I3: unavailability
is TYPED ({"success": False, "unavailable": True, ...}) — never a
fabricated number, never a raw exception.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_AGG_SQL = {
    "sum": "SUM({col})",
    "avg": "AVG({col})",
    "count": "COUNT({col})",
    "min": "MIN({col})",
    "max": "MAX({col})",
    "distinct_count": "COUNT(DISTINCT {col})",
}

_FILTER_OPS = {
    "eq": "{col} = :p_{i}",
    "ne": "{col} <> :p_{i}",
    "gt": "{col} > :p_{i}",
    "lt": "{col} < :p_{i}",
    "ge": "{col} >= :p_{i}",
    "le": "{col} <= :p_{i}",
    "in": "{col} IN :p_{i}",
    "notnull": "{col} IS NOT NULL",
}


def _unavailable(reason: str, **extra) -> dict:
    out = {
        "success": False,
        "unavailable": True,
        "evidence_status": "unavailable",
        "reason": reason,
    }
    out.update(extra)
    return out


def resolve_metric(db: Session, org_id: str, metric_ref: str):
    """Org-scoped resolution by id, canonical name (case-insensitive), or
    synonym. Returns (metric, None) or (None, reason). Only published,
    enabled, non-deleted definitions resolve."""
    from app.models.metric_definition import MetricDefinition

    ref = (metric_ref or "").strip()
    if not ref:
        return None, "no metric specified"
    row = (
        db.query(MetricDefinition)
        .filter(
            MetricDefinition.id == ref,
            MetricDefinition.org_id == org_id,
            MetricDefinition.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    if row is None:
        # Name/synonym resolution WITHOUT the enabled/lifecycle filters —
        # a disabled or retired metric must be FOUND and then honestly
        # reported, not disguised as "not found".
        rows = (
            db.query(MetricDefinition)
            .filter(
                MetricDefinition.org_id == org_id,
                MetricDefinition.is_deleted == False,  # noqa: E712
            )
            .all()
        )
        for r in rows:
            names = [str(r.name or "").lower()]
            names += [str(s or "").lower() for s in (r.synonyms or [])]
            if ref.lower() in names:
                row = r
                break
        else:
            return None, f"metric {metric_ref!r} not found in this org"
    if not row.is_enabled:
        return None, f"metric {row.name!r} is disabled"
    if (row.lifecycle or "published") != "published":
        return None, (
            f"metric {row.name!r} is {row.lifecycle} — "
            "not computable until published" if row.lifecycle == "draft"
            else f"metric {row.name!r} is retired")
    return row, None


def _build_sql(metric, extra_filters: dict | None) -> tuple[str, dict, list]:
    """Deterministic SQL from the declared binding. Raises ValueError on
    anything non-deterministic (unknown aggregation, bad identifiers)."""
    agg = (metric.aggregation or "").strip().lower()
    if agg == "custom":
        raise ValueError(
            "custom aggregation cannot be computed deterministically "
            "without an executable definition")
    template = _AGG_SQL.get(agg)
    if template is None:
        raise ValueError(f"unsupported aggregation {metric.aggregation!r}")
    table = (metric.base_table or "").strip()
    column = (metric.base_column or "").strip()
    if not _IDENT_RE.match(table):
        raise ValueError(f"invalid base table {table!r}")
    if column == "*" and agg == "count":
        col_sql = "*"
    elif not _IDENT_RE.match(column):
        raise ValueError(f"invalid base column {column!r}")
    else:
        col_sql = column

    where: list[str] = []
    params: dict[str, Any] = {}
    i = 0
    for f in (metric.filters_json or []):
        if not isinstance(f, dict):
            continue
        col = str(f.get("column") or "").strip()
        op = str(f.get("op") or "").strip()
        if not _IDENT_RE.match(col) or op not in _FILTER_OPS:
            raise ValueError(f"invalid stored filter {f!r}")
        where.append(_FILTER_OPS[op].format(col=col, i=i))
        if op != "notnull":
            params[f"p_{i}"] = f.get("value")
        i += 1
    for key, value in (extra_filters or {}).items():
        if value is None or not _IDENT_RE.match(str(key)):
            continue
        where.append(f"{key} = :p_{i}")
        params[f"p_{i}"] = value
        i += 1

    sql = f"SELECT {template.format(col=col_sql)} AS metric_value FROM {table}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    return sql, params, [f.get("column") for f in (metric.filters_json or [])
                         if isinstance(f, dict)]


def compute_metric(
    db: Session,
    org_id: str,
    metric_ref: str,
    *,
    filters: dict | None = None,
) -> dict:
    """Resolve + compute a governed metric. Never raises for business
    refusals — typed unavailable instead."""
    from app.models.knowledge_base import KnowledgeBase

    metric, reason = resolve_metric(db, org_id, metric_ref)
    if metric is None:
        return _unavailable(reason)

    kb = (
        db.query(KnowledgeBase)
        .filter(
            KnowledgeBase.id == metric.datasource_id,
            KnowledgeBase.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    if kb is None or (kb.org_id or "default-org") != org_id:
        return _unavailable(
            f"metric {metric.name!r} data source not found in this org")

    try:
        sql, params, declared_filter_cols = _build_sql(metric, filters)
    except ValueError as exc:
        return _unavailable(str(exc))

    from app.services.db.execution_gate import ExecutionValidationError
    from app.services.db.query_service import QueryService

    t0 = time.time()
    try:
        qs = QueryService(db)
        result = qs.execute(kb.id, sql, params=params or None)
        validation = getattr(qs, "last_validation", None) or {}
    except ExecutionValidationError as exc:
        return _unavailable(
            "the metric's SQL was refused by the execution gate",
            gate_violations=[str(exc)])
    except Exception as exc:  # noqa: BLE001 — executor/driver failure
        logger.warning("compute_metric execution failed (%s): %s",
                       metric.name, exc)
        return _unavailable(f"metric computation failed: {exc}")

    rows = result.get("rows") or []
    if not rows:
        return _unavailable("no rows matched the metric's filters")
    value = (rows[0] or {}).get("metric_value")
    if value is None:
        # Honest NULL (e.g. empty set with AVG, or zero-denominator=null
        # semantics) — reported as null, never coerced to 0.
        value = None

    duration_ms = int((time.time() - t0) * 1000)
    return {
        "success": True,
        "unavailable": False,
        "value": value,
        "metric": {
            "id": metric.id,
            "name": metric.name,
            "version": metric.version or 1,
            "aggregation": metric.aggregation,
            "unit": metric.unit,
            "currency": metric.currency,
            "evidence_status": metric.evidence_status,
        },
        "provenance": {
            "datasource_id": kb.id,
            "datasource_name": kb.name,
            "base_table": metric.base_table,
            "base_column": metric.base_column,
            "time_column": metric.time_column,
            "filters_applied": declared_filter_cols
            + [k for k in (filters or {}) if _IDENT_RE.match(str(k))],
        },
        "evidence_status": metric.evidence_status,
        "execution_trace": {
            "sql": sql,
            "gate": "passed",
            "checks": validation.get("checks") if isinstance(validation, dict) else None,
            "duration_ms": duration_ms,
        },
    }
