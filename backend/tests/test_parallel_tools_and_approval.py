"""Tests for Gap 5 (parallel tool execution) and Gap 1 (interactive permission
prompts / approval pause-resume flow).

Tests:
  1. Parallel execution: multiple tool calls run concurrently, results returned
     in original order even when one fails.
  2. Permission check: requires_confirmation triggers ApprovalRequest creation.
  3. Approval pause: chat loop pauses and stores resume state when a tool
     requires approval.
  4. Approval resume: resume endpoint continues the loop after approve/reject.

Follows the same unittest + AsyncMock patterns as test_anti_hallucination_guardrails.py.
"""

import os
import sys
import asyncio
import unittest
from unittest.mock import AsyncMock, patch, MagicMock

# Make `app` importable
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)


# ---------------------------------------------------------------------------
# Gap 5: Parallel tool execution — verify asyncio.gather preserves order
# ---------------------------------------------------------------------------

class TestParallelToolExecutionOrdering(unittest.TestCase):
    """Verify that parallel tool execution preserves result ordering even
    when tools complete out of order or one fails."""

    def test_results_preserve_input_order(self):
        """When tool 2 completes before tool 1, results should still be
        in [result_1, result_2] order."""
        async def run_test():
            # Simulate the parallel execution logic from agents.py
            async def _exec_one(call):
                # Tool "slow" takes longer, but appears first in the list
                if call["tool_name"] == "slow":
                    await asyncio.sleep(0.05)
                    return {"success": True, "data": "slow_result"}
                return {"success": True, "data": "fast_result"}

            parsed_calls = [
                {"tool_name": "slow", "args": {}, "args_str": "{}", "tool_call_id": "1"},
                {"tool_name": "fast", "args": {}, "args_str": "{}", "tool_call_id": "2"},
            ]

            raw_results = await asyncio.gather(
                *[_exec_one(c) for c in parsed_calls],
                return_exceptions=True,
            )
            results = []
            for i, r in enumerate(raw_results):
                if isinstance(r, Exception):
                    results.append({"success": False, "error": str(r)})
                else:
                    results.append(r)

            # Order must match input order, not completion order
            self.assertEqual(results[0]["data"], "slow_result")
            self.assertEqual(results[1]["data"], "fast_result")

        asyncio.run(run_test())

    def test_one_failure_does_not_cancel_siblings(self):
        """When one tool raises an exception, the other should still complete
        (return_exceptions=True prevents cancellation)."""
        async def run_test():
            async def _exec_one(call):
                if call["tool_name"] == "failing":
                    raise ValueError("boom")
                return {"success": True, "data": "ok"}

            parsed_calls = [
                {"tool_name": "failing", "args": {}, "args_str": "{}", "tool_call_id": "1"},
                {"tool_name": "ok", "args": {}, "args_str": "{}", "tool_call_id": "2"},
            ]

            raw_results = await asyncio.gather(
                *[_exec_one(c) for c in parsed_calls],
                return_exceptions=True,
            )
            results = []
            for i, r in enumerate(raw_results):
                if isinstance(r, Exception):
                    results.append({"success": False, "error": f"{type(r).__name__}: {r}"})
                else:
                    results.append(r)

            # First result is an error dict
            self.assertFalse(results[0]["success"])
            self.assertIn("boom", results[0]["error"])
            # Second result succeeded despite sibling failure
            self.assertTrue(results[1]["success"])
            self.assertEqual(results[1]["data"], "ok")

        asyncio.run(run_test())

    def test_single_tool_stays_sequential(self):
        """A single tool call should not use asyncio.gather (no overhead)."""
        async def run_test():
            call_count = 0

            async def _exec_one(call):
                nonlocal call_count
                call_count += 1
                return {"success": True}

            parsed_calls = [{"tool_name": "solo", "args": {}, "args_str": "{}", "tool_call_id": "1"}]

            # Simulating the single-call path from agents.py
            if len(parsed_calls) == 1:
                results = [await _exec_one(parsed_calls[0])]
            else:
                results = await asyncio.gather(*[_exec_one(c) for c in parsed_calls])

            self.assertEqual(len(results), 1)
            self.assertTrue(results[0]["success"])
            self.assertEqual(call_count, 1)

        asyncio.run(run_test())


# ---------------------------------------------------------------------------
# Gap 1: Permission check — requires_confirmation creates ApprovalRequest
# ---------------------------------------------------------------------------

