"""_payload_from_execution — map cached DataExecution to ReportCardPayload dict."""
from __future__ import annotations
from typing import Any


def _payload_from_execution(execution: Any) -> dict:
    result = getattr(execution, "result", None) or {}
    rows = result.get("rows") or []
    columns = result.get("columns") or []
    summary = result.get("summary") or result.get("text") or ""

    title = result.get("title") or f"Report from {execution.tool_name}"
    methodology = (
        f"Data sourced from {execution.tool_name} "
        f"({len(rows)} rows, {len(columns)} columns). "
        f"Cached at {getattr(execution, 'created_at', 'unknown')}."
    )

    kpis = []
    if rows and len(rows) == 1 and len(columns) <= 6:
        for col, val in zip(columns, rows[0]):
            kpis.append({"label": str(col), "value": val, "caption": f"From {execution.tool_name}"})

    chart = None
    if rows and len(rows) > 1 and len(columns) >= 2:
        chart = {
            "type": "bar",
            "title": f"{columns[0]} by {columns[1]}",
            "data": {
                "x": [r[0] for r in rows[:50]],
                "y": [r[1] for r in rows[:50]],
                "x_label": str(columns[0]),
                "y_label": str(columns[1]),
            },
        }

    return {
        "title": title,
        "summary": summary[:1000] if summary else "",
        "methodology": methodology,
        "kpis": kpis,
        "chart": chart,
        "source": execution.tool_name,
    }
