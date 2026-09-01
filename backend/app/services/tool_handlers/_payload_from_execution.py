"""_payload_from_execution — map cached DataExecution to ReportCardPayload dict."""
from __future__ import annotations
import logging
from typing import Any, Optional

from app.config import settings

logger = logging.getLogger(__name__)


def _payload_from_execution(
    execution: Any,
    *,
    request_text: str = "",
    user_context: Optional[dict] = None,
) -> dict:
    result = getattr(execution, "result", None) or {}
    rows = result.get("rows") or []
    # Defensive: ask_data_agent (and some other tools) never populate
    # result["columns"].  Derive from the first row's keys when rows are
    # dicts so that KPIs, charts, sections and the methodology string are
    # not collapsed to "0 columns".
    _raw_columns = result.get("columns")
    if _raw_columns:
        columns = _raw_columns
    elif rows and isinstance(rows[0], dict):
        columns = list(rows[0].keys())
    else:
        columns = []
    summary = result.get("summary") or result.get("text") or ""

    title = result.get("title") or f"Report from {execution.tool_name}"

    # Resolve feature flag once, before any branch below references it.
    flag_on = bool(getattr(settings, "REPORT_AUTO_ANALYSIS_ENABLED", True))

    # summary can legitimately be a dict (e.g. fetch_data_batch stores a
    # batch-stats object {successful, failed, total_rows, ...}). Only slice
    # strings — never dicts (dict[:N] raises "unhashable type: 'slice'").
    if isinstance(summary, str):
        summary_s = summary[:1000]
    else:
        summary_s = str(summary)[:1000]

    kpis = []
    if rows and len(rows) == 1 and len(columns) <= 6:
        row = rows[0]
        if isinstance(row, dict):
            _items = [(col, row.get(col)) for col in columns]
        else:
            _items = list(zip(columns, row))
        for col, val in _items:
            kpis.append({"label": str(col), "value": val, "caption": f"From {execution.tool_name}"})

    # The legacy 2-col chart below is intentionally skipped when we have a
    # wide table — it only fits a column1-by-column2 bar chart and would
    # either truncate a 7+ col dataset or push the wrong x/y axis. The
    # auto-analysis path (below) produces a properly-aggregated chart for
    # any width.
    _will_run_auto_analysis = (
        flag_on and len(rows) >= 2 and len(columns) > 6
    )
    chart = None
    if rows and len(rows) > 1 and len(columns) >= 2 and not _will_run_auto_analysis:
        _col0, _col1 = columns[0], columns[1]
        if rows and isinstance(rows[0], dict):
            _x = [r.get(_col0) for r in rows[:50]]
            _y = [r.get(_col1) for r in rows[:50]]
        else:
            _x = [r[0] for r in rows[:50]]
            _y = [r[1] for r in rows[:50]]
        chart = {
            "type": "bar",
            "title": f"{_col0} by {_col1}",
            "data": {
                "x": _x,
                "y": _y,
                "x_label": str(_col0),
                "y_label": str(_col1),
            },
        }

    base = {
        "title": title,
        "summary": summary_s,
        "methodology": (
            f"Data sourced from {execution.tool_name} "
            f"({len(rows)} rows, {len(columns)} columns). "
            f"Cached at {getattr(execution, 'created_at', 'unknown')}."
        ),
        "kpis": kpis,
        "chart": chart,
        "source": execution.tool_name,
    }

    # Augment with rich auto-analysis when the simple heuristics above yield
    # a sparse payload (most realistic case: many rows + many columns).
    # Gated by feature flag so it can be disabled instantly if it misfires.
    sparse = (
        len(rows) >= 2
        and (not kpis or len(columns) > 6)
    )
    if flag_on and sparse:
        try:
            from app.services.tool_handlers._report_auto_analysis import (
                auto_analyze,
            )
            enriched = auto_analyze(
                rows,
                columns,
                tool_name=execution.tool_name,
                title_hint=title,
            )
            if isinstance(enriched, dict) and enriched:
                # Auto-analysis fields fill only what base left empty —
                # explicit LLM payload (in artifact_tool._create_artifact_tool)
                # will still win over both.
                for key in ("summary", "kpis", "key_findings",
                             "recommendations", "chart", "sections",
                             "methodology", "title"):
                    if key not in base or not base.get(key):
                        if enriched.get(key):
                            base[key] = enriched[key]
                # Append "Auto-analysis" note so the report is transparent
                # about how it was derived.
                if enriched.get("methodology"):
                    base["methodology"] = (
                        f"{base.get('methodology', '').rstrip(' ')} "
                        f"{enriched['methodology']}"
                    ).strip()
        except Exception:
            # Never break the export on auto-analysis issues; keep the base.
            pass

    # Phase: fully-dynamic document generation. When the cached execution
    # carries enough data, synthesize an adaptive block plan (cover, exec
    # summary, KPI grid, data-driven sections with charts, findings,
    # recommendations, methodology, appendix) so the rendered docx/pptx is
    # rich and tailored to the data — not the bare fixed dump. Any `blocks`
    # the agent puts directly in its create_artifact payload still win over
    # this (merged on top after this returns).
    if flag_on and len(rows) >= 2 and len(columns) > 1 and not base.get("blocks"):
        try:
            from app.services.artifacts.architect import synthesize_plan
            _plan = synthesize_plan(
                title=title,
                rows=rows,
                columns=columns,
                summary=summary_s,
                kpis=kpis,
                findings=result.get("key_findings") or [],
                recommendations=result.get("recommendations") or [],
                request_text=request_text,
                user_context=user_context,
                theme="zhanlu-blue",
            )
            if _plan and _plan.blocks:
                # Hybrid layer: let the LLM rewrite the narrative prose
                # (executive summary, findings, recommendations) on top of the
                # deterministic structure. Never breaks the export — any failure
                # leaves the architect's text in place.
                if getattr(settings, "DYNAMIC_DOCUMENT_LLM_NARRATIVE_ENABLED", True):
                    try:
                        from app.services.artifacts.llm_narrative import (
                            enrich_plan_narrative_sync,
                        )
                        enrich_plan_narrative_sync(
                            _plan,
                            rows=rows,
                            columns=columns,
                            request_text=request_text or title,
                            user_context=user_context or getattr(execution, "user_context", None),
                        )
                    except Exception as _enrich_err:  # pragma: no cover
                        # Deterministic narrative already in _plan; keep it.
                        logger.warning(
                            "dynamic narrative enrichment skipped: %s", _enrich_err
                        )
                base["blocks"] = [b.model_dump() for b in _plan.blocks]
        except Exception:
            # Never break the export on architect issues; keep the base.
            pass

    return base