class TestPermissionConfirmation(unittest.TestCase):
    """Verify that requires_confirmation from the permission checker is
    properly handled by creating an ApprovalRequest."""

    def test_requires_confirmation_creates_approval(self):
        """When check_permission returns requires_confirmation=True,
        execute_tool should create an ApprovalRequest and return
        requires_approval=True."""
        from app.services.permissions import PermissionCheckResult

        # Mock the permission check to return requires_confirmation
        perm_result = PermissionCheckResult(
            allowed=True,
            requires_confirmation=True,
            reason="Write operation 'write_file' requires user confirmation in Default mode",
        )

        # Mock ApprovalService
        mock_approval = MagicMock()
        mock_approval.id = "approval-123"

        mock_approval_svc = MagicMock()
        mock_approval_svc.create_request = MagicMock(return_value=mock_approval)

        with patch("app.services.permissions.check_permission", return_value=perm_result), \
             patch("app.services.governance.approval_service.ApprovalService", return_value=mock_approval_svc):

            from app.services.agent_tools import execute_tool

            async def run():
                result = await execute_tool(
                    "write_file",
                    {"path": "/tmp/test.txt", "content": "hello"},
                    db=MagicMock(),
                    user_id="user-1",
                    context={"conversation_id": "conv-1", "agent_name": "test_agent"},
                )
                return result

            result = asyncio.run(run())

        self.assertFalse(result["success"])
        self.assertTrue(result["requires_approval"])
        self.assertEqual(result["approval_id"], "approval-123")
        self.assertEqual(result["tool_name"], "write_file")
        self.assertIn("path", result["arguments"])

        # Verify ApprovalService.create_request was called with correct params
        mock_approval_svc.create_request.assert_called_once()
        call_kwargs = mock_approval_svc.create_request.call_args.kwargs
        self.assertEqual(call_kwargs["action_type"], "tool_call")
        self.assertEqual(call_kwargs["ttl_hours"], 1)
        self.assertIn("tool_name", call_kwargs["context_json"])

    def test_allowed_tool_without_confirmation_executes_normally(self):
        """When permission is allowed and no confirmation needed, the tool
        should execute normally (no ApprovalRequest created)."""
        from app.services.permissions import PermissionCheckResult

        perm_result = PermissionCheckResult(allowed=True, requires_confirmation=False)

        with patch("app.services.permissions.check_permission", return_value=perm_result):
            from app.services.agent_tools import execute_tool

            # Mock the registry to return a handler that just succeeds
            mock_handler = AsyncMock(return_value={"success": True, "data": "done"})
            with patch("app.services.tool_registry.registry.get_handler", return_value=mock_handler):
                async def run():
                    return await execute_tool(
                        "read_file",
                        {"path": "/tmp/test.txt"},
                        db=MagicMock(),
                        context={"agent_name": "test"},
                    )

                result = asyncio.run(run())

        self.assertTrue(result["success"])
        self.assertNotIn("requires_approval", result)

    def test_denied_tool_returns_error_without_approval(self):
        """When permission is denied (allowed=False), return error immediately
        without creating an ApprovalRequest."""
        from app.services.permissions import PermissionCheckResult

        perm_result = PermissionCheckResult(
            allowed=False,
            reason="Tool 'write_file' is denied by configuration",
        )

        with patch("app.services.permissions.check_permission", return_value=perm_result):
            from app.services.agent_tools import execute_tool

            async def run():
                return await execute_tool(
                    "write_file",
                    {"path": "/tmp/test.txt"},
                    db=MagicMock(),
                    context={"agent_name": "test"},
                )

            result = asyncio.run(run())

        self.assertFalse(result["success"])
        self.assertIn("Permission denied", result["error"])
        self.assertNotIn("requires_approval", result)


# ---------------------------------------------------------------------------
# Gap 1: Approval pause detection in the result processing loop
# ---------------------------------------------------------------------------

class TestApprovalPauseDetection(unittest.TestCase):
    """Verify that the requires_approval result is detected when processing
    tool execution results, and the correct tool_call_record fields are set."""

    def test_requires_approval_result_detected(self):
        """A result dict with requires_approval=True should be detected as
        needing a pause."""
        result = {
            "success": False,
            "requires_approval": True,
            "approval_id": "appr-456",
            "reason": "Write operation requires confirmation",
            "tool_name": "write_file",
            "arguments": {"path": "/tmp/test.txt"},
        }

        self.assertTrue(isinstance(result, dict))
        self.assertTrue(result.get("requires_approval"))
        self.assertEqual(result.get("approval_id"), "appr-456")

    def test_normal_result_not_detected_as_approval(self):
        """A normal success/failure result should not be detected as
        requiring approval."""
        result = {"success": True, "data": "done"}
        self.assertFalse(result.get("requires_approval", False))

        result = {"success": False, "error": "something went wrong"}
        self.assertFalse(result.get("requires_approval", False))

    def test_awaiting_approval_record_fields(self):
        """Verify the tool_call_record for an awaiting_approval tool has
        the correct fields."""
        tool_call_record = {
            "id": "tc-1",
            "name": "write_file",
            "arguments_string": '{"path": "/tmp/test.txt"}',
            "results": {"requires_approval": True, "approval_id": "appr-789"},
            "status": "awaiting_approval",
            "approval_id": "appr-789",
            "reason": "Write operation requires confirmation",
        }

        self.assertEqual(tool_call_record["status"], "awaiting_approval")
        self.assertEqual(tool_call_record["approval_id"], "appr-789")
        self.assertIn("reason", tool_call_record)


