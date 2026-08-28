"""HTML-design render path must run the SHARED audit, not fabricate PASS.

The ``_render_deck_pipeline`` sandbox branch used to short-circuit with a
hand-written ``{"status": "PASS", "summary": "html_design_renderer"}`` report,
skipping the audit entirely — so un-audited HTML-rendered bytes could ship and
the recorded audit report lied.  Fix: audit the HTML bytes with the shared
``render_dispatcher._audit_bytes``; on FAIL fall back to the structured
layout-engine path (which runs the full audit/repair/blocking loop); on PASS
return the REAL audit report.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.config import settings
from app.services.artifacts.exporters._common import ExportContext
from app.services.artifacts.exporters.service import ExportService
from app.services.synexia.contracts import DeckPlan, SlidePlan

PASS_REPORT: dict[str, Any] = {
    "tool": "audit_deck",
    "status": "PASS",
    "summary": {"pass": 2, "warn": 0, "fail": 0, "total": 2},
    "rules": [{"id": "source_citation", "title": "Source citation", "level": "PASS", "detail": ""}],
}

FAIL_REPORT: dict[str, Any] = {
    "tool": "audit_deck",
    "status": "FAIL",
    "summary": {"pass": 0, "warn": 0, "fail": 1, "total": 1},
    "rules": [{"id": "source_citation", "title": "Source citation", "level": "FAIL", "detail": "no footer"}],
}


def _plan() -> DeckPlan:
    return DeckPlan(
        title="Design Deck",
        deck_type="data_report",
        slides=[SlidePlan(layout="cover", title="Design Deck")],
    )


def _enable_sandbox_route(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "PPT_DECK_PLANNER_ENABLED", True)
    monkeypatch.setattr(settings, "PPT_SMART_ROUTER_ENABLED", True)
    monkeypatch.setattr(settings, "HTML_DESIGN_RENDERER_ENABLED", True)
    monkeypatch.setattr(settings, "PPT_AUDIT_ENABLED", True)
    monkeypatch.setattr(settings, "PPT_AUDIT_BLOCKING_ENABLED", True)
    monkeypatch.setattr(settings, "PPT_LLM_POLISH_ENABLED", False)

    async def fake_classify(intent, explicit=None, allow_llm_fallback=False):
        return "data_report"

    async def fake_plan(user_intent, rows, theme_recommendation=None, profile_name=None, *,
                        theme_tokens=None, user_context=None):
        return _plan(), "data_report"

    def fake_context(db, *, artifact=None, user_id=None, conversation_id=None,
                     user_message="", agent_app_id=None, org_id="default-org", app_id="default-app"):
        return None

    monkeypatch.setattr("app.services.artifacts.deck_router.classify_profile", fake_classify)
    monkeypatch.setattr("app.services.artifacts.deck_planner.build_deck_plan", fake_plan)
    monkeypatch.setattr("app.services.artifacts.exporters.service.build_deck_user_context", fake_context)
    monkeypatch.setattr("app.services.artifacts.deck_router.route_deck", lambda plan, msg: "sandbox")


def _run_pipeline(monkeypatch: pytest.MonkeyPatch, *, html_bytes: bytes):
    monkeypatch.setattr("app.services.artifacts.render_html_deck.render_html_deck", lambda plan, ctx: html_bytes)
    monkeypatch.setattr("app.services.artifacts.render_html_deck.html_design_available", lambda: True)
    svc = ExportService(None)
    return svc._render_deck_pipeline(
        None, ExportContext(source="kb"), [],
        user_message="design-heavy deck",
    )


def test_html_design_pass_returns_real_audit_report(monkeypatch: pytest.MonkeyPatch) -> None:
    """On PASS the html_design path returns the REAL audit report (with rule
    details) — NOT the fabricated ``html_design_renderer`` summary."""
    _enable_sandbox_route(monkeypatch)
    monkeypatch.setattr(
        "app.services.artifacts.render_dispatcher._audit_bytes",
        lambda data: PASS_REPORT,
    )

    data, mime, ext, report = _run_pipeline(monkeypatch, html_bytes=b"HTMLBYTES")

    assert data == b"HTMLBYTES"
    assert ext == "pptx"
    assert report == PASS_REPORT
    assert report.get("summary") != "html_design_renderer"
    assert report["rules"][0]["id"] == "source_citation"
    # The audit really ran on the html bytes.
    assert mime.startswith("application/vnd.openxmlformats-officedocument")


def test_html_design_fail_falls_back_to_layout_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """On FAIL the html_design bytes are NOT shipped: the pipeline falls back
    to the structured layout-engine path (full audit/repair/blocking loop)."""
    _enable_sandbox_route(monkeypatch)
    monkeypatch.setattr(
        "app.services.artifacts.render_dispatcher._audit_bytes",
        lambda data: FAIL_REPORT,
    )
    layout_report = {"status": "PASS", "summary": {"pass": 1, "warn": 0, "fail": 0, "total": 1}, "rules": []}
    monkeypatch.setattr(
        "app.services.artifacts.render_dispatcher.render_pptx_from_plan_sync",
        lambda plan, rows, ctx: (b"LAYOUTBYTES", layout_report),
    )

    data, _mime, _ext, report = _run_pipeline(monkeypatch, html_bytes=b"HTMLBYTES")

    assert data == b"LAYOUTBYTES"  # fell back — html bytes discarded
    assert report == layout_report  # layout path's real report
