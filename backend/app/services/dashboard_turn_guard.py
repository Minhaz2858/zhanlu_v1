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


def _fuzzy_dashboard_word(token: str) -> bool:
    """Consonant-skeleton match for typo'd 'dashboard' (Fix 5).

    Pipeline: lowercase -> strip non-alpha -> drop vowels, then compare the
    remaining consonant skeleton against ``dshbrd``/``dshbrds``. Because all
    vowels are dropped, vowel-order typos are absorbed, and consonant
    transpositions that preserve the skeleton (e.g. "Dashbord") match:
    "dashbord" / "dashboard" / "dash-board" all normalize to ``dshbrd``.

    Flag-gated by ``DASHBOARD_FUZZY_MATCH_ENABLED`` (default False) so the
    guard is byte-identical to before when the flag is off.
    """
    try:
        from app.config import settings

        if not getattr(settings, "DASHBOARD_FUZZY_MATCH_ENABLED", False):
            return False
    except Exception:
        return False
    if not token:
        return False
    skeleton = re.sub(r"[^a-z]", "", token.lower())
    skeleton = re.sub(r"[aeiou]", "", skeleton)
    return skeleton in {"dshbrd", "dshbrds"}


def fuzzy_dashboard_request(user_content: str | None) -> bool:
    """True when any word in ``user_content`` is a fuzzy 'dashboard' match.

    Hyphens are kept as token separators (``dash-board`` stays one token);
    all other non-alpha characters split tokens. Returns False immediately
    when ``DASHBOARD_FUZZY_MATCH_ENABLED`` is off.
    """
    if not user_content:
        return False
    # Goal-Contract mode: the typo-tolerant normalizer is the single source
    # of truth — it catches the exact keyword, Dashbord-class typos AND the
    # Chinese variants, so the fuzzy path is a superset of the skeleton match.
    try:
        from app.config import settings

        if getattr(settings, "GOAL_CONTRACT_ENABLED", False):
            from app.services.goal_contract import normalize_deliverable_intent

            return normalize_deliverable_intent(user_content) == "dashboard"
    except Exception:
        pass
    for token in re.split(r"[^a-zA-Z-]+", user_content.lower()):
        if _fuzzy_dashboard_word(token):
            return True
    return False


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
    if fuzzy_dashboard_request(user_content):
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


# ── T12: describe_schema per-turn cap ──────────────────────────────────────
# Tools whose repeated pre-build invocation burns the tool-loop budget. The
# v3 stream loop blocks further calls once ``MAX_DESCRIBE_SCHEMA_PER_DASHBOARD_TURN``
# is reached for a dashboard-shaped request.
DASHBOARD_SCHEMA_CAP_TOOLS = frozenset({"describe_schema"})


def describe_schema_cap_reached(
    user_content: str | None,
    executed_names: list[str] | None,
    max_cap: int,
) -> bool:
    """True when the per-turn ``describe_schema`` cap is reached for a
    dashboard-shaped request and the NEXT ``describe_schema`` call should be
    blocked.

    ``executed_names`` must be a list of CANONICAL tool names already executed
    this turn (not frontend display names — the loop appends ``tool_name`` for
    every executed call). This keeps the helper pure and unit-testable while
    the v3 stream loop feeds it from its own execution counter.

    - ``max_cap <= 0`` → cap disabled (inert).
    - Non-dashboard requests → always False (the cap is dashboard-turn-scoped).
    - Build tool not flag-enabled → False (guard inert).
    - Build tool already called this turn → False (schema inspection after a
      build is legitimate iteration on the dashboard).
    - Otherwise: count ``describe_schema`` calls already executed this turn;
      True when ``count >= max_cap``.
    """
    if max_cap <= 0:
        return False
    if not user_content or not is_live_dashboard_request(user_content):
        return False
    build_tool = dashboard_build_tool()
    if not build_tool:
        return False
    names = list(executed_names or [])
    if build_tool in names:
        return False  # already built this turn — further schema calls are iteration
    count = sum(1 for n in names if n in DASHBOARD_SCHEMA_CAP_TOOLS)
    return count >= max_cap


# ── Fix 2: hard-block anti-tools on dashboard turns ────────────────────────
# When the user asked for a dashboard and the full-stack build tool has NOT
# been called yet, static-HTML fallbacks (create_artifact) and the legacy
# build tool (create_dashboard) are blocked so a weak model cannot silently
# ship a static page instead of the real-time dashboard it was asked for.
DASHBOARD_ANTITOOLS = frozenset({"create_artifact", "create_dashboard"})


