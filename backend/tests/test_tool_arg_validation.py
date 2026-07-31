"""SP2-WS-B: upfront tool-arg validation + reliability wrapper integration."""
import asyncio

import pytest

from app.services.reliability import (
    LoopState,
    ReliabilityConfig,
    get_conversation_loop_state,
    set_conversation_loop_state,
    reset_conversation_loop_state,
)


# ── Upfront validation ───────────────────────────────────────────────────

def test_validate_tool_args_passes_valid():
    from app.services.tool_arg_validator import validate_tool_args
    schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }
    assert validate_tool_args({"name": "ok"}, schema) is None


def test_validate_tool_args_rejects_missing_required():
    from app.services.tool_arg_validator import validate_tool_args
    schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }
    err = validate_tool_args({}, schema)
    assert err is not None
    assert "name" in err.lower()


def test_validate_tool_args_rejects_wrong_type():
    from app.services.tool_arg_validator import validate_tool_args
    schema = {
        "type": "object",
        "properties": {"count": {"type": "integer"}},
        "required": ["count"],
    }
    err = validate_tool_args({"count": "not-a-number"}, schema)
    assert err is not None


def test_get_tool_schema_unknown_returns_none():
    from app.services.tool_arg_validator import get_tool_schema
    # A name that definitely isn't registered.
    assert get_tool_schema("__nonexistent_tool_xyz__") is None


# ── LoopState contextvar ─────────────────────────────────────────────────

def test_loop_state_contextvar_default_none():
    assert get_conversation_loop_state() is None


def test_loop_state_contextvar_set_reset():
    ls = LoopState()
    token = set_conversation_loop_state(ls)
    assert get_conversation_loop_state() is ls
    reset_conversation_loop_state(token)
    assert get_conversation_loop_state() is None


# ── _execute_with_reliability integration ────────────────────────────────

@pytest.mark.asyncio
async def test_execute_with_reliability_success_path():
    """A handler returning success should pass through unchanged."""
    from app.services.agent_tools import _execute_with_reliability

    async def handler(args, db, user_id, context=None):
        return {"success": True, "id": "abc", "data": args}

    result = await _execute_with_reliability(
        "noop_tool", {"x": 1}, handler, db=None, user_id=None,
        context=None, use_context=True,
    )
    assert result["success"] is True
    assert result["id"] == "abc"


@pytest.mark.asyncio
async def test_execute_with_reliability_permanent_failure_carries_failure_kind():
    """A handler returning a permanent failure dict must surface failure_kind
    so the outer execute_tool_with_retry doesn't double-retry."""
    from app.services.agent_tools import _execute_with_reliability

    async def handler(args, db, user_id, context=None):
        return {"success": False, "error": "permission denied: no access"}

    result = await _execute_with_reliability(
        "failing_tool", {"x": 1}, handler, db=None, user_id=None,
        context=None, use_context=True,
    )
    assert result["success"] is False
    assert "failure_kind" in result  # prevents outer double-retry


@pytest.mark.asyncio
async def test_execute_with_reliability_graceful_fallback():
    """If run_tool_with_reliability itself blows up, fall back to direct call."""
    from app.services.agent_tools import _execute_with_reliability

    async def handler(args, db, user_id, context=None):
        return {"success": True, "fallback": True}

    # Patch run_tool_with_reliability to raise.
    import unittest.mock as mock
    with mock.patch(
        "app.services.reliability.run_tool_with_reliability",
        side_effect=RuntimeError("infra exploded"),
    ):
        result = await _execute_with_reliability(
            "some_tool", {"x": 1}, handler, db=None, user_id=None,
            context=None, use_context=True,
        )
    assert result["success"] is True
    assert result.get("fallback") is True


@pytest.mark.asyncio
async def test_execute_with_reliability_retries_transient_then_succeeds():
    """A handler that fails transiently then succeeds should be retried."""
    from app.services.agent_tools import _execute_with_reliability
    from app.services.reliability import ReliabilityConfig

    calls = {"n": 0}

    async def handler(args, db, user_id, context=None):
        calls["n"] += 1
        if calls["n"] < 2:
            return {"success": False, "error": "connection timeout"}
        return {"success": True, "attempt": calls["n"]}

    # Patch the config to make retries fast.
    import unittest.mock as mock
    fast_cfg = ReliabilityConfig(max_retries=3, backoff_base_seconds=0.001, backoff_jitter=0, max_reformulations=0)
    with mock.patch(
        "app.services.reliability.ReliabilityConfig.from_settings", return_value=fast_cfg
    ):
        result = await _execute_with_reliability(
            "flaky_tool", {"x": 1}, handler, db=None, user_id=None,
            context=None, use_context=True,
        )
    assert result["success"] is True
    assert calls["n"] >= 2  # retried at least once
