"""Locked server-side analytics for fullstack dashboards.

"Python computes, LLM narrates."  Every number the dashboard shows in its
KPI deltas, sparkline badges, top-item callouts, and auto insight strip is
computed HERE by running the metric's OWN SQL through the same
QueryService execution path the generated app uses.  The LLM never
invents these figures — the Aug 2026 failure (agent narrated spot prices
of 3,400-3,600 when the real value was 6,824) is exactly what this module
prevents: numbers are locked at build time from the live datasource.

All functions are defensive: any SQL error, missing column, or timeout
skips that metric's enrichment (returns the metric unchanged) and never
blocks the dashboard build.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

_MAX_ROWS = 500
_TIMEOUT_S = 15


def _first_numeric_value(row: dict) -> Optional[float]:
    """First numeric-ish value in a row, skipping ids/labels/dates."""
    for _k, v in row.items():
        if v is None:
            continue
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
        if isinstance(v, str):
            s = v.strip().replace(",", "")
            try:
                return float(s)
            except ValueError:
                continue
    return None


def _last_value(rows: list[dict]) -> Optional[float]:
    if not rows:
        return None
    return _first_numeric_value(rows[-1])


def _prev_value(rows: list[dict]) -> Optional[float]:
    if not rows or len(rows) < 2:
        return None
    return _first_numeric_value(rows[-2])


def compute_series_delta(rows: list[dict]) -> Optional[dict]:
    """Last-vs-previous-period delta from a time-ordered series.

    Returns ``{"delta_pct": float, "current": float, "previous": float}``
    or None when the series is too short / non-numeric.
    """
    if not rows or len(rows) < 2:
        return None
    current = _last_value(rows)
    previous = _prev_value(rows)
    if current is None or previous is None or previous == 0:
        return None
    return {
        "delta_pct": round((current - previous) / abs(previous) * 100, 2),
        "current": current,
        "previous": previous,
    }


def compute_top_item(rows: list[dict], label_keys: tuple[str, ...]) -> Optional[dict]:
    """Largest-value row from a breakdown (bar/pie/table).

    Returns ``{"label": str, "value": float, "share_pct": float}`` or None.
    """
    best = None
    total = 0.0
    for row in rows:
        val = _first_numeric_value(row)
        if val is None:
            continue
        total += val
        if best is None or val > best["value"]:
            label = None
            for k in label_keys:
                if k in row and row[k] is not None:
                    label = str(row[k])
                    break
            best = {"label": label, "value": val}
    if best is None or total == 0:
        return None
    best["share_pct"] = round(best["value"] / total * 100, 1)
    return best


def _run_metric_sql(db, kb_id: str, sql: str, dimensions: list[dict]) -> list[dict]:
    """Execute one widget SQL through the shared QueryService path.

    Works in BOTH sync and async callers: when called from inside a running
    event loop (e.g. the async tool handlers), run the underlying sync
    ``QueryService.execute`` directly; otherwise go through the async
    ``_run_single_sql`` wrapper. Never raises.
    """
    try:
        import asyncio

        from app.services.dashboard_query import render_widget_sql, validate_widget_sql

        rendered = render_widget_sql(sql, {"filters": {}}, dimensions)
        validate_widget_sql(sql)
        try:
            from app.services.dashboard_query import QueryService
        except Exception:
            from app.services.query_service import QueryService

        def _exec() -> dict:
            res = QueryService(db).execute(kb_id, rendered, max_rows=_MAX_ROWS, timeout_s=_TIMEOUT_S)
            return {
                "columns": list(res["rows"][0].keys()) if res["rows"] else [],
                "rows": res["rows"],
                "error": None,
                "truncated": res.get("truncated", False),
            }

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None:
            # Inside a running loop — run the sync query directly (blocking
            # briefly is fine; these are small bounded SELECTs).
            result = _exec()
        else:
            from app.services.dashboard_query import _run_single_sql
            result = asyncio.run(
                _run_single_sql(
                    db, kb_id, sql, {"filters": {}}, dimensions,
                    _MAX_ROWS, _TIMEOUT_S,
                )
            )
    except Exception as exc:  # noqa: BLE001 — never block a build on analytics
        logger.warning("locked analytics: metric sql run failed (non-fatal): %s", exc)
        return []
    if result.get("error"):
        logger.warning("locked analytics: metric sql error (non-fatal): %s", result["error"])
        return []
    return result.get("rows") or []


def enrich_metric(db, kb_id: str, metric: dict) -> dict:
    """Compute locked analytics for ONE metric and attach to ``options``.

    - kpi/line/area: last-vs-previous delta → ``options.delta`` +
      ``options.deltaLabel`` (the KPI widget already renders these).
    - bar/pie/table: top item + share → ``options.topItem``.
    Returns the metric dict (mutated copy) — never raises.
    """
    metric = dict(metric)
    options = dict(metric.get("options") or {})
    mtype = metric.get("type")
    sql = metric.get("sql")
    if not sql:
        return metric
    dimensions = []
    for f in (options.get("filters") or []):
        if f.get("key") and f.get("column"):
            dimensions.append({"key": f["key"], "column": f["column"]})
    rows = _run_metric_sql(db, kb_id, sql, dimensions)
    if not rows:
        return metric

    if mtype in ("kpi", "line", "area", "combo"):
        delta = compute_series_delta(rows)
        if delta and options.get("delta") is None:
            options["delta"] = delta["delta_pct"]
            options.setdefault("deltaLabel", "vs prev. period")
            options["_locked"] = {
                "current": delta["current"],
                "previous": delta["previous"],
            }
    if mtype in ("bar", "pie", "table", "radar"):
        # Guess label keys: first non-numeric column in the first row.
        label_keys = []
        if rows and isinstance(rows[0], dict):
            for k in rows[0].keys():
                if _first_numeric_value({k: rows[0][k]}) is None and rows[0][k] is not None:
                    label_keys.append(k)
                if len(label_keys) >= 2:
                    break
        top = compute_top_item(rows, tuple(label_keys or ["label", "name", "product", "category"]))
        if top:
            options["topItem"] = top
    if options != (metric.get("options") or {}):
        metric["options"] = options
    return metric


def build_locked_insights(metrics: list[dict]) -> list[dict]:
    """Deterministic insight bullets from computed metrics.

    Only emits bullets for metrics that HAVE locked analytics (never
    fabricates). Returns [] when nothing computable.
    """
    insights = []
    for m in metrics:
        options = m.get("options") or {}
        delta = options.get("delta")
        top = options.get("topItem")
        title = m.get("title") or m.get("id") or "Metric"
        if delta is not None:
            direction = "up" if delta >= 0 else "down"
            emoji = "📈" if delta >= 0 else "📉"
            insights.append({
                "title": f"{emoji} {title}",
                "body": (
                    f"{title} moved {abs(delta):.1f}% {direction} vs the "
                    f"previous period (locked server-side from the live "
                    f"datasource at build time)."
                ),
            })
        if top:
            label = top.get("label") or "top item"
            insights.append({
                "title": f"🏆 Top: {label}",
                "body": (
                    f"{label} leads with {top['value']:,.0f} "
                    f"({top['share_pct']:.1f}% of the total)."
                ),
            })
    return insights[:3]


def enrich_spec(db, kb_id: str, spec: dict) -> dict:
    """Enrich a full DashboardSpec with locked analytics.

    Runs each metric's SQL once, attaches computed deltas / top items to
    ``options``, and appends deterministic insight bullets (deduped by
    title against any agent-authored insights).  Never raises.
    """
    try:
        metrics = [enrich_metric(db, kb_id, m) for m in (spec.get("metrics") or [])]
        if metrics:
            spec["metrics"] = metrics
        existing_titles = {i.get("title") for i in (spec.get("insights") or [])}
        locked = [
            i for i in build_locked_insights(metrics)
            if i.get("title") not in existing_titles
        ]
        if locked:
            spec["insights"] = list((spec.get("insights") or [])[:2]) + locked[:3]
    except Exception as exc:  # noqa: BLE001
        logger.warning("locked analytics: spec enrichment failed (non-fatal): %s", exc)
    return spec