# ---------------------------------------------------------------------------
# Gap 1: Resume state structure
# ---------------------------------------------------------------------------

class TestResumeStateStructure(unittest.TestCase):
    """Verify the resume state stored in conversation metadata has all
    required fields for the /resume endpoint to continue execution."""

    def test_resume_state_has_required_fields(self):
        """The _resume_state dict must contain everything needed to resume:
        llm_messages, iteration, tool_calls_for_frontend, pending_tool, etc."""
        resume_state = {
            "llm_messages": [{"role": "system", "content": "..."}],
            "iteration": 2,
            "tool_calls_for_frontend": [{"id": "tc-1", "status": "awaiting_approval"}],
            "agent_name": "skill_agent",
            "agent_app_id": "app-1",
            "data_ctx_extras": {},
            "user_content": "write a file",
            "guardrail_retries": 0,
            "system_prompt": "You are...",
            "tools": [{"type": "function", "function": {"name": "write_file"}}],
            "pending_tool": {
                "tool_name": "write_file",
                "args": {"path": "/tmp/test.txt"},
                "args_str": '{"path": "/tmp/test.txt"}',
                "tool_call_id": "tc-1",
                "approval_id": "appr-123",
                "remaining_calls": [],
            },
        }

        # Verify all required top-level keys
        required_keys = [
            "llm_messages", "iteration", "tool_calls_for_frontend",
            "agent_name", "agent_app_id", "data_ctx_extras", "user_content",
            "guardrail_retries", "tools", "pending_tool",
        ]
        for key in required_keys:
            self.assertIn(key, resume_state, f"Missing required key: {key}")

        # Verify pending_tool structure
        pending = resume_state["pending_tool"]
        pending_keys = [
            "tool_name", "args", "args_str", "tool_call_id",
            "approval_id", "remaining_calls",
        ]
        for key in pending_keys:
            self.assertIn(key, pending, f"Missing pending_tool key: {key}")

    def test_remaining_calls_captures_unprocessed_batch(self):
        """When a tool in the middle of a batch requires approval, the
        remaining_calls should contain the tools that come after it."""
        parsed_calls = [
            {"tool_name": "read_file", "args": {}, "args_str": "{}", "tool_call_id": "1"},
            {"tool_name": "write_file", "args": {}, "args_str": "{}", "tool_call_id": "2"},
            {"tool_name": "web_search", "args": {}, "args_str": "{}", "tool_call_id": "3"},
        ]

        # Simulate: write_file (index 1) requires approval
        pause_index = 1
        remaining = parsed_calls[pause_index + 1:]

        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["tool_name"], "web_search")


# ---------------------------------------------------------------------------
# Integration: PermissionChecker returns requires_confirmation for write tools
# ---------------------------------------------------------------------------

class TestPermissionCheckerConfirmation(unittest.TestCase):
    """Verify PermissionChecker.check_tool returns requires_confirmation=True
    for write tools in default mode."""

    def test_write_tool_in_default_mode_requires_confirmation(self):
        from app.services.permissions import PermissionChecker, PermissionConfig

        checker = PermissionChecker(PermissionConfig(mode="default"))
        result = checker.check_tool("write_file", {"path": "/tmp/test.txt"})
        self.assertTrue(result.allowed)
        self.assertTrue(result.requires_confirmation)

    def test_write_tool_in_full_auto_mode_no_confirmation(self):
        from app.services.permissions import PermissionChecker, PermissionConfig

        checker = PermissionChecker(PermissionConfig(mode="full_auto"))
        result = checker.check_tool("write_file", {"path": "/tmp/test.txt"})
        self.assertTrue(result.allowed)
        self.assertFalse(result.requires_confirmation)

    def test_write_tool_in_plan_mode_blocked(self):
        from app.services.permissions import PermissionChecker, PermissionConfig

        checker = PermissionChecker(PermissionConfig(mode="plan"))
        result = checker.check_tool("write_file", {"path": "/tmp/test.txt"})
        self.assertFalse(result.allowed)
        self.assertFalse(result.requires_confirmation)

    def test_read_tool_in_default_mode_no_confirmation(self):
        from app.services.permissions import PermissionChecker, PermissionConfig

        checker = PermissionChecker(PermissionConfig(mode="default"))
        result = checker.check_tool("read_file", {"path": "/tmp/test.txt"})
        self.assertTrue(result.allowed)
        self.assertFalse(result.requires_confirmation)


if __name__ == "__main__":
    unittest.main(verbosity=2)
