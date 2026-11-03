"""Phase 10 / W5 (audit S5.1): delegation targets must be organization-
scoped.

``_resolve_agent_app`` previously resolved a PUBLISHED AgentApp by name
across ALL orgs (cross-org prompt/tool_config loadable; same-name
resolution nondeterministic). Contract now:

* resolution takes the caller's org and filters by it;
* when the caller's org CANNOT be resolved and AgentApp-aware delegation
  is enabled, the delegation is BLOCKED (fail closed) — never resolved
  cross-org.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest


class _Q:
    """Fake query that EVALUATES the filter expressions against the row
    (each expr is a SQLAlchemy BinaryExpression Column == value), so the
    org wall is actually verified rather than stubbed out."""

    def __init__(self, row):
        self._row = row
        self._exprs = []

    def filter(self, *exprs):
        self._exprs.extend(exprs)
        return self

    def first(self):
        for e in self._exprs:
            left = getattr(e, "left", None)
            right = getattr(e, "right", None)
            if left is None or right is None:
                continue
            col = getattr(left, "key", None)
            if col is None:
                continue
            if getattr(self._row, col, None) != getattr(right, "value", right):
                return None
        return self._row


def test_resolve_agent_app_filters_by_caller_org():
    from app.services.tool_handlers.delegate_tool import _resolve_agent_app

    same_org = SimpleNamespace(id="a1", org_id="org-ok", name="specialist",
                               is_deleted=False, status="published")
    db = SimpleNamespace(query=lambda *a: _Q(same_org))
    out = _resolve_agent_app(db, "specialist", caller_org_id="org-ok")
    assert out is same_org


def test_resolve_agent_app_refuses_cross_org():
    from app.services.tool_handlers.delegate_tool import _resolve_agent_app

    cross_org = SimpleNamespace(id="a2", org_id="org-OTHER", name="specialist",
                                is_deleted=False, status="published")
    db = SimpleNamespace(query=lambda *a: _Q(cross_org))
    out = _resolve_agent_app(db, "specialist", caller_org_id="org-ok")
    assert out is None, "a published AgentApp of ANOTHER org must never resolve"


@pytest.mark.asyncio
async def test_delegate_blocked_when_caller_org_unresolvable(monkeypatch):
    from app.config import settings
    from app.services.tool_handlers import delegate_tool as dt

    monkeypatch.setattr(settings, "DELEGATE_AGENTAPP_AWARE", True,
                        raising=False)
    # user_id=None and no conversation → caller org unresolvable
    with patch.object(dt, "_resolve_caller_org", return_value=None):
        result = await dt._delegate_task(
            args={"task": "do things", "agent_name": "specialist"},
            db=None, user_id=None, context={})
    assert result["success"] is False
    assert result["status"] == "blocked"
    assert "organization" in (result.get("error") or "").lower()


@pytest.mark.asyncio
async def test_delegate_resolves_target_within_caller_org(monkeypatch):
    """The happy path: the org wall filters the AgentApp query (verified by
    the unit tests above); here we pin that _delegate_task PASSES the
    caller org into resolution instead of the unscoped name-only lookup."""
    from app.config import settings
    from app.services.tool_handlers import delegate_tool as dt

    monkeypatch.setattr(settings, "DELEGATE_AGENTAPP_AWARE", True,
                        raising=False)
    seen = {}

    def fake_resolve(db, agent_name, caller_org_id=None):
        seen["caller_org_id"] = caller_org_id
        return None  # fall through to the legacy builtin path

    ran = {}

    async def fake_run(*a, **k):
        ran["ok"] = True
        return {"success": True, "status": "completed"}

    with patch.object(dt, "_resolve_caller_org",
                      return_value="org-ok"), \
         patch.object(dt, "_resolve_agent_app", fake_resolve):
        # Downstream (LLM loop, broker, invocation persistence) is
        # exercised by the existing delegation suites; this test only
        # pins that the caller org reaches the AgentApp resolution —
        # which happens BEFORE any downstream work. Downstream failures
        # with db=None / no LLM key are irrelevant to that contract.
        try:
            await dt._delegate_task(
                args={"task": "t", "agent_name": "specialist"},
                db=None, user_id="u1", context={})
        except Exception:  # noqa: BLE001 — downstream db=None noise
            pass
    assert seen["caller_org_id"] == "org-ok"
