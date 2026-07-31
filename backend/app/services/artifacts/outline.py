"""Outline-first generation support (P0.2).

Builds a deterministic, user-reviewable *outline spec* from a
``ReportCardPayload`` BEFORE any heavyweight rendering happens.  This is
the Kimi/Gamma "outline gate" pattern: wrong structure is caught at the
cheap JSON stage, not after a full render cycle.

The outline is stored on the artifact (``metadata_json["outline"]``) so:

* the chat UI can render it as an editable approval card, and
* the renderers / future partial-regeneration paths can consume the same
  structure.

Gate behavior is controlled by ``ZHANLU_OUTLINE_GATE``:

* ``off``   — do not build outlines at all
* ``auto``  — build + record the outline, continue rendering (default;
  Kimi-style optional gate — the UI may surface it, the flow never blocks)
* ``block`` — build + record, and mark the artifact ``draft`` so an
  approval step must flip it before eager rendering proceeds

The builder is deterministic (no LLM call) so it is safe to run in the
request path for every artifact and every tenant.
"""

from __future__ import annotations

import os
from typing import Any

from app.services.synexia.contracts import ReportCardPayload

OUTLINE_VERSION = 1


def outline_gate_mode() -> str:
    """Return the configured outline-gate mode: off | auto | block."""
    mode = os.environ.get("ZHANLU_OUTLINE_GATE", "auto").strip().lower()
    return mode if mode in ("off", "auto", "block") else "auto"


def build_outline(payload: ReportCardPayload) -> dict[str, Any]:
    """Build the outline spec for a report payload.

    Produces a dual-purpose structure: ``deck`` lists the slides the PPTX
    renderer will emit (mirroring ``pptx_export.py``'s slide order), and
    ``doc`` lists the DOCX section order.  Both are derived from the same
    payload so deck and doc never diverge structurally.
    """
    p = payload
    slides: list[dict[str, Any]] = [
        {"index": 1, "type": "cover", "title": p.title or "Report"},
    ]
    idx = 2
    if p.kpis:
        slides.append({
            "index": idx, "type": "kpi",
            "title": "Key metrics",
            "items": [k.label for k in p.kpis][:8],
        })
        idx += 1
    if p.chart and p.chart.data:
        slides.append({
            "index": idx, "type": "chart",
            "title": p.chart.title or "Chart",
            "chart_type": p.chart.type or "bar",
            "rows": len(p.chart.data),
        })
        idx += 1
    if p.key_findings:
        slides.append({
            "index": idx, "type": "findings",
            "title": "Key findings",
            "items": [f.text for f in p.key_findings][:6],
        })
        idx += 1
    if p.insights:
        slides.append({
            "index": idx, "type": "insights",
            "title": "Insights",
            "items": [i.text for i in p.insights][:6],
        })
        idx += 1
    if p.recommendations:
        slides.append({
            "index": idx, "type": "recommendations",
            "title": "Recommendations",
            "items": [r.text for r in p.recommendations][:6],
        })
        idx += 1
    # next_step is deliberately excluded from outlines: it is
    # conversational guidance for the chat user, not report/deck content.

    sections: list[dict[str, Any]] = [
        {"index": 1, "type": "summary", "title": "Executive summary"},
    ]
    sidx = 2
    for s in (p.sections or []):
        sections.append({"index": sidx, "type": "section", "title": s.title})
        sidx += 1
    if p.key_findings:
        sections.append({"index": sidx, "type": "findings", "title": "Key findings"})
        sidx += 1
    if p.chart and p.chart.data:
        sections.append({"index": sidx, "type": "data", "title": "Data"})
        sidx += 1
    if p.recommendations:
        sections.append({"index": sidx, "type": "recommendations", "title": "Recommendations"})
        sidx += 1
    if p.methodology:
        sections.append({"index": sidx, "type": "methodology", "title": "Methodology"})
        sidx += 1

    return {
        "version": OUTLINE_VERSION,
        "title": p.title or "Report",
        "gate": outline_gate_mode(),
        "approved": outline_gate_mode() != "block",
        "deck": {"slide_count": len(slides), "slides": slides},
        "doc": {"section_count": len(sections), "sections": sections},
    }


__all__ = ["build_outline", "outline_gate_mode", "OUTLINE_VERSION"]
