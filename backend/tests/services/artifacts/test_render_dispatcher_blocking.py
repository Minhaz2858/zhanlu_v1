"""Blocking audit gate tests for render_dispatcher.

A deck whose semantic audit still reports FAIL after the deterministic repair
loop must NOT be delivered: ``render_pptx_from_plan*`` returns empty bytes
plus the FAIL report instead of the rendered deck.  The gate is controlled by
``PPT_AUDIT_BLOCKING_ENABLED`` (default True); deployments can set it False to
restore the historical ship-anyway behavior.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.config import settings
from app.services.artifacts import render_dispatcher as rd
from app.services.synexia.contracts import DeckPlan

FAIL_REPORT: dict[str, Any] = {
    "tool": "audit_deck",
    "status": "FAIL",
    "summary": {"pass": 0, "warn": 0, "fail": 2, "total": 2},
    "rules": [
        {
            "id": "density_6x6",
            "title": "Density",
            "level": "FAIL",
            "detail": "bullets too dense",
            "evidence": [],
        },
        {
            "id": "font_floor",
            "title": "Font floor",
            "level": "FAIL",
            "detail": "font too small",
            "evidence": [],
        },
    ],
}

PASS_REPORT: dict[str, Any] = {
    "tool": "audit_deck",
    "status": "PASS",
    "summary": {"pass": 2, "warn": 0, "fail": 0, "total": 2},
    "rules": [],
}


def _enable_audit(monkeypatch: pytest.MonkeyPatch, *, blocking: bool = True) -> None:
    monkeypatch.setattr(settings, "PPT_AUDIT_ENABLED", True)
    monkeypatch.setattr(settings, "PPT_AUDIT_BLOCKING_ENABLED", blocking)


def test_fail_after_repairs_returns_empty_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deck that still FAILs the audit after the repair loop is NOT delivered."""
    _enable_audit(monkeypatch, blocking=True)
    monkeypatch.setattr(rd, "_repair_bytes", lambda data, report: None)  # nothing fixable
    monkeypatch.setattr(rd, "_audit_bytes", lambda data: FAIL_REPORT)

    data, report = rd.render_pptx_from_plan_sync(DeckPlan(title="t", slides=[]), [], {})

    assert data == b""
    assert report["status"] == "FAIL"


def test_pass_delivers_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deck that PASSes the audit is delivered normally."""
    _enable_audit(monkeypatch, blocking=True)
    monkeypatch.setattr(rd, "_render_once", lambda plan, rows, ctx: b"PPTXDATA")
    monkeypatch.setattr(rd, "_repair_bytes", lambda data, report: None)
    monkeypatch.setattr(rd, "_audit_bytes", lambda data: PASS_REPORT)

    data, report = rd.render_pptx_from_plan_sync(DeckPlan(title="t", slides=[]), [], {})

    assert data == b"PPTXDATA"
    assert report["status"] == "PASS"


def test_blocking_off_keeps_old_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    """PPT_AUDIT_BLOCKING_ENABLED=False ships a FAIL deck anyway (old behavior)."""
    _enable_audit(monkeypatch, blocking=False)
    monkeypatch.setattr(rd, "_render_once", lambda plan, rows, ctx: b"PPTXDATA")
    monkeypatch.setattr(rd, "_repair_bytes", lambda data, report: None)
    monkeypatch.setattr(rd, "_audit_bytes", lambda data: FAIL_REPORT)

    data, report = rd.render_pptx_from_plan_sync(DeckPlan(title="t", slides=[]), [], {})

    assert data == b"PPTXDATA"
    assert report["status"] == "FAIL"
