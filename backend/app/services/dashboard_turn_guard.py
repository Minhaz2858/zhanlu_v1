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


# ── Inline-analytics intent (bypasses Path 2 speculative forcing) ──
# When the user clearly wants inline data analysis (not a dashboard),
# the speculative dashboard forcing must NOT fire, regardless of
# whether the agent has done schema/design prep.  These keywords are
# checked against the raw user message (lower-cased, EN + ZH).
_INLINE_ANALYTICS_EN_RE = re.compile(
    r"(?:"
    r"\bsales\s+report\b"
    r"|\bsupply\s+chain\b|\bsupply-chain\b"
    r"|\binventory\s+(?:data|report|level|status|check|overview)\b"
    r"|\brevenue\s+(?:report|breakdown|by\s+\w+|data|overview)\b"
    r"|\bgross\s+margin\b|\bmargin\s+analysis\b"
    r"|\btop\s+\d+\b"
    r"|\brank\s+(?:the\s+)?(?:customers?|products?|materials?|suppliers?|skus?)\b"
    r"|\bskus?\b|\bsku\s+volume\b"
    r"|\bwhat\s+were\b"
    r"|\bdrill\s+(?:down|into)\b"
    r"|\bgive\s+me\s+(?:a|an|the)?\b"
    r"|\bshow\s+me\b|\btell\s+me\b"
    r"|\blist\s+(?:the|all)\b"
    r"|\bcompare\b|\bbreakdown\b|\btrend\b"
    r"|\bmonth[- ]over[- ]month\b|\bYoY\b|\bQoQ\b"
    r"|\blast\s+\d+\s+days?\b|\blast\s+quarter\b|\blast\s+month\b"
    r"|\bJuly\s+\d{4}\b|\bJune\s+\d{4}\b|\bAugust\s+\d{4}\b"
    r"|\b202[4-9]\b|\b203[0-9]\b"
    r")",
    re.IGNORECASE,
)
_INLINE_ANALYTICS_ZH = (
    "销售报告", "销售报表", "销量", "库存", "供应链",
    "营业收入", "毛利率", "营收",
    "对比", "排名", "前几", "排行",
    "明细", "清单", "汇总",
    "告诉我", "给我", "看看",
    "上个月", "过去", "最近", "产品收入", "客户收入", "月度",
)


def _is_inline_analytics_intent(user_content: str) -> bool:
    """True when the user clearly wants inline data analysis, NOT a dashboard.

    Returns True for queries like:
      - "i want July 2026 sales report (volume, revenue, margin, inventory)"
      - "give me supply chain data for last 30 days"
      - "show me top 10 products by revenue"
      - "compare June and July sales"

    Returns False for queries like:
      - "build a dashboard for sales"
      - "create a dashboard showing KPIs"
      - "open the inventory dashboard"
    """
    if not user_content:
        return False
    text = user_content.lower()
    if _INLINE_ANALYTICS_EN_RE.search(text):
        return True
    return any(kw in user_content for kw in _INLINE_ANALYTICS_ZH)


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
    # The flag is authoritative: when off, this function is byte-identical to
    # the legacy no-fuzzy behavior even when GOAL_CONTRACT_ENABLED would
    # otherwise normalize "Dashbord" → dashboard (the goal-contract normalizer
    # is a SUPERSET of the skeleton match and must not bypass the flag gate).
    try:
        from app.config import settings

        if not getattr(settings, "DASHBOARD_FUZZY_MATCH_ENABLED", False):
            return False
    except Exception:
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
       has dashboard semantics (``is_dashboard_project=True``) and
       the agent has already done ``describe_schema`` + a design tool, force
       the call. This catches the "user said hi in a dashboard project" case
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
    #
    # Escape hatch: when the user CLEARLY wants inline data analysis
    # (sales report, supply chain, revenue breakdown, top-N list, etc.),
    # do NOT force a dashboard — the user wants prose + numbers, not a
    # widget grid.  This preserves the original intent even when the
    # agent speculatively loads dashboard skills.
    if is_dashboard_project and has_schema and has_design:
        if _is_inline_analytics_intent(user_content):
            return False
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


def dashboard_guard_should_block_queries(
    parsed_tool_names,
    build_tool: str | None,
    dashboard_forced: bool,
) -> bool:
    """True when ``execute_query``-style calls in the current tool-call batch
    must be intercepted and redirected to the build tool.

    Fires ONLY when a dashboard build is genuinely in play:

    - the LLM itself called the build tool (``build_tool``) in this batch, OR
    - the dashboard guard forced the build tool this turn
      (``dashboard_forced`` — the user asked for a dashboard and the
      schema/design pass completed, so further query exploration is waste).

    It does NOT fire merely because the build tool is REGISTERED in the
    agent's toolset. That registry-presence check (the pre-fix condition
    ``build_tool in _tool_names``) blocked legitimate ``execute_query`` calls
    in every non-dashboard conversation whose agent happened to list the
    build tool in ``enabled_tools`` — e.g. the skill_agent chat, where a
    plain "weekly sales report" request got "only create_dashboard allowed"
    and the agent apologized instead of querying (observed 2026-08-29,
    conv 8ffb436e).
    """
    if not parsed_tool_names or not build_tool:
        return False
    names = set(parsed_tool_names)
    if not (build_tool in names or dashboard_forced):
        return False
    return bool(names & dashboard_guard_blocked_tools())


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
    failed_names: set[str] | None = None,
) -> bool:
    """True when the post-loop orchestrator must be skipped on this turn.

    Mirrors ``dashboard_antitools_should_block``:

    - Non-dashboard requests → always False (guard is dashboard-turn-scoped).
    - Build tool not flag-enabled → False (guard inert).
    - Build tool called AND succeeded this turn → False (post-build the
      orchestrator may legitimately run marker fulfillment / static export).
    - Build tool called but FAILED this turn (returned ``{"success": False}``
      or threw) → True: the orchestrator must NOT fabricate a static artifact
      on top of a crashed build (observed in smoke #5 — the 78 KB static
      "Web page" artifact after ``create_fullstack_dashboard`` crashed with
      ``NameError: name 'Path' is not defined``).
    - Otherwise → True: the orchestrator must not fabricate a dashboard
      artifact; the model should have used the build tool in-loop.
    """
    if not user_content or not is_live_dashboard_request(user_content):
        return False
    build_tool = dashboard_build_tool()
    if not build_tool:
        return False
    names = set(executed_names or [])
    failed = set(failed_names or [])
    if build_tool in names:
        if build_tool in failed:
            return True  # build tool CRASHED — orchestrator must not fabricate
        return False  # build succeeded — orchestrator is allowed
    return True


