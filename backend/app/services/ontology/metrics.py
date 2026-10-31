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


def _build_sql(metric, extra_filters: dict | None = None,
               filter_overrides: dict | None = None) -> tuple[str, dict, list]:
    """Deterministic SQL from the declared binding. Raises ValueError on
    anything non-deterministic (unknown aggregation, bad identifiers).

    `filter_overrides` is the governed period/segment selector: it REPLACES
    (and may add) equality filters on the metric's declared binding. It can
    never change base_table/base_column/aggregation, so the MetricDefinition
    stays authoritative. `extra_filters` remain additional AND constraints (the
    original behaviour — used by run_business_question for ad-hoc segments)."""
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

    # Merge stored filters with governed period/segment overrides.
    stored = [f for f in (metric.filters_json or []) if isinstance(f, dict)]
    replaced: set[str] = set()
    merged: list[dict] = []
    for f in stored:
        col = str(f.get("column") or "").strip()
        if filter_overrides and col in filter_overrides:
            f = {**f, "value": filter_overrides[col]}
            replaced.add(col)
        merged.append(f)
    for col, val in (filter_overrides or {}).items():
        if col in replaced or not _IDENT_RE.match(str(col)):
            continue
        merged.append({"column": col, "op": "eq", "value": val})

    where: list[str] = []
    params: dict[str, Any] = {}
    applied_cols: list[str] = []
    i = 0
    for f in merged:
        col = str(f.get("column") or "").strip()
        op = str(f.get("op") or "").strip()
        if not _IDENT_RE.match(col) or op not in _FILTER_OPS:
            raise ValueError(f"invalid stored filter {f!r}")
        if op == "in":
            # List params are NOT portable across connectors (sqlite3 and
            # plain-dict SQLAlchemy text() don't expand them) — expand to
            # one placeholder per value (review finding 1).
            values = f.get("value") or []
            if not isinstance(values, list) or not values:
                raise ValueError("'in' filter needs a non-empty list value")
            phs = ", ".join(f":p_{i}_{j}" for j in range(len(values)))
            where.append(f"{col} IN ({phs})")
            for j, v in enumerate(values):
                params[f"p_{i}_{j}"] = v
        else:
            where.append(_FILTER_OPS[op].format(col=col, i=i))
            if op != "notnull":
                params[f"p_{i}"] = f.get("value")
        applied_cols.append(col)
        i += 1
    for key, value in (extra_filters or {}).items():
        if value is None or not _IDENT_RE.match(str(key)):
            continue
        where.append(f"{key} = :p_{i}")
        params[f"p_{i}"] = value
        applied_cols.append(str(key))
        i += 1

    sql = f"SELECT {template.format(col=col_sql)} AS metric_value FROM {table}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    return sql, params, applied_cols


def compute_metric(
    db: Session,
    org_id: str,
    metric_ref: str,
    *,
    filters: dict | None = None,
    filter_overrides: dict | None = None,
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
        sql, params, declared_filter_cols = _build_sql(metric, filters, filter_overrides)
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
            "depends_on_metrics": metric.depends_on_metrics or [],
        },
        "provenance": {
            "datasource_id": kb.id,
            "datasource_name": kb.name,
            "base_table": metric.base_table,
            "base_column": metric.base_column,
            "time_column": metric.time_column,
            "filters_applied": declared_filter_cols,
            "filter_overrides_applied": [
                k for k in (filter_overrides or {}) if _IDENT_RE.match(str(k))],
        },
        "evidence_status": metric.evidence_status,
        "execution_trace": {
            "sql": sql,
            "gate": "passed",
            "checks": validation.get("checks") if isinstance(validation, dict) else None,
            "duration_ms": duration_ms,
        },
    }


def compare_metric_periods(
    db: Session, org_id: str, metric_ref: str, *,
    current_period: str, prior_period: str,
    segment_column: str | None = None, segments: list | None = None,
    extra_filters: dict | None = None,
) -> dict:
    """Governed period comparison (current vs prior). Deterministic — reuses
    compute_metric; never fabricates a delta. Honest unavailable states only."""
    metric, reason = resolve_metric(db, org_id, metric_ref)
    if metric is None:
        return _unavailable(reason)
    time_col = metric.time_column
    if not time_col:
        return _unavailable("metric has no time dimension for period comparison")

    def _compute(period: str, segment=None):
        fo: dict = {time_col: period}
        if segment is not None and segment_column:
            fo[segment_column] = segment
        if extra_filters:
            fo.update(extra_filters)
        return compute_metric(db, org_id, metric_ref, filter_overrides=fo)

    current = _compute(current_period)
    prior = _compute(prior_period)
    out: dict = {
        "success": True,
        "unavailable": False,
        "metric": {
            "id": metric.id, "name": metric.name, "version": metric.version or 1,
            "aggregation": metric.aggregation, "unit": metric.unit,
            "currency": metric.currency, "time_column": time_col,
        },
        "current_period": current_period,
        "prior_period": prior_period,
        "current": current,
        "prior": prior,
    }
    if segment_column and segments:
        rows = []
        for seg in segments:
            c = _compute(current_period, seg)
            p = _compute(prior_period, seg)
            cv = c.get("value") if c.get("success") else None
            pv = p.get("value") if p.get("success") else None
            delta = (cv - pv) if (cv is not None and pv is not None) else None
            pct = (delta / pv * 100.0) if (delta is not None and pv) else None
            rows.append({
                "segment": seg, "current_value": cv, "prior_value": pv,
                "delta": delta, "delta_pct": pct,
                "declining": bool(delta is not None and delta < 0),
                "available": bool(c.get("success") and p.get("success")),
            })
        out["segments"] = rows
        out["declining_segments"] = [r["segment"] for r in rows if r["declining"]]
    return out


