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


# ── Scope-aware planner system prompt (2026-09-03) ────────────────────
# Pre-fix the planner received ONLY the user question — it never knew which
# tools existed or what scope meant, so deepseek defaulted every step to
# web_search and internal questions never reached the bound warehouse.

@pytest.mark.asyncio
async def test_planner_system_prompt_names_tools_for_both_scope():
    """scope=both: system prompt must name BOTH tool families and mandate
    hybrid coverage (internal-data steps for product/sales questions)."""
    with patch(
        "app.services.deep_research.llm_backend.call_llm",
        new=AsyncMock(return_value={"data": {"steps": []}}),
    ) as mock_llm:
        await build_planner(scope="both")([{"role": "user", "content": "C5C9 sales?"}])
    sent = mock_llm.await_args.kwargs["messages"]
    assert sent[0]["role"] == "system"
    sys_text = sent[0]["content"]
    assert "ask_data_agent" in sys_text
    assert "web_search" in sys_text
    assert "hybrid" in sys_text.lower() or "both" in sys_text.lower()
    assert "C5C9" in sys_text  # example internal product code in the prompt
    assert sent[1] == {"role": "user", "content": "C5C9 sales?"}


@pytest.mark.asyncio
async def test_planner_system_prompt_web_scope_excludes_internal_tools():
    with patch(
        "app.services.deep_research.llm_backend.call_llm",
        new=AsyncMock(return_value={"data": {"steps": []}}),
    ) as mock_llm:
        await build_planner(scope="web")([{"role": "user", "content": "market?"}])
    sys_text = mock_llm.await_args.kwargs["messages"][0]["content"]
    assert "WEB scope" in sys_text
    assert "Do NOT call web tools" not in sys_text


@pytest.mark.asyncio
async def test_planner_system_prompt_internal_scope_forbids_web():
    with patch(
        "app.services.deep_research.llm_backend.call_llm",
        new=AsyncMock(return_value={"data": {"steps": []}}),
    ) as mock_llm:
        await build_planner(scope="internal")([{"role": "user", "content": "sales?"}])
    sys_text = mock_llm.await_args.kwargs["messages"][0]["content"]
    assert "INTERNAL DATA scope" in sys_text
    assert "Do NOT call web tools" in sys_text


@pytest.mark.asyncio
async def test_planner_default_scope_is_both():
    """Backward-compat: build_planner() with no scope still sends a system
    prompt (defaults to both) rather than the old prompt-less message."""
    with patch(
        "app.services.deep_research.llm_backend.call_llm",
        new=AsyncMock(return_value={"data": {"steps": []}}),
    ) as mock_llm:
        await build_planner()([{"role": "user", "content": "Q"}])
    sent = mock_llm.await_args.kwargs["messages"]
    assert sent[0]["role"] == "system"
    assert "Tools available" in sent[0]["content"]
