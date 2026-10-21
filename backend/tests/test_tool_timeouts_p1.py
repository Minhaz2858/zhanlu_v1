"""Regression tests for audit P1 — central tool timeout.

The data-agent loop wall-clock used to become ``float("inf")`` when
``QUERY_TIMEOUT_DISABLED`` was on, allowing a single data turn to run
unbounded. ``delegate_task`` had an iteration clamp but no wall-clock cap.
These tests lock in the finite ceilings.
"""
from types import SimpleNamespace

import app.config as config
from app.services.tool_handlers.delegation_tools import (
    DATA_AGENT_BUDGET_SECONDS,
    _resolve_data_agent_budget,
)


def test_query_timeout_disabled_no_longer_infinite():
    s = SimpleNamespace(QUERY_TIMEOUT_DISABLED=True, DATA_AGENT_MAX_WALL_SECONDS=600.0)
    budget = _resolve_data_agent_budget({}, s)
    assert budget == 600.0
    assert budget != float("inf")
    assert budget > 0


def test_explicit_budget_seconds_wins():
    s = SimpleNamespace(QUERY_TIMEOUT_DISABLED=True, DATA_AGENT_MAX_WALL_SECONDS=600.0)
    assert _resolve_data_agent_budget({"budget_seconds": 30}, s) == 30.0


def test_normal_path_uses_constant():
    s = SimpleNamespace(QUERY_TIMEOUT_DISABLED=False)
    assert _resolve_data_agent_budget({}, s) == DATA_AGENT_BUDGET_SECONDS


def test_config_has_wall_clock_knobs():
    assert hasattr(config.settings, "DELEGATE_WALL_SECONDS")
    assert hasattr(config.settings, "DATA_AGENT_MAX_WALL_SECONDS")
    assert config.settings.DELEGATE_WALL_SECONDS > 0
    assert config.settings.DATA_AGENT_MAX_WALL_SECONDS > 0
