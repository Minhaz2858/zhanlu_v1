"""Helpers for keeping live-dashboard turns from exhausting tool budget."""

from __future__ import annotations

import re

# English dashboard keywords — \b works fine here.
_LIVE_DASHBOARD_EN_RE = re.compile(
    r"\b(?:dashboard|dash-board|kpi\s+view|metrics\s+board)\b",
    re.IGNORECASE,
)

# Chinese dashboard keywords — \b does NOT work with CJK, so we use
# plain substring matching instead.
_LIVE_DASHBOARD_ZH = ("仪表盘", "看板", "数据面板", "数据看板", "仪表板")


def is_live_dashboard_request(user_content: str | None) -> bool:
    """True for ordinary live-dashboard requests.

    This deliberately ignores generic file-intent priority because users often
    say "not HTML" while asking for a live dashboard; the word HTML must not
    steal ownership from the dashboard workflow.
    """
    if not user_content:
        return False
    if _LIVE_DASHBOARD_EN_RE.search(user_content):
        return True
    return any(kw in user_content for kw in _LIVE_DASHBOARD_ZH)


def should_force_create_dashboard(
    user_content: str | None,
    tool_calls_for_frontend: list[dict],
    *,
    has_dashboard_tool: bool = True,
    is_dashboard_project: bool = False,
) -> bool:
    """After a small schema/design pass, force the next call to create_dashboard.

    Two trigger paths:

    1. **Explicit**: user_content matches the live-dashboard keywords and the
       agent has already done ``list_data_sources`` + ``describe_schema`` +
       one of the design tools (``uiux_design_system`` / ``uiux_search`` /
       ``Skill``).

    2. **Speculative (project-context)**: when the agent is in a project that
       has dashboard semantics (Ecisco BI / ``is_dashboard_project=True``) and
       the agent has already done ``describe_schema`` + a design tool, force
       the call. This catches the "user said hi in an Ecisco BI chat" case
       where the agent speculatively loads dashboard skills and then dumps
       markdown instead of calling ``create_dashboard``.
    """
    if not has_dashboard_tool:
        return False
    if not user_content:
        return False
    names = [tc.get("name") or tc.get("tool_name") for tc in (tool_calls_for_frontend or [])]
    if "create_dashboard" in names:
        return False

    has_schema = "describe_schema" in names
    has_design = any(n in names for n in ("uiux_design_system", "uiux_search", "Skill"))

    # Path 1: explicit user request
    if is_live_dashboard_request(user_content):
        has_source = "list_data_sources" in names
        return has_source and has_schema and has_design

    # Path 2: speculative / project-context — the agent is prepping a
    # dashboard without being told. We've already loaded dashboard skills,
    # called describe_schema, and used ui-ux-pro-max. It's time to ship.
    if is_dashboard_project and has_schema and has_design:
        return True

    return False


def dashboard_guard_blocked_tools() -> frozenset[str]:
    """Tools that the dashboard guard should NOT let execute when its
    conditions are met — only ``create_dashboard`` (or harmless side-effect
    tools) should run. We block additional ``execute_query`` calls because
    they just produce more rows that the agent then dumps as markdown
    instead of shipping a dashboard.
    """
    return frozenset({"execute_query", "execute_sql", "sql_query"})