def dashboard_antitools_should_block(
    user_content: str | None,
    executed_names: list[str] | None,
) -> bool:
    """True when an anti-tool call (create_artifact / legacy create_dashboard)
    should be blocked on this dashboard-shaped turn.

    - Non-dashboard requests → always False (anti-tool blocking is
      dashboard-turn-scoped).
    - Build tool not flag-enabled → False (guard inert).
    - Build tool already called this turn → False (post-build the agent may
      legitimately export a static copy).
    - Otherwise → True: any create_artifact / create_dashboard call is waste
      that would bypass the full-stack pipeline.
    """
    if not user_content or not is_live_dashboard_request(user_content):
        return False
    build_tool = dashboard_build_tool()
    if not build_tool:
        return False
    names = set(executed_names or [])
    if build_tool in names:
        return False  # already built this turn — anti-tools are allowed
    return True


# ── Fix C: dashboard-orchestrator guard (post-loop routing conflict) ───────
# DASHBOARD_ANTITOOLS only intercepts tool calls inside the v3 loop. The
# finalize-phase orchestrator (``ensure_artifact_for_doc_request`` with
# ``doc_format="dashboard"``) runs AFTER the loop breaks and would auto-create
# a static HTML "Dashboard" artifact on dashboard-intent turns where the build
# tool was never called — silently shipping a static report card instead of the
# requested dashboard (observed on conversation caeeda3b). This guard blocks
# that post-loop path.


def dashboard_orchestrator_should_block(
    user_content: str | None,
    executed_names: list[str] | None,
) -> bool:
    """True when the post-loop orchestrator must be skipped on this turn.

    Mirrors ``dashboard_antitools_should_block``:

    - Non-dashboard requests → always False (guard is dashboard-turn-scoped).
    - Build tool not flag-enabled → False (guard inert).
    - Build tool already called this turn → False (post-build the orchestrator
      may legitimately run marker fulfillment / static export).
    - Otherwise → True: the orchestrator must not fabricate a dashboard
      artifact; the model should have used the build tool in-loop.
    """
    if not user_content or not is_live_dashboard_request(user_content):
        return False
    build_tool = dashboard_build_tool()
    if not build_tool:
        return False
    names = set(executed_names or [])
    if build_tool in names:
        return False  # already built this turn — orchestrator is allowed
    return True


# ── Fix 3: total-exploration cap (describe_schema + execute_query + …) ─────
# The T12 cap above only limits describe_schema. A weak model can still burn
# the whole budget on execute_query exploration. This cap counts ALL
# exploration tools combined (schema inspection + query execution) so the
# agent is forced to build once total exploration hits the threshold.
DASHBOARD_EXPLORATION_TOOLS = frozenset({
    "describe_schema", "execute_query", "execute_sql", "sql_query",
})


def dashboard_exploration_cap_reached(
    user_content: str | None,
    executed_names: list[str] | None,
    max_cap: int,
) -> bool:
    """True when total exploration (schema inspection + query execution)
    exceeds ``max_cap`` on a dashboard-shaped turn and the NEXT exploration
    call should be blocked.

    Mirrors ``describe_schema_cap_reached``:
    - ``max_cap <= 0`` → cap disabled (inert).
    - Non-dashboard requests → always False.
    - Build tool not flag-enabled → False (guard inert).
    - Build tool already called this turn → False (post-build queries are
      legitimate iteration).
    - Otherwise: count exploration tools executed this turn; True when
      ``count >= max_cap``.
    """
    if max_cap <= 0:
        return False
    if not user_content or not is_live_dashboard_request(user_content):
        return False
    build_tool = dashboard_build_tool()
    if not build_tool:
        return False
    names = list(executed_names or [])
    if build_tool in names:
        return False  # already built this turn — further queries are iteration
    count = sum(1 for n in names if n in DASHBOARD_EXPLORATION_TOOLS)
    return count >= max_cap


# ── Fix 6: dashboard-narration nudge guard ─────────────────────────────────
# The model sometimes exits the v3 loop with ONLY narration ("I'll build you
# an ERP dashboard...") and no tool call. None of the existing exit-chain
# guards fire because narration IS content (empty-answer net skips), no data
# was retrieved (promise-strip skips), and no tools ran (self-eval returns
# "none"). This nudge injects the EXACT next workflow step and continues the
# loop so the model takes its first real action.
#
# Tools that count as "workflow progress" on a dashboard turn — the adaptive
# nudge uses these to decide which step is next.
DASHBOARD_NARRATION_NUDGE_TOOLS = frozenset({
    "uiux_design_system", "uiux_search", "Skill",
    "describe_schema",
    "create_fullstack_dashboard", "create_dashboard",
})

