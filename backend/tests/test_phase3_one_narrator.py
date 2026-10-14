"""Option C Phase 3 tests — one narrator for data turns.

V3_DATA_ROUTE_MODE=fsm (default): data turns route to the SynexiaFSM brain
(unchanged). react: the FSM narrator is retired for top-level turns — the
ReAct orchestrator calls the boxed `query_database` tool and narrates from
grounded rows. Automation-forced planning always keeps the FSM top-level.
"""

import pytest

from app.config import settings
from app.services.turn_router import data_route_mode, route_data_turn


# ── Mode resolution ──────────────────────────────────────────────────────


def test_data_route_mode_defaults_to_fsm(monkeypatch):
    monkeypatch.delattr(settings, "V3_DATA_ROUTE_MODE", raising=False)
    assert data_route_mode() == "fsm"


def test_data_route_mode_reads_env(monkeypatch):
    monkeypatch.setattr(settings, "V3_DATA_ROUTE_MODE", "react")
    assert data_route_mode() == "react"


def test_data_route_mode_invalid_falls_back(monkeypatch):
    monkeypatch.setattr(settings, "V3_DATA_ROUTE_MODE", "bogus")
    assert data_route_mode() == "fsm"


# ── Routing decision ─────────────────────────────────────────────────────


def test_fsm_mode_never_routes_react(monkeypatch):
    monkeypatch.setattr(settings, "V3_DATA_ROUTE_MODE", "fsm")
    assert route_data_turn("data-bound-override", forced=False) is False
    assert route_data_turn("llm-router", forced=False) is False


def test_react_mode_routes_data_turns(monkeypatch):
    monkeypatch.setattr(settings, "V3_DATA_ROUTE_MODE", "react")
    assert route_data_turn("data-bound-override", forced=False) is True
    assert route_data_turn("llm-router", forced=False) is True
    assert route_data_turn("followup-override", forced=False) is True


def test_react_mode_never_overrides_automation_forced(monkeypatch):
    """Scheduled/automation jobs must stay deterministic — FSM top-level."""
    monkeypatch.setattr(settings, "V3_DATA_ROUTE_MODE", "react")
    assert route_data_turn("automation-forced", forced=True) is False
    assert route_data_turn("automation-forced", forced=False) is False


def test_react_mode_ignores_untriggered_turns(monkeypatch):
    monkeypatch.setattr(settings, "V3_DATA_ROUTE_MODE", "react")
    assert route_data_turn(None, forced=False) is False


# ── Directive text ───────────────────────────────────────────────────────


def test_react_directive_prefers_query_database(monkeypatch):
    from app.services.data_source_runtime.data_source_runtime import (
        _build_anti_hallucination_directive,
    )

    monkeypatch.setattr(settings, "V3_DATA_ROUTE_MODE", "react")
    d = _build_anti_hallucination_directive()
    assert "`query_database`" in d
    assert "MUST call the `query_database` tool FIRST" in d
    # ask_data_agent demoted to explicit fallback, still available
    assert "ask_data_agent" in d
    assert "ONLY when" in d


def test_fsm_directive_unchanged(monkeypatch):
    from app.services.data_source_runtime.data_source_runtime import (
        _build_anti_hallucination_directive,
    )

    monkeypatch.setattr(settings, "V3_DATA_ROUTE_MODE", "fsm")
    d = _build_anti_hallucination_directive()
    assert "MUST call the `ask_data_agent` tool FIRST" in d
    assert "query_database" not in d


# ── Injection: the tool rides along for data-bound agents ───────────────


def test_query_database_injected_in_data_runtime_source():
    """The injection site must register query_database alongside
    ask_data_agent/fetch_data_batch — source-scan guard (Phase 0 rule:
    behavior via tools, verified structurally)."""
    import inspect

    from app.services.data_source_runtime import data_source_runtime as dsr

    src = inspect.getsource(dsr)
    assert 'registry.get_entry("query_database")' in src


def test_query_database_in_react_guardrail_set():
    from app.routers.agents import _DATA_TOOL_NAMES

    assert "query_database" in _DATA_TOOL_NAMES
