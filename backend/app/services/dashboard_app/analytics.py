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

Hardening (2026-09-02):
- Dimension tokens are declared as {"token": ...} (render_widget_sql's
  contract) — previously the analytics pass built {"key": ...} entries, so
  EVERY metric that declared a filter crashed the render with KeyError and
  silently lost its locked analytics.
- A freshly computed delta ALWAYS overrides an authored/stale delta. The
  old "only set when None" gate preserved LLM-authored estimates — the exact
  fabrication this module exists to kill — and left stale numbers on
  re-enrich.
- Value columns are isolated per row-set: series/breakdowns with multiple
  numeric columns (SELECT date, revenue, target …) used to sum EVERY numeric
  column into the top-item share total and could pick a secondary measure as
  the "value". Now one value column is chosen per metric (first numeric col
  after the leading x/label column — the same convention the frontend
  widgets use), and totals/shares/deltas only touch that column.
- Series deltas are sign-safe: if the SQL orders a parseable date column
  newest-first, rows are normalized to chronological order before the
  last-vs-previous comparison so the delta never reports inverted movement.
"""

from __future__ import annotations

import datetime as _dt
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

_MAX_ROWS = 500
_TIMEOUT_S = 15


def _is_numeric_value(v) -> bool:
    """True when ``v`` looks like a usable number (never bool/None)."""
    if v is None:
        return False
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return True
    if isinstance(v, str):
        try:
            float(v.strip().replace(",", ""))
            return True
        except ValueError:
            return False
    return False


def _as_float(v) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip().replace(",", ""))
        except ValueError:
            return None
    return None


def _value_and_label_keys(row: dict, declared_value_key: Optional[str] = None
                          ) -> tuple[Optional[str], Optional[str]]:
    """Pick (label_key, value_key) for a row using the FRONTEND convention.

    The first column is the x/label dimension; the value is the first
    numeric column AFTER it (mirrors widgets/index.jsx numericKeys()).
    When the row has a single numeric column, that column is the value.
    A declared value key (options.valueKey) wins when present in the row.
    """
    keys = list(row.keys())
    if not keys:
        return None, None
    value_key = None
    if declared_value_key and declared_value_key in row:
        value_key = declared_value_key
    else:
        for k in keys:
            if k == keys[0] and len(keys) > 1:
                continue  # leading column is the dimension, not the measure
            if _is_numeric_value(row.get(k)):
                value_key = k
                break
        if value_key is None and _is_numeric_value(row.get(keys[0])):
            value_key = keys[0]  # single-column numeric row
    label_key = next((k for k in keys if k != value_key), None)
    return label_key, value_key


def _parse_date(v) -> Optional[_dt.datetime]:
    """Parse a cell as a datetime, tolerating str / native date-time types."""
    if v is None:
        return None
    if isinstance(v, _dt.datetime):
        return v
    if isinstance(v, _dt.date):
        return _dt.datetime(v.year, v.month, v.day)
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return _dt.datetime.fromisoformat(s)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d", "%d/%m/%Y"):
        try:
            return _dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _ensure_chronological(rows: list[dict]) -> list[dict]:
    """Reverse rows when a confidently-parseable date column is DESCENDING.

    Metric SQL is expected to order time series ascending, but LLM-generated
    SQL frequently emits ``ORDER BY <date> DESC``; a blind last-vs-previous
    delta would then report inverted movement. Only flips when a date-ish
    column parses across the whole series and is strictly descending —
    never guesses on ambiguous data.
    """
    if not rows or len(rows) < 2:
        return rows
    # Candidate columns: rank by parse coverage, prefer the earliest column
    # that is confidently a date (parseable ratio >= 0.6).
    best_col = None
    best_ratio = 0.0
    for k in rows[0].keys():
        parsed = [_parse_date(r.get(k)) for r in rows]
        ok = [p for p in parsed if p is not None]
        if not ok:
            continue
        ratio = len(ok) / len(rows)
        if ratio >= 0.6 and ratio > best_ratio:
            best_col = ok
            best_ratio = ratio
        if ratio >= 0.6:
            break  # earliest column that is clearly a date wins
    if best_col is None or len(best_col) < 2:
        return rows
    desc = all(a >= b for a, b in zip(best_col, best_col[1:])) and any(
        a > b for a, b in zip(best_col, best_col[1:])
    )
    if not desc:
        return rows
    return list(reversed(rows))


def _row_value_key(rows: list[dict], declared_value_key: Optional[str] = None) -> Optional[str]:
    """Pick the value column ONCE for a whole row-set (isolation)."""
    for row in rows:
        if isinstance(row, dict) and row:
            _label, value_key = _value_and_label_keys(row, declared_value_key)
            if value_key:
                return value_key
    return None


def _last_value(rows: list[dict], value_key: Optional[str] = None) -> Optional[float]:
    if not rows:
        return None
    value_key = value_key or _row_value_key(rows)
    if not value_key:
        return None
    return _as_float(rows[-1].get(value_key))


def _prev_value(rows: list[dict], value_key: Optional[str] = None) -> Optional[float]:
    if not rows or len(rows) < 2:
        return None
    value_key = value_key or _row_value_key(rows)
    if not value_key:
        return None
    return _as_float(rows[-2].get(value_key))


def compute_series_delta(rows: list[dict]) -> Optional[dict]:
    """Last-vs-previous-period delta from a time-ordered series.

    Returns ``{"delta_pct": float, "current": float, "previous": float}``
    or None when the series is too short / non-numeric. The series is first
    normalized to chronological order when a parseable date column is
    descending, so the sign always reflects real movement.
    """
    if not rows or len(rows) < 2:
        return None
    rows = _ensure_chronological(rows)
    value_key = _row_value_key(rows)
    current = _last_value(rows, value_key)
    previous = _prev_value(rows, value_key)
    if current is None or previous is None or previous == 0:
        return None
    return {
        "delta_pct": round((current - previous) / abs(previous) * 100, 2),
        "current": current,
        "previous": previous,
    }


def compute_top_item(rows: list[dict], label_keys: tuple[str, ...] = ()) -> Optional[dict]:
    """Largest-value row from a breakdown (bar/pie/table).

    Returns ``{"label": str, "value": float, "share_pct": float}`` or None.
    The value column is picked ONCE for the row-set (first numeric column
    after the leading label column) and the share total only sums THAT
    column — multi-measure rows (product, revenue, target) no longer inflate
    the denominator by adding every numeric column together.
    """
    if not rows:
        return None
    value_key = _row_value_key(rows)
    if not value_key:
        return None
    best = None
    total = 0.0
    for row in rows:
        if not isinstance(row, dict):
            continue
        val = _as_float(row.get(value_key))
        if val is None:
            continue
        total += val
        if best is None or val > best["value"]:
            label = None
            # Preferred label columns first, then the first non-value column.
            for k in (*label_keys, *[k for k in row.keys() if k != value_key]):
                if k in row and row[k] is not None and str(row[k]).strip() != "":
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
    result: Optional[dict] = None
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
    if result is None or result.get("error"):
        logger.warning("locked analytics: metric sql error (non-fatal): %s",
                       result.get("error") if result else "no result")
        return []
    return result.get("rows") or []


def _dimensions_from_options(options: dict) -> list[dict]:
    """Declared dims in render_widget_sql's shape: {"token", "column"}.

    2026-09-02 fix: the old code emitted {"key", "column"}, which
    render_widget_sql rejects (it indexes d["token"]) — so every metric that
    declared a filter crashed the analytics render and silently lost its
    locked delta/top-item. The FILTER KEY is the token the metric's SQL uses
    (:dim_<key>), and dimensions maps token → real column.
    """
    dims = []
    for f in (options.get("filters") or []):
        if not isinstance(f, dict):
            continue
        if f.get("key") and f.get("column"):
            dims.append({"token": f["key"], "column": f["column"]})
    return dims


def enrich_metric(db, kb_id: str, metric: dict, options_hint: Optional[dict] = None) -> dict:
    """Compute locked analytics for ONE metric and attach to ``options``.

    - kpi/line/area: last-vs-previous delta → ``options.delta`` +
      ``options.deltaLabel`` (the KPI widget already renders these).
    - bar/pie/table: top item + share → ``options.topItem``.

    A freshly computed delta ALWAYS overwrites any existing/authored delta
    (2026-09-02 — the old "only when None" gate preserved LLM-authored
    estimates, the exact fabrication this module prevents). Returns the
    metric dict (copied) — never raises.
    """
    metric = dict(metric)
    options = dict(metric.get("options") or {})
    if options_hint is not None:
        options = {**options, **dict(options_hint)}
    mtype = metric.get("type")
    sql = metric.get("sql")
    if not sql:
        return metric
    dimensions = _dimensions_from_options(options)
    rows = _run_metric_sql(db, kb_id, sql, dimensions)
    if not rows:
        return metric

    declared_value = options.get("valueKey") or options.get("yKey")
    if mtype in ("kpi", "line", "area", "combo"):
        delta = compute_series_delta(rows)
        if delta:
            # Override authored/stale values — locked numbers win.
            options["delta"] = delta["delta_pct"]
            options.setdefault("deltaLabel", "vs prev. period")
            options["_locked"] = {
                "current": delta["current"],
                "previous": delta["previous"],
            }
    if mtype in ("bar", "pie", "table", "radar"):
        label_keys = []
        if rows and isinstance(rows[0], dict):
            for k in rows[0].keys():
                if k != _row_value_key(rows) and not _is_numeric_value(rows[0].get(k)):
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
