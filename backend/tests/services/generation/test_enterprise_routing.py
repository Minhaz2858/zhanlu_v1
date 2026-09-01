"""Tests for the enterprise pipeline short-circuit in the docx
exporter and the orchestrator's enterprise-payload miner.

When ``collect_enterprise_data`` produced an executive report this
turn, the deliverable must be the 6-section executive DOCX (cover,
exec summary with citations, KPI grid, breakdown, drivers, risks,
actions, lineage appendix) — NOT a generic ReportCard.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "app"))


# ---------------------------------------------------------------------------
# _mine_enterprise_payload (in generation_orchestrator.py)
# ---------------------------------------------------------------------------

def test_mine_enterprise_payload_returns_executive_payload():
    from app.services.generation_orchestrator import _mine_enterprise_payload

    tool_calls = [
        {
            "name": "ask_data_agent",
            "results": {"rows": [{"x": 1}], "report_card_payload": {"summary": "old"}},
        },
        {
            "name": "collect_enterprise_data",
            "results": {
                "success": True,
                "enterprise_report_kind": "executive",
                "payload": {"title": "Q3 Sales", "enterprise_report_kind": "executive"},
            },
        },
    ]
    payload = _mine_enterprise_payload(tool_calls)
    assert payload is not None
    assert payload["title"] == "Q3 Sales"
    assert payload["enterprise_report_kind"] == "executive"


def test_mine_enterprise_payload_returns_none_when_no_enterprise():
    from app.services.generation_orchestrator import _mine_enterprise_payload

    tool_calls = [
        {
            "name": "ask_data_agent",
            "results": {"rows": [{"x": 1}]},
        },
    ]
    assert _mine_enterprise_payload(tool_calls) is None


def test_mine_enterprise_payload_skips_superseded():
    from app.services.generation_orchestrator import _mine_enterprise_payload

    tool_calls = [
        {
            "name": "collect_enterprise_data",
            "__superseded": True,
            "results": {
                "success": True,
                "enterprise_report_kind": "executive",
                "payload": {"title": "OLD", "enterprise_report_kind": "executive"},
            },
        },
    ]
    assert _mine_enterprise_payload(tool_calls) is None


def test_mine_enterprise_payload_skips_failed_results():
    from app.services.generation_orchestrator import _mine_enterprise_payload

    tool_calls = [
        {
            "name": "collect_enterprise_data",
            "results": {
                "success": False,
                "enterprise_report_kind": "executive",
                "payload": {"title": "FAILED"},
            },
        },
    ]
    assert _mine_enterprise_payload(tool_calls) is None


def test_mine_enterprise_payload_skips_wrong_kind():
    from app.services.generation_orchestrator import _mine_enterprise_payload

    tool_calls = [
        {
            "name": "collect_enterprise_data",
            "results": {
                "success": True,
                "enterprise_report_kind": "summary",
                "payload": {"title": "WRONG"},
            },
        },
    ]
    assert _mine_enterprise_payload(tool_calls) is None


# ---------------------------------------------------------------------------
# docx_export.render delegation
# ---------------------------------------------------------------------------

def test_docx_export_delegates_to_enterprise_renderer(monkeypatch):
    """When the payload carries enterprise_report_kind=='executive',
    docx_export.render must delegate to render_enterprise_docx and
    return its bytes (not a generic ReportCard DOCX)."""
    captured = {}

    def _spy(payload):
        captured["called"] = True
        captured["title"] = payload.get("title")
        # Return a distinguishable byte string.
        return b"ENTERPRISE-DOCX-MARKER"

    monkeypatch.setattr(
        "app.services.enterprise_orchestrator.renderers.render_enterprise_docx",
        _spy,
    )

    from app.services.artifacts.exporters import docx_export
    enterprise_payload = {
        "enterprise_report_kind": "executive",
        "title": "Q3 2026 Sales Report",
        "executive_summary": "Revenue grew 12%.",
    }
    data, mime, ext = docx_export.render(enterprise_payload)
    assert captured.get("called") is True
    assert captured["title"] == "Q3 2026 Sales Report"
    assert data == b"ENTERPRISE-DOCX-MARKER"
    assert mime == docx_export.MIME
    assert ext == docx_export.EXT


def test_docx_export_falls_back_to_generic_when_no_marker(monkeypatch):
    """When the payload does NOT carry enterprise_report_kind, the
    exporter must use the generic python-docx/pandoc path (NOT
    delegate to render_enterprise_docx)."""
    from app.services.artifacts.exporters import docx_export

    called = {"yes": False}

    def _spy(payload):
        called["yes"] = True
        return b"SHOULD-NOT-USE"

    monkeypatch.setattr(
        "app.services.enterprise_orchestrator.renderers.render_enterprise_docx",
        _spy,
    )

    # Build a payload without the enterprise marker. The python-docx
    # path may still fail in the test env, but the important
    # assertion is that the enterprise renderer was NOT invoked.
    try:
        docx_export.render({"title": "Generic", "summary": "x"})
    except Exception:
        pass  # python-docx/pandoc unavailable in test env is fine

    assert called["yes"] is False
