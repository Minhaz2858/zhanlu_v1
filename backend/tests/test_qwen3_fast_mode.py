"""Tests for qwen3.6-27b per-model fast-mode overrides.

Run in-container:
  /usr/local/bin/python3.11 -c "import sys; sys.path.insert(0, '/app/venv/lib/python3.11/site-packages'); sys.path.insert(0, '/app'); import pytest; exit(pytest.main(['-xvs', 'tests/test_qwen3_fast_mode.py']))"
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from types import SimpleNamespace
from app.routers.agents import _is_qwen_local_model


def test_is_qwen_local_model_true_for_qwen3_private():
    endpoint = SimpleNamespace(
        model_id="qwen3.6-27b",
        is_private=True,
        api_key="EMPTY",
    )
    assert _is_qwen_local_model(endpoint) is True


def test_is_qwen_local_model_true_for_qwen3_empty_key():
    endpoint = SimpleNamespace(
        model_id="qwen3.6-27b-awq4",
        is_private=False,
        api_key="",
    )
    assert _is_qwen_local_model(endpoint) is True


def test_is_qwen_local_model_false_for_deepseek():
    endpoint = SimpleNamespace(
        model_id="deepseek-chat",
        is_private=False,
        api_key="sk-real-key",
    )
    assert _is_qwen_local_model(endpoint) is False


def test_is_qwen_local_model_false_for_qwen3_with_real_api_key():
    """qwen3 with a real API key means it's a cloud-hosted qwen, not local vLLM."""
    endpoint = SimpleNamespace(
        model_id="qwen3.6-27b",
        is_private=False,
        api_key="sk-real-cloud-key",
    )
    assert _is_qwen_local_model(endpoint) is False


def test_is_qwen_local_model_false_for_none():
    assert _is_qwen_local_model(None) is False


def test_is_qwen_local_model_false_for_empty_model_id():
    endpoint = SimpleNamespace(model_id="", is_private=True, api_key="")
    assert _is_qwen_local_model(endpoint) is False


# ── Task 6: effective accessor tests ─────────────────────────────────────────


def test_qwen3_max_iterations_is_10_not_40():
    """When endpoint is qwen3 local, effective MAX_TOOL_ITERATIONS is 10."""
    from app.routers.agents import _effective_max_tool_iterations
    endpoint = SimpleNamespace(model_id="qwen3.6-27b", is_private=True, api_key="EMPTY")
    assert _effective_max_tool_iterations(endpoint) == 10


def test_deepseek_max_iterations_is_40():
    """When endpoint is deepseek, MAX_TOOL_ITERATIONS stays 40."""
    from app.routers.agents import _effective_max_tool_iterations, MAX_TOOL_ITERATIONS
    endpoint = SimpleNamespace(model_id="deepseek-chat", is_private=False, api_key="sk-real")
    assert _effective_max_tool_iterations(endpoint) == MAX_TOOL_ITERATIONS


def test_qwen3_goal_contract_disabled():
    """When endpoint is qwen3 local, goal-contract is disabled."""
    from app.routers.agents import _effective_goal_contract_enabled
    endpoint = SimpleNamespace(model_id="qwen3.6-27b", is_private=True, api_key="EMPTY")
    assert _effective_goal_contract_enabled(endpoint) is False


def test_qwen3_verify_nudge_max_is_zero():
    """When endpoint is qwen3 local, VERIFY_NUDGE_MAX is 0."""
    from app.routers.agents import _effective_verify_nudge_max
    endpoint = SimpleNamespace(model_id="qwen3.6-27b", is_private=True, api_key="EMPTY")
    assert _effective_verify_nudge_max(endpoint) == 0


def test_qwen3_data_agent_budget_is_30s():
    """When endpoint is qwen3 local, DATA_AGENT_BUDGET_SECONDS is 30."""
    from app.routers.agents import _effective_data_agent_budget_seconds
    endpoint = SimpleNamespace(model_id="qwen3.6-27b", is_private=True, api_key="EMPTY")
    assert _effective_data_agent_budget_seconds(endpoint) == 30.0


def test_qwen3_synthesis_max_tokens_is_3072():
    """When endpoint is qwen3 local, synthesis max_tokens is 3072."""
    from app.routers.agents import _effective_synthesis_max_tokens
    endpoint = SimpleNamespace(model_id="qwen3.6-27b", is_private=True, api_key="EMPTY")
    assert _effective_synthesis_max_tokens(endpoint) == 3072
