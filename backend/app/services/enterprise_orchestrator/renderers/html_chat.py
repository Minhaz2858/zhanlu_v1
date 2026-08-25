"""HTML (chat markdown) renderer for the enterprise payload (spec §10.2).

Produces inline markdown with the same 6 sections as the DOCX renderer,
plus a collapsed ``<details>`` Data Lineage block whose claim entries carry
``id="claim-N"`` anchors so the executive-summary citations ``[N]`` can link
to them.
"""
from __future__ import annotations

from typing import Any


def _table_columns(rows: list[dict]) -> list[str]:
    cols: list[str] = []
    for row in rows:
        if isinstance(row, dict):
            for key in row:
                if key not in cols:
                    cols.append(key)
    return cols


def _markdown_table(rows: list[dict]) -> str:
    if not rows:
        return "*(data unavailable)*\n"
    cols = _table_columns(rows)
    header = "| " + " | ".join(cols) + " |"
    sep = "| " + " | ".join("---" for _ in cols) + " |"
    body = []
    for row in rows:
        body.append("| " + " | ".join(str(row.get(c, "")) for c in cols) + " |")
    return "\n".join([header, sep, *body]) + "\n"


def _bullets(items: list[str]) -> str:
    if not items:
        return "*(data unavailable)*\n"
    return "\n".join(f"- {i}" for i in items) + "\n"


def _render_lineage(payload: dict) -> str:
    """Render the collapsed Data Lineage block with claim anchors."""
    confidence = payload.get("data_confidence") or {}
    missing_reasons = confidence.get("missing_reasons") or {}
    missing_facets = confidence.get("missing_facets") or []

    lines = ["<details>", "<summary>Data Lineage</summary>", ""]

    lineage = payload.get("lineage")
    if lineage:
        for facet_id, info in lineage.items():
            lines.append(f"### Facet: {facet_id}")
            if not info.get("available"):
                reason = missing_reasons.get(facet_id, "data unavailable")
                lines.append(f"*(data unavailable: {reason})*")
                lines.append("")
                continue
            if info.get("source_label"):
                lines.append(f"- Source: {info['source_label']}")
            if info.get("source_sql"):
                lines.append("```sql")
                lines.append(str(info["source_sql"]))
                lines.append("```")
            lines.append(f"- Row count: {info.get('row_count', 0)}")
            for entry in info.get("execution_log") or []:
                step = entry.get("step", "?")
                latency = entry.get("latency_ms", "")
                status = entry.get("status", "")
                lines.append(f"- {step} ({latency} ms, {status})")
            lines.append("")
    else:
        claims = payload.get("claims") or []
        for i, claim in enumerate(claims, start=1):
            lines.append(f'<a id="claim-{i}"></a>')
            lines.append(f"### Claim {i}: {claim.get('claim_id', '?')}")
            lines.append(f"- {claim.get('text', '')}")
            if claim.get("source_sql"):
                lines.append("```sql")
                lines.append(str(claim["source_sql"]))
                lines.append("```")
            lines.append("")

    listed = set((payload.get("lineage") or {}).keys())
    for facet_id in missing_facets:
        if facet_id in listed:
            continue
        lines.append(f"### Facet: {facet_id}")
        lines.append(f"*(data unavailable: {missing_reasons.get(facet_id, 'no data')})*")
        lines.append("")

    lines.append("</details>")
    return "\n".join(lines)


def render_enterprise_html(payload: dict) -> str:
    """Render an enterprise payload into inline chat markdown (str)."""
    out: list[str] = []

    title = payload.get("title") or "Enterprise Data Report"
    out.append(f"# {title}")
    out.append("")
    if payload.get("period_label"):
        out.append(f"**Period:** {payload['period_label']}")
    if payload.get("source_label"):
        out.append(f"**Source:** {payload['source_label']}")
    out.append("")

    # Section 1: Executive Summary (with citation anchors).
    out.append("## Executive Summary")
    out.append("")
    summary = payload.get("executive_summary", "")
    citations = "".join(f'<a href="#claim-{i}">[{i}]</a>' for i in range(1, len(payload.get("claims") or []) + 1))
    out.append(f"{summary}{citations}")
    out.append("")

    # Section 2: Primary Metric Breakdown.
    pmb = payload.get("primary_metric_breakdown") or {}
    out.append(f"## {pmb.get('label') or 'Primary Metric Breakdown'}")
    out.append("")
    if pmb.get("available"):
        kpi = pmb.get("kpi") or {}
        if kpi:
            out.append("**KPI:** " + "; ".join(f"{k}: {v}" for k, v in kpi.items()))
            out.append("")
        out.append(_markdown_table(pmb.get("rows") or []))
    else:
        out.append(f"*(data unavailable: {pmb.get('unavailable_reason', '')})*")
        out.append("")

    # Section 3: Segment Decomposition.
    seg = payload.get("segment_decomposition") or {}
    out.append(f"## {seg.get('label') or 'Segment Decomposition'}")
    out.append("")
    if seg.get("available"):
        out.append(_markdown_table(seg.get("rows") or []))
        for obs in seg.get("observations") or []:
            out.append(f"- {obs}")
        out.append("")
    else:
        out.append(f"*(data unavailable: {seg.get('unavailable_reason', '')})*")
        out.append("")

    # Section 4: Operational Drivers.
    drivers = payload.get("operational_drivers") or {}
    out.append(f"## {drivers.get('label') or 'Operational Drivers'}")
    out.append("")
    if drivers.get("available"):
        out.append(_bullets(drivers.get("drivers") or []))
    else:
        out.append("*(data unavailable)*")
        out.append("")

    # Section 5: Risk Section.
    risk = payload.get("risk_section") or {}
    out.append(f"## {risk.get('label') or 'Risk Section'}")
    out.append("")
    if risk.get("available"):
        out.append(_bullets(risk.get("risks") or []))
    else:
        out.append("*(data unavailable)*")
        out.append("")

    # Section 6: Recommended Actions.
    out.append("## Recommended Actions")
    out.append("")
    actions = payload.get("recommended_actions") or []
    if not actions:
        out.append("*(none)*")
    for i, action in enumerate(actions, start=1):
        severity = action.get("severity", "medium")
        out.append(f"{i}. **[{severity.upper()}]** {action.get('action', '')}")
    out.append("")

    # Appendix: Data Lineage (collapsed).
    out.append(_render_lineage(payload))

    return "\n".join(out).strip() + "\n"
