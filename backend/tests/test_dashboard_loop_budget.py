"""Dashboard builds must NOT be killed by fast-mode loop budgets.

2026-08-27 regression: the user asked for a dashboard via the UI "+"
flow. The prefill intent chip routed to dashboard-generation correctly and
the agent ran the pipeline (design → schema → data gathering), but the v3
stream loop was cut at ~183s (wall-clock cap 180s) and 10 iterations
(AGENT_FAST_MAX_TOOL_ITERATIONS) BEFORE create_fullstack_dashboard could
run. The user got the canned "this turn ended before completing" note
instead of the live app.

Fix: dashboard turns get their own generous budget —
DASHBOARD_BUILD_MAX_TOOL_ITERATIONS (40) and
DASHBOARD_BUILD_WALL_CLOCK_CAP_S (1800s), applied in agents.py wherever
`_is_dashboard_build` is computed. These tests pin the routing + settings
that make the budget active.
"""
from app.config import settings
from app.services.synexia.intent_router import detect_file_intent


def test_dashboard_prefill_routes_to_dashboard_format():
    """The dialog's intent-chip prefill must resolve to 'dashboard' —
    that is what flips `_is_dashboard_build` and unlocks the big budget."""
    prefill = (
        "Build a FULL-STACK REALTIME DASHBOARD (use create_fullstack_dashboard):\n"
        "- Mode: FULLSTACK_REALTIME — design-system-first (uiux_design_system), "
        "real data from the bound datasource, WebSocket live updates\n"
        "- Refresh interval: 30s\n"
        "- Name: Sales Performance Dashboard\n"
        "- Project: Data Analysis\n"
        "- Description: Live revenue, order volume, regional split and "
        "top-product trends from the bound business database\n"
    )
    assert detect_file_intent(prefill) == "dashboard"


def test_dashboard_build_budgets_are_generous():
    """A full-stack build is 15-30+ tool calls over several minutes on a
    local LLM. The fast-mode defaults (10 iters / 180s) must not apply."""
    assert settings.DASHBOARD_BUILD_MAX_TOOL_ITERATIONS >= 30
    assert settings.DASHBOARD_BUILD_WALL_CLOCK_CAP_S >= 900.0


def test_fast_mode_defaults_are_tighter_than_dashboard_budget():
    """Sanity: the dashboard budget must actually be a relaxation, or the
    fix is a no-op."""
    assert settings.DASHBOARD_BUILD_MAX_TOOL_ITERATIONS > settings.AGENT_FAST_MAX_TOOL_ITERATIONS
    assert settings.DASHBOARD_BUILD_WALL_CLOCK_CAP_S > 180.0


def test_dashboard_delegate_budget_beats_default_delegate_budget():
    """The ask_data_agent delegate defaults to a 60s wall clock which
    truncated dashboard data collection mid-build. Dashboard turns inject
    `budget_seconds`; verify the configured value is a relaxation."""
    from app.services.tool_handlers.delegation_tools import DATA_AGENT_BUDGET_SECONDS
    assert settings.DASHBOARD_DELEGATE_BUDGET_SECONDS > DATA_AGENT_BUDGET_SECONDS
    assert settings.DASHBOARD_DELEGATE_BUDGET_SECONDS >= 300.0
