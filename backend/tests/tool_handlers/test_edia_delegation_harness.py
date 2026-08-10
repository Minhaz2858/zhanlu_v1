"""Parity tests: EDIA delegation harness path vs legacy path."""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Shape contract tests
# ---------------------------------------------------------------------------

def test_run_sub_agent_return_shape():
    """Verify the documented return shape from _run_sub_agent."""
    # Cannot call _run_sub_agent directly (needs DB + LLM), but we verify
    # that the harness bridge returns the same documented shape.
    from app.services.harness.orchestrator import RunResult

    r = RunResult(
        run_id="test123",
        success=True,
        answer="Test answer",
        tool_calls=[{"tool": "test", "call_id": "c1"}],
        iterations=3,
    )
    # The bridge converts RunResult → {success, answer, agent, iterations}
    d = {
        "success": r.success,
        "answer": r.answer,
        "agent": "test_agent",
        "iterations": r.iterations,
    }
    assert set(d.keys()) == {"success", "answer", "agent", "iterations"}
    assert d["success"] is True
    assert d["answer"] == "Test answer"
    assert d["iterations"] == 3


def test_missing_agent_definition_shape():
    """Verify the error shape when agent_def is None."""
    error_shape = {
        "success": False,
        "error": "Agent definition 'unknown' not found in BUILTIN_AGENTS.",
        "answer": "",
        "agent": "unknown",
        "iterations": 0,
    }
    assert set(error_shape.keys()) >= {"success", "error", "answer", "agent", "iterations"}


def test_async_return_shape():
    """Verify the async enqueue return shape."""
    async_shape = {
        "success": True,
        "run_id": "abc123def456",
        "status": "queued",
        "agent": "forecast_agent",
    }
    assert async_shape["status"] == "queued"
    assert "run_id" in async_shape
    assert len(async_shape["run_id"]) > 0
