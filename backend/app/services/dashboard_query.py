"""Live-dashboard query orchestration + widget-SQL safety guard.

The guard is applied at create time (in the ``create_dashboard`` tool) AND at
query time (in the ``/query`` endpoint) — stored SQL is never trusted.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone

from app.services.db.query_service import QueryService

# DDL/DML/admin keywords that must never appear as a statement-leading verb.
_FORBIDDEN_LEADING = re.compile(
    r"^\s*(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|MERGE|REPLACE|CALL|EXEC|EXECUTE)\b",
    re.IGNORECASE,
)

# A semicolon followed by more non-whitespace SQL = multiple statements.
_MULTI_STMT = re.compile(r";\s*\S")

MIN_REFRESH = 10
MAX_REFRESH = 300
DEFAULT_REFRESH = 30

DEFAULT_MAX_ROWS = 1000
DEFAULT_TIMEOUT_S = 10


def validate_widget_sql(sql: str) -> None:
    """Raise ValueError if ``sql`` is not a single read-only SELECT/WITH statement."""
    if sql is None or not sql.strip():
        raise ValueError("Widget SQL is empty")
    if _FORBIDDEN_LEADING.match(sql):
        raise ValueError("Widget SQL must be read-only (SELECT/WITH only)")
    if _MULTI_STMT.search(sql):
        raise ValueError("Widget SQL must be a single statement")
    # Must start with SELECT or WITH after stripping leading parens.
    stripped = sql.strip().lstrip("(").strip()
    if not re.match(r"^(SELECT|WITH)\b", stripped, re.IGNORECASE):
        raise ValueError("Widget SQL must start with SELECT or WITH")


def clamp_refresh_interval(seconds: int | None) -> int:
    if seconds is None:
        return DEFAULT_REFRESH
    return max(MIN_REFRESH, min(MAX_REFRESH, int(seconds)))


# --- Whitelisted token render layer -----------------------------------------
# Substitutes :from/:to/:date (date window), :dim_<token> (cross-widget filter),
# :drill_value (drill-down) in widget SQL. Unknown tokens raise ValueError.
# The rendered SQL is re-validated by validate_widget_sql before returning
# (defense in depth — stored SQL is never trusted).

_TOKEN_RE = re.compile(r"(?<!:):([a-zA-Z_][a-zA-Z0-9_]*)")
_ALLOWED_TOKENS = {"from", "to", "date", "drill_value"}


def _literal(s) -> str:
    """Quote a value as a SQL string literal, escaping embedded single quotes."""
    return "'" + str(s).replace("'", "''") + "'"


def _resolve_window(params: dict | None) -> tuple[datetime, datetime]:
    """Return (from, to) timezone-aware datetimes. Defaults to last-30-days."""
    p = params or {}

    def _parse(v):
        try:
            d = datetime.fromisoformat(v) if v else None
        except (TypeError, ValueError):
            return None
        if d is not None and d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)  # treat naive ISO as UTC
        return d

    to = _parse(p.get("to")) or datetime.now(timezone.utc)
    frm = _parse(p.get("from")) or (to - timedelta(days=30))
    return frm, to


def render_widget_sql(sql: str, params: dict | None, dimensions: list[dict] | None = None) -> str:
    """Substitute whitelisted tokens, then re-validate the rendered SQL.

    Tokens: :from :to :date (date window), :dim_<token> (cross-widget filter),
    :drill_value (drill-down). Unknown tokens raise ValueError. The rendered
    SQL is passed through validate_widget_sql again before returning (defense
    in depth — stored SQL is never trusted). The negative lookbehind avoids
    matching Postgres ``::`` casts.
    """
    params = params or {}
    dimensions = dimensions or []
    # LLM-output hygiene (2026-08-28): models sometimes emit Python-style
    # doubled percents (DATE_FORMAT(x, '%%Y-%%m')) which MySQL rejects with
    # 1064. Normalize BEFORE token substitution + validation so both the
    # create-time analytics pass and query-time execution get clean SQL.
    sql = sql.replace("%%", "%")
    frm, to = _resolve_window(params)
    dim_map = {d["token"]: d["column"] for d in dimensions}
    active_dims = params.get("filters") or {}

    # Reject any :token that isn't whitelisted BEFORE substituting.
    for m in _TOKEN_RE.finditer(sql):
        tok = m.group(1)
        if tok in _ALLOWED_TOKENS:
            continue
        if tok.startswith("dim_"):
            if tok[4:] in dim_map:
                continue
            raise ValueError(f"Unknown dimension token :{tok}")
        raise ValueError(f"Unknown token :{tok}")

    def _sub(m):
        tok = m.group(1)
        if tok == "from":
            return _literal(frm.strftime("%Y-%m-%d %H:%M:%S"))
        if tok == "to":
            return _literal(to.strftime("%Y-%m-%d %H:%M:%S"))
        if tok == "date":
            return _literal(to.strftime("%Y-%m-%d"))
        if tok == "drill_value":
            return _literal(params.get("drill_value", ""))
        # dim_<token> (declared — validated above)
        name = tok[4:]
        val = active_dims.get(name)
        if val is None or val == "":
            return "1=1"
        return f"{dim_map[name]} = {_literal(val)}"

    rendered = _TOKEN_RE.sub(_sub, sql)
    validate_widget_sql(rendered)  # defense in depth
    return rendered


async def _run_single_sql(db, kb_id, sql, params, dimensions, max_rows, timeout_s) -> dict:
    """Render + validate + execute ONE widget SQL.

    Shared by ``run_dashboard_query`` (per-widget), ``_run_drill``, and the
    ``/preview-sql`` endpoint. Returns ``{columns, rows, error, truncated}``.
    Render/validation/DB errors are caught and reported in ``error`` (never
    raised) so one bad query cannot abort a dashboard or a preview round-trip.
    """
    try:
        rendered = render_widget_sql(sql, params, dimensions)
    except ValueError as e:
        return {"columns": [], "rows": [], "error": str(e), "truncated": False}
    try:
        res = await asyncio.to_thread(
            lambda: QueryService(db).execute(
                kb_id, rendered, max_rows=max_rows, timeout_s=timeout_s
            )
        )
        return {
            "columns": list(res["rows"][0].keys()) if res["rows"] else [],
            "rows": res["rows"],
            "error": None,
            "truncated": res.get("truncated", False),
        }
    except Exception as e:  # ValueError, DriverUnavailable, DB error, etc.
        return {"columns": [], "rows": [], "error": str(e), "truncated": False}


async def run_dashboard_query(db, dashboard, params: dict | None = None) -> dict:
    """Run every widget's SQL (token-rendered) against the dashboard's datasource.

    ``params`` may carry: ``from``/``to`` (ISO), ``filters`` {token: value},
    ``drill`` {widget_id, value}. None/empty params = current behavior
    (last-30-days default, no filters). Each widget is rendered + re-validated
    + executed independently; a failing widget sets that widget's ``error``
    and does NOT abort the others (one bad SQL = one red widget, not a blank
    dashboard). All widgets use the bound KB's query controls as upper bounds.
    """
    params = params or {}
    widgets = (dashboard.definition or {}).get("widgets", []) or []
    # Resolve KB controls as upper bounds (fall back to defaults if KB missing).
    max_rows = DEFAULT_MAX_ROWS
    timeout_s = DEFAULT_TIMEOUT_S
    try:
        from app.models.knowledge_base import KnowledgeBase
        kb = db.query(KnowledgeBase).filter(
            KnowledgeBase.id == dashboard.datasource_kb_id
        ).first()
        if kb is not None:
            max_rows = int(getattr(kb, "max_rows_per_query", DEFAULT_MAX_ROWS) or DEFAULT_MAX_ROWS)
            timeout_s = int(getattr(kb, "timeout_seconds", DEFAULT_TIMEOUT_S) or DEFAULT_TIMEOUT_S)
    except Exception:
        pass

    async def _run_one(widget: dict) -> tuple[str, dict]:
        wid = widget.get("id") or widget.get("title")
        dimensions = (widget.get("options") or {}).get("dimensions") or []
        return wid, await _run_single_sql(
            db, dashboard.datasource_kb_id, widget.get("sql") or "",
            params, dimensions, max_rows, timeout_s)

    pairs = await asyncio.gather(*[_run_one(w) for w in widgets])
    results = {wid: payload for wid, payload in pairs}

    drill = params.get("drill")
    if isinstance(drill, dict) and drill.get("widget_id"):
        results["__drill__"] = await _run_drill(
            db, dashboard, params, drill, max_rows, timeout_s)

    return {
        "dashboard_id": dashboard.id,
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
        "results": results,
    }


async def _run_drill(db, dashboard, params, drill, max_rows, timeout_s) -> dict:
    """Execute the clicked widget's ``options.drill.sql`` with :drill_value set.

    Additive — normal widgets still run so the dashboard stays live. The drill
    SQL is rendered (token substitution + re-validation) just like widget SQL.
    """
    src_id = drill.get("widget_id")
    value = drill.get("value", "")
    widgets = (dashboard.definition or {}).get("widgets", []) or []
    src = next((w for w in widgets if (w.get("id") or w.get("title")) == src_id), None)
    base = {"columns": [], "rows": [], "error": None, "truncated": False,
            "source_widget_id": src_id, "drill_value": value}
    if not src:
        base["error"] = "Drill source widget not found"
        return base
    drill_opts = (src.get("options") or {}).get("drill") or {}
    drill_sql = drill_opts.get("sql")
    if not drill_sql:
        base["error"] = "Widget has no drill SQL"
        return base
    dimensions = (src.get("options") or {}).get("dimensions") or []
    result = await _run_single_sql(
        db, dashboard.datasource_kb_id, drill_sql,
        {**params, "drill_value": value}, dimensions, max_rows, timeout_s)
    # Merge the shared helper's columns/rows/error/truncated, keeping the
    # drill-specific metadata (source_widget_id, drill_value) on `base`.
    base["columns"] = result["columns"]
    base["rows"] = result["rows"]
    base["error"] = result["error"]
    base["truncated"] = result["truncated"]
    return base