# Design tools = the ui-ux-pro-max skill entry points (prompt HARD RULE:
# dashboard turns start with uiux_design_system).
_DASHBOARD_DESIGN_TOOLS = frozenset({"uiux_design_system", "uiux_search", "Skill"})


def _narration_is_confirmation_question(
    executed_names: list[str] | None,
    narration: str | None,
) -> bool:
    """True when the narration is a data-contract confirmation question.

    The prompt HARD RULE lets the agent ask ONE clarifying question after
    schema inspection (e.g. "which sales metrics should the dashboard
    show?"). Nagging it here would fight the intended flow, so the nudge is
    skipped when ``describe_schema`` already ran AND the narration asks a
    question (``?`` or ``？``).
    """
    if not narration or not executed_names:
        return False
    if "describe_schema" not in executed_names:
        return False
    return "?" in narration or "？" in narration


def dashboard_narration_needs_nudge(
    user_content: str | None,
    executed_names: list[str] | None,
    nudges_used: int,
    max_nudges: int,
    *,
    narration: str | None = None,
) -> bool:
    """True when a narration-only loop exit on a dashboard-shaped turn should
    be intercepted with a hard nudge.

    Returns False when:
    - ``max_nudges <= 0`` (guard disabled),
    - ``nudges_used >= max_nudges`` (cap reached — accept the exit),
    - the request is not dashboard-shaped,
    - the build tool is not flag-enabled (guard inert),
    - the build tool has already been called this turn (post-build exits are
      legitimate wrap-up narration),
    - the narration is a data-contract confirmation question (``narration``
      contains ``?``/``？`` and ``describe_schema`` already ran this turn).

    ``executed_names`` must be a list of CANONICAL tool names already
    executed this turn.
    """
    if max_nudges <= 0:
        return False
    if nudges_used >= max_nudges:
        return False
    if not user_content or not is_live_dashboard_request(user_content):
        return False
    build_tool = dashboard_build_tool()
    if not build_tool:
        return False
    names = set(executed_names or [])
    if build_tool in names:
        return False  # already built — narration is legit wrap-up
    if _narration_is_confirmation_question(executed_names, narration):
        return False
    return True


def build_dashboard_narration_nudge_message(
    executed_names: list[str] | None,
    build_tool: str | None,
) -> str:
    """Adaptive nudge: inspect what the agent already did and name the EXACT
    next workflow step.

    3 tiers:
    1. No design tool yet → call ``uiux_design_system`` first (the prompt
       HARD RULE for dashboard turns).
    2. Design done, no schema → call ``describe_schema`` on the target table.
    3. Design + schema done → call the build tool with a DashboardSpec.

    ``build_tool`` falls back to the active pipeline tool name when omitted.
    """
    build_tool = build_tool or dashboard_build_tool() or "create_fullstack_dashboard"
    names = set(executed_names or [])
    if not (names & _DASHBOARD_DESIGN_TOOLS):
        next_step = (
            "Call `uiux_design_system(query=…, persist=True)` first — every "
            "dashboard build starts with the ui-ux-pro-max design system."
        )
    elif "describe_schema" not in names:
        next_step = (
            "Call `describe_schema(table=…)` on the target ERP table so the "
            "dashboard is grounded in real columns."
        )
    else:
        next_step = (
            f"Call `{build_tool}` now with a complete DashboardSpec "
            "(title, metrics, chart config, scope) — you have everything "
            "needed to build."
        )
    return (
        "STOP. You only produced narration — no tool call. Take the first "
        "real dashboard action now. " + next_step
        + " Do not reply with prose; make the tool call."
    )


# ── Fix 4: duplicate create_artifact title tracking ────────────────────────
def parse_artifact_title(call_args_json: str | None) -> str | None:
    """Extract the artifact title from a ``create_artifact`` call's JSON args.

    Looks for ``title`` then ``name`` (lowercased, stripped). Returns None for
    malformed JSON or when neither field is present, so a duplicate-check miss
    degrades to "no title" rather than raising.
    """
    if not call_args_json:
        return None
    try:
        import json as _json

        data = _json.loads(call_args_json)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    for key in ("title", "name"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip().lower()
    return None


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
