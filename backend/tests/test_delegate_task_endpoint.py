"""Tests for LLMEndpoint threading in delegate_task.

Verify that:
1. ``_run_sub_agent`` forwards the endpoint to ``call_llm_with_reliability``.
2. ``_delegate_task`` reads ``context["endpoint"]`` and forwards it.
3. When no endpoint is present, ``None`` is forwarded (global fallback).
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)


class TestRunSubAgentEndpoint(unittest.TestCase):
    """Verify _run_sub_agent forwards the endpoint to the reliability wrapper."""

    def _make_endpoint(self):
        from app.services.llm_router import LLMEndpoint

        return LLMEndpoint(
            base_url="https://api.moonshot.cn/v1",
            api_key="kimi-secret-key",
            model_id="kimi-k2.6",
            provider="moonshot",
        )

    def test_run_sub_agent_forwards_endpoint(self):
        from app.services.tool_handlers import delegate_tool

        endpoint = self._make_endpoint()
        captured = {}

        async def fake_reliability(messages, tools, endpoint=None):
            captured["endpoint"] = endpoint
            # Return a final answer immediately (no tool calls) to end the loop.
            return {"content": "done", "tool_calls": [], "reasoning": ""}

        def fake_system_prompt(name):
            return "system"

        with patch(
            "app.services.agent_prompts.get_system_prompt", side_effect=fake_system_prompt
        ):
            with patch(
                "app.services.agent_prompts._get_all_crud_schemas", return_value={}
            ):
                with patch.object(
                    delegate_tool, "call_llm_with_reliability", side_effect=fake_reliability
                ):
                    with patch.object(delegate_tool.registry, "list_available", return_value=[]):
                        result = asyncio.run(
                            delegate_tool._run_sub_agent(
                                "hello", "general_assistant", None, None, 3, endpoint=endpoint
                            )
                        )

        self.assertTrue(result["success"])
        self.assertIs(captured["endpoint"], endpoint)

    def test_run_sub_agent_endpoint_none_defaults(self):
        from app.services.tool_handlers import delegate_tool

        captured = {}

        async def fake_reliability(messages, tools, endpoint=None):
            captured["endpoint"] = endpoint
            return {"content": "done", "tool_calls": [], "reasoning": ""}

        def fake_system_prompt(name):
            return "system"

        with patch(
            "app.services.agent_prompts.get_system_prompt", side_effect=fake_system_prompt
        ):
            with patch(
                "app.services.agent_prompts._get_all_crud_schemas", return_value={}
            ):
                with patch.object(
                    delegate_tool, "call_llm_with_reliability", side_effect=fake_reliability
                ):
                    with patch.object(delegate_tool.registry, "list_available", return_value=[]):
                        asyncio.run(
                            delegate_tool._run_sub_agent(
                                "hello", "general_assistant", None, None, 3
                            )
                        )

        self.assertIsNone(captured["endpoint"])


class TestDelegateTaskEndpoint(unittest.TestCase):
    """Verify _delegate_task reads context["endpoint"] and forwards it."""

    def test_delegate_task_forwards_context_endpoint(self):
        from app.services.tool_handlers import delegate_tool

        endpoint = MagicMock(name="kimi-endpoint")
        captured = {}

        async def fake_run_sub_agent(task, agent_name, db, user_id, max_iterations, endpoint=None):
            captured["endpoint"] = endpoint
            return {"success": True, "task": task, "response": "ok"}

        with patch.object(delegate_tool, "_run_sub_agent", side_effect=fake_run_sub_agent):
            result = asyncio.run(
                delegate_tool._delegate_task(
                    {"task": "hello"},
                    None,
                    None,
                    context={"endpoint": endpoint},
                )
            )

        self.assertTrue(result["success"])
        self.assertIs(captured["endpoint"], endpoint)

    def test_delegate_task_no_context_endpoint_defaults(self):
        from app.services.tool_handlers import delegate_tool

        captured = {}

        async def fake_run_sub_agent(task, agent_name, db, user_id, max_iterations, endpoint=None):
            captured["endpoint"] = endpoint
            return {"success": True, "task": task, "response": "ok"}

        with patch.object(delegate_tool, "_run_sub_agent", side_effect=fake_run_sub_agent):
            asyncio.run(
                delegate_tool._delegate_task(
                    {"task": "hello"},
                    None,
                    None,
                    context=None,
                )
            )

        self.assertIsNone(captured["endpoint"])


if __name__ == "__main__":
    unittest.main()
