"""Tests that EDIA sub-agent tools inherit the project LLM endpoint from the
tool context (``context["endpoint"]``) and pass it to every
``call_llm_with_reliability`` call site.

Covers all 3 call sites:
  1. Main sub-agent loop (``_run_sub_agent``)
  2. Empty-answer synthesis turn (``_run_sub_agent``)
  3. Harness bridge ``_harness_llm`` (``_run_sub_agent_via_harness``)

Regression: before the fix these tools always used the legacy global provider,
so a sub-agent inside a project ran on the wrong model.
"""
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, AsyncMock, patch

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

import pytest

from app.services.llm_router import LLMEndpoint

PROJECT_ENDPOINT = LLMEndpoint(
    base_url="https://project-llm.example/v1",
    api_key="sk-project",
    model_id="project-llm",
    is_private=True,
    bypass_hallucination_guardrail=False,
)

_AGENT_DEF = SimpleNamespace(
    system_prompt="You are a test agent.",
    tools=[],
)


def _fake_llm_response(content: str, tool_calls=None):
    return {"content": content, "tool_calls": tool_calls or [], "reasoning": ""}


def _run_imports():
    from app.services.tool_handlers import edia_delegation_tools as edt

    return edt


@pytest.mark.asyncio
@patch("app.services.agent_definitions.get_agent_definition", return_value=_AGENT_DEF)
@patch("app.services.tool_handlers.edia_delegation_tools.call_llm_with_reliability", new_callable=AsyncMock)
async def test_main_loop_inherits_project_endpoint(mock_llm, _mock_def):
    """Call site 1: the main loop must receive the project endpoint."""
    edt = _run_imports()
    mock_llm.return_value = _fake_llm_response("final answer")

    result = await edt._run_sub_agent(
        "perception_agent",
        "What is the market doing?",
        db=MagicMock(),
        user_id="user-1",
        context={"endpoint": PROJECT_ENDPOINT, "bound_kb_ids": []},
    )

    assert result["success"] is True
    assert result["answer"] == "final answer"
    mock_llm.assert_awaited_once()
    assert mock_llm.call_args.kwargs["endpoint"] is PROJECT_ENDPOINT


@pytest.mark.asyncio
@patch("app.services.agent_definitions.get_agent_definition", return_value=_AGENT_DEF)
@patch("app.services.tool_handlers.edia_delegation_tools.call_llm_with_reliability", new_callable=AsyncMock)
async def test_synthesis_turn_inherits_project_endpoint(mock_llm, _mock_def):
    """Call site 2: the forced synthesis turn must also receive the endpoint."""
    edt = _run_imports()
    mock_llm.side_effect = [
        _fake_llm_response(""),  # empty content, no tool calls → force synthesis
        _fake_llm_response("synthesized prose"),
    ]

    result = await edt._run_sub_agent(
        "perception_agent",
        "question",
        db=MagicMock(),
        user_id="user-1",
        context={"endpoint": PROJECT_ENDPOINT},
    )

    assert result["answer"] == "synthesized prose"
    assert mock_llm.await_count == 2
    for call in mock_llm.await_args_list:
        assert call.kwargs["endpoint"] is PROJECT_ENDPOINT


@pytest.mark.asyncio
@patch("app.services.agent_definitions.get_agent_definition", return_value=_AGENT_DEF)
@patch("app.services.tool_handlers.edia_delegation_tools.call_llm_with_reliability", new_callable=AsyncMock)
async def test_no_endpoint_in_context_uses_none(mock_llm, _mock_def):
    """When context has no endpoint, the call falls back to legacy (None)."""
    edt = _run_imports()
    mock_llm.return_value = _fake_llm_response("standalone answer")

    await edt._run_sub_agent(
        "perception_agent",
        "question",
        db=MagicMock(),
        user_id="user-1",
        context={"bound_kb_ids": []},
    )

    mock_llm.assert_awaited_once()
    assert mock_llm.call_args.kwargs["endpoint"] is None


@pytest.mark.asyncio
@patch("app.services.agent_definitions.get_agent_definition", return_value=_AGENT_DEF)
@patch("app.services.tool_handlers.edia_delegation_tools.call_llm_with_reliability", new_callable=AsyncMock)
async def test_harness_bridge_inherits_project_endpoint(mock_llm, _mock_def):
    """Call site 3: the harness bridge closure must forward the endpoint."""
    edt = _run_imports()

    class _FakeOrch:
        def __init__(self, **kwargs):
            self.llm_fn = kwargs["llm_fn"]
            self.result = None

        async def run(self):
            self.result = await self.llm_fn(
                [{"role": "user", "content": "hi"}], [], temperature=0.3
            )
            return SimpleNamespace(success=True, answer=self.result["content"], iterations=1)

    mock_llm.return_value = _fake_llm_response("harness answer")

    with patch(
        "app.services.harness.orchestrator.AgentRunOrchestrator",
        _FakeOrch,
    ):
        result = await edt._run_sub_agent_via_harness(
            agent_name="perception_agent",
            question="question",
            system_prompt="prompt",
            allowed_tools=[],
            tool_schemas=[],
            denied_tools=set(),
            db=MagicMock(),
            user_id="user-1",
            context={"endpoint": PROJECT_ENDPOINT},
            max_iterations=2,
        )

    assert result["success"] is True
    mock_llm.assert_awaited_once()
    assert mock_llm.call_args.kwargs["endpoint"] is PROJECT_ENDPOINT


@pytest.mark.asyncio
@patch("app.services.agent_definitions.get_agent_definition", return_value=_AGENT_DEF)
@patch("app.services.tool_handlers.edia_delegation_tools.call_llm_with_reliability", new_callable=AsyncMock)
async def test_entry_point_forwards_context(mock_llm, _mock_def):
    """Tool entry points (e.g. _ask_perception) forward the context verbatim,
    so the endpoint set by the main loop reaches the sub-agent loop."""
    edt = _run_imports()
    mock_llm.return_value = _fake_llm_response("ok")

    result = await edt._ask_perception(
        {"question": "market update"},
        db=MagicMock(),
        user_id="user-1",
        context={"endpoint": PROJECT_ENDPOINT},
    )

    assert result["success"] is True
    mock_llm.assert_awaited_once()
    assert mock_llm.call_args.kwargs["endpoint"] is PROJECT_ENDPOINT
