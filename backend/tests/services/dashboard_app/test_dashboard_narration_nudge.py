"""Fix 6: dashboard-narration nudge guard.

Symptom: "Make ERP Dashbord for me…" → the model printed 3 lines of narration
("I'll build you an ERP dashboard… Let me first check what's in your data
warehouse…") then exited the v3 loop with ZERO tool calls. None of the six
existing exit-chain guards fired because narration IS content (the empty-
answer net skips), no data was retrieved (promise-strip skips), and no tools
ran (self-eval returns "none").

Fix 6 adds a seventh guard at the end of the exit chain: on a dashboard-
shaped turn where the build tool has not been called yet, inject a hard nudge
naming the EXACT next workflow step and ``continue`` the loop (capped at
``MAX_DASHBOARD_NARRATION_NUDGES`` per turn). The nudge message is adaptive —
it inspects what the agent already did (design → schema → build) and tells it
the precise next action.

This file also carries the import-smoke regression test: the Fix 2-4
interception blocks in agents.py reference 5 dashboard_turn_guard symbols that
were lost from the import block at agents.py:64-73 — a latent NameError. The
smoke test imports the agents module so any future missing-symbol regression
fails here, permanently.
"""
import importlib

import pytest

from app.config import settings
from app.services.dashboard_turn_guard import (
    DASHBOARD_NARRATION_NUDGE_TOOLS,
    build_dashboard_narration_nudge_message,
    dashboard_narration_needs_nudge,
)


@pytest.fixture
def fullstack_on(monkeypatch):
    monkeypatch.setattr(settings, "FULLSTACK_DASHBOARD_ENABLED", True)
    monkeypatch.setattr(settings, "LEGACY_DASHBOARD_ENABLED", False)
    yield


# ── predicate: when the nudge SHOULD fire ───────────────────────────────────

def test_nudge_fires_on_narration_only_dashboard_turn(fullstack_on):
    """Dashboard turn, nothing executed yet, cap free → nudge."""
    assert dashboard_narration_needs_nudge(
        "build me a dashboard", [], 0, 1,
    ) is True


def test_nudge_fires_after_partial_work(fullstack_on):
    """Design done but no schema and no build → still nudge (tier 2)."""
    assert dashboard_narration_needs_nudge(
        "make a sales dashboard", ["uiux_design_system"], 0, 1,
    ) is True


def test_nudge_fires_with_none_executed_list(fullstack_on):
    assert dashboard_narration_needs_nudge(
        "build me a dashboard", None, 0, 1,
    ) is True


# ── predicate: when the nudge must NOT fire ─────────────────────────────────

def test_nudge_inert_for_non_dashboard(fullstack_on):
    assert dashboard_narration_needs_nudge("summarize the report", [], 0, 1) is False
    assert dashboard_narration_needs_nudge("", [], 0, 1) is False
    assert dashboard_narration_needs_nudge(None, [], 0, 1) is False


def test_nudge_inert_after_build_called(fullstack_on):
    """Post-build narration is legit wrap-up — never nudge."""
    executed = ["uiux_design_system", "describe_schema", "create_fullstack_dashboard"]
    assert dashboard_narration_needs_nudge("build me a dashboard", executed, 0, 1) is False


def test_nudge_inert_when_cap_reached(fullstack_on):
    """nudges_used >= max_nudges → accept the exit, no infinite nagging."""
    assert dashboard_narration_needs_nudge("build me a dashboard", [], 1, 1) is False
    assert dashboard_narration_needs_nudge("build me a dashboard", [], 3, 2) is False


def test_nudge_inert_when_max_zero(fullstack_on):
    assert dashboard_narration_needs_nudge("build me a dashboard", [], 0, 0) is False


def test_nudge_inert_when_flags_off(monkeypatch):
    """Both dashboard pipelines disabled → build tool is None → guard inert."""
    monkeypatch.setattr(settings, "FULLSTACK_DASHBOARD_ENABLED", False)
    monkeypatch.setattr(settings, "LEGACY_DASHBOARD_ENABLED", False)
    assert dashboard_narration_needs_nudge("build me a dashboard", [], 0, 1) is False


def test_nudge_legacy_mode_uses_create_dashboard(monkeypatch):
    """Legacy mode: the build tool is create_dashboard — post-build exits are
    legit, pre-build narration is nudged."""
    monkeypatch.setattr(settings, "FULLSTACK_DASHBOARD_ENABLED", False)
    monkeypatch.setattr(settings, "LEGACY_DASHBOARD_ENABLED", True)
    assert dashboard_narration_needs_nudge("build me a dashboard", [], 0, 1) is True
    assert dashboard_narration_needs_nudge(
        "build me a dashboard", ["create_dashboard"], 0, 1,
    ) is False


# ── predicate: confirmation-question bypass ─────────────────────────────────

