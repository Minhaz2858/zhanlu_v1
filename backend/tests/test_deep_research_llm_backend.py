"""Unit tests for the real deep-research LLM/tool wiring (llm_backend).

``call_llm`` / ``execute_tool`` are mocked — these tests pin the builders'
contracts (planner JSON parsing + fallbacks, tool dispatch args, text
response extraction) without a live model.
"""
import os
import sys
from unittest.mock import AsyncMock, patch

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

import pytest

from app.services.deep_research.llm_backend import (
    build_critic,
    build_planner,
    build_synthesizer,
    build_tool_executor,
)


@pytest.mark.asyncio
async def test_planner_uses_parsed_json_data():
    with patch(
        "app.services.deep_research.llm_backend.call_llm",
        new=AsyncMock(return_value={"data": {"steps": [{"question": "Q", "tools": ["web_search"]}]}}),
    ) as mock_llm:
        planner = build_planner()
        plan = await planner([{"role": "user", "content": "hi"}])
    assert plan["steps"][0]["question"] == "Q"
    mock_llm.assert_awaited_once()


@pytest.mark.asyncio
async def test_planner_falls_back_to_response_json():
    with patch(
        "app.services.deep_research.llm_backend.call_llm",
        new=AsyncMock(return_value={"response": '{"steps": []}'}),
    ):
        plan = await build_planner()([{"role": "user", "content": "hi"}])
    assert plan == {"steps": []}


@pytest.mark.asyncio
async def test_planner_returns_empty_on_llm_error():
    with patch(
        "app.services.deep_research.llm_backend.call_llm",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        plan = await build_planner()([{"role": "user", "content": "hi"}])
    assert plan == {}


@pytest.mark.asyncio
async def test_tool_executor_dispatches_through_agent_tools():
    with patch(
        "app.services.deep_research.llm_backend.execute_tool",
        new=AsyncMock(return_value={"success": True, "rows": [1]}),
    ) as mock_exec:
        executor = build_tool_executor(db="db", user_id="u1", context={"conversation_id": "c1"})
        result = await executor("web_search", {"query": "Q"})
    assert result == {"success": True, "rows": [1]}
    mock_exec.assert_awaited_once_with(
        "web_search", {"query": "Q"}, "db", user_id="u1", context={"conversation_id": "c1"}
    )


@pytest.mark.asyncio
async def test_synthesizer_returns_response_text():
    with patch(
        "app.services.deep_research.llm_backend.call_llm",
        new=AsyncMock(return_value={"response": "Report [source: X]."}),
    ) as mock_llm:
        out = await build_synthesizer()([{"role": "user", "content": "hi"}])
    assert out == "Report [source: X]."
    mock_llm.assert_awaited_once()


@pytest.mark.asyncio
async def test_critic_returns_response_text():
    with patch(
        "app.services.deep_research.llm_backend.call_llm",
        new=AsyncMock(return_value={"response": "Need newer data."}),
    ):
        out = await build_critic()([{"role": "user", "content": "hi"}])
    assert out == "Need newer data."
