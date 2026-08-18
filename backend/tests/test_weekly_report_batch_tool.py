"""Tests for the ``ask_perception_intelligence_diagnosis`` batch tool.

Verifies:

* **Dispatch** — the handler fans a single ``question`` out to
  ``perception_agent``, ``intelligence_agent``, and ``diagnosis_agent``.
* **Parallelism** — the three sub-agent calls are launched concurrently
  (all three coroutines start before the slowest completes).
* **Combined result** — the handler returns a dict with ``perception``,
  ``intelligence``, ``diagnosis`` keys plus a combined ``answer``.
* **Registry** — the tool is registered and discoverable.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))


@pytest.fixture
def sub_agent_results():
    """Three distinguishable sub-agent results."""
    return {
        "perception": {
            "success": True,
            "answer": "Perception: prices flat this week.",
            "agent": "perception_agent",
            "iterations": 2,
        },
        "intelligence": {
            "success": True,
            "answer": "Intelligence: naphtha rally news.",
            "agent": "intelligence_agent",
            "iterations": 1,
        },
        "diagnosis": {
            "success": True,
            "answer": "Diagnosis: supply tightness driver.",
            "agent": "diagnosis_agent",
            "iterations": 3,
        },
    }


@pytest.mark.asyncio
async def test_batch_tool_dispatches_to_three_sub_agents(sub_agent_results):
    """A single question fans out to perception, intelligence, diagnosis."""
    from app.services.tool_handlers import edia_delegation_tools as edt

    async def _fake_run(agent_name, question, db, user_id, context=None, max_iterations=5):
        key = {
            "perception_agent": "perception",
            "intelligence_agent": "intelligence",
            "diagnosis_agent": "diagnosis",
        }[agent_name]
        return dict(sub_agent_results[key])

    with patch.object(edt, "_run_sub_agent", new=_fake_run):
        result = await edt._ask_perception_intelligence_diagnosis(
            {"question": "weekly market report for C5 resin"},
            db=None,
            user_id="u1",
            context={},
        )

    assert result["success"] is True
    assert result["perception"]["answer"].startswith("Perception:")
    assert result["intelligence"]["answer"].startswith("Intelligence:")
    assert result["diagnosis"]["answer"].startswith("Diagnosis:")
    assert "Perception:" in result["answer"]
    assert "Intelligence:" in result["answer"]
    assert "Diagnosis:" in result["answer"]
    assert result["agent"] == "perception+intelligence+diagnosis"


@pytest.mark.asyncio
async def test_batch_tool_runs_sub_agents_concurrently():
    """All three sub-agents must start before any completes (parallel)."""
    from app.services.tool_handlers import edia_delegation_tools as edt

    started = 0
    barrier = asyncio.Event()

    async def _slow_run(agent_name, question, db, user_id, context=None, max_iterations=5):
        nonlocal started
        started += 1
        barrier.set()  # all callers reach here before anyone returns
        await asyncio.sleep(0.05)
        return {
            "success": True,
            "answer": f"{agent_name} answer",
            "agent": agent_name,
            "iterations": 1,
        }

    with patch.object(edt, "_run_sub_agent", new=_slow_run):
        task = asyncio.create_task(
            edt._ask_perception_intelligence_diagnosis(
                {"question": "test"},
                db=None,
                user_id="u1",
                context={},
            )
        )
        # Give the gather time to launch all three coroutines.
        await asyncio.wait_for(barrier.wait(), timeout=2.0)
        await task

    assert started == 3, f"expected 3 concurrent starts, got {started}"


@pytest.mark.asyncio
async def test_batch_tool_requires_question():
    """Missing question returns an error dict (no sub-agent calls)."""
    from app.services.tool_handlers import edia_delegation_tools as edt

    with patch.object(edt, "_run_sub_agent", new=AsyncMock()) as m:
        result = await edt._ask_perception_intelligence_diagnosis(
            {"question": "   "},
            db=None,
            user_id="u1",
            context={},
        )
    assert result["success"] is False
    assert "question" in result["error"]
    m.assert_not_called()


@pytest.mark.asyncio
async def test_batch_tool_survives_sub_agent_failure():
    """One failing sub-agent must not kill the other results."""
    from app.services.tool_handlers import edia_delegation_tools as edt

    async def _mixed_run(agent_name, question, db, user_id, context=None, max_iterations=5):
        if agent_name == "diagnosis_agent":
            return {
                "success": False,
                "error": "boom",
                "answer": "",
                "agent": agent_name,
                "iterations": 0,
            }
        return {
            "success": True,
            "answer": f"{agent_name} ok",
            "agent": agent_name,
            "iterations": 1,
        }

    with patch.object(edt, "_run_sub_agent", new=_mixed_run):
        result = await edt._ask_perception_intelligence_diagnosis(
            {"question": "test"},
            db=None,
            user_id="u1",
            context={},
        )

    assert result["success"] is True
    assert result["perception"]["success"] is True
    assert result["intelligence"]["success"] is True
    assert result["diagnosis"]["success"] is False
    assert "diagnosis failed" in result["answer"]


def test_batch_tool_registered_in_registry():
    """The batch tool must be discoverable through the shared registry."""
    from app.services.tool_registry import registry

    entry = registry.get_entry("ask_perception_intelligence_diagnosis")
    assert entry is not None, "ask_perception_intelligence_diagnosis not registered"
    assert entry.schema is not None
    fn = entry.schema["function"]
    assert fn["name"] == "ask_perception_intelligence_diagnosis"
    assert "question" in fn["parameters"]["properties"]
    assert "parallel" in fn["description"].lower() or "batch" in fn["description"].lower()
    assert entry.handler is not None


def test_batch_tool_added_to_denied_recursive():
    """Sub-agents must not recurse into the batch tool."""
    from app.services.tool_handlers import edia_delegation_tools as edt

    assert "ask_perception_intelligence_diagnosis" in edt._DENIED_RECURSIVE


# ---------------------------------------------------------------------------
# ask_forecast_pricing — Step 3 batch tool (forecast + pricing in parallel)
# ---------------------------------------------------------------------------


@pytest.fixture
def forecast_pricing_sub_agent_results():
    """Two distinguishable sub-agent results."""
    return {
        "forecast": {
            "success": True,
            "answer": "Forecast: C5 resin volumes rising next 4 weeks.",
            "agent": "forecast_agent",
            "iterations": 3,
        },
        "pricing": {
            "success": True,
            "answer": "Pricing: recommend 8600 with a 5% range.",
            "agent": "pricing_agent",
            "iterations": 2,
        },
    }


@pytest.mark.asyncio
async def test_forecast_pricing_dispatches_to_two_sub_agents(forecast_pricing_sub_agent_results):
    """A single question fans out to forecast_agent and pricing_agent."""
    from app.services.tool_handlers import edia_delegation_tools as edt

    async def _fake_run(agent_name, question, db, user_id, context=None, max_iterations=5):
        key = {
            "forecast_agent": "forecast",
            "pricing_agent": "pricing",
        }[agent_name]
        return dict(forecast_pricing_sub_agent_results[key])

    with patch.object(edt, "_run_sub_agent", new=_fake_run):
        result = await edt._ask_forecast_pricing(
            {"question": "forecast and price C5 resin for next week"},
            db=None,
            user_id="u1",
            context={},
        )

    assert result["success"] is True
    assert result["forecast"]["answer"].startswith("Forecast:")
    assert result["pricing"]["answer"].startswith("Pricing:")
    assert "Forecast:" in result["answer"]
    assert "Pricing:" in result["answer"]
    assert result["agent"] == "forecast+pricing"


@pytest.mark.asyncio
async def test_forecast_pricing_runs_sub_agents_concurrently():
    """Both sub-agents must start before any completes (parallel)."""
    from app.services.tool_handlers import edia_delegation_tools as edt

    started = 0
    barrier = asyncio.Event()

    async def _slow_run(agent_name, question, db, user_id, context=None, max_iterations=5):
        nonlocal started
        started += 1
        barrier.set()
        await asyncio.sleep(0.05)
        return {
            "success": True,
            "answer": f"{agent_name} answer",
            "agent": agent_name,
            "iterations": 1,
        }

    with patch.object(edt, "_run_sub_agent", new=_slow_run):
        task = asyncio.create_task(
            edt._ask_forecast_pricing(
                {"question": "test"},
                db=None,
                user_id="u1",
                context={},
            )
        )
        await asyncio.wait_for(barrier.wait(), timeout=2.0)
        await task

    assert started == 2, f"expected 2 concurrent starts, got {started}"


@pytest.mark.asyncio
async def test_forecast_pricing_requires_question():
    """Missing question returns an error dict (no sub-agent calls)."""
    from app.services.tool_handlers import edia_delegation_tools as edt

    with patch.object(edt, "_run_sub_agent", new=AsyncMock()) as m:
        result = await edt._ask_forecast_pricing(
            {"question": "   "},
            db=None,
            user_id="u1",
            context={},
        )
    assert result["success"] is False
    assert "question" in result["error"]
    m.assert_not_called()


@pytest.mark.asyncio
async def test_forecast_pricing_survives_sub_agent_failure():
    """One failing sub-agent must not kill the other result."""
    from app.services.tool_handlers import edia_delegation_tools as edt

    async def _mixed_run(agent_name, question, db, user_id, context=None, max_iterations=5):
        if agent_name == "pricing_agent":
            return {
                "success": False,
                "error": "boom",
                "answer": "",
                "agent": agent_name,
                "iterations": 0,
            }
        return {
            "success": True,
            "answer": f"{agent_name} ok",
            "agent": agent_name,
            "iterations": 1,
        }

    with patch.object(edt, "_run_sub_agent", new=_mixed_run):
        result = await edt._ask_forecast_pricing(
            {"question": "test"},
            db=None,
            user_id="u1",
            context={},
        )

    assert result["success"] is True
    assert result["forecast"]["success"] is True
    assert result["pricing"]["success"] is False
    assert "pricing failed" in result["answer"]


def test_forecast_pricing_registered_in_registry():
    """The batch tool must be discoverable through the shared registry."""
    from app.services.tool_registry import registry

    entry = registry.get_entry("ask_forecast_pricing")
    assert entry is not None, "ask_forecast_pricing not registered"
    assert entry.schema is not None
    fn = entry.schema["function"]
    assert fn["name"] == "ask_forecast_pricing"
    assert "question" in fn["parameters"]["properties"]
    assert "parallel" in fn["description"].lower() or "batch" in fn["description"].lower()
    assert entry.handler is not None


def test_forecast_pricing_added_to_denied_recursive():
    """Sub-agents must not recurse into the batch tool."""
    from app.services.tool_handlers import edia_delegation_tools as edt

    assert "ask_forecast_pricing" in edt._DENIED_RECURSIVE
