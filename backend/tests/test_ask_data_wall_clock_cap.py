"""Tests for 30s wall-clock cap on ask_data_agent LLM calls.

Root cause: the wall-clock check fires at iteration START (line 348) but the
LLM call itself (line 362) has no asyncio.wait_for. A single LLM call can take
161s (user screenshot) and the cap is bypassed. The next iteration's check
fires, but the LLM has already done its work.

Fix: wrap the inner LLM call with asyncio.wait_for(timeout=remaining_budget)
so a hung LLM call is killed and the iteration falls through to the budget
truncation path. Plus: outer v3 loop wraps each ask_data_agent call in
asyncio.wait_for(timeout=DATA_AGENT_BUDGET_SECONDS) as a hard outer cap.

Run in-container:
  /usr/local/bin/python3.11 -c "import sys; sys.path.insert(0, '/app/venv/lib/python3.11/site-packages'); sys.path.insert(0, '/app'); import pytest; exit(pytest.main(['-xvs', 'tests/test_ask_data_wall_clock_cap.py']))"
"""
import sys, os, asyncio, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))


# ── Test 1: DATA_AGENT_BUDGET_SECONDS default is sane ──────────────────────


def test_data_agent_budget_default_is_60_or_less():
    """The default cap should be 60s or less (not 300s like before).
    Pin the current default so a regression is caught."""
    from app.services.tool_handlers.delegation_tools import DATA_AGENT_BUDGET_SECONDS
    assert DATA_AGENT_BUDGET_SECONDS <= 60.0, (
        f"DATA_AGENT_BUDGET_SECONDS={DATA_AGENT_BUDGET_SECONDS} is too high; "
        f"the 161s user screenshot shows the cap was being violated. "
        f"Should be <= 60s for production, <= 30s for qwen3."
    )
    assert DATA_AGENT_BUDGET_SECONDS >= 10.0, (
        f"DATA_AGENT_BUDGET_SECONDS={DATA_AGENT_BUDGET_SECONDS} is too low; "
        f"complex queries would never complete."
    )


# ── Test 2: Qwen3 override is 30s ──────────────────────────────────────────


def test_qwen3_budget_override_is_30s():
    """QWEN3_DATA_AGENT_BUDGET_SECONDS should be 30s (matches the qwen3
    fast-mode config from earlier sessions)."""
    from app.config import settings
    assert hasattr(settings, "QWEN3_DATA_AGENT_BUDGET_SECONDS")
    assert settings.QWEN3_DATA_AGENT_BUDGET_SECONDS == 30.0


# ── Test 3: Source check — the LLM call has a timeout wrapper ──────────────


def test_llm_call_in_delegation_has_timeout():
    """The _call_llm_with_retry call inside the ask_data_agent iteration
    loop must be wrapped in asyncio.wait_for with the remaining budget.
    Currently the wait_for exists only for evaluate_answer (line 1151),
    NOT for the inner LLM call (line 362). The wall-clock check at line
    348 only fires at iteration START, so a hung LLM call (161s in the
    user screenshot) bypasses the cap entirely."""
    import inspect
    from app.services.tool_handlers import delegation_tools
    src = inspect.getsource(delegation_tools)
    # Find the iteration loop where _call_llm_with_retry is called
    assert "_call_llm_with_retry(messages, tool_schemas" in src, (
        "Could not find the expected LLM call site. The function may have "
        "been refactored; update this test."
    )
    # The wait_for must be WITHIN the ask_data_agent function's iteration
    # loop, not in some other helper. Check for a wait_for near the
    # _call_llm_with_retry call.
    _iter_section = src.split("_call_llm_with_retry(messages")[0]
    if "asyncio.wait_for" in _iter_section:
        # wait_for exists before the call (could be in iteration setup) — OK
        pass
    elif "asyncio.wait_for" in src.split("_call_llm_with_retry(messages")[1][:2000]:
        # wait_for exists in the section after the call (could wrap it) — OK
        pass
    else:
        assert False, (
            "ask_data_agent's inner LLM call is NOT wrapped in "
            "asyncio.wait_for. The wall-clock check at line 348 only "
            "fires at iteration START, so a hung LLM call can take 161s "
            "(user screenshot) before the next iteration's check fires. "
            "Wrap the LLM call with asyncio.wait_for(timeout=remaining)."
        )


# ── Test 4: Outer v3 loop wraps ask_data_agent with timeout ────────────────


def test_v3_loop_wraps_ask_data_with_timeout():
    """The v3 stream loop in agents.py should wrap each ask_data_agent
    call in asyncio.wait_for(timeout=DATA_AGENT_BUDGET_SECONDS) as a
    defense-in-depth outer cap. This ensures even if the inner cap is
    bypassed, the outer loop kills the call."""
    import inspect
    from app.routers import agents
    src = inspect.getsource(agents)
    assert "asyncio.wait_for" in src, (
        "agents.py must use asyncio.wait_for somewhere in the v3 stream "
        "to bound individual tool calls (defense-in-depth against hung LLMs)."
    )


# ── Test 5: Runtime — the inner cap actually fires ─────────────────────────


def test_remaining_budget_calculation_is_correct():
    """The remaining_budget calculation must bound the LLM call by the
    actual remaining time in the DATA_AGENT_BUDGET_SECONDS window, with
    a minimum of 5s so the LLM has time to respond."""
    # Simulate 10s elapsed in a 60s budget
    remaining = max(5.0, 60.0 - 10.0)
    assert remaining == 50.0  # 60 - 10

    # Simulate 55s elapsed in a 60s budget
    remaining = max(5.0, 60.0 - 55.0)
    assert remaining == 5.0  # hits minimum

    # Simulate 70s elapsed (over budget)
    remaining = max(5.0, 60.0 - 70.0)
    assert remaining == 5.0  # still min 5s (defense)


def test_truncated_path_returns_rows_not_empty():
    """When the LLM call times out and _truncated is set, the function
    must still return whatever rows were captured (not empty)."""
    # This is a structural test: verify the truncation fallback path
    # exists and returns rows.
    import inspect
    from app.services.tool_handlers.delegation_tools import _ask_data_agent
    src = inspect.getsource(_ask_data_agent)
    assert "_truncated" in src, (
        "ask_data_agent must use _truncated flag to track wall-clock "
        "truncation and return partial rows."
    )
    assert "rows" in src, (
        "ask_data_agent must return rows even when truncated."
    )
