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


def dashboard_build_tool() -> str | None:
    """The active dashboard build tool name, flag-aware.

    Returns:
    - ``create_fullstack_dashboard`` when ``FULLSTACK_DASHBOARD_ENABLED=True``
      (the new full-stack pipeline),
    - ``create_dashboard`` when ``LEGACY_DASHBOARD_ENABLED=True``
      (rollback pipeline),
    - ``None`` when both are disabled → the guard is inert.
    """
    try:
        from app.config import settings
    except Exception:
        return None
    if getattr(settings, "FULLSTACK_DASHBOARD_ENABLED", False):
        return "create_fullstack_dashboard"
    if getattr(settings, "LEGACY_DASHBOARD_ENABLED", False):
        return "create_dashboard"
    return None


def should_force_create_dashboard(
    user_content: str | None,
    tool_calls_for_frontend: list[dict],
    *,
    has_dashboard_tool: bool = True,
    is_dashboard_project: bool = False,
) -> bool:
    """After a small schema/design pass, force the next build-tool call.

    The build tool is flag-aware: ``create_fullstack_dashboard`` (new
    pipeline) or ``create_dashboard`` (legacy).

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
       markdown instead of calling the build tool.
    """
    build_tool = dashboard_build_tool()
    if not build_tool:
        return False
    if not has_dashboard_tool:
        return False
    if not user_content:
        return False
    names = [tc.get("name") or tc.get("tool_name") for tc in (tool_calls_for_frontend or [])]
    if build_tool in names:
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
    conditions are met — only the active build tool (``create_fullstack_dashboard``
    or ``create_dashboard``) or harmless side-effect tools should run. We block
    additional ``execute_query`` calls because they just produce more rows that
    the agent then dumps as markdown instead of shipping a dashboard.
    """
    return frozenset({"execute_query", "execute_sql", "sql_query"})


# ── T7: data-contract confirmation gate ────────────────────────────────────
# Schema-inspection tools count as "grounding": if the agent has run any of
# these this turn, its SQL references real tables/columns (the tool result is
# the actual schema), so the build is allowed through.
_SCHEMA_INSPECTION_TOOLS = frozenset({
    "describe_schema",
    "describe_table",
    "list_data_sources",
    "inspect_data_source",
})

# Approval words — either language signals the user accepted a proposed
# data contract. \b fails on CJK, so Chinese approval is matched separately.
_CONTRACT_APPROVAL_EN_RE = re.compile(
    r"\b(?:confirm(?:ed|ation)?|approved?|agree|ok|okay|yes|sure|"
    r"go ahead|looks? good|great|perfect)\b",
    re.IGNORECASE,
)
_CONTRACT_APPROVAL_ZH = ("确认", "可以", "同意", "没问题", "就用", "就按", "好的")

# Entity-shaped token: snake_case identifiers or ALL_CAPS acronyms — the shape
# of real table/column names (e.g. `erp_product_sales_details`, `FNAME`).
_ENTITY_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")


def mentions_concrete_entities(user_content: str) -> bool:
    """True when the user request names concrete tables/columns (e.g.
    ``erp_product_sales_details``, ``FNAME``, ``shipment_date``)."""
    return any("_" in tok or tok.isupper() for tok in _ENTITY_TOKEN_RE.findall(user_content))


def _has_user_approval(user_content: str) -> bool:
    if _CONTRACT_APPROVAL_EN_RE.search(user_content):
        return True
    return any(kw in user_content for kw in _CONTRACT_APPROVAL_ZH)


def contract_confirmation_needed(
    user_content: str | None,
    tool_calls_for_frontend: list[dict],
) -> bool:
    """T7 data-contract confirmation gate.

    True when the agent is trying to build a live dashboard while the data
    contract is NOT confirmed:

    - the request is dashboard-shaped,
    - the build tool is flag-enabled (gate is inert otherwise),
    - the build tool has not already run this turn,
    - no schema-inspection tool ran this turn (the agent has NO grounding —
      it would be guessing table/column names),
    - the user has not named concrete entities or approved a proposed contract.

    When True, the chat loop must block the build tool call and inject a
    synthetic clarification so the agent asks instead of fabricating data.
    """
    if not user_content or not is_live_dashboard_request(user_content):
        return False
    build_tool = dashboard_build_tool()
    if not build_tool:
        return False
    names = {
        tc.get("name") or tc.get("tool_name")
        for tc in (tool_calls_for_frontend or [])
    }
    if build_tool in names:
        return False  # already built this turn
    if names & _SCHEMA_INSPECTION_TOOLS:
        return False  # grounded in the real schema
    if _has_user_approval(user_content):
        return False  # user approved the contract
    if mentions_concrete_entities(user_content):
        return False  # user named real tables/columns — inspect and build
    return True