def test_nudge_skips_confirmation_question_after_schema(fullstack_on):
    """HARD RULE lets the agent ask ONE clarifying question AFTER schema
    inspection — nagging it here would fight the intended flow."""
    executed = ["describe_schema"]
    narration = "Which sales metrics should the dashboard show?"
    assert dashboard_narration_needs_nudge(
        "build me a dashboard", executed, 0, 1, narration=narration,
    ) is False


def test_nudge_skips_confirmation_question_fullwidth(fullstack_on):
    executed = ["describe_schema"]
    narration = "看哪个指标？"
    assert dashboard_narration_needs_nudge(
        "build me a dashboard", executed, 0, 1, narration=narration,
    ) is False


def test_nudge_still_fires_on_narration_without_question(fullstack_on):
    """describe_schema ran but the narration asks nothing — that's plain
    narration stall, nudge it."""
    executed = ["describe_schema"]
    narration = "I'll gather the data now and build the dashboard."
    assert dashboard_narration_needs_nudge(
        "build me a dashboard", executed, 0, 1, narration=narration,
    ) is True


def test_nudge_fires_on_question_before_schema(fullstack_on):
    """A question BEFORE schema inspection is not a data-contract confirmation
    (HARD RULE only protects questions after describe_schema) — nudge."""
    assert dashboard_narration_needs_nudge(
        "build me a dashboard", [], 0, 1, narration="want a dashboard?",
    ) is True


def test_nudge_bare_narration_without_content(fullstack_on):
    """narration param absent/empty → no bypass."""
    assert dashboard_narration_needs_nudge(
        "build me a dashboard", ["describe_schema"], 0, 1,
    ) is True


# ── message builder: adaptive 3 tiers ───────────────────────────────────────

def test_builder_tier1_no_design(fullstack_on):
    """Nothing done → name uiux_design_system as the next step."""
    msg = build_dashboard_narration_nudge_message([], "create_fullstack_dashboard")
    assert "uiux_design_system" in msg


def test_builder_tier2_design_no_schema(fullstack_on):
    """Design done, no schema → name describe_schema."""
    msg = build_dashboard_narration_nudge_message(
        ["uiux_design_system"], "create_fullstack_dashboard",
    )
    assert "describe_schema" in msg
    assert "uiux_design_system" not in msg


def test_builder_tier3_ready_to_build(fullstack_on):
    """Design + schema done → name the build tool."""
    msg = build_dashboard_narration_nudge_message(
        ["uiux_design_system", "describe_schema"], "create_fullstack_dashboard",
    )
    assert "create_fullstack_dashboard" in msg


def test_builder_tier3_uses_legacy_build_tool(monkeypatch):
    monkeypatch.setattr(settings, "FULLSTACK_DASHBOARD_ENABLED", False)
    monkeypatch.setattr(settings, "LEGACY_DASHBOARD_ENABLED", True)
    msg = build_dashboard_narration_nudge_message(
        ["uiux_design_system", "describe_schema"], None,
    )
    assert "create_dashboard" in msg


def test_builder_falls_back_to_active_tool(fullstack_on):
    """build_tool=None → fall back to the flag-aware active tool."""
    msg = build_dashboard_narration_nudge_message(["uiux_design_system", "describe_schema"], None)
    assert "create_fullstack_dashboard" in msg


def test_builder_message_contains_stop_keyword(fullstack_on):
    msg = build_dashboard_narration_nudge_message([], "create_fullstack_dashboard")
    assert "STOP" in msg
    assert "tool call" in msg


# ── constant ────────────────────────────────────────────────────────────────

def test_narration_nudge_tools_constant():
    assert DASHBOARD_NARRATION_NUDGE_TOOLS == frozenset({
        "uiux_design_system", "uiux_search", "Skill",
        "describe_schema",
        "create_fullstack_dashboard", "create_dashboard",
    })


def test_narration_nudge_tools_cover_workflow(fullstack_on):
    """The nudge's "progress" set must cover the whole dashboard workflow so
    the adaptive tiers can distinguish design / schema / build."""
    assert {"uiux_design_system", "describe_schema", "create_fullstack_dashboard"} <= DASHBOARD_NARRATION_NUDGE_TOOLS


# ── import smoke test (missing-symbol regression guard) ────────────────────

def test_agents_module_imports_guard_symbols():
    """The Fix 2-4 interception blocks in agents.py reference 5
    dashboard_turn_guard symbols that were lost from the import block at
    agents.py:64-73 — a latent NameError. Re-importing the module here fails
    loudly on any future missing-symbol regression."""
    agents_mod = importlib.import_module("app.routers.agents")
    for symbol in (
        "DASHBOARD_ANTITOOLS",
        "dashboard_antitools_should_block",
        "DASHBOARD_EXPLORATION_TOOLS",
        "dashboard_exploration_cap_reached",
        "parse_artifact_title",
        "dashboard_narration_needs_nudge",
        "build_dashboard_narration_nudge_message",
        "dashboard_orchestrator_should_block",
    ):
        assert hasattr(agents_mod, symbol), f"agents.py missing import: {symbol}"
