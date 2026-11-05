"""Phase 2.7 — approval hardening: destructive tier is ALWAYS gated.

The SYSTEM_META_AGENTS bypass (agent_builder, skill_agent, automation_agent,
general_assistant, power_user) previously skipped requires_confirmation
unconditionally — including DESTRUCTIVE actions. With
EXEC_APPROVAL_DESTRUCTIVE_ALWAYS=True (default), destructive-tier calls
require human approval regardless of agent class. Lower tiers keep existing
behavior. Approval-pending results are never retried (pinned elsewhere).

Run: pytest tests/test_approval_hardening.py -q
"""
from unittest.mock import MagicMock, patch

import pytest

from app.config import settings


def _perm(risk="destructive", action="delete", requires_confirmation=True):
    p = MagicMock()
    p.allowed = True
    p.requires_confirmation = requires_confirmation
    p.reason = "needs approval"
    p.risk = risk
    p.action = action
    return p


@pytest.mark.asyncio
async def test_destructive_requires_approval_even_for_meta_agent():
    perm = _perm()
    with patch("app.services.permissions.check_permission", return_value=perm), \
         patch("app.services.governance.approval_service.ApprovalService") as MockSvc:
        MockSvc.return_value.create_request.return_value = MagicMock(id="ar-1")
        from app.services.agent_tools import execute_tool

        result = await execute_tool(
            "cronjob", {"action": "delete", "id": "j1"},
            db=MagicMock(), user_id="u1",
            context={"agent_name": "general_assistant"},
        )
    assert result.get("requires_approval") is True, (
        "destructive action bypassed the approval gate for a meta-agent"
    )


@pytest.mark.asyncio
async def test_destructive_gate_can_be_disabled_by_flag():
    perm = _perm()
    with patch("app.services.permissions.check_permission", return_value=perm), \
         patch.object(settings, "EXEC_APPROVAL_DESTRUCTIVE_ALWAYS", False):
        from app.services.agent_tools import execute_tool

        result = await execute_tool(
            "cronjob", {"action": "delete", "id": "j1"},
            db=MagicMock(), user_id="u1",
            context={"agent_name": "general_assistant"},
        )
    # Legacy behavior: bypassed for meta-agents (falls through to execution;
    # the handler itself may fail on the fake db — we only assert the GATE
    # did not pause).
    assert result.get("requires_approval") is None


@pytest.mark.asyncio
async def test_write_tier_meta_agent_bypass_unchanged():
    # Lower tiers keep the existing behavior (no new friction).
    perm = _perm(risk="write", action=None)
    with patch("app.services.permissions.check_permission", return_value=perm):
        from app.services.agent_tools import execute_tool

        result = await execute_tool(
            "update_agent", {"agent_id": "a1", "name": "x"},
            db=MagicMock(), user_id="u1",
            context={"agent_name": "general_assistant"},
        )
    assert result.get("requires_approval") is None


def test_flag_defaults_on():
    assert settings.EXEC_APPROVAL_DESTRUCTIVE_ALWAYS is True
