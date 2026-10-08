"""Phase 1a: harness budgets are policy, not literals buried in the router.

These tests pin two things: the defaults are EXACTLY the historical literals (so
Phase 1a is a refactor with zero behaviour change), and a bad override can never
produce an unbounded loop.
"""

from __future__ import annotations

import os

import pytest

from app.services.harness.policy import (
    HarnessBudgets,
    HarnessPolicy,
    get_policy,
    reset_policy,
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for key in list(os.environ):
        if key.startswith("HARNESS_BUDGET"):
            monkeypatch.delenv(key, raising=False)
    reset_policy()
    yield
    reset_policy()


def test_defaults_are_the_historical_literals():
    """The values agents.py used before Phase 1a — a change here is a behaviour change."""
    b = HarnessBudgets()
    assert (b.max_tool_iterations, b.tool_call_hard_cap) == (40, 10)
    assert b.guardrail_retries == 2
    assert (b.non_answer_max_chars, b.weak_content_max_chars) == (600, 160)
    assert b.weak_content_synth_min_chars == 400
    assert (b.max_inline_rows, b.max_inline_chars) == (100, 4000)
    assert (b.max_command_chars, b.max_output_chars) == (600, 500)


def test_budgets_are_immutable():
    b = HarnessBudgets()
    with pytest.raises(Exception):
        b.max_tool_iterations = 99  # type: ignore[misc]


def test_field_override(monkeypatch):
    monkeypatch.setenv("HARNESS_BUDGET_MAX_TOOL_ITERATIONS", "60")
    assert HarnessPolicy().budgets().max_tool_iterations == 60


def test_json_block_override(monkeypatch):
    monkeypatch.setenv(
        "HARNESS_BUDGETS_JSON", '{"max_tool_iterations": 75, "tool_call_hard_cap": 25}'
    )
    b = HarnessPolicy().budgets()
    assert (b.max_tool_iterations, b.tool_call_hard_cap) == (75, 25)


def test_per_field_beats_json_block(monkeypatch):
    monkeypatch.setenv("HARNESS_BUDGETS_JSON", '{"max_tool_iterations": 75}')
    monkeypatch.setenv("HARNESS_BUDGET_MAX_TOOL_ITERATIONS", "60")
    assert HarnessPolicy().budgets().max_tool_iterations == 60


def test_agent_class_override_beats_global(monkeypatch):
    monkeypatch.setenv("HARNESS_BUDGETS_JSON", '{"max_tool_iterations": 75}')
    monkeypatch.setenv(
        "HARNESS_BUDGETS_GENERAL_ASSISTANT_JSON", '{"max_tool_iterations": 12}'
    )
    p = HarnessPolicy()
    assert p.for_agent_class("general_assistant").max_tool_iterations == 12
    # other classes keep the global view
    assert p.for_agent_class("power_user").max_tool_iterations == 75


def test_agent_class_normalisation(monkeypatch):
    monkeypatch.setenv("HARNESS_BUDGETS_BI_ASSISTANT_JSON", '{"guardrail_retries": 5}')
    p = HarnessPolicy()
    assert p.for_agent_class("acme-bi-assistant").guardrail_retries == 5
    assert p.for_agent_class("Acme BI Assistant").guardrail_retries == 5


def test_unknown_or_absent_class_falls_back_to_global():
    p = HarnessPolicy()
    assert p.for_agent_class(None).max_tool_iterations == 40
    assert p.for_agent_class("").max_tool_iterations == 40
    assert p.for_agent_class("brand_new_agent").max_tool_iterations == 40


@pytest.mark.parametrize(
    "value", ["0", "-5", "abc", "", "1.5"]
)
def test_bad_values_are_refused_never_guessed(monkeypatch, value):
    """An unparseable or non-positive budget must NOT become the effective value."""
    monkeypatch.setenv("HARNESS_BUDGET_MAX_TOOL_ITERATIONS", value)
    assert HarnessPolicy().budgets().max_tool_iterations == 40


def test_invalid_json_is_ignored(monkeypatch):
    monkeypatch.setenv("HARNESS_BUDGETS_JSON", "{not json")
    assert HarnessPolicy().budgets().max_tool_iterations == 40


def test_non_object_json_is_ignored(monkeypatch):
    monkeypatch.setenv("HARNESS_BUDGETS_JSON", "[1, 2, 3]")
    assert HarnessPolicy().budgets().max_tool_iterations == 40


def test_unknown_budget_key_is_ignored(monkeypatch):
    monkeypatch.setenv("HARNESS_BUDGETS_JSON", '{"not_a_budget": 5, "tool_call_hard_cap": 7}')
    b = HarnessPolicy().budgets()
    assert b.tool_call_hard_cap == 7
    assert not hasattr(b, "not_a_budget")


def test_describe_exposes_global_and_class_view(monkeypatch):
    monkeypatch.setenv("HARNESS_BUDGETS_GENERAL_ASSISTANT_JSON", '{"max_tool_iterations": 12}')
    p = HarnessPolicy()
    p.for_agent_class("general_assistant")
    d = p.describe()
    assert d["global"]["max_tool_iterations"] == 40
    assert d["by_agent_class"]["GENERAL_ASSISTANT"]["max_tool_iterations"] == 12


def test_singleton_and_reset():
    a = get_policy()
    assert a is get_policy()
    reset_policy()
    assert get_policy() is not a