# ── Fix 3: total-exploration cap (describe_schema + execute_query + …) ─────
# The T12 cap above only limits describe_schema. A weak model can still burn
# the whole budget on execute_query exploration. This cap counts ALL
# exploration tools combined (schema inspection + query execution) so the
# agent is forced to build once total exploration hits the threshold.
DASHBOARD_EXPLORATION_TOOLS = frozenset({
    "describe_schema", "execute_query", "execute_sql", "sql_query",
    # 2026-08-27: fetch_data_batch is the direct-parallel-SQL fast path the
    # local model prefers for schema exploration (SHOW TABLES / DESCRIBE /
    # SHOW COLUMNS batched per call). It MUST count toward the exploration
    # cap, otherwise a weak model can explore forever via fetch_data_batch
    # and never reach the build step.
    "fetch_data_batch",
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
# of real table/column names (e.g. `sales_table`, `PRODUCT_ID`).
_ENTITY_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")


def mentions_concrete_entities(user_content: str) -> bool:
    """True when the user request names concrete tables/columns (e.g.
    ``sales_table``, ``PRODUCT_ID``, ``shipment_date``)."""
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


def verify_dashboard_build_produced_app(
    db,
    conversation_id: str | None,
    executed_tool_names: list[str] | None,
    build_tool: str | None = None,
) -> str | None:
    """POST-BUILD verification gate (T-something, Aug 2026).

    Returns a human-readable warning when a dashboard turn CLAIMED it built a
    dashboard (the build tool was called) but NO ``dashboard_apps`` row exists
    for this conversation. This is the deterministic guard against the silent
    "delivered a text report instead of a dashboard" failure: the agent can
    narrate success without ever persisting the app.

    Returns ``None`` when:
      - the build tool was never called (nothing to verify — the narration
        nudge handles that case),
      - the build tool is not flag-enabled,
      - a row with ``chat_thread_id == conversation_id`` EXISTS (success).

    The caller appends the returned message to the final stream content so the
    user sees a clear failure instead of a confident-but-fake success story.
    """
    if not conversation_id or not executed_tool_names:
        return None
    build_tool = build_tool or dashboard_build_tool()
    if not build_tool:
        return None
    names = set(executed_tool_names or [])
    if build_tool not in names:
        return None  # build tool never ran — nothing to verify
    try:
        from app.models.dashboard_app import DashboardApp
        exists = (
            db.query(DashboardApp.id)
            .filter(
                DashboardApp.chat_thread_id == conversation_id,
                DashboardApp.is_deleted == False,  # noqa: E712
            )
            .first()
        )
        if exists:
            return None
        return (
            "⚠️ BUILD VERIFICATION FAILED: the agent said it built a "
            "dashboard but no dashboard app record exists for this "
            "conversation. The dashboard was NOT actually created. "
            "Please ask the agent to call the dashboard build tool again "
            "and confirm the app URL appears."
        )
    except Exception as exc:  # noqa: BLE001 — verification must never crash the stream
        logger.warning("verify_dashboard_build_produced_app failed (non-fatal): %s", exc)
        return None


def verify_dashboard_quality_refined(
    worst_grade: str | None,
    refined: bool,
    hard_gaps: list[str] | None = None,
) -> str | None:
    """POST-BUILD quality gate (Aug 2026, Tier 1 auto-refine enforcement).

    The create/update_fullstack_dashboard tools return a deterministic
    ``quality`` verdict (grade A/B/C + hard_gaps + recommendations). A turn
    that BUILT a dashboard with grade B/C and then ended WITHOUT calling
    ``update_fullstack_dashboard`` shipped a thin board on purpose — the
    agent saw the quality report and ignored it.

    Returns a human-readable warning when:
      - ``worst_grade`` is B or C (the build tool returned a quality report
        that was not grade A), AND
      - ``refined`` is False (no update_fullstack_dashboard followed the
        build in the same turn).

    Returns ``None`` when the grade is A or unknown, or an update followed.
    The caller appends the returned message to the final stream content so
    the user sees why the dashboard is below the professional standard.
    """
    if worst_grade not in ("B", "C"):
        return None
    if refined:
        return None
    gaps = ", ".join(hard_gaps or []) or "widget mix below standard"
    return (
        "⚠️ BUILD QUALITY " + worst_grade + ": the dashboard was created but "
        "the build's own quality report flagged missing requirements "
        f"({gaps}) and NO refinement update (update_fullstack_dashboard) "
        "followed. The board is live but below the professional standard — "
        "ask the agent to call update_fullstack_dashboard to close the gaps "
        "(filters, sections, KPI row, trend, insight strip) in one pass."
    )
