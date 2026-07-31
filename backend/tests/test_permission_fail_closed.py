"""SP2-WS-C: permission check must fail CLOSED (deny) on any exception.

Previously both the approval-creation failure and the permission-check
exception fell back to ALLOW, widening access on error paths. After SP2 both
must return a denied result dict.
"""
import pytest


@pytest.mark.asyncio
async def test_permission_check_exception_denies():
    """An exception inside check_permission must deny, not allow."""
    from unittest.mock import patch
    from app.services.agent_tools import execute_tool

    with patch(
        "app.services.permissions.check_permission", side_effect=RuntimeError("boom")
    ):
        result = await execute_tool(
            "create_agent", {}, db=None, user_id="u1",
            context={"agent_name": "general_assistant"},
        )

    assert result["success"] is False
    assert "fail-closed" in result["error"].lower() or "permission check failed" in result["error"].lower()


@pytest.mark.asyncio
async def test_approval_creation_failure_denies():
    """When requires_confirmation=True but ApprovalService.create_request raises,
    the tool must be denied, not allowed."""
    from unittest.mock import patch, MagicMock
    from app.services.agent_tools import execute_tool

    perm_result = MagicMock()
    perm_result.allowed = True
    perm_result.requires_confirmation = True
    perm_result.reason = "needs approval"

    with patch("app.services.permissions.check_permission", return_value=perm_result), \
         patch("app.services.governance.approval_service.ApprovalService") as MockApprovalSvc:
        MockApprovalSvc.return_value.create_request.side_effect = RuntimeError("db down")
        result = await execute_tool(
            "create_agent", {"name": "x"},
            db=MagicMock(), user_id="u1",
            context={"agent_name": "some_user_agent"},  # NOT in SYSTEM_META_AGENTS
        )

    assert result["success"] is False
    assert "fail-closed" in result["error"].lower() or "approval gate unavailable" in result["error"].lower()