def trend_metric(
    db: Session, org_id: str, metric_ref: str, *,
    periods: list[str], extra_filters: dict | None = None,
) -> dict:
    """Governed multi-period series for charting. Reuses compute_metric per
    period; unavailable periods are returned as null points (never fabricated)."""
    metric, reason = resolve_metric(db, org_id, metric_ref)
    if metric is None:
        return _unavailable(reason)
    time_col = metric.time_column
    if not time_col:
        return _unavailable("metric has no time dimension for trend")
    points = []
    for period in periods:
        res = compute_metric(
            db, org_id, metric_ref,
            filter_overrides={time_col: period, **(extra_filters or {})})
        points.append({
            "period": period,
            "value": res.get("value") if res.get("success") else None,
            "unavailable": res.get("unavailable", False),
            "reason": res.get("reason") if not res.get("success") else None,
        })
    return {
        "success": True, "unavailable": False,
        "metric": {"id": metric.id, "name": metric.name, "time_column": time_col},
        "periods": points,
    }


def discover_distinct(db: Session, org_id: str, metric, column: str,
                     periods: list[str] | None = None):
    """Governed DISTINCT segment discovery through the execution gate — used so
    the UI need not hardcode business keys. Returns a list of values."""
    from app.models.knowledge_base import KnowledgeBase
    from app.services.db.query_service import QueryService

    if not _IDENT_RE.match(column) or not _IDENT_RE.match(metric.base_table):
        return _unavailable("invalid segment column")
    kb = (
        db.query(KnowledgeBase)
        .filter(KnowledgeBase.id == metric.datasource_id,
                KnowledgeBase.is_deleted == False)  # noqa: E712
        .first())
    if kb is None or (kb.org_id or "default-org") != org_id:
        return _unavailable("data source not found")
    where = []
    params: dict[str, Any] = {}
    if periods and metric.time_column and _IDENT_RE.match(metric.time_column):
        phs = ", ".join(f":t_{j}" for j in range(len(periods)))
        where.append(f"{metric.time_column} IN ({phs})")
        params = {f"t_{j}": p for j, p in enumerate(periods)}
    sql = (
        f"SELECT DISTINCT {column} AS v FROM {metric.base_table}"
        + (f" WHERE {' AND '.join(where)}" if where else ""))
    try:
        qs = QueryService(db)
        res = qs.execute(kb.id, sql, params=params or None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("discover_distinct failed (%s): %s", column, exc)
        return _unavailable("segment discovery failed")
    return [r.get("v") for r in (res.get("rows") or []) if r.get("v") is not None]


def discover_periods(db: Session, org_id: str, metric, n: int = 6):
    """Governed discovery of a metric's latest time periods from its own
    time_column/base_table (through the execution gate). Keeps the UI free of
    hardcoded business calendars."""
    from app.models.knowledge_base import KnowledgeBase
    from app.services.db.query_service import QueryService

    time_col = metric.time_column
    if not time_col or not _IDENT_RE.match(time_col) or not _IDENT_RE.match(metric.base_table):
        return _unavailable("metric has no time dimension")
    kb = (
        db.query(KnowledgeBase)
        .filter(KnowledgeBase.id == metric.datasource_id,
                KnowledgeBase.is_deleted == False)  # noqa: E712
        .first())
    if kb is None or (kb.org_id or "default-org") != org_id:
        return _unavailable("data source not found")
    sql = (
        f"SELECT DISTINCT {time_col} AS v FROM {metric.base_table} "
        f"ORDER BY {time_col} DESC LIMIT {int(n)}")
    try:
        qs = QueryService(db)
        res = qs.execute(kb.id, sql)
    except Exception as exc:  # noqa: BLE001
        logger.warning("discover_periods failed: %s", exc)
        return _unavailable("period discovery failed")
    vals = [r.get("v") for r in (res.get("rows") or []) if r.get("v") is not None]
    return list(reversed(vals))  # ascending chronological order
