"""Phase 2.3/2.4 wiring — delegation results carry the structured decision
and authority-aware alternatives.

Run: pytest tests/test_delegate_phase2_wiring.py -q
"""
import asyncio

import pytest


class _FakeReport:
    def __init__(self, d, needs_evidence):
        self._d = d
        self.needs_evidence = needs_evidence

    def to_dict(self):
        return self._d


def _patch_review(monkeypatch, report):
    monkeypatch.setattr(
        "app.services.agent_collaboration.reviewer.review_findings", lambda *a, **k: report
    )


def _patch_sub_agent(monkeypatch, result):
    async def fake_run(*a, **k):
        return result

    monkeypatch.setattr(
        "app.services.tool_handlers.delegate_tool._run_sub_agent", fake_run
    )


def test_reviewed_delegation_carries_execution_decision(monkeypatch):
    from app.services.tool_handlers import delegate_tool as dt

    _patch_review(monkeypatch, _FakeReport(
        {"verdict": "needs_revision", "is_complete": False, "needs_evidence": True,
         "missing_evidence": ["verified cost data per model"]},
        needs_evidence=["verified cost data per model"],
    ))
    _patch_sub_agent(monkeypatch, {"success": True, "response": "draft"})

    result = asyncio.run(dt._maybe_review_and_refine(
        "Calculate profitability by vehicle model", "analyst", None, None, None,
        {"_enable_review": True},
        {"success": True, "response": "draft"},
    ))
    decision = result.get("execution_decision")
    assert decision is not None
    assert decision["decision"] == "delegate_specialist"
    assert "cost data" in (decision.get("affected_task") or "")
    # no chain-of-thought keys
    assert "reasoning" not in decision and "thought" not in decision


def test_approved_review_decision_is_complete(monkeypatch):
    from app.services.tool_handlers import delegate_tool as dt

    _patch_review(monkeypatch, _FakeReport(
        {"verdict": "approved", "is_complete": True}, needs_evidence=[],
    ))
    _patch_sub_agent(monkeypatch, {"success": True, "response": "done"})

    result = asyncio.run(dt._maybe_review_and_refine(
        "t", "a", None, None, None, {"_enable_review": True},
        {"success": True, "response": "done"},
    ))
    assert result["execution_decision"]["decision"] == "complete"


def test_failed_delegation_carries_alternative_capabilities(monkeypatch):
    from app.services.tool_handlers import delegate_tool as dt

    _patch_sub_agent(monkeypatch, {"success": False, "error": "sub-agent failed"})
    monkeypatch.setattr(dt, "_published_agent_candidates", lambda db: [
        {"name": "sales_analyst", "description": "Vehicle sales revenue analysis", "published": True},
        {"name": "revenue_analyst", "description": "Revenue and sales performance reporting", "published": True},
        {"name": "finance_agent", "description": "Cost and margin analysis", "published": True},
    ])

    result = asyncio.run(dt._delegate_task(
        {"task": "Analyze vehicle sales revenue", "agent_name": "sales_analyst"},
        None, None, context={},
    ))
    alts = result.get("alternative_capabilities")
    assert isinstance(alts, list) and alts
    assert all("published" in a["rationale"].lower() for a in alts)
    # The failed agent itself is excluded from its own fallback list.
    assert all(a["name"] != "sales_analyst" for a in alts)


def test_failed_delegation_without_alternatives_reports_empty_honestly(monkeypatch):
    from app.services.tool_handlers import delegate_tool as dt

    _patch_sub_agent(monkeypatch, {"success": False, "error": "boom"})
    monkeypatch.setattr(dt, "_published_agent_candidates", lambda db: [])

    result = asyncio.run(dt._delegate_task(
        {"task": "Do a very unusual thing", "agent_name": "nobody"},
        None, None, context={},
    ))
    assert result.get("alternative_capabilities") == []


def test_successful_delegation_has_no_alternatives_field(monkeypatch):
    from app.services.tool_handlers import delegate_tool as dt

    _patch_sub_agent(monkeypatch, {"success": True, "response": "done"})
    result = asyncio.run(dt._delegate_task(
        {"task": "x", "agent_name": "a"}, None, None, context={},
    ))
    assert "alternative_capabilities" not in result
